#!/usr/bin/env python3
"""读 GeoJSON bbox，下载 Copernicus GLO-30 1°瓦片并合并。

要点：
- GLO-30 COG 瓦片是 float32 且通常未声明 nodata，水体/境外区域会写成 -32768。
  合并时显式传入 nodata，避免相邻瓦片边缘留 nodata 黑缝。
- 默认用 GDAL FillNodata 对残余空洞插值补洞（`--no-fill` 关闭）。
- 瓦片默认缓存在 skill 目录 `_cache/tiles/`（跨工作目录共享，换目录不用重下），
  `--cache <dir>` 指定其他位置。
- **`--clean-cache`：仅清空瓦片缓存后退出**（维护操作，不触发下载），可直接
  `python fetch_cop30.py --clean-cache` 或 `cn_dem.py <区划> --clean-cache`。
  旧版 cwd 下的 `_cop30_tiles/` 会被自动迁移进共享缓存。

下载内核（v2.3 提速）：
- 走 `curl` 分块并行下载（本机网关对单连接大响应会限速 + 截断，整块 requests.get
  下载会被掐断成损坏文件还不报错）。
- 每块用 HTTP Range 拉取并做**字节级大小校验**，大小不符即重试；
  持续截断的块自动**二分细分**（512KiB→256KiB）直到能在代理窗口内传完。
- **所有瓦片的所有分块共享一个全局线程池**（`--workers`，默认 16）跨瓦片并行，
  多瓦片区域不再逐瓦片排队；瓦片大小探测（HEAD）也并入同一连接池**并行执行**，
  消除多瓦片区域逐片串行排队的启动延迟。
- curl 超时**封顶 110s**：被网关截断的段快速失败→快速重试/二分，不再干等。
- 无 curl 环境（`shutil.which("curl") is None`）自动回退到 requests 整块下载。
"""
import sys
import json
import math
import shutil
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.fill import fillnodata
from shapely.geometry import shape

try:
    import requests  # 仅用于无 curl 时的回退路径
except Exception:
    requests = None

BUCKET_HTTPS = "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com"
NODATA_RAW = -32768.0          # Copernicus 惯用填充值
DEFAULT_CACHE = Path(__file__).parent.parent / "_cache" / "tiles"
LEGACY_DIR = "_cop30_tiles"
OPT_WITH_VALUE = ("--cache", "--workers", "--chunk-mb")
CURL = shutil.which("curl")

# 代理对单连接大响应会限速(~17KB/s)并约 90s 截断；512KiB 在 8 并发下约 56s 落于窗口内。
DEFAULT_CHUNK = 512 * 1024     # 512 KiB
MIN_CHUNK = 256 * 1024         # 最小细分到 256 KiB
DEFAULT_WORKERS = 16


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


# ---------- 下载内核（curl 分块并行 + 校验 + 自适应二分） ----------

def _have_curl():
    return CURL is not None


def get_tile_size(url, timeout=30):
    """返回瓦片总字节数；不存在/出错返回 None（视为无此瓦片）。"""
    if not _have_curl():
        return None
    try:
        r = subprocess.run(
            ["curl", "-sI", "-r", "0-0", url],
            capture_output=True, timeout=timeout + 10)
        head = r.stdout.decode("utf-8", "ignore")
        for line in head.splitlines():
            if line.lower().startswith("content-range:"):
                # Content-Range: bytes 0-0/46759232
                total = line.split("/")[-1].strip()
                try:
                    return int(total)
                except ValueError:
                    pass
        # 退化：普通 HEAD 取 Content-Length
        r2 = subprocess.run(
            ["curl", "-sI", url], capture_output=True, timeout=timeout + 10)
        for line in r2.stdout.decode("utf-8", "ignore").splitlines():
            if line.lower().startswith("content-length:"):
                try:
                    return int(line.split(":")[1].strip())
                except ValueError:
                    pass
    except Exception:
        return None
    return None


def _download_chunk(url, start, end, out_path, timeout):
    """下载 [start,end] 闭区间到 out_path；大小精确一致返回 True，否则清理并返回 False。"""
    try:
        subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-r", f"{start}-{end}", url, "-o", str(out_path)],
            check=False, timeout=timeout + 30)
    except Exception:
        pass
    if out_path.exists():
        if out_path.stat().st_size == (end - start + 1):
            return True
        out_path.unlink()
    return False


def _fetch_rec(url, s, e, tmp):
    """递归下载 [s,e]：成功返回 [(start, path)]；持续截断则二分细分；
    极小块仍失败返回 [(s, None)] 标记失败。"""
    sz = e - s + 1
    # 代理约 90s 硬截断单连接。超时按最差 ~6KB/s 估算，但**封顶 110s**：
    # 任何一段若未在 ~110s 内传完，基本已被代理截断，与其干等不如快速失败→重试/二分。
    timeout = min(110, max(60, int(sz / 6000) + 25))
    p = tmp / f"seg_{s}_{e}"
    if _download_chunk(url, s, e, p, timeout):
        return [(s, p)]
    for _ in range(2):  # 简单重试
        if _download_chunk(url, s, e, p, timeout):
            return [(s, p)]
    if sz > MIN_CHUNK:
        mid = (s + e) // 2
        return _fetch_rec(url, s, mid, tmp) + _fetch_rec(url, mid + 1, e, tmp)
    # 已到最小块仍失败：再试一次
    if _download_chunk(url, s, e, p, timeout):
        return [(s, p)]
    return [(s, None)]


