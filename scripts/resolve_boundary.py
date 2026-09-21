#!/usr/bin/env python3
"""把中文区划名解析为 DataV adcode，并下载边界 GeoJSON。

支持省 / 市 / 县区三级：
  1) 100000_full.json  -> 省级
  2) {省adcode}_full   -> 地市级
  3) {市adcode}_full   -> 县区级
索引缓存在脚本同级 _adcode_cache.json（`--refresh` 强制重建）。

同名区划（如朝阳区在北京和长春都有）不会静默选错：会列出候选并要求用
`--parent 长春市` 或直接传 adcode 消歧。
"""
import sys
import json
import time
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

BASE = "https://geo.datav.aliyun.com/areas_v3/bound"
INDEX_URL = f"{BASE}/100000_full.json"
CACHE = Path(__file__).parent / "_adcode_cache.json"
CACHE_TTL = 7 * 86400  # 7 天
CACHE_VERSION = 3      # 索引结构变更时 +1，强制重建

# 备用名称索引：一次请求拿到省/市/县三级（DataV 级联要 370+ 次，容易触发限流）
PCAS_URL = "https://unpkg.com/china-division@2.5.0/dist/pcas-code.json"
BOUNDARY_CACHE = Path(__file__).parent.parent / "_cache" / "boundary"
OPT_WITH_VALUE = ("--parent", "--geojson")


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


BACKOFF = (5, 15, 40)   # DataV 限流时的退避秒数


def _get(url, timeout=30, retries=3, strict=False):
    """取 JSON。

    DataV 对高频请求会返回 403（IP 级临时限流），这里做退避重试。
    404 / 403(重试耗尽) 视为"不存在"返回 None；strict=True 时 403 直接抛错，
    避免残缺索引被静默写入缓存。
    """
    for i in range(retries + 1):
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code == 403:
            if strict:
                raise RuntimeError(f"DataV 限流(403): {url} —— 请稍后重试，"
                                   f"或删掉 _adcode_cache.json 前先等待几分钟")
            if i < retries:
                w = BACKOFF[min(i, len(BACKOFF) - 1)]
                print(f"  DataV 403 限流，{w}s 后重试 ({i + 1}/{retries})")
                time.sleep(w)
                continue
            return None
        r.raise_for_status()
    return None


def _names(adcode):
    """返回 {adcode}_full 下的 [{'adcode':..,'name':..}, ...]"""
    j = _get(f"{BASE}/{adcode}_full.json", retries=1, strict=True)
    if not j:
        return []
    return [{"adcode": f["properties"]["adcode"], "name": f["properties"].get("name", "")}
            for f in j.get("features", [])]


def _pad(code, width=6):
    return int(str(code).ljust(width, "0"))


def build_from_pcas(data):
    """pcas-code.json: [{'code':'11','name':'北京市','children':[...]}, ...]"""
    prov, cities, districts = [], [], []
    for p in data:
        pname = p["name"]
        prov.append({"adcode": _pad(p["code"]), "name": pname, "parent": None})
        for c in p.get("children", []):
            cname = c["name"]
            cities.append({"adcode": _pad(c["code"]), "name": cname,
                           "parent": pname, "province": pname})
            # "市辖区"/"县" 是虚拟层级，parent 直接显示省名更可读
            shown = pname if cname in ("市辖区", "县", "市", "省直辖县级行政区划") else cname
            for d in c.get("children", []):
                districts.append({"adcode": _pad(d["code"]), "name": d["name"],
                                  "parent": shown, "province": pname})
    if len(districts) < 2000:
        raise RuntimeError("备用索引数据不完整")
    print(f"  索引来自备用源（1 次请求）: {len(prov)} 省 / {len(cities)} 市 / {len(districts)} 区县")
    return {"time": time.time(), "version": CACHE_VERSION,
            "province": prov, "city": cities, "district": districts}


