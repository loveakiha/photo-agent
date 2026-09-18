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
