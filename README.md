# 美股信号验证器

本项目不选股，只负责验证历史真实推送是否有效，并验证 DXDX 信号出现在强势板块时是否更有效。

它是第三个独立项目：只读 `dingdinglean/gupiao` 与
`dingdinglean/strong-pullback-screener` 的公开 GitHub Actions Artifact，绝不
读取、依赖或修改那两个项目的代码、状态或 Secrets。

## 记录规则

- 统计单位是**每一次具体股票推送**，不是 S/A/B 级别汇总；同一股票在不同
  run 或不同信号 K 线再次推送，都会保留新记录。
- 只有日线/4H Artifact 的 `dxdx_report.txt`，或周/月 Artifact 的
  `long_dxdx_report.txt`，明确写出 `邮件是否发送：是`（或明确的成功投递状态）时，
  相应 CSV 才会进入正式历史。dry run 与未发送邮件的 run 永远不入库。
- 每条记录均保留来源雷达与周期：`daily_4h`（`daily`、`4h`、`daily+4h`）或
  `weekly_monthly`（`weekly`、`monthly`）。周/月 ID 包含 timeframe，避免同一股票
  同期双信号碰撞。
- 同日强势板块来自 V4 Artifact 的 `sector_strength.csv`：Rank 1 是主线，
  Rank 2–3 是强势板块，其他或缺失为非强势/未知。入库时冻结板块、排名和
  判断，今后不会因策略或元数据变化而回写历史。

## 衡量方式

记录同时保留 `signal_date` / `signal_price`（指标所属K）与 `push_date` /
`push_price`（用户实际收到邮件时的市场日和最新完整 RTH 日K收盘）。收益基准固定为
冻结的 `push_price`；`T+1 / 3 / 5 / 10 / 20` 从 `push_date` 后第 N 个**实际交易日**
开始。Yahoo Finance 数据始终以 `prepost=False` 获取 RTH 日线；旧周/月 artifact 缺少
`push_price` 时只回补一次当时可获得的 RTH 收盘并写入历史，之后绝不重算。

- MFE / MAE：未来 10、20 个交易日最高价 / 最低价相对 `push_price` 的变化。
- 10 日有效性：先触及 `+5%` 为“有效”，先触及 `-5%` 为“无效”；两者均未触及
  为“中性”。若同一根日 K 同时触及两侧，保留“无法判定”，不猜测盘中先后。
- 未走满所需交易日的指标保持空白，`effectiveness=pending`；不会用当前价伪造
  未来表现。

## 输出

- `data/signal_history.csv`：长期、逐次正式推送历史。
- `output/tracker_report.csv`：每一条推送的最新明细。
- `output/tracker_report.txt`：总数、成熟数、强势/非强势分布与最近逐笔记录。
- `data/sector_snapshots.csv`：已冻结的每日 Top 3 板块快照。

## 自动化

工作流使用 Python 3.11，于工作日 `23:30 UTC`（`30 23 * * 1-5`）运行，晚于
强势板块雷达和 DXDX 雷达。它下载两个源仓库的最新成功 Artifact、更新历史、
提交有变化的数据，并上传上述报告 Artifact。权限仅为 `contents: write`；不会在
日志写入 token、邮箱或认证信息。

验证器每天自动更新，但仅在新增正式推送、T+N 里程碑更新或有效性结果产生变化时
发送邮件，避免无效日报。邮件使用 Gmail SMTP 的 STARTTLS（端口 `587`），全部
配置都来自 Actions Secrets：`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、
`SMTP_PASSWORD`、`EMAIL_TO`；仓库不保存真实邮箱或密码。手动触发时可设置
`test_email=true`，该模式只做邮件连通性测试，不运行 tracker、不会修改历史。

初始追踪起点为 `2026-09-06T00:00:00Z`，可通过 `TRACKING_START_UTC` 覆盖。

## 本地验证

```bash
pip install -r requirements.txt
python -m pytest -q
python tracker.py
```
