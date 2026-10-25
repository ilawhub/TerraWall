#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时地球壁纸（轻量版）— 即刻地球的简易本地实现
================================================
核心功能：从 NOAA 静止气象卫星（GOES-19，可切换 GOES-18/16）及
Himawari-9 亚洲视角影像（可见光/合成图/红外）与 NASA 静态地球图中
抓取实时或经典地球照片，按屏幕比例裁剪后设为 macOS 桌面壁纸。

- 零第三方 Python 依赖（仅标准库 + macOS 自带 sips / osascript）
- 支持前台循环运行 / 单次更新 / launchd 后台定时（--install）
- 离线或抓取失败时自动回退到本地最近一张缓存

用法示例：
  python3 earth_wallpaper.py --once                # 立即更新一次壁纸
  python3 earth_wallpaper.py --interval 10         # 前台每 10 分钟循环更新
  python3 earth_wallpaper.py --install             # 注册后台任务（每 10 分钟一次）
  python3 earth_wallpaper.py --uninstall           # 移除后台任务
  python3 earth_wallpaper.py --source goes18       # 指定球体源（完整球体模式，不达标自动降级）
  python3 earth_wallpaper.py --source bluemarble   # 显式指定非球体源（尊重用户选择）
  python3 earth_wallpaper.py --check               # 只检查数据源连通性，不换壁纸
  python3 earth_wallpaper.py --list-cache          # 查看缓存
  python3 earth_wallpaper.py --clear-cache         # 清空缓存
