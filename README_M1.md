# photo-agent M1

M1 在 M0 的非破坏式索引基础上加入近似重复候选发现。

## 核心规则

- 使用 `imagehash` 计算 pHash + dHash，每张照片保存两个 64-bit hash（HEX 字符串）。
- `HASH_VERSION=m1-v1` 参与缓存判断；未来改变 hash 算法（bump 版本号）时，旧缓存自动失效并重新计算。
- M1 默认候选阈值：pHash Hamming distance `<= 8` 且 dHash `<= 12`（可用 `cli.py near --phash/--dhash` 覆盖）。
- 阈值只是候选实验参数，不是最终“应该删除”的判断。
- 精确 SHA-256 重复属于 M0 EXACT；NEAR 比较前按 SHA-256 折叠成单一规范行，字节级副本不会再次作为近似重复出现。
- NEAR 使用 union-find 的连通分量：同一组中并非每一对照片都直接满足阈值（A~B、B~C 但 A!~C 时三者同组）。
- 近似重复阶段只考虑：当前文件系统仍存在、且每个 `abs_path` 的最新数据库记录；M0 历史记录不会污染 M1。
- 只有 `algorithm_version` 等于当前 `HASH_VERSION` 且两 hash 非 NULL 的 `photo_hashes` 行参与分组；旧版本行视为 stale。
- M1 不写 `decisions`，不删除、不移动原图。

## 报告

`reports/latest.md` 的「近似重复」一节展示每个候选组成员的 **pHash Δ / dHash Δ**（到代表的实际距离，即候选判据的证据），不使用综合相似度分。

## 性能边界

当前实现对候选照片做 O(N²) 两两比较（N=10k 约 5000 万次比较，N=100k 约 50 亿次），适合 M1.0 的验证和中小型图库。图库规模明显增大后，M1.1 引入 hash 分桶/候选索引（LSH 等）以减少比较次数，且需先在真实数据上基准测试。

## CLI

```bash
python cli.py scan
python cli.py dedup
python cli.py near [--phash 8] [--dhash 12]
python cli.py all --dir "D:\你的照片目录"
python -m pytest -q
```

首次使用请先安装 `requirements.txt`，其中 `imagehash>=4.3` 是 M1 必需依赖。