def build_from_datav():
    print("  备用源不可用，回退 DataV 级联（370+ 次请求，可能被限流）")
    prov = [dict(p, parent=None) for p in _names(100000)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        city_groups = list(ex.map(lambda p: _names(p["adcode"]), prov))
    cities = []
    for p, grp in zip(prov, city_groups):
        for c in grp:
            cities.append(dict(c, parent=p["name"], province=p["name"]))
    with ThreadPoolExecutor(max_workers=16) as ex:
        dist_groups = list(ex.map(lambda c: _names(c["adcode"]), cities))
    districts = []
    for c, grp in zip(cities, dist_groups):
        for d in grp:
            districts.append(dict(d, parent=c["name"], province=c.get("province")))
    return {"time": time.time(), "version": CACHE_VERSION,
            "province": prov, "city": cities, "district": districts}


def build_index():
    try:
        r = requests.get(PCAS_URL, timeout=60)
        if r.status_code == 200:
            return build_from_pcas(r.json())
    except Exception as e:
        print("  备用索引源失败:", type(e).__name__, e)
    return build_from_datav()


def load_index(refresh=False):
    if not refresh and CACHE.exists():
        try:
            c = json.loads(CACHE.read_text(encoding="utf-8"))
            fresh = time.time() - c.get("time", 0) < CACHE_TTL
            if fresh and c.get("district") and c.get("version") == CACHE_VERSION:
                return c
        except Exception:
            pass
    idx = build_index()
    CACHE.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    return idx


def candidates(idx, name):
    """返回 [(level, feature), ...]，精确匹配优先，其次包含匹配"""
    name = name.strip()
    hits = []
    for level in ("province", "city", "district"):
        for f in idx[level]:
            if f["name"] == name or str(f["adcode"]) == name:
                hits.append((level, f))
    if hits:
        return hits
    for level in ("district", "city", "province"):
        for f in idx[level]:
            if name and name in f["name"]:
                hits.append((level, f))
    hits.sort(key=lambda x: len(x[1]["name"]))
    return hits


def describe(level, f):
    if level == "district" and f.get("parent"):
        return f"{f['name']} ({f['parent']} / {f.get('province')}) adcode={f['adcode']}"
    if level == "city" and f.get("parent"):
        return f"{f['name']} ({f['parent']}) adcode={f['adcode']}"
    return f"{f['name']} ({level}) adcode={f['adcode']}"


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if not pos:
        print('用法: python resolve_boundary.py 瑶海区 [out.json] '
              '[--parent 合肥市] [--list] [--refresh]')
        sys.exit(1)
    name = pos[0]
    out = pos[1] if len(pos) > 1 else f"{name}.json"
    parent = opts.get("parent")
    refresh = "refresh" in flags
    list_only = "list" in flags

    idx = load_index(refresh=refresh)
    hits = candidates(idx, name)
    if not hits:
        print("找不到区划:", name, "（可加 --refresh 重建索引）")
        sys.exit(1)

    if parent:
        narrowed = [h for h in hits
                    if parent in (h[1].get("parent") or "") or parent in (h[1].get("province") or "")]
        hits = narrowed or hits
        if not narrowed:
            print(f"警告: --parent {parent} 未匹配到，回退到全部候选")

    if list_only:
        for level, f in hits:
            print(describe(level, f))
        return

    if len(hits) > 1:
        print(f"「{name}」有多个同名区划，请消歧:")
        for level, f in hits:
            print("  -", describe(level, f))
        print("用 --parent <上级名> 指定，或直接用 adcode（如 110105）")
        sys.exit(2)

    level, f = hits[0]
    adcode = f["adcode"]

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    # 1) 用户自备边界  2) 本地缓存  3) DataV 下载
    if opts.get("geojson"):
        src = Path(opts["geojson"])
        Path(out).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"使用自备边界: {src} -> {out}")
        return

    BOUNDARY_CACHE.mkdir(parents=True, exist_ok=True)
    cached = BOUNDARY_CACHE / f"{adcode}.json"
    if cached.exists() and "refresh" not in flags:
        Path(out).write_text(cached.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"边界命中缓存: {cached} -> {out}")
        return

    data = _get(f"{BASE}/{adcode}_full.json") or _get(f"{BASE}/{adcode}.json")
    if not data:
        print(f"边界下载失败: {adcode}（DataV 可能限流）。"
              f"可稍后重试，或用 --geojson <文件> 自备边界")
        sys.exit(3)
    cached.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    Path(out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    n = len(data.get("features", []))
    print(f"{f['name']}({level}, {f.get('parent') or '-'}) adcode={adcode} "
          f"features={n} -> {out}")


if __name__ == "__main__":
    main()
