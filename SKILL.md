---
name: cn-dem
version: "2.0.0"
display_name: "中国 30 米 DEM 下载器"
display_name_en: "CN 30m DEM Downloader"
description_zh: >-
  按行政区划或经纬度范围下载中国 30 米 DEM（Copernicus GLO-30，免 Key），按边界裁切成 GeoTIFF 并出山体阴影预览图。
  支持坡度/坡向/晕渲导出、统计报告、EGM2008↔椭球高换算。输出坐标系固定 WGS84（EPSG:4326）。
description_en: >-
  Download 30m DEM for China (Copernicus GLO-30, no API key required) by admin division or bbox,
  clip to boundary as GeoTIFF, and generate hillshade preview. Supports slope/aspect/shaded relief,
  statistics report, and EGM2008↔ellipsoidal height conversion. Output is always EPSG:4326.
agent_created: true
argument-hint: "<区划名 或 adcode> [输出目录] [--buffer 1000] [--bbox minx,miny,maxx,maxy]"
---

# CN DEM Downloader (Copernicus GLO-30, Zero Key)

> **坐标系统一为 WGS84 地理坐标（EPSG:4326）**。所有产物一律是经纬度格网，
> 不再提供/接受 UTM 等投影选项。需要投影坐标系时在 QGIS/ArcGIS 里自行重投影。

## 用户会怎么提需求（先看这里）
大多数情况下一句 `cn_dem.py <名字> <输出目录>` 就够了，剩下的用默认值。拿到需求后按下面映射执行：

| 用户说法 | 怎么跑 |
|---|---|
| 「瑶海区的 DEM」/「下载瑶海区高程」 | `cn_dem.py 瑶海区 瑶海区` |
| 「合肥市 DEM」 | `cn_dem.py 合肥市 合肥市` |
| 「朝阳区的地形」（同名多地） | 先 `--parent` 消歧：`resolve_boundary.py 朝阳区 --list`，再 `cn_dem.py 朝阳区 朝阳区 --parent 长春市` |
| 「117.2,31.8 到 117.45,32.05 这段范围」 | `cn_dem.py hefei out --bbox "117.2,31.8,117.45,32.05"` |
| 「按我这个 shp/geojson 裁」 | `cn_dem.py area out --geojson my.json` |
| 「再给个坡度/统计/1985 高程」 | 在 dem.tif 上跑 `terrain_products.py --all` / `dem_stats.py` / `datum_convert.py` |

**用户没说清范围时不要直接报错** —— 用一句话问清楚（哪个区县/市，或给经纬度范围），并附上示例。
输出 float32 + NaN nodata，行政边界已做 GCJ-02→WGS84 校正，不会出现 0 值黑边。

## 数据源
- **DEM**: Copernicus GLO-30 Public on AWS Open Data（无需 Key）
  https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/
  瓦片名: Copernicus_DSM_COG_10_{N|S}xx_00_{E|W}xxx_00_DEM
- **名称索引**: 默认 unpkg `china-division/pcas-code.json`（**1 次请求**拿到 31 省 / 342 市 / 2990 区县）
- **行政边界几何**: 阿里 DataV GeoAtlas（GCJ-02）
  https://geo.datav.aliyun.com/areas_v3/bound/{adcode}.json

## 环境
优先使用隔离环境（Windows）:
  `C:/Users/wangch/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
依赖: `pip install -r requirements.txt`（rasterio / shapely / numpy / requests / tqdm /
Pillow / pyproj）。注意本机 bash 的 PATH 不完整，`ls`/`tail` 等不可用，
列文件可用 python 替代，pip 输出不要接管道。
高程基准换算还需要 PROJ 的 EGM2008 网格 `us_nga_egm08_25.tif`（77 MB，
`datum_convert.py --auto-download` 会自动下载到 pyproj 数据目录）。

## 流程
```bash
PY=C:/Users/wangch/.workbuddy/binaries/python/envs/default/Scripts/python.exe
S=C:/Users/wangch/.workbuddy/skills/cn-dem/scripts
$PY $S/cn_dem.py "瑶海区" out            # 一键：边界 -> 下载 -> 裁切 -> 预览
# 按经纬度范围取数（不经行政区划，坐标为 WGS84）
$PY $S/cn_dem.py hefei_bbox out --bbox "117.20,31.80,117.45,32.05"
# 或分步
$PY $S/resolve_boundary.py "瑶海区" out/瑶海区.json
$PY $S/fetch_cop30.py      out/瑶海区.json out/瑶海区_raw.tif
$PY $S/clip_by_boundary.py out/瑶海区_raw.tif out/瑶海区.json out/瑶海区_dem.tif
$PY $S/preview_dem.py      out/瑶海区_dem.tif out/瑶海区_dem_preview.png

