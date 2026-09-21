#!/usr/bin/env python3
"""输出 DEM 统计报告 + 元数据（JSON，可选文本/CSV 直方图）。

用法:
  python dem_stats.py dem.tif [--json out.json] [--csv hist.csv] [--bin 50] [--quiet]

JSON 内容:
  source / crs / 分辨率 / bbox / 有效面积(km²) / nodata 占比 /
  高程 min,max,mean,median,std,p10,p25,p75,p90 / 分带直方图 /
  垂直基准(默认 EGM2008) / 生成时间与所用参数
"""
import sys
import json
import math
import datetime
import numpy as np
import rasterio

OPT_WITH_VALUE = ("--json", "--csv", "--bin", "--datum")


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


def pixel_area_km2(transform, crs, height, width):
    """单像元面积 km²（地理坐标系按纬度逐行换算）"""
    if crs and crs.is_projected:
        return abs(transform.a * transform.e) / 1e6
    ys = transform.f + (np.arange(height) + 0.5) * transform.e
    lat = np.radians(ys)
    dx = abs(transform.a) * 111320.0 * np.cos(lat)
    dy = abs(transform.e) * 110574.0
    return float((dx * dy).mean() / 1e6)


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if not pos:
        print(__doc__)
        sys.exit(1)
    src = pos[0]
    bin_m = float(opts.get("bin", 50))
    quiet = "quiet" in flags

    with rasterio.open(src) as ds:
        a = ds.read(1).astype(np.float32)
        nd = ds.nodata
        crs = ds.crs
        tr = ds.transform
        h, w = ds.height, ds.width
        bounds = ds.bounds

    invalid = ~np.isfinite(a) if (nd is None or np.isnan(nd)) else (a == nd)
    v = a[~invalid]
    area_km2 = pixel_area_km2(tr, crs, h, w)

    if v.size == 0:
        print("没有有效像元")
        sys.exit(1)

    qs = np.percentile(v, [10, 25, 50, 75, 90])
    lo = float(math.floor(v.min() / bin_m) * bin_m)
    hi = float(math.ceil(v.max() / bin_m) * bin_m)
    edges = np.arange(lo, hi + bin_m, bin_m)
    counts, _ = np.histogram(v, bins=edges)
    hist = [{"range": [float(edges[i]), float(edges[i + 1])],
             "count": int(counts[i]),
             "area_km2": round(float(counts[i]) * area_km2, 3)}
            for i in range(len(counts)) if counts[i] > 0]

    report = {
        "file": str(src),
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "Copernicus GLO-30 (Copernicus DSM, AWS Open Data)",
        "data_type": "DSM（含植被与建筑，非裸地表）",
        "vertical_datum": opts.get("datum", "EGM2008 orthometric height"),
        "crs": crs.to_string() if crs else None,
        "size": [w, h],
        "resolution": [abs(tr.a), abs(tr.e)],
        "resolution_unit": "degree" if (crs is None or crs.is_geographic) else "meter",
        "bounds": [float(bounds.left), float(bounds.bottom),
                   float(bounds.right), float(bounds.top)],
        "pixel_area_km2": round(area_km2, 8),
        "nodata_value": (None if nd is None or np.isnan(nd) else float(nd)),
        "valid_pixels": int(v.size),
        "total_pixels": int(a.size),
        "nodata_pct": round(100 * float(invalid.mean()), 2),
        "area_km2": round(float(v.size) * area_km2, 3),
        "elevation_m": {
            "min": round(float(v.min()), 2),
            "max": round(float(v.max()), 2),
            "mean": round(float(v.mean()), 2),
            "median": round(float(qs[2]), 2),
            "std": round(float(v.std()), 2),
            "p10": round(float(qs[0]), 2), "p25": round(float(qs[1]), 2),
            "p75": round(float(qs[3]), 2), "p90": round(float(qs[4]), 2),
        },
        "histogram_bin_m": bin_m,
        "histogram": hist,
    }

    out_json = opts.get("json") or str(src).rsplit(".", 1)[0] + "_report.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("报告:", out_json)

    if opts.get("csv"):
        with open(opts["csv"], "w", encoding="utf-8") as f:
            f.write("from_m,to_m,count,area_km2\n")
            for r in hist:
                f.write(f"{r['range'][0]:.1f},{r['range'][1]:.1f},{r['count']},{r['area_km2']}\n")
        print("直方图 CSV:", opts["csv"])

    if not quiet:
        e = report["elevation_m"]
        print(f"有效像元 {report['valid_pixels']}/{report['total_pixels']} "
              f"({100 - report['nodata_pct']:.1f}%)  面积 {report['area_km2']:.2f} km²")
        print(f"高程 min {e['min']} / 中位 {e['median']} / max {e['max']} "
              f"/ mean {e['mean']} / std {e['std']} m")
        print(f"分带统计（{bin_m:.0f} m 间隔）: "
              + ", ".join(f"{r['range'][0]:.0f}~{r['range'][1]:.0f}m:{r['count']}"
                          for r in hist[:6]) + (" ..." if len(hist) > 6 else ""))


if __name__ == "__main__":
    main()