"""

import argparse
import datetime
import glob
import json
import os
import plistlib
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# 可选视觉增强：已安装 Pillow 时启用自动色阶/饱和度/锐化渲染；
# 未安装则保持零依赖，仅做裁剪缩放（功能不受影响）。
try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

APP_NAME = "EarthWallpaper"
APP_DIR = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", APP_NAME
)
CACHE_DIR = os.path.join(APP_DIR, "cache")
LOG_FILE = os.path.join(os.path.expanduser("~"), "Library", "Logs", "EarthWallpaper.log")
LAUNCH_AGENT = os.path.join(
    os.path.expanduser("~"), "Library", "LaunchAgents", "com.local.earthwallpaper.plist"
)

# 数据源：NOAA STAR 静止卫星全盘（Full Disk）GEOCOLOR 彩色图 +
# Himawari-9 亚洲视角影像 + NASA 经典静态地球图
SOURCES = {
    "goes19": {
        "name": "GOES-19（新一代，美洲/大西洋视角，主力）",
        "url": "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/GEOCOLOR/latest.jpg",
    },
    "goes18": {
        "name": "GOES-18（西太平洋/美洲西岸视角）",
        "url": "https://cdn.star.nesdis.noaa.gov/GOES18/ABI/FD/GEOCOLOR/latest.jpg",
    },
    "goes16": {
        "name": "GOES-16（美洲/大西洋视角）",
        "url": "https://cdn.star.nesdis.noaa.gov/GOES16/ABI/FD/GEOCOLOR/latest.jpg",
    },
    "himawari9": {
        "name": "Himawari-9（日本葵花9号，亚洲/西太平洋视角，可见光B03，每10分钟更新）",
        "meta_url": "https://himawari.asia/img/D531106/latest.json",
        "url": None,  # 动态拼帧，见 resolve_source_url()
    },
    "agora": {
        "name": "NII Digital Typhoon 合成图（亚洲视角，Blue Marble 底图 + Himawari-9 实时云层，每小时更新）",
        "url": "https://agora.ex.nii.ac.jp/digital-typhoon/wallpaper/globe/1024x768/latest.jpg",
    },
    "bluemarble": {
        "name": "NASA Blue Marble（经典地球静态图，5400x2700）",
        "url": "https://eoimages.gsfc.nasa.gov/images/imagerecords/74000/74218/world.200412.3x5400x2700.jpg",
    },
    "night": {
        "name": "NASA 地球夜景（静态图，3600x1800）",
        "url": "https://eoimages.gsfc.nasa.gov/images/imagerecords/79000/79765/dnb_land_ocean_ice.2012.3600x1800.jpg",
    },
    "planets": {
        "name": "NASA Images API（行星/星云图库，随机关键词 + 随机选图）",
        "url": None,  # 动态解析，见 resolve_source_url()
    },
    "moon": {
        "name": "NASA Images API（月球影像库，LRO / Apollo 任务图等）",
        "url": None,  # 动态解析，见 resolve_source_url()
    },
    "sun": {
        "name": "NASA SDO 太阳动力学天文台实时 171Å 图（4096x4096，Public Domain）",
        "url": "https://sdo.gsfc.nasa.gov/assets/img/latest/latest_4096_0171.jpg",
    },
}

DEFAULT_INTERVAL = 10  # 分钟

# 数据源优先级（按"好看且色彩真实"降序）：
# 自然色 GEOCOLOR 实时彩色图优先，Blue Marble / 夜景 为经典自然色兜底。
# 单源下载失败或质量评分不达标时，按此顺序自动尝试下一个源。
SOURCE_PRIORITY = [
    "goes19", "goes18", "goes16", "himawari9", "agora",
    "bluemarble", "night",
]

# 完整球体源（卫星全盘 Full Disk 图：圆形地球居中 + 四周太空黑边）。
# 只有这些源能保证"完整地球球体"视觉效果；其余为区域合成或平面投影。
FULL_DISK_SOURCES = ("goes19", "goes18", "goes16", "himawari9")
NON_DISK_SOURCES = ("agora", "bluemarble", "night")

# 太阳圆盘源（SDO 4096x4096 方形 + 圆盘居中 + 四角太空黑，
# 与地球全盘图同构，沿用球体检测与"保圆盘 + 黑边"裁剪模式）。
SOLAR_DISK_SOURCES = ("sun",)

# NASA 影像库非球体源：跳过球体检测，保留质量评分。
PLANET_SOURCES = ("planets", "moon")

# NASA Images API 关键词池（planets 随机抽取；moon 固定用 moon）
NASA_IMAGES_API = "https://images-api.nasa.gov/search"
PLANET_KEYWORDS = [
    "jupiter", "mars", "saturn", "neptune", "uranus",
    "venus", "mercury", "pluto",
    "nebula", "galaxy", "andromeda",
]


def log(msg: str) -> None:
    """追加一行带时间戳的日志。"""
    line = "[{}] {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def run_cmd(args, timeout=60):
    """执行外部命令，返回 (returncode, stdout)。"""
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return -1, str(exc)


def screen_resolution():
    """探测主显示器物理像素分辨率与 Retina 缩放因子。返回 (w, h, scale) 或 None。

    壁纸目标分辨率使用物理像素（Retina 屏为逻辑分辨率的 2 倍），保证清晰锐利：
      1) 优先 system_profiler SPDisplaysDataType（Resolution 行为物理像素，
         带 Retina 标记时缩放因子 2.0，主屏通常排第一，满足多显示器适配主屏）；
      2) 备选 CoreGraphics（ctypes 直调系统框架，零第三方依赖）。
    """
    scale = 1.0
    rc, out = run_cmd(["system_profiler", "SPDisplaysDataType"], timeout=90)
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.lower().startswith("resolution"):
                # 形如 "Resolution: 2560 x 1664 Retina"
                parts = line.split(":", 1)[1].strip().split()
                try:
                    w = int(parts[0])
                    h = int(parts[2])
                    if "retina" in line.lower():
                        scale = 2.0
                    return w, h, scale
                except (ValueError, IndexError):
                    continue
    # 备选：CoreGraphics（获取主屏物理像素）
    try:
        import ctypes
        cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
        cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
        cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
        main = cg.CGMainDisplayID()
        w = int(cg.CGDisplayPixelsWide(main))
        h = int(cg.CGDisplayPixelsHigh(main))
        if w > 0 and h > 0:
            return w, h, scale
    except Exception:
        pass
    return None


def download_image(url, dest):
    """用系统 curl 下载图片到 dest，返回 True/False。

    注意：NOAA CDN 传完数据后常不主动关闭连接，curl 会等到超时才退出，
    但文件实际已完整。因此不以 curl 退出码为唯一判据，而是做完整性校验：
      1) 服务器返回 Content-Length 时，比对实际下载字节数（防中途截断）；
      2) JPEG 文件末尾必须是 EOI 标记 FF D9（截断的 JPEG 通常缺失该标记）；
      3) 安装了 Pillow 时用 Image.load() 完整解码验证（能真正读完整图）。
    任一校验失败即视为损坏：删除文件、记录日志并返回 False。
    NASA CDN 偶发连接重置（curl 56），失败后自动重试一次再判定。
    """
    def _try_once():
        # 用 -w 输出实际下载字节与响应头 Content-Length，供后续比对
        rc, out = run_cmd(
            ["curl", "-sL", "--fail", "--max-time", "180", "-o", dest,
             "-w", "\\n__EW_DL__%{size_download}__EW_CL__%{size_header}", url],
            timeout=200,
        )
        ok = False
        length_ok = eoi_ok = pil_ok = True
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            # 1) 长度比对：服务器声明了 Content-Length（>0）时，下载量必须不小于它
            m = re.search(r"__EW_DL__(\d+)__EW_CL__(\d+)", out)
            length_ok = True
            if m:
                size_download = int(m.group(1))
                content_length = int(m.group(2))
                if content_length > 0 and size_download < content_length:
                    length_ok = False

            # 2) JPEG EOI 标记检查
            eoi_ok = False
            try:
                with open(dest, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    if fh.tell() >= 4:
                        fh.seek(-2, os.SEEK_END)
                        eoi_ok = fh.read(2) == b"\xff\xd9"
            except OSError:
                eoi_ok = False

            # 3) PIL 完整解码验证（可用时）
            pil_ok = True
            if HAVE_PIL:
                try:
                    im = Image.open(dest)
                    im.load()
                except Exception:
                    pil_ok = False

            ok = length_ok and eoi_ok and pil_ok

        if not ok:
            try:
                os.remove(dest)
            except OSError:
                pass
        return ok, rc, out, length_ok, eoi_ok, pil_ok

    for attempt in (1, 2):
        ok, rc, out, length_ok, eoi_ok, pil_ok = _try_once()
        if ok:
            return True
        if attempt == 1:
            log("下载失败（第 1 次，将重试）: {}".format(
                out.strip().splitlines()[0] if out.strip() else "无响应"))
            time.sleep(2)
            continue
        log("下载失败/校验不通过: {}（curl 退出码 {}，长度校验 {}，EOI {}，PIL {}）".format(
            out.strip().splitlines()[0] if out.strip() else "无响应",
            rc, length_ok, eoi_ok, "skip" if not HAVE_PIL else ("ok" if pil_ok else "fail")))
    return False


def nasa_pick_url(keyword, rng):
    """调用 NASA Images API 搜索 keyword，随机返回一张图的 ~orig.jpg 直链。

    返回 (url, title)。API 返回的 links[0].href 是 ~thumb.jpg 缩略图；
    NASA 资产命名规律为 images-assets.nasa.gov/image/<id>/<id>~orig.jpg，
    直接拼接原图直链（经实测可用；~medium.jpg 会 403）。
    零第三方依赖：仅用 urllib + json 解析。
    """
    qs = urllib.parse.urlencode({
        "q": keyword, "media_type": "image", "page_size": 30,
    })
    req = urllib.request.Request(
        NASA_IMAGES_API + "?" + qs,
        headers={"User-Agent": "TerraWall/1.0 (macOS wallpaper)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("NASA Images API 请求失败（keyword={}）: {}".format(
            keyword, exc))

    items = data.get("collection", {}).get("items") or []
    valid = [
        it for it in items
        if it.get("links") and it["links"][0].get("href")
        and it.get("data") and it["data"][0].get("nasa_id")
    ]
    if not valid:
        raise RuntimeError(
            "NASA Images API 无有效结果（keyword={}）".format(keyword))
    it = rng.choice(valid)
    nasa_id = it["data"][0]["nasa_id"]
    title = it["data"][0].get("title") or nasa_id

    # links 数组包含多个尺寸（~small/~thumb/~medium/~large/~orig）。
    # 优先中等分辨率直链（~large/~medium），避免超大 ~orig 图下载慢、易失败；
    # 只有 small/thumb/orig 的资产才回退到 ~orig.jpg。
    hrefs = [l.get("href", "") for l in it.get("links", [])]
    picked = None
    for size in ("~large.jpg", "~medium.jpg", "~orig.jpg"):
        cand = next((h for h in hrefs if h.endswith(size)), None)
        if cand:
            picked = cand
            break
    if not picked:
        href = next((h for h in hrefs if h), "")
        picked = href.rsplit("~", 1)[0] + "~orig.jpg"
    return picked, title


def resolve_source_url(source_key, cfg):
    """返回数据源实际下载 URL。

    himawari9 的帧地址是动态的：先请求 latest.json 获取最新帧的
    date 字段（形如 "2026-10-24 13:50:00"），解析出 YYYY/MM/DD/HHMMSS
    拼出帧 URL（https://himawari.asia/img/D531106/1d/550/YYYY/MM/DD/HHMMSS_0_0.png）。
    planets/moon 走 NASA Images API：随机关键词 + 随机选一张图，拼原图直链。
    sun 为固定 SDO 实时图。
    网络异常或 JSON 解析失败时抛出 RuntimeError 并给出明确原因。
    """
    if source_key in ("planets", "moon"):
        rng = random.Random()
        keyword = rng.choice(PLANET_KEYWORDS) if source_key == "planets" else "moon"
        url, title = nasa_pick_url(keyword, rng)
        log("{}: NASA Images API 命中「{}」-> {}（{}）".format(
            source_key, keyword, title, url.rsplit("/", 1)[-1]))
        return url

    if source_key != "himawari9":
        return cfg["url"]

    meta_url = cfg.get("meta_url", "")
    rc, out = run_cmd(
        ["curl", "-sL", "--fail", "--max-time", "30", "-o", "-", meta_url],
        timeout=40,
    )
    if rc != 0 or not out.strip():
        raise RuntimeError(
            "himawari9: 无法获取帧清单 latest.json（curl 退出码 {}）".format(rc)
        )
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise RuntimeError("himawari9: latest.json 解析失败: {}".format(exc))

    date_str = (data.get("date") or "").strip()
    if not date_str:
        raise RuntimeError("himawari9: latest.json 缺少 date 字段")
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise RuntimeError(
            "himawari9: date 字段格式无法解析: {}（原文 {}）".format(exc, date_str)
        )

    # 按 date 解析出的 HHMMSS 拼帧名（示例：135000_0_0.png）
    frame = "{}_0_0.png".format(dt.strftime("%H%M%S"))
    url = "https://himawari.asia/img/D531106/1d/550/{}/{}/{}/{}".format(
        dt.strftime("%Y"), dt.strftime("%m"), dt.strftime("%d"), frame
    )
    return url


def latest_cache():
    """返回缓存目录中最近一张壁纸的路径，无则 None。"""
    if not os.path.isdir(CACHE_DIR):
        return None
    files = glob.glob(os.path.join(CACHE_DIR, "wall_*.jpg"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def crop_to_screen(src, dst, dw, dh, keep_disk=False):
    """用 sips 将卫星图中心裁剪为屏幕比例并缩放至目标分辨率。

    keep_disk=False（默认）：按源图宽高比与屏幕比判定裁剪方向：
      - 源图更方/更窄（src_ratio <= ratio）：裁高（保留全宽，裁掉上下），
        如 10848x10848 方形源图 -> 高 10848/1.538≈7053，比例匹配屏幕；
      - 源图更宽（src_ratio > ratio）：裁宽（保留全高，裁掉左右）。
      任何情况下若计算出的裁剪尺寸超出源图边界（越界保护），则放弃裁剪，
      回退为整图等比缩放到目标分辨率，避免 sips 用填充色补齐产生灰块。

    keep_disk=True（完整球体模式）：不再裁切画面，改为整图等比缩放保留
    完整地球圆盘，宽度不足部分用太空黑（0,0,0）左右填充，得到
    "中央完整圆形地球 + 四周太空" 的宽屏壁纸。需 Pillow 合成；无 Pillow
    或等比后宽度超出目标时回退普通裁剪。
    """
    # 读取源图尺寸
    rc, out = run_cmd(["sips", "-g", "pixelWidth", "-g", "pixelHeight", src])
    sw = sh = 0
    for line in out.splitlines():
        line = line.strip()
        if "pixelWidth" in line:
            sw = int(line.split(":")[1].strip())
        elif "pixelHeight" in line:
            sh = int(line.split(":")[1].strip())
    if not sw or not sh:
        log("无法读取源图尺寸: {}".format(out))
        return False

    # 完整球体模式：保圆盘完整，黑边填充
    if keep_disk:
        target_w = int(round(sw * dh / float(sh)))
        if target_w > dw:
            log("完整球体保留模式等比后宽度超出（{}>{}），回退普通裁剪"
                .format(target_w, dw))
        elif HAVE_PIL:
            try:
                im = Image.open(src).convert("RGB")
                scaled = im.resize((target_w, dh), Image.LANCZOS)
                canvas = Image.new("RGB", (dw, dh), (0, 0, 0))
                canvas.paste(scaled, ((dw - target_w) // 2, 0))
                canvas.save(dst, "JPEG", quality=92)
                return True
            except Exception as exc:
                log("完整球体保留模式合成失败（{}），回退普通裁剪".format(exc))
        else:
            log("Pillow 不可用，完整球体保留模式回退普通裁剪")

    ratio = dw / dh
    src_ratio = sw / sh

    # 正确分支：更方/更窄的源图裁高（保留全宽），更宽的源图裁宽（保留全高）
    if src_ratio <= ratio:
        cw = sw
        ch = int(sw / ratio)  # 裁高，宽度不变
        off_x = 0
        off_y = int((sh - ch) / 2)
    else:
        cw = int(sh * ratio)  # 裁宽，高度不变
        ch = sh
        off_x = int((sw - cw) / 2)
        off_y = 0

    # 越界保护：裁剪尺寸超出源图边界（含负偏移）时回退整图等比缩放
    if (ch > sh or cw > sw or ch <= 0 or cw <= 0
            or off_x < 0 or off_y < 0):
        log("裁剪参数越界（sw={} sh={} ratio={:.3f} -> cw={} ch={} off=({},{})），"
            "回退为整图等比缩放".format(sw, sh, ratio, cw, ch, off_x, off_y))
        rc, out = run_cmd(["sips", "-z", str(dh), str(dw), src, "--out", dst],
                          timeout=120)
        return rc == 0

    # 第一步：中心裁剪
    rc, out = run_cmd(
        ["sips", "--cropOffset", str(off_y), str(off_x),
         "--cropToHeightWidth", str(ch), str(cw), src, "--out", dst], timeout=120
    )
    if rc != 0:
        log("裁剪失败: {}".format(out))
        return False

    # 第二步：缩放至目标分辨率
    rc, out = run_cmd(["sips", "-z", str(dh), str(dw), dst], timeout=120)
    if rc != 0:
        log("缩放失败: {}".format(out))
        return False
    return True


def set_wallpaper(path):
    """通过 System Events 将图片设为所有桌面壁纸。

    macOS 26 上 'set picture of every desktop'（复数批量）实测返回 0 但静默
    不生效，改为逐个 desktop 设置：desktop 1 为主屏，其余按实际数量遍历，
    多显示器时各屏同步更新。
    """
    # 路径含空格需用 POSIX file 转义；AppleScript 字符串转义
    esc = path.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "System Events"\n'
        '  set n to count of desktops\n'
        '  repeat with i from 1 to n\n'
        '    set picture of desktop i to POSIX file "{}"\n'
        '  end repeat\n'
        "end tell".format(esc)
    )
    rc, out = run_cmd(["osascript", "-e", script], timeout=30)
    if rc != 0:
        log("设置壁纸失败: {}".format(out))
        return False
    return True


def enhance_image(path):
    """视觉增强渲染（可选，需 Pillow）：自动色阶 + 饱和度提升 + 轻度锐化。

    对已裁剪好的壁纸原地处理，返回 True/False。未安装 Pillow 时跳过增强，
    返回 False（不阻塞壁纸设置）。
    """
    if not HAVE_PIL:
        return False
    try:
        im = Image.open(path).convert("RGB")
        im = ImageOps.autocontrast(im, cutoff=1)
        im = ImageEnhance.Color(im).enhance(1.15)
        im = ImageEnhance.Brightness(im).enhance(1.02)
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))
        im.save(path, "JPEG", quality=92)
        return True
    except Exception as exc:
        log("视觉增强跳过: {}".format(exc))
        return False


def score_image(path):
    """对下载的 raw 图做"适合做壁纸"质量评分。

    返回 (score, reasons)：
      - score 为 0~100 分，>= SCORE_THRESHOLD 视为合格；
      - reasons 非空表示存在不合格项，按严重程度扣分。
    检查项：可解码性、大面积纯色/灰色块（截断图、数据缺失图）、
    整体过暗/过曝、对比度过低、色彩过于单调（有效颜色数过少）、
    无地球主体轮廓。未安装 Pillow 时返回 (100, [])（不拦截，保持零依赖可运行）。
    """
    if not HAVE_PIL:
        return 100, []
    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size
        # 降采样到 200px 宽，控制分析开销（raw 可能达 10848x10848）
        tw = min(w, 200)
        th = max(1, int(h * tw / w))
        thumb = im.resize((tw, th), Image.LANCZOS)
    except Exception as exc:
        return 0, ["图片无法解码: {}".format(exc)]

    px = list(thumb.getdata())
    n = len(px)
    if n < 100:
        return 0, ["图片过小"]

    lum = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in px]
    mean = sum(lum) / n
    var = sum((v - mean) ** 2 for v in lum) / n
    std = var ** 0.5

    # 量化颜色统计（每通道 4bit -> 4096 桶）
    q = [(r >> 4, g >> 4, b >> 4) for r, g, b in px]
    from collections import Counter
    counter = Counter(q)
    top_color, top_count = counter.most_common(1)[0]
    max_share = top_count / n

    # 最大同色块的灰度程度（判定"灰色块"而非"黑色太空"）
    top_rgb = ((top_color[0] << 4) + 8, (top_color[1] << 4) + 8, (top_color[2] << 4) + 8)
    grayness = max(top_rgb) - min(top_rgb)

    # 均匀灰块网格检测（截断/数据缺失图的特征：大面积无纹理的纯灰块；
    # 真实地球图的云/冰虽偏灰白，但有亮度纹理，网格内亮度波动较大不会误伤）
    grid_n = 6
    gray_cells = 0
    total_cells = 0
    for gy in range(grid_n):
        for gx in range(grid_n):
            ys = gy * th // grid_n
            ye = (gy + 1) * th // grid_n
            xs = gx * tw // grid_n
            xe = (gx + 1) * tw // grid_n
            cell = []
            for yy in range(ys, ye):
                cell.extend(px[yy * tw + xs: yy * tw + xe])
            if not cell:
                continue
            total_cells += 1
            cell_lum = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in cell]
            cm = sum(cell_lum) / len(cell_lum)
            cv = sum((v - cm) ** 2 for v in cell_lum) / len(cell_lum)
            cstd = cv ** 0.5
            cr = sum(v[0] for v in cell) // len(cell)
            cg2 = sum(v[1] for v in cell) // len(cell)
            cb = sum(v[2] for v in cell) // len(cell)
            if 40 <= cm <= 220 and cstd < 8 and max(cr, cg2, cb) - min(cr, cg2, cb) <= 25:
                gray_cells += 1
    gray_cell_share = gray_cells / total_cells if total_cells else 0

    # 非黑色内容占比（地球主体）
    content_share = sum(1 for v in lum if v > 30) / n

    # 非黑像素中单一量化色占比（黑色太空是卫星图正常背景，不算缺陷）
    non_dark = [(r, g, b) for r, g, b in px
                if (r * 299 + g * 587 + b * 114) // 1000 > 30]
    nd_share = 0.0
    nd_rgb = None
    if non_dark:
        nd_counter = Counter(((r >> 4, g >> 4, b >> 4) for r, g, b in non_dark))
        nd_top, nd_count = nd_counter.most_common(1)[0]
        nd_share = nd_count / len(non_dark)
        nd_rgb = ((nd_top[0] << 4) + 8, (nd_top[1] << 4) + 8, (nd_top[2] << 4) + 8)

    reasons = []
    if mean < 10:
        reasons.append("整体过暗（平均亮度 {:.1f}）".format(mean))
    elif mean > 245:
        reasons.append("整体过曝（平均亮度 {:.1f}）".format(mean))
    if std < 12:
        reasons.append("对比度过低（亮度标准差 {:.1f}）".format(std))
    if len(counter) < 6:
        reasons.append("色彩过于单调（有效颜色数 {}）".format(len(counter)))
    if gray_cell_share > 0.33:
        reasons.append("存在大面积灰色块（均匀灰网格占 {:.0%}）".format(gray_cell_share))
    if nd_share > 0.60:
        reasons.append("大面积纯色（{} 占非黑内容 {:.0%}）".format(nd_rgb, nd_share))
    if content_share < 0.08:
        reasons.append("无地球主体轮廓（非黑内容仅 {:.1%}）".format(content_share))

    # 每项不合格扣 20 分，最多扣到 0
    score = max(0, 100 - 20 * len(reasons))
    return score, reasons


SCORE_THRESHOLD = 60  # 质量评分合格线


def detect_full_disk(path):
    """检测图片是否呈现完整地球圆盘（卫星全盘图特征）。

    基于降采样缩略图的像素分布，返回 (is_disk, detail)：
      1) 四角太空黑：4 个角落区域（每边 10%）非黑像素占比 < 25% 的角至少 3 个
         —— 全盘图圆盘内切于画面、四角为太空黑；平面投影/区域图四角有内容。
      2) 方形画面：源图宽高比 0.9~1.1
         —— 卫星全盘图为正方形画布（GOES 10848x10848、Himawari 550x550）；
         平面投影/区域图为矩形（Blue Marble 2:1、agora 4:3）。
      3) 主体居中：非黑像素质心位于画面中心 ±35%
         —— 圆盘整体居中；GOES/Himawari 云与陆海亮度不均会使质心略偏
         （如 Himawari 可见光海洋暗、亮内容偏左），±35% 容忍自然偏移；
         靠边裁剪/残图会被排除。
      4) 圆盘占比合理：非黑像素占比 10%~92%
         —— 内切圆约 78.5%，但 GEOCOLOR 圆盘带太空暗晕（35%~50% 属正常），
         Himawari 可见光海洋偏暗（约 17%），均算完整球体；整图铺满内容
         接近 100% 的矩形图已被第 2 条排除。
    四项全部满足判定为完整球体；Pillow 不可用或解码失败时放行（避免误杀）。
    """
    if not HAVE_PIL:
        return True, "Pillow 不可用，跳过球体检测"
    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size
        tw = min(w, 200)
        th = max(1, int(round(h * tw / w)))
        thumb = im.resize((tw, th), Image.LANCZOS)
    except Exception as exc:
        return False, "图片无法解码: {}".format(exc)

    px = list(thumb.getdata())
    n = tw * th
    # 亮度掩码：>30 视为可见内容（太空黑亮度极低）
    mask = [((r * 299 + g * 587 + b * 114) // 1000) > 30 for r, g, b in px]
    non_dark = sum(mask)
    if non_dark == 0:
        return False, "无可见主体（几乎全黑）"
    share = non_dark / float(n)

    # 四角区域（每边 10%）
    cs_w = max(2, tw // 10)
    cs_h = max(2, th // 10)
    corners = []
    for x0, y0 in ((0, 0), (tw - cs_w, 0), (0, th - cs_h), (tw - cs_w, th - cs_h)):
        cnt = tot = 0
        for yy in range(y0, min(y0 + cs_h, th)):
            start = yy * tw + x0
            end = start + cs_w
            cnt += sum(mask[start:end])
            tot += end - start
        corners.append(cnt / float(tot) if tot else 1.0)
    black_corners = sum(1 for c in corners if c < 0.25)

    # 非黑像素质心（居中判定）
    xs = [i % tw for i, m in enumerate(mask) if m]
    ys = [i // tw for i, m in enumerate(mask) if m]
    cx = sum(xs) / float(len(xs)) / tw
    cy = sum(ys) / float(len(ys)) / th
    centered = abs(cx - 0.5) < 0.35 and abs(cy - 0.5) < 0.35

    # 画面宽高比（方形画布是全盘图特征）
    frame_aspect = w / float(h)

    is_disk = (black_corners >= 3
               and 0.9 <= frame_aspect <= 1.1
               and centered
               and 0.10 <= share <= 0.92)
    detail = "四角黑 {}/4，画面比例 {:.2f}，非黑占比 {:.1%}，质心 ({:.2f},{:.2f})".format(
        black_corners, frame_aspect, share, cx, cy)
    return is_disk, detail


def update_wallpaper(source_key):
    """执行一次完整更新：下载 -> 球体检测 -> 质量评分 -> 裁剪 -> 设壁纸。返回是否成功。

    - source_key 为 None（默认）或球体源（goes19/18/16、himawari9）时进入
      "完整球体模式"：只从球体源按优先级尝试，每张图强制通过球体检测，
      不达标自动降级到下一个球体源；全部不达标则保留现有壁纸并跳过本次更新。
    - 用户显式指定非球体源（agora/bluemarble/night）时尊重用户选择：
      仅使用该源，跳过球体检测（仍做质量评分），失败则保留现有壁纸。
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    if source_key is None:
        # 默认：完整球体模式，全部球体源按优先级
        ordered = [k for k in SOURCE_PRIORITY if k in FULL_DISK_SOURCES]
        force_disk = True
    elif source_key in FULL_DISK_SOURCES:
        # 指定球体源：从该源开始（只降级到其他球体源），强制球体检测
        idx = SOURCE_PRIORITY.index(source_key)
        ordered = [k for k in SOURCE_PRIORITY[idx:]
                   if k in FULL_DISK_SOURCES]
        ordered += [k for k in SOURCE_PRIORITY[:idx]
                    if k in FULL_DISK_SOURCES]
        force_disk = True
    elif source_key in SOLAR_DISK_SOURCES:
        # 太阳圆盘源（SDO）：单源，沿用球体检测与保圆盘裁剪
        ordered = [source_key]
        force_disk = True
    elif source_key in NON_DISK_SOURCES or source_key in PLANET_SOURCES:
        # 显式指定非球体源：尊重用户，仅用该源
        ordered = [source_key]
        force_disk = False
    else:
        log("未知数据源: {}".format(source_key))
        return False

    # 探测屏幕分辨率（物理像素，Retina 感知；仅一次，各源共用）
    res = screen_resolution()
    if not res:
        log("无法探测屏幕分辨率，回退缓存")
        cached = latest_cache()
        return cached and set_wallpaper(cached)
    dw, dh, scale = res
    log("屏幕分辨率（物理像素）: {}x{}（Retina 缩放 x{}）".format(dw, dh, scale))

    tried = []
    for key in ordered:
        tried.append(key)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        raw = os.path.join(CACHE_DIR, "raw_{}_{}.jpg".format(ts, key))
        final = os.path.join(CACHE_DIR, "wall_{}.jpg".format(ts))
        src = SOURCES[key]
        log("尝试数据源 [{}]: {}".format(key, src["name"]))

        # 1. 解析实际 URL（himawari9 需先拉 latest.json 动态拼帧），再下载
        try:
            url = resolve_source_url(key, src)
        except RuntimeError as exc:
            log("{}: {}".format(key, exc))
            continue
        if not download_image(url, raw):
            continue

        # 2. 球体检测（仅完整球体模式）：非球体图直接降级，保证壁纸是完整地球
        if force_disk:
            is_disk, disk_detail = detect_full_disk(raw)
            if not is_disk:
                log("{} 非完整球体（{}），降级下一源".format(key, disk_detail))
                try:
                    os.remove(raw)
                except OSError:
                    pass
                continue
            log("{} 完整球体检测通过: {}".format(key, disk_detail))

        # 3. 质量评分：排除损坏/灰块/过暗过曝/单调/无地球主体的图
        score, reasons = score_image(raw)
        if reasons:
            log("{} 质量不合格（{}分）: {}".format(
                key, score, "；".join(reasons)))
            try:
                os.remove(raw)
            except OSError:
                pass
            continue
        log("{} 质量评分通过: {}分".format(key, score))

        # 4. 裁剪并缩放至屏幕分辨率（球体模式保圆盘完整+黑边，非球体模式普通裁剪）
        if not crop_to_screen(raw, final, dw, dh, keep_disk=force_disk):
            for p in (raw, final):
                try:
                    os.remove(p)
                except OSError:
                    pass
            continue
        if enhance_image(final):
            log("已完成视觉增强渲染")

        # 5. 设置壁纸
        if not set_wallpaper(final):
            return False
        log("壁纸已更新: {}".format(final))

        # 6. 清理旧文件（保留最近 3 份原始图和 5 份成品）
        for kind, keep in (("raw_*", 3), ("wall_*", 5)):
            files = sorted(glob.glob(os.path.join(CACHE_DIR, kind)),
                           key=os.path.getmtime, reverse=True)
            for old in files[keep:]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        return True

    # 全部源失败/不合格：保留现有壁纸，跳过本次更新
    log("数据源 {} 均失败或不合格，跳过本次更新".format("、".join(tried)))
    cached = latest_cache()
    if cached:
        log("保留现有壁纸: {}".format(cached))
    else:
        log("无可用缓存壁纸")
    return False


