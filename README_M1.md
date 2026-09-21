# photo-agent M1

M1 在 M0 的非破坏式索引基础上加入近似重复候选发现。

## 核心规则

- 使用 `imagehash` 计算 pHash + dHash，每张照片保存两个 64-bit hash（HEX 字符串）。
- `HASH_VERSION=m1-v1` 参与缓存判断；未来改变 hash 算法（bump 版本号）时，旧缓存自动失效并重新计算。
- M1 定稿候选阈值：pHash Hamming distance `<= 12` 且 dHash `<= 16`（可用 `cli.py near --phash/--dhash` 覆盖）。
- 阈值只是候选实验参数，不是最终“应该删除”的判断。
- 精确 SHA-256 重复属于 M0 EXACT；NEAR 比较前按 SHA-256 折叠成单一规范行，字节级副本不会再次作为近似重复出现。
- NEAR 使用 union-find 的连通分量：同一组中并非每一对照片都直接满足阈值（A~B、B~C 但 A!~C 时三者同组）。
- 近似重复阶段只考虑：当前文件系统仍存在、且每个 `abs_path` 的最新数据库记录；M0 历史记录不会污染 M1。
- 只有 `algorithm_version` 等于当前 `HASH_VERSION` 且两 hash 非 NULL 的 `photo_hashes` 行参与分组；旧版本行视为 stale。
- M1 不写 `decisions`，不删除、不移动原图。

## 阈值定稿（M1 收尾实验）

M1 收尾在 **96 张真实 test_pics 库**上跑 25 组合扫参（pHash∈{4,6,8,10,12} × dHash∈{8,10,12,14,16}），
`cli.py sweep` 纯只读（不写 `groups`/`decisions`/`photos`），输出 `reports/sweep/<日期>/summary.md`。

**观察：结果只随 pHash 分三档台阶，dHash 几乎不敏感**（dHash 只在 pHash 取 6/8/12 时有微小台阶）。

| pHash 档 | dHash 无关 | 组数 | 成员 | 覆盖率 |
|:---:|:---:|:---:|:---:|:---:|
| 4 | 任意 | 2 | 5 | 5.2% |
| 6 | ≤10 / ≥12 | 2→3 | 5→7 | 5.2→7.3% |
| 8 | ≤10 / ≥12 | 3→4 | 8→10 | 8.3→10.4% |
| 10 | ≤10 / ≥12 | 3→4 | 8→10 | 8.3→10.4% |
| 12 | ≤10 / ≥12 | 4→5 | 10→13 | 10.4→13.5% |

**取最宽档 phash12/dhash16**（5 组 13 张，13.5% 覆盖）。理由（用户定调，M1→后续总设计原则）：
> near 阶段优先追求「快、广、够用」：宁可多抓一些候选，也不要为降低少量误报而牺牲筛选速度和召回率；最终精度交给后续人工/VLM。

用户本地用本地模型跑、token 不限、只关心墙钟时间，故不收紧阈值。
注意：pHash 12 档下 dHash 12/14/16 结果**完全相同**（13 张在 dHash 12 已全部召回），
故 12/16 比 12/12 在当前库上无额外召回，但对未来大库**更宽容**
（dHash 13–16 边界对在 12/12 会漏、12/16 会抓，符合"宁可多抓"），故取 12/16。

### near 的能力天花板（重要边界）

perceptual hash 只能抓「**同版本变体**」：重编码、轻微裁剪、缩放、HEIC/JPEG 互换。
**同场景不同构图/角度**超出其能力：实测一组同场景照片两两 pΔ=18–26、dΔ=21–32，
远超最大阈值 12/16，任何 25 组合组合都合并不了。这类召回由 **M3 本地 CLIP embedding + VLM** 补齐（见「架构」）。

## 架构：四级漏斗

每一级**更快、更宽、更便宜**，贵的一级只裁决前一级漏掉的候选——完全符合「快、广、宁可多抓」原则。

```
精确重复  →  SHA-256          0 成本            (M0, 已完成)
同版本    →  pHash/dHash      0 成本 ~15min/10万张 (M1, 本里程碑, 定稿 12/12 最宽)
同场景    →  本地 CLIP 聚类    4090D 一次性 ~10min/5万张 (M3a, 规划)
模糊边界  →  本地 7B/14B VLM 拼图裁决  只跑 embedding 灰区 (M3b, 规划)
最终      →  人工 decisions    (已预留表)
```

- **M3a embedding 场景聚类**：本地 CLIP（ViT-B/16，~0.3G fp16）逐张编码，
  numpy/torch matmul 暴力检索（10万×512 维 fp16 ≈ 102MB 内存，每查询毫秒级，**不需要 FAISS**），
  按 sha256 内容键增量编码（与缩略图同模式）。embedding 表 `001_initial.sql` 已预留。
- **M3b VLM 灰区裁决**：Qwen2.5-VL（ollama 本地）只做两件事——(1) 簇边界/灰区候选对拼 contact sheet 批量问「哪些同场景」；(2) 合并/拆分建议。embedding 自动归组率预计 80–90%，到 VLM 的是长尾。
- 用户本地开发机 **RTX 4090D 24G**：CLIP 与 Qwen-VL 串行跑、不同时驻留，峰值 ~5G，24G 宽裕；token 不限、优化墙钟时间。

## 报告

`reports/latest.md` 的「近似重复」一节展示每个候选组成员的 **pHash Δ / dHash Δ**（到代表的实际距离，即候选判据的证据），不使用综合相似度分。

## 性能

M1.1 v1 保留完整 O(N²) 两两比较，但把每个 64-bit hash 预解析为 int 一次，
pHash XOR 距离 > 阈值即 early-exit（跳过 dHash 计算）。
与逐对 hex 规则**逐位等价**（`tests/test_near_equivalence.py` 钉死，含距离恰=阈值的边界），
零新依赖。实测 96 张全对 25 组 sweep 仅 0.01s（扫描 hash 4.9s 是大头）。
N=50k≈3min、N=100k≈15min；真实库基准仍慢时下一步 numpy 向量化，多探针 LSH 备选，
高 32 位分桶永久排除（会静默漏掉约 1/3 阈值边界候选对，违背召回优先）。

## CLI

```bash
python cli.py scan
python cli.py dedup
python cli.py near [--phash 12] [--dhash 12]     # 默认取 config.yaml near.*
python cli.py sweep                              # 只读扫参 25 组合 → reports/sweep/<日期>/summary.md
python cli.py sweep --sheets phash12_dhash12,... # 选中组合按需拼图（仅 size>=2 组）
python cli.py all --dir "D:\你的照片目录"
python -m pytest -q
```

首次使用请先安装 `requirements.txt`，其中 `imagehash>=4.3` 是 M1 必需依赖。
