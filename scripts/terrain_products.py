#!/usr/bin/env python3
"""从 DEM 导出地形衍生栅格：坡度(度) / 坡向(度) / 山体阴影(0-255)。

用法:
  python terrain_products.py dem.tif [--slope] [--aspect] [--hillshade] [--all]
                             [--az 315] [--alt 45] [--z-factor 1.0] [--outdir <dir>]

输出与输入同目录: <stem>_slope.tif / <stem>_aspect.tif / <stem>_hillshade.tif
- 坡度/坡向: float32，nodata 沿用输入
- 山体阴影: uint8，nodata=0
- 坡向按 ESRI 约定：正北=0°，顺时针（东=90、南=180、西=270）
- 输入为 WGS84 地理坐标（EPSG:4326），按中心纬度把度换算成米；经纬度格网
  在东西/南北方向尺寸不同（合肥处约 24 m × 31 m），坡度按各自尺度计算
"""
import sys
import math
import numpy as np
import rasterio

OPT_WITH_VALUE = ("--az", "--alt", "--z-factor", "--outdir")


def parse_args(argv):
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


def pixel_sizes(transform, crs, height, width):
    """返回 (dx_m, dy_m)：像元的东西向 / 南北向实际尺寸（米）"""
    if crs and crs.is_projected and crs.linear_units_factor:
        unit_m = crs.linear_units_factor[1] if isinstance(crs.linear_units_factor, tuple) else 1.0
        return abs(transform.a) * unit_m, abs(transform.e) * unit_m
    # 地理坐标系：按中心纬度换算
    lat_c = transform.f + height / 2 * transform.e
    dx = abs(transform.a) * 111320.0 * math.cos(math.radians(lat_c))
    dy = abs(transform.e) * 110574.0
    return dx, dy


def gradients(dem, dx, dy, nodata):
    """返回 (dz_dx, dz_dy)：向东 / 向南（行增大方向）的高程变化率"""
    fill = np.where(np.isfinite(dem), dem, 0.0) if nodata is None or np.isnan(nodata) \
        else np.where(dem == nodata, 0.0, dem)
    d_row, d_col = np.gradient(fill, dy, dx)   # axis0=行(向南), axis1=列(向东)
    return d_col, d_row


def slope_aspect(dzdx, dzdy, z_factor=1.0):
    dzdx = dzdx * z_factor
    dzdy = dzdy * z_factor
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    a = np.degrees(np.arctan2(dzdy, -dzdx))    # ESRI: atan2([dz/dy], -[dz/dx])
    aspect = np.where(a < 0, 90.0 - a,
                      np.where(a > 90.0, 360.0 - a + 90.0, 90.0 - a))
    return slope.astype(np.float32), (aspect % 360.0).astype(np.float32)


def hillshade(dzdx, dzdy, az=315.0, alt=45.0, z_factor=1.0):
    zf = z_factor
    slope = np.arctan(np.hypot(dzdx * zf, dzdy * zf))
    aspect = np.arctan2(dzdy, -dzdx)
    azr, altr = math.radians(az), math.radians(alt)
    hs = (np.sin(altr) * np.cos(slope)
          + np.cos(altr) * np.sin(slope) * np.cos(azr - aspect))
    return np.clip(hs * 255.0, 0, 255).astype(np.uint8)


def write_like(src_path, arr, out_path, dtype, nodata, count=1):
    with rasterio.open(src_path) as src:
        meta = src.meta.copy()
    meta.update({"dtype": dtype, "count": count, "nodata": nodata,
                 "compress": "deflate", "tiled": True})
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(arr[None, ...] if arr.ndim == 2 else arr)


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if not pos:
        print(__doc__)
        sys.exit(1)
    src_path = pos[0]
    want_all = "all" in flags
    want = {k: (f"--{k}" in sys.argv or want_all) for k in ("slope", "aspect", "hillshade")}
    if not any(want.values()):
        want = {"slope": True, "aspect": True, "hillshade": True}
    az = float(opts.get("az", 315))
    alt = float(opts.get("alt", 45))
    zf = float(opts.get("z-factor", 1.0))
    outdir = opts.get("outdir")

    from pathlib import Path
    p = Path(src_path)
    out_dir = Path(outdir) if outdir else p.parent

    with rasterio.open(src_path) as ds:
        dem = ds.read(1).astype(np.float32)
        nodata = ds.nodata
        crs = ds.crs
        dx, dy = pixel_sizes(ds.transform, crs, ds.height, ds.width)

    # 输入恒为 EPSG:4326；像元非正方形，dx/dy 已按纬度分别换算成米
    print(f"像元尺寸: 东西 {dx:.2f} m / 南北 {dy:.2f} m（WGS84 经纬度格网，非正方形）")

    invalid = ~np.isfinite(dem) if (nodata is None or np.isnan(nodata)) else (dem == nodata)
    dzdx, dzdy = gradients(dem, dx, dy, nodata)

    if want["slope"] or want["aspect"]:
        slope, aspect = slope_aspect(dzdx, dzdy, zf)
        if want["slope"]:
            slope[invalid] = np.nan
            op = str(out_dir / f"{p.stem}_slope.tif")
            write_like(src_path, slope, op, "float32", np.nan)
            v = slope[np.isfinite(slope)]
            print(f"坡度 -> {op}  均值 {v.mean():.2f}°  最大 {v.max():.1f}°")
        if want["aspect"]:
            aspect[invalid] = np.nan
            op = str(out_dir / f"{p.stem}_aspect.tif")
            write_like(src_path, aspect, op, "float32", np.nan)
            print(f"坡向 -> {op}")
    if want["hillshade"]:
        hs = hillshade(dzdx, dzdy, az, alt, zf)
        hs[invalid] = 0
        op = str(out_dir / f"{p.stem}_hillshade.tif")
        write_like(src_path, hs, op, "uint8", 0)
        print(f"晕渲 -> {op}  (az={az} alt={alt})")


if __name__ == "__main__":
    main()
