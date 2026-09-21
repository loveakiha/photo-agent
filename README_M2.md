# README_M2 — 技术质量测量（Measurement）

> M2 = **Measurement 层**：纯本地、CPU、可解释、可复现。只回答「是否存在
> **明显**技术问题（模糊 / 裁剪曝光 / 噪声）」；不回答审美、价值、保留与否
> （那是 M3b VLM 与后续 Decision 层的事）。不删除/不移动/不修改原图（V1）。

## 三个 raw 指标

全部在**长边 2048 的 8-bit 灰度图**（EXIF 定向后降采样）上计算，
保证跨原始尺寸可比：

| 维度 | 算法 | 方向 |
|---|---|---|
| sharpness | `cv2.Laplacian(CV_64F)` 全图方差 | 越高越好 |
| exposure | 8-bit 直方图两端裁剪率（≤1 或 ≥254 像素占比） | 越低越好 |
| noise | Laplacian 的 5×5 非重叠窗口方差**中位数**（中位数抗边缘/纹理） | 越低越好 |

raw 值**永久保存**（事实）；分数是解释。

## 打分：两档线性钳制

```
score = clamp((raw - low) / (high - low))     # higher-is-better
score = clamp(1 - (raw - low) / (high - low)) # lower-is-better
```

每个维度两个冻结数（`config.yaml quality.thresholds`），方向内置。

| 维度 | low（=0 分） | high（=1 分） | 依据 |
|---|---:|---:|---|
| sharpness | 15 | 550 | 96 张真实库 p10=13.7 / p90=554 |
| exposure | 0.005 | 0.05 | 真实库 p90=0.009 之上为明显裁剪；fixture 轻裁剪档 ≈0.009 |
| noise | 10 | 60 | 真实库 p90=118 之下多数干净；fixture 轻噪声档 ≈59 |

`quality_score`（聚合）**暂不冻结**：mean/min/加权/惩罚待实验数据决定，
期间该列留 NULL，报告只展示三维度。

## 标定与验证（2026-09-21）

- 实验：96 张真实库 + 21 张合成片（3 基图 shapes/texture/dark ×
  (1 基图 + 6 变体：blur 轻/重、过曝/欠曝、噪声轻/重)，固定种子、
  临时生成、不入库、不碰 test_pics 原图）。
- 排序验证 **18/18 通过**（每基图：base > blur_light > blur_strong 等）。
- 夜景不误判：dark 基图（均值 ≈35，无裁剪）exposure_raw = 0 → 分数 1.0。
- 报告：`reports/calibration/20260921_174432/summary.md`
  （完整分布表 + 排序验证 + 冻结值）。
- 复跑标定：`.venv/Scripts/python calibrate.py`

## 用法

```
.venv/Scripts/python -m cli quality            # 增量：缺行/版本不符/sha256 变化才重算
.venv/Scripts/python -m cli quality --force    # 全量重算
.venv/Scripts/python -m cli all --dir <目录>   # scan→hash→near→quality→report
```

报告新增「质量评分」段：三维度分布 + 低分榜（任一维度 < 0.3，最多 10 张）。

## 缓存与失效

`quality` 表（schema v3）新增 `sha256` 列：重算条件 = 行缺失 /
`algorithm_version ≠ m2-v1` / `photos.sha256` 变化。`--force` 忽略缓存。

## 依赖

`opencv-python`（CPU 后端）+ `numpy`。doctor 中 OpenCV 为 M2 必需项。

## 范围外

- 不做组间比较/代表选择/审美判断（M3b / Decision 层）
- 不改 001 六表结构（仅 v3 给 quality 加 sha256 列）
- 不动 test_pics 原图
