#!/usr/bin/env python3
"""把 DEM 渲染成山体阴影 PNG 预览图（地形配色 + 阴影）。

nodata 区域输出为透明（RGBA），不会留下黑边/黑块。
加 `--bg white` 可改成白色不透明底图。

用法: python preview_dem.py dem.tif out.png [最大边长] [--bg transparent|white]
"""
import sys
import numpy as np
import rasterio
from PIL import Image

STOPS = [(0, (44, 106, 76)), (20, (92, 148, 84)), (45, (176, 190, 110)),
         (80, (216, 200, 148)), (150, (168, 142, 108)), (300, (150, 150, 150)),
         (1000, (245, 245, 245)), (4000, (255, 255, 255))]


def colorize(h):
    h = np.clip(h, STOPS[0][0], STOPS[-1][0])
    out = np.zeros(h.shape + (3,), dtype=np.float32)
    for i in range(len(STOPS) - 1):
        v0, c0 = STOPS[i]
        v1, c1 = STOPS[i + 1]
        m = (h >= v0) & (h < v1) if i < len(STOPS) - 2 else (h >= v0)
        t = ((h - v0) / (v1 - v0))[m]
        for k in range(3):
            out[..., k][m] = c0[k] + (c1[k] - c0[k]) * t
    return out


def hillshade(a, res, az=315.0, alt=45.0):
    gy, gx = np.gradient(a, res)
    slope = np.arctan(np.hypot(gx, gy) / res)
    aspect = np.arctan2(-gx, gy)
    az, alt = np.radians(az), np.radians(alt)
    return (np.sin(alt) * np.cos(slope)
            + np.cos(alt) * np.sin(slope) * np.cos(az - aspect))


OPT_WITH_VALUE = ("--bg",)


def parse_args(argv):
    """把带值选项与布尔选项分开，返回 (位置参数列表, 选项字典, 布尔集合)"""
    pos, opts, flags = [], {}, set()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in OPT_WITH_VALUE:
            opts[a.lstrip("-")] = argv[i + 1]
            i += 2
        elif a.startswith("--"):
            flags.add(a.lstrip("-"))
            i += 1
        else:
            pos.append(a)
            i += 1
    return pos, opts, flags


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if len(pos) < 2:
        print("用法: python preview_dem.py dem.tif out.png [最大边长] [--bg transparent|white]")
        sys.exit(1)
    src, out = pos[0], pos[1]
    maxside = int(pos[2]) if len(pos) > 2 else 1600
    bg = opts.get("bg", "transparent")

    with rasterio.open(src) as ds:
        a = ds.read(1).astype(np.float32)
        nd = ds.nodata
        res = abs(ds.transform.a)

    # nodata 掩膜：支持 NaN 与数值 nodata，另兜住 -32768 这类填充值
    if nd is None or (isinstance(nd, float) and np.isnan(nd)):
        invalid = ~np.isfinite(a)
    else:
        invalid = (a == nd) | ~np.isfinite(a)
    invalid |= a <= -32767

    a = np.where(invalid, 0.0, a)
    sh = hillshade(a, res)
    rgb = colorize(a) * (0.35 + 0.65 * np.clip(sh, 0, 1))[..., None]
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)

    if bg == "white":
        rgb[invalid] = (255, 255, 255)
        img = Image.fromarray(rgb, "RGB")
    else:
        alpha = np.where(invalid, 0, 255).astype(np.uint8)
        rgba = np.dstack([rgb, alpha])
        img = Image.fromarray(rgba, "RGBA")
    print(f"  nodata 占比 {100 * invalid.mean():.1f}% -> {'白色' if bg == 'white' else '透明'}")
    w, h = img.size
    if max(w, h) > maxside:
        s = maxside / max(w, h)
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    img.save(out)
    print("预览图:", out, img.size, img.mode)


if __name__ == "__main__":
    main()
