# Photo Agent M0

本目录是 Photo Agent M0 的第一版可运行骨架。

## 运行

```powershell
python -m pytest -q
python cli.py doctor
python cli.py all --dir "D:\你的照片目录" --limit 200
```

V1 安全原则：
- 不删除原图
- 不移动原图
- 不修改原图
- 中间文件只写入 `work/`
- 报告只写入 `reports/`

## M0 数据模型说明

`photos` 中一行代表一个“扫描到的文件实例”。

因此两个不同路径的完全相同文件允许同时存在，由 SHA-256 分组完成 exact dedup。

M0 的幂等键是 `(rel_path, sha256)`。

注意：M0 暂不建立“文件被移动后仍识别为同一实体”的复杂身份追踪机制；同一路径内容发生变化时会形成新的记录，旧记录暂时保留，后续可在扫描状态/历史机制中处理。

## 移动文件调和与清理（M0 补丁）

- **移动调和**：`upsert_photo` 检测到同一 `sha256` 的文件出现在新路径、而旧路径已不存在
  （即文件被移动，或副本被删）时，**原地更新既有行**的 `rel_path`/`abs_path`，而不是插入新行——
  避免把一次移动误判成“精确重复”。
- **扫描末清理**：`prune_missing_files` 删除文件已不存在的 `photos` 行（stale 行），
  按 FK 安全顺序级联清理其 `photo_hashes`/`quality`/`embeddings`/`vlm_analysis`/
  `decisions` 及所属 `groups`/`group_members`；扫描报告打印 `moved` 与 `pruned` 计数。
- **HEIC**：`heif.py` 一次性注册 HEIF opener，exif/thumbnails/hashes 导入它，
  HEIC 解码与导入顺序无关；`pillow-heif` 为必需依赖。

## M1 说明（pHash/dHash 近似重复）

M1 在 M0 之上增加：
- `photo_hashes` 表：每张照片的 pHash 与 dHash（64-bit hex），记录 `algorithm_version` 与 `computed_at`
- `near.py`：pHash 距离 ≤ 12 **且** dHash 距离 ≤ 16 的照片对视为候选（阈值是工程参数，非数学常数，定稿见 README_M1「阈值定稿」）
- 候选对经 union-find 聚成**连通分量候选组**，写入 `groups(kind='near')` + `group_members`
- 只产出候选，不写 `decisions`——算法结果与用户决策严格分开

### 语义与边界（重要）

1. **连通分量 ≠ 全对满足阈值**：组内只保证“相邻成员满足阈值”，
   A~B、B~C 但 A!~C 时三者仍在同一组。这是有意的设计选择。
2. **NEAR 排除 EXACT**：SHA-256 相同（字节级一致）的文件在分组前折叠为
   一个规范行，只出现在“精确重复”一节；NEAR 专指“文件不同但视觉相似”。
3. **hash 缓存按算法版本失效**：只有 `algorithm_version` 等于当前
   `hashes.HASH_VERSION` 且两 hash 非 NULL 的行才视为有效缓存；
   旧版本行（含 M0 升级遗留的 NULL 行）会被自动重算覆盖。
4. **只分组现存文件的最新记录**：每个 `abs_path` 只取最新 DB 记录，
   且文件必须仍存在于磁盘；M0 保留的历史行与已删除文件的行不参与
   NEAR，避免产生“幽灵组”。

### 规模边界

M1.1 v1（当前）保留 O(N²) 两两比较，但 hash 预解析为 64-bit int 一次、
pHash XOR 超阈值即 early-exit（跳过 dHash）。N=10k 约 3min、N=100k 约 15min，
可在真实大库上直接扫参（`cli.py sweep` 只读扫参）。
真实库基准仍慢时下一步 numpy 向量化，多探针 LSH 备选；
高 32 位分桶永久排除（会静默漏掉约 1/3 阈值边界候选对，违背召回优先）。
