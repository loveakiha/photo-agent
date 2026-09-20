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

## M1 说明（pHash/dHash 近似重复）

M1 在 M0 之上增加：
- `photo_hashes` 表：每张照片的 pHash 与 dHash（64-bit hex），记录 `algorithm_version` 与 `computed_at`
- `near.py`：pHash 距离 ≤ 8 **且** dHash 距离 ≤ 12 的照片对视为候选（阈值是工程参数，非数学常数）
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

### 规模边界（M1.0 冻结范围）

M1.0 的候选配对是 **O(N²)** 暴力两两比较（N=10k 约 5000 万次比较，
N=100k 约 50 亿次）。因此：

- M1.0 用于**小规模/中等规模**验证阈值与分组逻辑（1k~10k 量级可接受）
- **不要**用 M1.0 直接扫几万~十几万张的真实大库
- M1.1 再根据真实数据决定候选搜索算法（LSH / 桶化 / ANN 等），
  并先用真实照片库做阈值实验（`cli.py near --phash/--dhash` 扫参），
  人工抽样后确定 M1 默认阈值