def _concat(segs, dest, expected_size):
    """把 [(start, path)] 按 start 升序拼接，校验总字节后写入 dest。返回是否成功。"""
    segs = sorted(segs, key=lambda x: x[0] if x[1] else -1)
    if any(p is None for _, p in segs):
        return False
    total = 0
    with dest.open("wb") as fo:
        for _, p in segs:
            data = p.read_bytes()
            fo.write(data)
            total += len(data)
    if expected_size is not None and total != expected_size:
        print(f"  [warn] 拼接字节 {total} != 期望 {expected_size}，删除损坏文件")
        dest.unlink()
        return False
    return True


def _tile_segments(size, chunk_bytes):
    """返回单瓦片顶层分块 [(s,e), ...]"""
    n = math.ceil(size / chunk_bytes)
    return [(i * chunk_bytes, min((i + 1) * chunk_bytes, size) - 1) for i in range(n)]


def download_all(tiles, cache_dir, chunk_bytes, workers):
    """所有瓦片的分块共享一个全局线程池（连接数=workers）并行下载。
    已缓存的瓦片直接命中；返回成功落盘的 dest Path 列表。

    相比「逐瓦片各自开池串行」，全局池让多瓦片区域（市/省）在带宽充足的
    网络下真正并行，避免瓦片间排队。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    planned = []      # [(name, url, size, dest)]
    cached_paths = []
    to_fetch = []     # [(name, url, dest)]  未缓存需要下载的瓦片
    for name, url in tiles:
        dest = cache_dir / f"{name}.tif"
        if dest.exists() and dest.stat().st_size > 0:
            cached_paths.append(dest)
            print("cache", name)
        else:
            to_fetch.append((name, url, dest))

    if to_fetch:
        if not _have_curl():
            for name, url, dest in to_fetch:
                if download_requests(url, dest):
                    cached_paths.append(dest)
                    print("ok(req)", name)
                else:
                    print("skip (无瓦片或失败)", name)
        else:
            # 并行探测各瓦片大小（HEAD/Content-Range），避免多瓦片区域逐片串行排队
            size_futs = {}
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                for rec in to_fetch:
                    size_futs[ex.submit(get_tile_size, rec[1])] = rec
                for f in as_completed(size_futs):
                    rec = size_futs[f]
                    size = f.result()
                    if size:
                        planned.append((rec[0], rec[1], size, rec[2]))
                    else:
                        print("skip (无瓦片)", rec[0])

    if not planned:
        return cached_paths

    results = {}      # name -> list of (start, path)
    tmp_dirs = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {}
        for name, url, size, dest in planned:
            tmp = dest.parent / (dest.stem + ".parts")
            tmp.mkdir(parents=True, exist_ok=True)
            tmp_dirs[name] = tmp
            for (s, e) in _tile_segments(size, chunk_bytes):
                f = ex.submit(_fetch_rec, url, s, e, tmp)
                futures[f] = name
        for f in as_completed(futures):
            results.setdefault(futures[f], []).extend(f.result())

    for name, url, size, dest in planned:
        tmp = tmp_dirs[name]
        ok = _concat(results.get(name, []), dest, size)
        shutil.rmtree(tmp, ignore_errors=True)
        if ok:
            print("ok", name)
            cached_paths.append(dest)
        else:
            print("skip (失败)", name)
    return cached_paths


# ---------- 无 curl 回退（requests 整块，供直连环境使用） ----------

def download_requests(url, dest, retries=3):
    if not requests:
        return None
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=300)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            dest.write_bytes(r.content)
            return dest
        except Exception as e:
            print("  下载失败重试:", type(e).__name__, e)
    return None


def clear_cache(cache_dir):
    """清空瓦片缓存（维护操作）。打印被删除的瓦片数与释放空间，避免静默删除。"""
    if cache_dir.exists():
        files = list(cache_dir.glob("*.tif"))
        n = len(files)
        size = sum(f.stat().st_size for f in files)
        shutil.rmtree(cache_dir)
        print(f"已清空缓存: {cache_dir}  ({n} 个瓦片, 释放 {size / 1048576:.1f} MB)")
    else:
        print(f"缓存目录不存在，无需清理: {cache_dir}")


def main():
    """一键入口（CLI）"""
    pos, opts, flags = parse_args(sys.argv[1:])
    cache_dir = Path(opts["cache"]) if opts.get("cache") else DEFAULT_CACHE

    # 清除缓存：仅维护操作，清空后直接退出（不触发下载）
    if "clean-cache" in flags:
        clear_cache(cache_dir)
        sys.exit(0)

    if len(pos) < 2:
        print("用法: python fetch_cop30.py input.json output_raw.tif "
              "[--no-fill] [--cache <dir>] [--workers 16] [--chunk-mb 0.5] [--clean-cache]")
        print("      python fetch_cop30.py --clean-cache [--cache <dir>]   仅清空瓦片缓存后退出")
        sys.exit(1)
    do_fill = "no-fill" not in flags
    workers = max(1, int(float(opts.get("workers", DEFAULT_WORKERS))))
    chunk_bytes = int(float(opts.get("chunk-mb", DEFAULT_CHUNK / 1024 / 1024)) * 1024 * 1024)
    chunk_bytes = max(MIN_CHUNK, chunk_bytes)

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
    print(f"需要 {len(tiles)} 个 GLO-30 瓦片（缓存命中 {cached}），并发={workers} 分块={chunk_bytes//1024}KiB")
    if not _have_curl():
        print("[info] 未检测到 curl，使用 requests 单连接下载（可能受网关限速/截断）")

    paths = download_all(tiles, cache_dir, chunk_bytes, workers)
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
