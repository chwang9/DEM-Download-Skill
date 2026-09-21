#!/usr/bin/env python3
"""读 GeoJSON bbox，下载 Copernicus GLO-30 1°瓦片并合并。

要点：
- GLO-30 COG 瓦片是 float32 且通常未声明 nodata，水体/境外区域会写成 -32768。
  合并时显式传入 nodata，避免相邻瓦片边缘留 nodata 黑缝。
- 默认用 GDAL FillNodata 对残余空洞插值补洞（`--no-fill` 关闭）。
- 瓦片默认缓存在 skill 目录 `_cache/tiles/`（跨工作目录共享，换目录不用重下），
  `--cache <dir>` 指定其他位置，`--clean-cache` 清空。旧版 cwd 下的
  `_cop30_tiles/` 会被自动迁移进共享缓存。
- 多瓦片并发下载（默认 4 线程），带重试。
"""
import sys
import json
import math
import shutil
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.fill import fillnodata
from shapely.geometry import shape

BUCKET_HTTPS = "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com"
NODATA_RAW = -32768.0          # Copernicus 惯用填充值
DEFAULT_CACHE = Path(__file__).parent.parent / "_cache" / "tiles"
LEGACY_DIR = "_cop30_tiles"
OPT_WITH_VALUE = ("--cache", "--workers")


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


def migrate_legacy(cache_dir):
    """把旧版本留在 cwd 的 _cop30_tiles/ 迁到共享缓存，避免重复下载"""
    legacy = Path(LEGACY_DIR)
    if not legacy.is_dir():
        return 0
    n = 0
    for p in legacy.glob("*.tif"):
        dst = cache_dir / p.name
        if not dst.exists() and p.stat().st_size > 0:
            shutil.move(str(p), str(dst))
            n += 1
    return n


def tiles_for_bbox(minx, miny, maxx, maxy):
    out = []
    for lat in range(math.floor(miny), math.floor(maxy) + 1):
        for lon in range(math.floor(minx), math.floor(maxx) + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            name = f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"
            out.append((name, f"{BUCKET_HTTPS}/{name}/{name}.tif"))
    return out


def download(url, dest, retries=3):
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    for i in range(retries):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            dest.write_bytes(r.content)
            return dest
        except Exception as e:
            if i == retries - 1:
                print("  下载失败，跳过:", type(e).__name__, e)
                return None
            print(f"  重试 {i + 1}/{retries}: {type(e).__name__}")
    return None


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if len(pos) < 2:
        print("用法: python fetch_cop30.py input.json output_raw.tif "
              "[--no-fill] [--cache <dir>] [--workers 4] [--clean-cache]")
        sys.exit(1)
    do_fill = "no-fill" not in flags
    workers = int(opts.get("workers", 4))

    cache_dir = Path(opts["cache"]) if opts.get("cache") else DEFAULT_CACHE
    if "clean-cache" in flags and cache_dir.exists():
        shutil.rmtree(cache_dir)
        print("已清空缓存:", cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    moved = migrate_legacy(cache_dir)
    if moved:
        print(f"已迁移 {moved} 个旧瓦片到共享缓存")

    geojson = json.load(open(pos[0], encoding="utf-8"))
    out_tif = pos[1]

    geom = shape(geojson["features"][0]["geometry"]) if "features" in geojson else shape(geojson)
    minx, miny, maxx, maxy = geom.bounds

    tiles = tiles_for_bbox(minx, miny, maxx, maxy)
    cached = sum(1 for n, _ in tiles if (cache_dir / f"{n}.tif").exists())
    print(f"需要 {len(tiles)} 个 GLO-30 瓦片（缓存命中 {cached}）")

    def job(item):
        name, url = item
        p = download(url, cache_dir / f"{name}.tif")
        return name, p

    paths = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for name, p in ex.map(job, tiles):
            if p:
                paths.append(str(p))
                print("ok", name)
            else:
                print("skip (无瓦片)", name)
    if not paths:
        print("没有任何 DEM 瓦片，退出")
        sys.exit(1)

    srcs = [rasterio.open(p) for p in paths]
    try:
        # 显式 nodata：让 merge 在重叠/边缘处回退到有值的源，避免黑缝
        mosaic, transform = merge(srcs, nodata=NODATA_RAW, method="first")
        meta = srcs[0].meta.copy()
    finally:
        for s in srcs:
            s.close()

    mosaic = mosaic.astype(np.float32)
    bad = ~np.isfinite(mosaic) | (mosaic <= NODATA_RAW + 1)
    if bad.any():
        msg = f"  合并后无效像元 {int(bad.sum())} ({100 * bad.mean():.3f}%)"
        if do_fill:
            arr = mosaic[0]
            m = bad[0]
            filled = fillnodata(arr, mask=~m, max_search_distance=100,
                                smoothing_iterations=0)
            arr[m] = filled[m]
            mosaic[0] = arr
            left = int((~np.isfinite(mosaic) | (mosaic <= NODATA_RAW + 1)).sum())
            print(msg + f" -> 补洞后剩余 {left}")
        else:
            print(msg + " -> 未补洞")
    else:
        print("  无无效像元")

    meta.update({
        "dtype": "float32",
        "nodata": np.nan,
        "height": mosaic.shape[1],
        "width": mosaic.shape[2],
        "transform": transform,
        "compress": "deflate",
        "tiled": True,
    })
    with rasterio.open(out_tif, "w", **meta) as dst:
        dst.write(mosaic)
    print("合并完成:", out_tif, mosaic.shape[1:], "nodata=NaN")


if __name__ == "__main__":
    main()