def write_launch_agent(interval_min):
    """注册 LaunchAgent：每隔 interval_min 分钟执行一次 --once。"""
    os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
    py = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    plist = {
        "Label": "com.local.earthwallpaper",
        "ProgramArguments": [py, os.path.abspath(__file__), "--once"],
        "StartInterval": max(1, int(interval_min * 60)),
        "RunAtLoad": True,
        "StandardOutPath": LOG_FILE,
        "StandardErrorPath": LOG_FILE,
    }
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(plist, f)
    rc, out = run_cmd(["launchctl", "unload", LAUNCH_AGENT])
    rc2, out2 = run_cmd(["launchctl", "load", LAUNCH_AGENT])
    if rc2 != 0:
        log("launchctl 加载失败: {}".format(out2))
        return False
    log("已注册后台任务: {}（每 {} 分钟更新一次）".format(LAUNCH_AGENT, interval_min))
    return True


def remove_launch_agent():
    """卸载 LaunchAgent。"""
    if os.path.exists(LAUNCH_AGENT):
        run_cmd(["launchctl", "unload", LAUNCH_AGENT])
        os.remove(LAUNCH_AGENT)
        log("已移除后台任务")
    else:
        log("当前未安装后台任务")


def check_sources():
    """检查所有数据源连通性，仅打印，不换壁纸。"""
    for key, cfg in SOURCES.items():
        if key == "himawari9":
            url = cfg["meta_url"]
            extra = " [latest.json]"
        elif key in ("planets", "moon"):
            # 动态源：走 NASA Images API 解析出真实直链即视为连通
            # （NASA CDN 对连续 HEAD 请求会限流 403，实际 GET 下载不受影响，
            #  故此处不做 HEAD 探测）
            try:
                url = resolve_source_url(key, cfg)
            except RuntimeError as exc:
                print("{:<8} {}{} -> 不可用({})".format(
                    key, cfg["name"], " [NASA API]", exc))
                continue
            print("{:<8} {}{} -> OK（直链 {}）".format(
                key, cfg["name"], " [NASA API]", url.rsplit("/", 1)[-1]))
            continue
        else:
            url = cfg["url"]
            extra = ""
        rc, out = run_cmd(["curl", "-sIL", "--max-time", "10", url], timeout=20)
        ok = "HTTP/2 200" in out or "HTTP/1.1 200" in out or "200 OK" in out
        status = "OK" if ok else "不可用({})".format(
            out.strip().splitlines()[0] if out else "无响应")
        if key == "himawari9":
            try:
                frame = resolve_source_url(key, cfg)
                status = "{}，帧 {}".format(status, frame.rsplit("/", 1)[-1])
            except RuntimeError as exc:
                status = "不可用({})".format(exc)
        print("{:<8} {}{} -> {}".format(key, cfg["name"], extra, status))


