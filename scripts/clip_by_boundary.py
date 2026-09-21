#!/usr/bin/env python3
"""用行政边界 GeoJSON 掩膜裁切 DEM，**输出固定为 WGS84 地理坐标（EPSG:4326）**。

DataV 边界为 GCJ-02，Copernicus GLO-30 为 WGS84（EPSG:4326），
默认先把边界反解到 WGS84 再裁切，避免几百米偏移（`--no-convert` 关闭）。

黑边修复要点：GLO-30 瓦片未声明 nodata，若直接 mask 会把边界外填成 0，
在 GIS 里就是一圈比周边低几十米的深色"黑边"。这里强制以 NaN（或 --nodata
指定的值）填充边界外区域，并可选对内部空洞做插值补洞。

默认 all_touched=True（边界上被擦到的像元都保留，避免出现锯齿状缺口），
加 `--strict` 改为只保留中心落在边界内的像元。可用 `--buffer 500` 外扩边界（米）。

本 skill 只输出地理坐标系：像元为经纬度格网（约 1/3600°，合肥处东西 ~24 m、
南北 ~31 m，非正方形）。需要投影坐标系时请在 GIS 软件或下游自行重投影。

选项: --no-convert  --strict  --fill  --nodata <值>  --buffer <米>
"""
import os
import sys
import json
import math
import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.fill import fillnodata
from rasterio.crs import CRS
from rasterio.warp import calculate_default_transform, reproject, Resampling
from shapely.geometry import shape
from shapely.affinity import scale

GCJ_A = 6378245.0
GCJ_EE = 0.00669342162296594323


def _out_of_china(lng, lat):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _tlat(x, y):
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _tlng(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lng, lat):
    """GCJ-02 -> WGS84，迭代反解，误差 < 1m"""
    if _out_of_china(lng, lat):
        return lng, lat
    wlng, wlat = lng, lat
    for _ in range(3):
        dlat = _tlat(wlng - 105.0, wlat - 35.0)
        dlng = _tlng(wlng - 105.0, wlat - 35.0)
        radlat = wlat / 180.0 * math.pi
        magic = math.sin(radlat)
        magic = 1 - GCJ_EE * magic * magic
        sqrtmagic = math.sqrt(magic)
        dlat = (dlat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtmagic) * math.pi)
        dlng = (dlng * 180.0) / (GCJ_A / sqrtmagic * math.cos(radlat) * math.pi)
        wlng = lng - dlng
        wlat = lat - dlat
    return wlng, wlat


def convert_geom(geom):
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Point":
        return {"type": t, "coordinates": list(gcj02_to_wgs84(*c))}
    if t in ("MultiPoint", "LineString"):
        return {"type": t, "coordinates": [list(gcj02_to_wgs84(*p)) for p in c]}
    if t in ("MultiLineString", "Polygon"):
        return {"type": t, "coordinates": [[list(gcj02_to_wgs84(*p)) for p in ring] for ring in c]}
    if t == "MultiPolygon":
        return {"type": t, "coordinates": [[[list(gcj02_to_wgs84(*p)) for p in ring] for ring in poly]
                                           for poly in c]}
    return geom


OPT_WITH_VALUE = ("--nodata", "--buffer")


def buffer_geom(g, meters):
    """按米外扩 GeoJSON 几何（先在经度方向做 cos(lat) 校正，保证近似等距）"""
    geom = shape(g)
    if geom.is_empty:
        return g
    lat = geom.centroid.y
    f = 1.0 / max(math.cos(math.radians(lat)), 1e-6)
    d = meters / 111320.0
    g2 = scale(geom, xfact=f, yfact=1.0, origin=(0, 0))
    g2 = g2.buffer(d)
    g2 = scale(g2, xfact=1.0 / f, yfact=1.0, origin=(0, 0))
    return json.loads(json.dumps(g2.__geo_interface__))


TARGET_CRS = CRS.from_epsg(4326)   # 本 skill 唯一输出坐标系：WGS84 经纬度
DEPRECATED_OPTS = {"utm": "--utm", "epsg": "--epsg", "res": "--res"}


def ensure_wgs84(raw_path):
    """输入若不是 EPSG:4326，先重投影到 4326（返回临时文件路径，用完需删）。

    正常情况下 fetch_cop30.py 产出的 raw 已经是 4326，这里只是防止用户拿
    投影过的底图直接喂进来，导致边界与影像对不上。
    """
    with rasterio.open(raw_path) as src:
        if src.crs is None or src.crs == TARGET_CRS:
            return raw_path, None
        src_crs = src.crs
        arr = src.read(1).astype(np.float32)
        src_transform = src.transform
        h, w = arr.shape
        bounds = rasterio.transform.array_bounds(h, w, src_transform)

    dst_transform, dst_w, dst_h = calculate_default_transform(
        src_crs, TARGET_CRS, w, h, *bounds)
    dst = np.full((dst_h, dst_w), np.nan, dtype=np.float32)
    reproject(arr, dst,
              src_transform=src_transform, src_crs=src_crs,
              dst_transform=dst_transform, dst_crs=TARGET_CRS,
              src_nodata=np.nan, dst_nodata=np.nan,
              resampling=Resampling.bilinear)
    from pathlib import Path
    tmp = Path(raw_path).with_name(Path(raw_path).stem + "_wgs84.tif")
    meta = {"driver": "GTiff", "dtype": "float32", "count": 1, "nodata": np.nan,
            "crs": TARGET_CRS, "height": dst_h, "width": dst_w,
            "transform": dst_transform, "compress": "deflate", "tiled": True}
    with rasterio.open(tmp, "w", **meta) as f:
        f.write(dst[None, ...])
    print(f"输入 {src_crs.to_string()} 已重投影到 EPSG:4326 -> {tmp}")
    return str(tmp), str(tmp)


