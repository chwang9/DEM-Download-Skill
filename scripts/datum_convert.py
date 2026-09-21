#!/usr/bin/env python3
"""DEM 垂直基准换算（Copernicus GLO-30 默认是 EGM2008 正高）。

用法:
  python datum_convert.py dem.tif out.tif --to ellipsoidal|cn1985 [--offset -0.15]

- ellipsoidal: EGM2008 正高 -> WGS84 椭球高（加上大地水准面高 N）。
  需要 PROJ 的 EGM2008 网格 `us_nga_egm08_25.tif`；首次运行会提示下载方式。
- cn1985: EGM2008 正高 -> 1985 国家高程基准。**中国大陆没有公开的官方偏移格网**，
  两者差异通常为 ±0.5 m 内且随区域变化，默认按 0 处理，请用 `--offset` 填入
  本地区实测校准值（例如某测区已知 EGM2008 比 1985 高 0.12 m，则 --offset -0.12）。
- `--offset <m>`: 在换算结果上再叠加常数偏移。
"""
import sys
import numpy as np
import rasterio
from pyproj import Transformer, CRS
from pyproj.transformer import TransformerGroup

OPT_WITH_VALUE = ("--to", "--offset")

# 优先用 2.5' 版 EGM2008 网格（77 MB）。PROJ 默认会挑 1'x1' 版（400+ MB），
# 这里用 vgridshift 管线显式指定网格，避免它去要那个大文件。
# 注意：vgridshift 的默认 multiplier 是 **-1**（会把改正量反过来），
# 官方 EGM2008(正高) -> WGS84(椭球高) 的定义用的是 +multiplier=1，必须显式写，
# 否则符号会反（合肥应是 -4.19 m，漏写会得到 +4.19 m）。
GRID = "us_nga_egm08_25.tif"
PIPELINE = ("+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
            f"+step +proj=vgridshift +grids={GRID} +multiplier=1 "
            "+step +proj=unitconvert +xy_in=rad +xy_out=deg")


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


def grid_path():
    import os
    import pyproj
    return os.path.join(pyproj.datadir.get_data_dir(), GRID)


def ensure_geoid_grid():
    """检查 EGM2008 网格是否就位，缺了给出下载指引"""
    import os
    p = grid_path()
    if os.path.exists(p) and os.path.getsize(p) > 80_000_000:
        return True
    print(f"缺少 PROJ 网格: {GRID}")
    print("下载: https://cdn.proj.org/us_nga_egm08_25.tif (约 77 MB) ->")
    print("  ", p)
    print("或加 --auto-download 由脚本自动下载")
    return False


def download_grid():
    import os
    import requests
    import pyproj
    url = "https://cdn.proj.org/" + GRID
    dst = grid_path()
    if os.path.exists(dst) and os.path.getsize(dst) > 80_000_000:
        print("网格已存在:", dst)
        return True
    print("下载", url)
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    print("已保存:", dst)
    return True


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if len(pos) < 2:
        print(__doc__)
        sys.exit(1)
    src, out = pos[0], pos[1]
    to = opts.get("to", "ellipsoidal")
    offset = float(opts.get("offset", 0.0))

    if to == "ellipsoidal" and ("auto-download" in flags or "--auto-download" in sys.argv):
        download_grid()
    if to == "cn1985" and offset == 0.0:
        print("提示: 未给 --offset，按 0 处理；1985 基准请填入本区实测校准值")

    with rasterio.open(src) as ds:
        dem = ds.read(1).astype(np.float32)
        nd = ds.nodata
        crs = ds.crs
        tr = ds.transform
        h, w = ds.height, ds.width
        meta = ds.meta.copy()

    invalid = ~np.isfinite(dem) if (nd is None or np.isnan(nd)) else (dem == nd)
    out_arr = dem.copy()

    if to == "cn1985":
        delta = np.full(dem.shape, offset, dtype=np.float32)
        note = f"1985 基准（常数偏移 {offset:+.3f} m，无格网）"
    else:
        if not ensure_geoid_grid():
            sys.exit(2)
        # 像元中心 -> 经纬度
        cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
        xs, ys = tr * (cols, rows)
        if crs and not crs.is_geographic:
            t2 = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
            lons, lats = t2.transform(xs.ravel(), ys.ravel())
        else:
            lons, lats = xs.ravel(), ys.ravel()
        tf = Transformer.from_pipeline(PIPELINE)
        vals = np.where(np.isfinite(dem), dem, 0.0).ravel()
        _, _, ell = tf.transform(lons, lats, vals)
        ell = np.asarray(ell, dtype=np.float32).reshape(dem.shape)
        delta = (ell - dem) + offset
        note = "WGS84 椭球高（EGM2008 大地水准面高 N）"

    out_arr[~invalid] = dem[~invalid] + delta[~invalid]
    out_arr[invalid] = np.nan

    meta.update({"dtype": "float32", "nodata": np.nan, "count": 1,
                 "compress": "deflate", "tiled": True})
    with rasterio.open(out, "w", **meta) as dst:
        dst.write(out_arr[None, ...])
    v = out_arr[np.isfinite(out_arr)]
    if v.size:
        print(f"换算: {note}")
        print(f"  改正量: min {delta[~invalid].min():+.2f} / "
              f"max {delta[~invalid].max():+.2f} / mean {delta[~invalid].mean():+.2f} m")
        print(f"  结果: min {v.min():.2f} / max {v.max():.2f} / mean {v.mean():.2f} m")
    print("已写出:", out)


if __name__ == "__main__":
    main()
