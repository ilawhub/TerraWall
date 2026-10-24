#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时地球壁纸（轻量版）— 即刻地球的简易本地实现
================================================
核心功能：定期从 NOAA 静止气象卫星（GOES-19，可切换 GOES-18/16）抓取
实时地球全盘照片，按屏幕比例裁剪后设为 macOS 桌面壁纸。

- 零第三方 Python 依赖（仅标准库 + macOS 自带 sips / osascript）
- 支持前台循环运行 / 单次更新 / launchd 后台定时（--install）
- 离线或抓取失败时自动回退到本地最近一张缓存

用法示例：
  python3 earth_wallpaper.py --once                # 立即更新一次壁纸
  python3 earth_wallpaper.py --interval 10         # 前台每 10 分钟循环更新
  python3 earth_wallpaper.py --install             # 注册后台任务（每 10 分钟一次）
  python3 earth_wallpaper.py --uninstall           # 移除后台任务
  python3 earth_wallpaper.py --source goes18       # 切换数据源（goes19/goes18/goes16）
  python3 earth_wallpaper.py --check               # 只检查数据源连通性，不换壁纸
  python3 earth_wallpaper.py --list-cache          # 查看缓存
  python3 earth_wallpaper.py --clear-cache         # 清空缓存
"""

import argparse
import datetime
import glob
import os
import plistlib
import shutil
import subprocess
import sys
import time

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

# 数据源：NOAA STAR 静止卫星全盘（Full Disk）GEOCOLOR 彩色图
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
}

DEFAULT_INTERVAL = 10  # 分钟


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
    """探测主显示器分辨率（逻辑像素）。返回 (w, h) 或 None。"""
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
                    return w, h
                except (ValueError, IndexError):
                    continue
    return None


def download_image(url, dest):
    """用系统 curl 下载图片到 dest，返回 True/False。

    注意：NOAA CDN 传完数据后常不主动关闭连接，curl 会等到超时才退出，
    但文件实际已完整。因此以「sips 能读出尺寸」作为成功判据，而非退出码。
    """
    rc, out = run_cmd(
        ["curl", "-sL", "--fail", "--max-time", "180", "-o", dest, url], timeout=200
    )
    # 校验文件是否为有效 JPEG
    ok = False
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        vrc, vout = run_cmd(["sips", "-g", "pixelWidth", dest], timeout=30)
        if vrc == 0 and "pixelWidth" in vout:
            ok = True
    if not ok:
        try:
            os.remove(dest)
        except OSError:
            pass
        log("下载失败: {}（curl 退出码 {}）".format(
            out.strip().splitlines()[0] if out.strip() else "无响应", rc))
        return False
    return True


def latest_cache():
    """返回缓存目录中最近一张壁纸的路径，无则 None。"""
    if not os.path.isdir(CACHE_DIR):
        return None
    files = glob.glob(os.path.join(CACHE_DIR, "wall_*.jpg"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def crop_to_screen(src, dst, dw, dh):
    """用 sips 将正方形卫星图中心裁剪为屏幕比例并缩放至目标分辨率。"""
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

    ratio = dw / dh
    src_ratio = sw / sh
    if src_ratio > ratio:
        ch = int(sw / ratio)  # 裁高，宽度不变
        cw = sw
        off_y = int((sh - ch) / 2)
        off_x = 0
    else:
        cw = int(sh * ratio)  # 裁宽，高度不变
        ch = sh
        off_x = int((sw - cw) / 2)
        off_y = 0

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
    """通过 System Events 将图片设为所有桌面壁纸。"""
    # 路径含空格需用 POSIX file 转义；AppleScript 字符串转义
    esc = path.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "System Events"\n'
        '  set picture of every desktop to POSIX file "{}"\n'
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


def update_wallpaper(source_key):
    """执行一次完整更新：下载 -> 裁剪 -> 设壁纸。返回是否成功。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    raw = os.path.join(CACHE_DIR, "raw_{}.jpg".format(ts))
    final = os.path.join(CACHE_DIR, "wall_{}.jpg".format(ts))

    src = SOURCES.get(source_key)
    if not src:
        log("未知数据源: {}".format(source_key))
        return False
    log("数据源 [{}]: {}".format(source_key, src["name"]))

    # 1. 下载
    if not download_image(src["url"], raw):
        log("本次抓取失败，尝试回退本地缓存")
        cached = latest_cache()
        if cached and set_wallpaper(cached):
            log("已回退使用本地缓存壁纸: {}".format(cached))
            return True
        log("无可用缓存，本次未更新")
        return False

    # 2. 探测分辨率并裁剪
    res = screen_resolution()
    if not res:
        log("无法探测屏幕分辨率，回退缓存")
        cached = latest_cache()
        return cached and set_wallpaper(cached)
    dw, dh = res
    log("屏幕分辨率: {}x{}".format(dw, dh))
    if not crop_to_screen(raw, final, dw, dh):
        return False
    if enhance_image(final):
        log("已完成视觉增强渲染")

    # 3. 设置壁纸
    if not set_wallpaper(final):
        return False
    log("壁纸已更新: {}".format(final))

    # 4. 清理旧文件（保留最近 3 份原始图和 5 份成品）
    for kind, keep in (("raw_*", 3), ("wall_*", 5)):
        files = sorted(glob.glob(os.path.join(CACHE_DIR, kind)),
                       key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    return True


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
        rc, out = run_cmd(["curl", "-sI", "--max-time", "10", cfg["url"]], timeout=20)
        ok = "HTTP/2 200" in out or "HTTP/1.1 200" in out or "200 OK" in out
        print("{:<8} {} -> {}".format(key, cfg["name"], "OK" if ok else "不可用({})".format(out.strip().splitlines()[0] if out else "无响应")))


def main():
    parser = argparse.ArgumentParser(description="实时地球壁纸（轻量版）")
    parser.add_argument("--once", action="store_true", help="立即更新一次壁纸")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="前台循环更新的间隔分钟数（默认 10）")
    parser.add_argument("--source", default="goes19",
                        choices=list(SOURCES.keys()), help="卫星数据源")
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