def main():
    parser = argparse.ArgumentParser(description="实时地球壁纸（轻量版）")
    parser.add_argument("--once", action="store_true", help="立即更新一次壁纸")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="前台循环更新的间隔分钟数（默认 10）")
    parser.add_argument("--source", default=None,
                        choices=list(SOURCES.keys()),
                        help=("数据源。默认（不指定）进入完整球体模式：仅从球体源"
                              "（goes19/goes18/goes16/himawari9）按优先级选择并强制"
                              "通过球体检测，不达标自动降级。显式指定非球体源"
                              "（agora/bluemarble/night/planets/moon）时尊重用户选择，"
                              "仅用该源（planets 随机行星/星云图、moon 月球影像库）；"
                              "sun 为 SDO 太阳实时圆盘图（沿用球体检测）。"))
    parser.add_argument("--install", action="store_true", help="注册后台定时任务")
    parser.add_argument("--uninstall", action="store_true", help="移除后台定时任务")
    parser.add_argument("--check", action="store_true", help="检查数据源连通性")
    parser.add_argument("--list-cache", action="store_true", help="查看缓存文件")
    parser.add_argument("--clear-cache", action="store_true", help="清空缓存（保留当前壁纸）")
    args = parser.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)

    if args.check:
        check_sources()
        return
    if args.list_cache:
        files = sorted(glob.glob(os.path.join(CACHE_DIR, "*")),
                       key=os.path.getmtime, reverse=True)
        total = 0
        for f in files:
            sz = os.path.getsize(f)
            total += sz
            print("{}  {:>9}  {}".format(
                datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M"),
                sz // 1024, os.path.basename(f)))
        print("共 {} 个文件，{} MB".format(len(files), total // 1024 // 1024))
        return
    if args.clear_cache:
        n = 0
        for f in glob.glob(os.path.join(CACHE_DIR, "*")):
            os.remove(f)
            n += 1
        log("已清空缓存（{} 个文件）".format(n))
        return
    if args.install:
        write_launch_agent(args.interval)
        return
    if args.uninstall:
        remove_launch_agent()
        return
    if args.once:
        update_wallpaper(args.source)
        return

    # 默认：前台循环
    log("实时地球壁纸运行中，每 {} 分钟更新一次（Ctrl+C 退出）".format(args.interval))
    while True:
        try:
            update_wallpaper(args.source)
        except Exception as exc:
            log("更新异常: {}".format(exc))
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
