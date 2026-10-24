# TerraWall（地球时刻）— Real-time Earth Wallpaper

A lightweight macOS app that fetches the latest full-disk imagery of Earth from NOAA's
GOES-19 geostationary satellite, auto-crops it to your screen ratio, applies visual
enhancement, and sets it as your desktop wallpaper — every 10 minutes.

Inspired by "即刻地球" (JiKe Earth), a classic macOS wallpaper app that is no longer
available. This project uses NOAA public satellite data.

> 中文说明：实时地球壁纸轻量版，灵感源自「即刻地球」。

## Preview

Real-time Earth imagery from NOAA GOES satellites and classic NASA Earth photos,
all public-domain sources.

![GOES-19 Full Disk (Americas)](docs/preview-goes19-fd.jpg)
![GOES-18 Full Disk (Pacific)](docs/preview-goes18-fd.jpg)
![GOES-19 CONUS](docs/preview-goes19-conus.jpg)
![GOES-18 CONUS](docs/preview-goes18-conus.jpg)
![NASA Blue Marble](docs/preview-blue-marble.jpg)
![NASA Earth at Night](docs/preview-earth-night.jpg)

## Features

- Real-time Earth wallpaper from NOAA GOES-19 full-disk GEOCOLOR imagery (10848x10848)
- Auto-adapts to your display: center-crop + resize to exact screen resolution
- Optional visual enhancement: auto-contrast, saturation boost, mild sharpening (Pillow)
- Offline fallback: uses the latest cached image when fetch fails or offline
- Low resource usage: single image download (~3-15MB) per update
- Background scheduling: one-command launchd agent (every 10 minutes, auto-start)
- Cache management: list / clear via CLI

> Visual enhancement is optional: enabled automatically when Pillow is installed;
> without it the app stays zero-dependency (crop/resize only).
> Install: `pip install pillow`

## Quick Start

```bash
cd TerraWall

# Update wallpaper once (download -> crop -> enhance -> set)
python3 earth_wallpaper.py --once

# Foreground loop mode (update every 10 minutes, Ctrl+C to quit)
python3 earth_wallpaper.py --interval 10

# Register background agent (every 10 minutes, auto-start on login)
python3 earth_wallpaper.py --install

# Check data source connectivity
python3 earth_wallpaper.py --check

# Cache management
python3 earth_wallpaper.py --list-cache    # list cached files & usage
python3 earth_wallpaper.py --clear-cache   # clear cache
```

## Data Source

| Key    | Satellite | View          | Notes                    |
|--------|-----------|---------------|--------------------------|
| `goes19` | GOES-19 | Americas / Atlantic | Default                   |
| `goes18` | GOES-18 | W. Pacific / Americas | Alternative               |
| `goes16` | GOES-16 | Americas / Atlantic | Alternative (redirects to GOES-19) |

Switch source: `python3 earth_wallpaper.py --once --source goes18`

Imagery: NOAA GOES Series Full Disk GEOCOLOR, served by NOAA STAR
(`cdn.star.nesdis.noaa.gov`). NOAA data is in the public domain; please attribute
NOAA/NESDIS/STAR when redistributing. The original app used JMA Himawari-8, whose
public imagery endpoints are no longer available.

## Background Agent

- Install: `python3 earth_wallpaper.py --install` (every 10 minutes by default)
- Custom interval: `python3 earth_wallpaper.py --install --interval 30`
- Uninstall: `python3 earth_wallpaper.py --uninstall`

LaunchAgent: `~/Library/LaunchAgents/com.local.earthwallpaper.plist`
Log: `~/Library/Logs/EarthWallpaper.log`
Cache: `~/Library/Application Support/EarthWallpaper/cache/`

## Notes

- The NOAA CDN sometimes keeps the connection open after transfer completes, causing
  curl to time out — the app validates file integrity via `sips` instead of exit code.
- First download is ~3-15MB; updates are hourly; slower networks may take 1-3 minutes.
- Wallpaper is set via System Events; macOS may prompt for Automation/Accessibility
  permission once — allow it.

## License

MIT License. See [LICENSE](LICENSE).