# 可选后处理
$PY $S/terrain_products.py out/瑶海区_dem.tif --all          # 坡度/坡向/晕渲 GeoTIFF
$PY $S/dem_stats.py        out/瑶海区_dem.tif --csv hist.csv # 统计报告 + 直方图
$PY $S/datum_convert.py    out/瑶海区_dem.tif out/ellip.tif --to ellipsoidal
```

## 输出规格（所有产物统一）
| 项 | 值 |
|---|---|
| 坐标系 | **EPSG:4326（WGS84 经纬度）**，全部脚本不做其它投影 |
| 像元大小 | 约 0.0002778°（1/3600°），**非正方形**（合肥处约 26 m 东西 × 31 m 南北） |
| 数据类型 | float32 |
| nodata | NaN（可 `--nodata -9999` 改成数值） |
| 压缩 | deflate + tiled |
| 垂直基准 | EGM2008 正高（米） |
| 预览图 | RGBA PNG，边界外透明（`--bg white` 改白底） |

产物命名：`<名字>_dem.tif`（成品）/ `<名字>_dem_preview.png` / `<名字>.json`（边界）/
`<名字>_raw.tif`（未裁切中间文件，可删，约 40–160 MB）。

## 脚本说明
- `resolve_boundary.py`: 名称/adcode -> 边界 GeoJSON。
  - 同名区划（朝阳区北京/长春各一个）**不会静默选错**，会列出候选并要求
    `--parent 长春市` 或直接用 adcode；`--list` 只看候选。
  - 边界获取顺序：自备 `--geojson <file>` -> 本地缓存 `_cache/boundary/{adcode}.json`
    -> DataV 下载（下载成功即入缓存，之后离线可用）。
  - 名称索引缓存 `scripts/_adcode_cache.json`（7 天，`--refresh` 重建，结构变更自动失效）。
- `fetch_cop30.py`: 按 bbox 下载 1° 瓦片并合并（4 线程并发 + 重试）。
  - 瓦片缓存在 skill 目录 `_cache/tiles/`（**跨工作目录共享**，换目录不用重下 100MB），
    `--cache <dir>` 改位置、`--clean-cache` 清空；旧版 cwd 的 `_cop30_tiles/` 会自动迁移。
  - merge 显式传 nodata=-32768 防拼缝；输出 float32 / nodata=NaN；
    默认 FillNodata 补洞（`--no-fill` 关闭）。
  - 产物原生即 EPSG:4326，像元约 1/3600°。
- `clip_by_boundary.py`: 掩膜裁切，**输出恒为 EPSG:4326**。
  - **默认先把 GCJ-02 边界反解为 WGS84**（与 GLO-30 对齐，消除几百米偏移），`--no-convert` 跳过。
  - **黑边修复**：瓦片未声明 nodata，直接 mask 会把边界外填成 0（GIS 里即黑边），
    现强制 NaN 或 `--nodata <值>`；默认 all_touched=True 防锯齿（`--strict` 关闭）；`--fill` 补洞。
  - `--buffer 1000` 边界外扩 1000 m（按米计算，内部做 cos(lat) 校正）。
  - 若喂进来的底图不是 4326，会自动先重投影到 4326 再裁切（临时文件用完删除）。
  - 旧版 `--utm` / `--epsg` / `--res` 已废弃：传入只打印忽略提示，不再生效。
- `preview_dem.py`: 配色 + 山体阴影 PNG，nodata 输出为**透明**（RGBA），`--bg white` 改白底。
- `cn_dem.py`: 一键入口，选项透传给对应子脚本。`--bbox "minx,miny,maxx,maxy"`
  可跳过行政区划直接按经纬度取数（自动加 `--no-convert`，因用户输入是 WGS84）；
  `--geojson <file>` 用自备多边形。
- `terrain_products.py`: 导出坡度(度) / 坡向(度, 正北 0° 顺时针) / 晕渲(uint8) GeoTIFF，
  `--all` 一次全出；输入为 4326，内部按中心纬度把度换算成米（东西/南北分别算）。
- `dem_stats.py`: 统计报告 JSON（面积、高程分位数、分带直方图、CRS、分辨率、数据源、
  垂直基准）+ 可选 CSV 直方图。
- `datum_convert.py`: 垂直基准换算。`--to ellipsoidal` 用 EGM2008 网格把正高转
  WGS84 椭球高（合肥 N≈-4.2 m）；`--to cn1985` 走常数偏移（**无官方格网**，须用
  `--offset` 填本区实测校准值）。

## 黑边排查清单（影像有黑边时按此检查）
1. 裁切后边界外是 0 而不是 nodata？-> 瓦片无 nodata 声明，mask 必须显式传 nodata。
2. 瓦片拼接缝/水体是 -32768？-> merge 传 nodata=-32768 并 FillNodata 补洞。
3. PNG 预览黑边？-> 输出 RGBA，nodata 写 alpha=0。

## 限流与容错
- DataV 对高频请求返回 **403（IP 级临时限流）**，脚本会退避重试（5/15/40 s）。
  被封时优先走边界本地缓存，或用 `--geojson` 自备边界继续跑。
- 因此索引构建走一次请求的备用源，不要再用 370+ 次 DataV 级联（会触发封禁）。

## 注意
- **坐标系**：全部产物为 EPSG:4326 经纬度，像元约 1/3600° 且**不是正方形**
  （合肥处东西 ~24 m、南北 ~31 m）。要算面积、做工程图请在 GIS 中另行投影。
- GLO-30 是 **DSM**（含植被/建筑），不是裸地形 DTM；合肥城区因建筑均值约 34 m。
- 垂直基准是 **EGM2008 正高**（合肥 N≈-4.2 m，即椭球高比正高低约 4.2 m）。
  与国内 1985 国家高程基准差通常为 ±0.5 m 内但随区域变化，**没有公开官方格网**，
  工程使用请用实测点校准后填 `--offset`。
- **踩坑记录**: PROJ 的 `+proj=vgridshift` 默认 `multiplier=-1`（会把改正量反号），
  官方 EGM2008→椭球高定义显式用 `+multiplier=1`，漏写会得到符号相反的结果。
  PROJ 默认还想拉 1'x1' 版网格(400+ MB)，脚本已强制用 2.5' 版(77 MB)。
- 输出 float32；nodata 默认 NaN（ArcGIS 等若支持不好，加 `--nodata -9999`）。
- **导出/打包分享前**：先删掉 `scripts/__pycache__/`（以及 `_cache/`）。`cn-dem/scripts/__pycache__/*.pyc`
  属于三级目录，导入器会直接报「目录层级超限：Skill 包仅支持两级目录结构（根目录/二级目录/文件）」。
  合规结构只有 `cn-dem/SKILL.md` + `cn-dem/scripts/*.py` 这类两层。
- 海洋/境外无瓦片时跳过；可直接用 adcode 代替中文名（如 340102）。

## FAQ
- **要 UTM / CGCS2000 高斯投影怎么办？** 本 skill 不提供。用 QGIS 的「另存为」
  或 `gdalwarp -t_srs EPSG:32650 dem.tif dem_utm.tif` 自行转换。
- **`--utm` 还能用吗？** 能跑不报错，但会被忽略并打印提示。旧命令无需改写。
- **为什么裁切后 nodata 占一半？** 正常——外接矩形里边界外的部分全是 nodata（NaN），
  在 GIS 里显示为透明/空值，不是黑边（黑边是 0 值，本 skill 已修）。
- **想按自己的多边形裁？** `--geojson my.json`（需为 WGS84 经纬度；若是 GCJ-02 别加
  `--no-convert`，若是 WGS84 加 `--no-convert`）。
- **下载慢/失败？** 瓦片已缓存在 skill 目录 `_cache/tiles/`，第二次同区域秒出；
  换区域才会走网络。DataV 边界 403 时会退避重试，也可提前缓存边界。

## 变更记录
- **v2（当前）**：坐标系统一为 WGS84 地理坐标（EPSG:4326）。删除 `--utm` / `--epsg` /
  `--res` 投影选项（传入仅提示忽略）；`clip_by_boundary.py` 新增非 4326 输入的自动
  重投影兜底；裁切输出附带 CRS 与东西/南北实际像元尺寸；`terrain_products.py` 固定
  按 4326 口径计算。
- **v1**：初版，支持 `--utm` 等距投影输出。