def clip_to_boundary(raw, boundary, out, do_convert, buffer_m,
                     all_touched, out_nodata, do_fill):
    """掩膜裁切并写出，输出坐标系固定 EPSG:4326"""
    gj = json.load(open(boundary, encoding="utf-8"))
    feats = gj["features"] if "features" in gj else [{"geometry": gj}]
    geoms = []
    for f in feats:
        g = f.get("geometry")
        if not g:
            continue
        if do_convert:
            g = convert_geom(g)
        if buffer_m:
            g = buffer_geom(g, buffer_m)
        geoms.append(g)
    if do_convert:
        print("边界已 GCJ-02 -> WGS84")
    if buffer_m:
        print(f"边界外扩 {buffer_m:.0f} m")

    with rasterio.open(raw) as src:
        # 关键：显式给 nodata，禁止 rasterio 用 0 填充边界外（否则出现黑边）
        out_image, out_transform = mask(src, geoms, crop=True, all_touched=all_touched,
                                        nodata=out_nodata, filled=True)
        out_meta = src.meta.copy()

    arr = out_image[0].astype(np.float32)
    arr[(arr <= -32767) | ~np.isfinite(arr)] = out_nodata   # 源里的 -32768 填充值

    invalid = np.isnan(arr) if np.isnan(out_nodata) else (arr == out_nodata)
    valid = arr[~invalid]
    print(f"有效像元 {valid.size}/{arr.size} ({100 * valid.size / arr.size:.1f}%)")
    if valid.size:
        print(f"高程统计: min={valid.min():.1f} max={valid.max():.1f} mean={valid.mean():.1f} m")

    if do_fill and valid.size and invalid.any():
        filled = fillnodata(arr, mask=~invalid, max_search_distance=100, smoothing_iterations=0)
        arr[invalid] = filled[invalid]
        print(f"  补洞 {int(invalid.sum())} 像元")

    # 输出坐标系：WGS84 经纬度（度）。像元非正方形，附换算后的实际尺寸供参考
    lat_c = out_transform.f + arr.shape[0] / 2 * out_transform.e
    dx_m = abs(out_transform.a) * 111320.0 * math.cos(math.radians(lat_c))
    dy_m = abs(out_transform.e) * 110574.0
    out_meta.update({
        "dtype": "float32",
        "nodata": out_nodata,
        "count": 1,
        "crs": TARGET_CRS,
        "height": arr.shape[0],
        "width": arr.shape[1],
        "transform": out_transform,
        "compress": "deflate",
        "tiled": True,
    })
    out_image = arr[None, ...].astype(np.float32)
    with rasterio.open(out, "w", **out_meta) as dst:
        dst.write(out_image)
    print(f"裁切完成: {out}  CRS=EPSG:4326  像元≈{abs(out_transform.a):.7f}°"
          f"（约 {dx_m:.1f} m 东西 × {dy_m:.1f} m 南北）  nodata={out_nodata}")


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


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if len(pos) < 3:
        print("用法: python clip_by_boundary.py raw.tif boundary.json out.tif "
              "[--no-convert] [--strict] [--fill] [--nodata -9999]")
        sys.exit(1)

    raw, boundary, out = pos[0], pos[1], pos[2]
    do_convert = "no-convert" not in flags
    all_touched = "strict" not in flags
    do_fill = "fill" in flags
    nd_opt = opts.get("nodata")
    out_nodata = float(nd_opt) if nd_opt else np.nan
    buffer_m = float(opts["buffer"]) if opts.get("buffer") else 0.0
    # 旧版投影选项已废弃：本 skill 统一输出 WGS84，静默忽略并提示
    legacy = [DEPRECATED_OPTS[k] for k in ("utm", "epsg", "res") if k in flags or k in opts]
    if legacy:
        print(f"提示: {' / '.join(legacy)} 已废弃并忽略 —— 现统一输出 EPSG:4326 地理坐标")

    raw, tmp_wgs84 = ensure_wgs84(raw)
    try:
        clip_to_boundary(raw, boundary, out, do_convert, buffer_m, all_touched,
                         out_nodata, do_fill)
    finally:
        if tmp_wgs84:
            try:
                os.remove(tmp_wgs84)
            except OSError:
                pass


if __name__ == "__main__":
    main()
