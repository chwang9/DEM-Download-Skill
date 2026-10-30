#!/usr/bin/env python3
"""一条命令跑完: 区划名 -> 边界 -> 下载 GLO-30 -> 裁切 -> 预览图

用法:
  python cn_dem.py <区划名/adcode> [输出目录] [选项]
  python cn_dem.py <任意名字> [输出目录] --bbox "minx,miny,maxx,maxy" [选项]

不传输出目录时产物直接落在当前目录。例:
  cn_dem.py 瑶海区 out                合肥瑶海区
  cn_dem.py 合肥市 out                全市
  cn_dem.py hefei out --bbox "117.2,31.8,117.45,32.05"   按经纬度取数
  cn_dem.py 朝阳区 out --parent 长春市  同名区划消歧

输出坐标系固定为 WGS84 地理坐标（EPSG:4326），不做任何投影变换。

产物: <名字>_dem.tif(成品) / <名字>_dem_preview.png(预览) /
      <名字>.json(边界) / <名字>_raw.tif(中间文件，可删)

常用选项:
  --parent 合肥市     同名区划消歧（如朝阳区有北京/长春两个）
  --buffer 500        边界外扩 500 m
  --nodata -9999      用数值代替 NaN 作为 nodata（部分软件对 NaN 支持差）
  --fill              对内部空洞插值补洞
  --strict            只保留中心落在边界内的像元（默认 all_touched）
  --no-fill           合并瓦片时不补洞
  --bg white          预览图用白底（默认透明）
  --refresh           重新解析行政边界
  --clean-cache       仅清空瓦片缓存后退出（不下载，用于腾空间）
  --cache <dir>       指定瓦片缓存目录（默认 skill 内 _cache/tiles）
  --workers 16        下载并发连接数

注: 旧版的 --utm / --epsg / --res 投影选项已废弃，传入会被忽略（提示后继续）。
"""
import sys
import subprocess
from pathlib import Path

HERE = Path(__file__).parent

RESOLVE_OPTS = {"--parent": 1, "--refresh": 0, "--geojson": 1}
FETCH_OPTS = {"--no-fill": 0, "--clean-cache": 0, "--cache": 1, "--workers": 1}
CLIP_OPTS = {"--no-convert": 0, "--strict": 0, "--fill": 0, "--nodata": 1,
             "--buffer": 1, "--utm": 0, "--epsg": 1, "--res": 1}
PREVIEW_OPTS = {"--bg": 1}
AREA_OPTS = {"--bbox": 1}


def bbox_geojson(name, spec):
    """'minx,miny,maxx,maxy' -> GeoJSON Polygon（坐标为 WGS84）"""
    import json
    nums = [float(x) for x in spec.replace(";", ",").split(",")]
    if len(nums) != 4:
        raise ValueError("--bbox 需要 4 个数: minx,miny,maxx,maxy")
    minx, miny, maxx, maxy = nums
    ring = [[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature",
                          "properties": {"name": name},
                          "geometry": {"type": "Polygon", "coordinates": [ring]}}]}


def split_opts(argv, table):
    """从 argv 中挑出属于本步骤的选项（含带值选项的参数）"""
    picked, rest, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a in table:
            n = table[a]
            picked += argv[i:i + 1 + n]
            i += 1 + n
        else:
            rest.append(a)
            i += 1
    return picked, rest


def run(cmd):
    print("+", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(r.returncode)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if not argv:
        print(__doc__)
        sys.exit(1)
    name = argv[0]
    rest = argv[1:]
    workdir = Path("." if not rest or rest[0].startswith("--") else rest[0])
    if not (not rest or rest[0].startswith("--")):
        rest = rest[1:]
    workdir.mkdir(parents=True, exist_ok=True)

    a_opts, rest = split_opts(rest, AREA_OPTS)
    r_opts, rest = split_opts(rest, RESOLVE_OPTS)
    f_opts, rest = split_opts(rest, FETCH_OPTS)

    # 仅清空瓦片缓存（维护操作），不下载、不裁切
    if "--clean-cache" in f_opts:
        clean_cmd = [sys.executable, str(HERE / "fetch_cop30.py"), "--clean-cache"]
        if "--cache" in f_opts:
            ci = f_opts.index("--cache")
            clean_cmd += [f_opts[ci], f_opts[ci + 1]]
        run(clean_cmd)
        print("\n缓存已清空（仅维护操作，未触发下载）。不带 --clean-cache 重新运行即可下载。")
        sys.exit(0)

    c_opts, rest = split_opts(rest, CLIP_OPTS)
    p_opts, rest = split_opts(rest, PREVIEW_OPTS)
    if rest:
        print("未识别的参数:", rest)

    geojson = workdir / f"{name}.json"
    if a_opts:
        # bbox 模式：不查行政区划，直接用用户给的 WGS84 经纬度范围
        import json as _json
        gj = bbox_geojson(name, a_opts[1])
        workdir.mkdir(parents=True, exist_ok=True)
        geojson.write_text(_json.dumps(gj, ensure_ascii=False), encoding="utf-8")
        print(f"按 bbox 取数: {a_opts[1]} -> {geojson}")
        # 用户给的通常是 WGS84，跳过 GCJ-02 -> WGS84 转换
        if "--no-convert" not in c_opts:
            c_opts = c_opts + ["--no-convert"]
        r_opts = []
    raw = workdir / f"{name}_raw.tif"
    dem = workdir / f"{name}_dem.tif"
    png = workdir / f"{name}_dem_preview.png"

    py = sys.executable
    if not a_opts:
        run([py, str(HERE / "resolve_boundary.py"), name, str(geojson)] + r_opts)
    run([py, str(HERE / "fetch_cop30.py"), str(geojson), str(raw)] + f_opts)
    run([py, str(HERE / "clip_by_boundary.py"), str(raw), str(geojson), str(dem)] + c_opts)
    run([py, str(HERE / "preview_dem.py"), str(dem), str(png)] + p_opts)

    print("\n成品:")
    for p in (dem, png, geojson, raw):
        if p.exists():
            print(f"  {p}  ({p.stat().st_size / 1048576:.2f} MB)")


if __name__ == "__main__":
    main()
