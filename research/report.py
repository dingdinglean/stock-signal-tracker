"""Self-contained HTML plus the research CSVs. Screenshots are not a substitute."""
from __future__ import annotations

import base64
import csv
import html
from pathlib import Path

from research.metrics import number


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0].keys()) if rows else ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def _table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = []
    for row in rows:
        cells = []
        for key, _label in columns:
            value = row.get(key, "")
            if key.endswith("_mean") or key.endswith("_rate") or key.endswith("_share") or key.endswith("se_independent"):
                parsed = number(value)
                value = "" if parsed is None else f"{parsed:.4f}"
            cells.append(f"<td>{html.escape(str(value))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _sim_summary(sim_rows: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = {}
    for row in sim_rows:
        if row.get("cohort") not in {"daily", "weekly"}:
            continue
        key = (row.get("cohort"), row.get("strategy"), row.get("cost_mode"), row.get("sample_split"))
        grouped.setdefault(key, []).append(row)
    summary = []
    for (cohort, strategy, cost, split), rows in sorted(grouped.items()):
        closed = [row for row in rows if row.get("status") == "closed" and row.get("included_in_mean") == "yes"]
        returns = [float(row["gross_return"]) for row in closed if row.get("gross_return")]
        nets = [float(row["net_return"]) for row in closed if row.get("net_return")]
        optimistic = [float(row["optimistic_gross_return"]) for row in closed if row.get("optimistic_gross_return")]
        excess = []
        for row in closed:
            if row.get("gross_return") and row.get("benchmark_return"):
                excess.append(float(row["gross_return"]) - float(row["benchmark_return"]))
        drawdowns = [float(row["highwater_drawdown"]) for row in closed if row.get("highwater_drawdown")]
        summary.append(
            {
                "cohort": cohort,
                "sample_split": split,
                "strategy": strategy,
                "cost_mode": cost,
                "n_rows": len(rows),
                "n_closed": len(closed),
                "n_censored": sum(row.get("status") == "censored" for row in rows),
                "n_excluded_scale": sum(row.get("included_in_mean") == "no" for row in rows),
                "n_ambiguous": sum(row.get("ambiguous") == "yes" for row in closed),
                "gross_mean_conservative": f"{sum(returns) / len(returns):.6f}" if returns else "",
                "gross_mean_optimistic": f"{sum(optimistic) / len(optimistic):.6f}" if optimistic else "",
                "net_mean_conservative": f"{sum(nets) / len(nets):.6f}" if nets else "",
                "excess_mean": f"{sum(excess) / len(excess):.6f}" if excess else "",
                "highwater_drawdown_mean": f"{sum(drawdowns) / len(drawdowns):.6f}" if drawdowns else "",
                "note": "±5% 与固定持有 10/20 日分开。费用和滑点分列。同日双触的主结果是止损优先，乐观列是止盈优先。右删失不进均值。基准是同一入场日至出场日的 ^GSPC 开盘到收盘价格收益，不是 SPY，也不是分红总收益。",
            }
        )
    return summary


def _sim_sector_summary(sim_rows: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = {}
    for row in sim_rows:
        if row.get("cost_mode") not in {"zero", "fee_and_slippage"}:
            continue
        key = (row.get("cohort"), row.get("sector_status"), row.get("strategy"), row.get("cost_mode"))
        grouped.setdefault(key, []).append(row)
    summary = []
    for (cohort, sector, strategy, cost), rows in sorted(grouped.items(), key=lambda item: tuple(str(part) for part in item[0])):
        closed = [row for row in rows if row.get("status") == "closed" and row.get("included_in_mean") == "yes"]
        drawdowns = [float(row["highwater_drawdown"]) for row in closed if row.get("highwater_drawdown")]
        returns = [float(row["gross_return"]) for row in closed if row.get("gross_return")]
        summary.append({
            "cohort": cohort, "sector_status": sector, "strategy": strategy, "cost_mode": cost,
            "n_closed": len(closed),
            "highwater_drawdown_mean": f"{sum(drawdowns) / len(drawdowns):.6f}" if drawdowns else "",
            "gross_mean_conservative": f"{sum(returns) / len(returns):.6f}" if returns else "",
        })
    return summary


def render_html(
    path: Path,
    *,
    coverage: dict[str, str],
    answers: list[dict],
    groups: list[dict],
    sims: list[dict],
    charts: list[dict],
) -> None:
    group_columns = [
        ("ledger_scope", "账本范围"),
        ("cohort", "周期"),
        ("sample_split", "样本"),
        ("group", "分组"),
        ("role", "用途"),
        ("n_signals", "N"),
        ("n_symbols", "股票数"),
        ("return_20d_n", "20日分母"),
        ("return_20d_win_rate", "20日盈利率"),
        ("return_20d_mean", "20日均值"),
        ("return_20d_median", "20日中位"),
        ("return_10d_positive_share", "10日正收益占比"),
        ("return_20d_positive_share", "20日正收益占比"),
        ("first_plus_5_share", "先+5%"),
        ("first_minus_5_share", "先-5%"),
        ("ambiguous_share", "同日双触"),
        ("mfe_10d_mean", "MFE10"),
        ("mae_10d_mean", "MAE10"),
        ("mfe_20d_mean", "MFE20"),
        ("mae_20d_mean", "MAE20"),
        ("highwater_dd_20d_n", "成熟20日回撤分母"),
        ("highwater_dd_20d_mean", "成熟20日高水位回撤"),
        ("highwater_dd_20d_todate_n", "含未成熟回撤分母"),
        ("return_after_top3_20d_n", "转强后20日分母"),
        ("return_after_top3_20d_mean", "转强后20日均值"),
        ("n_sector_known", "板块已知"),
        ("n_sector_unknown", "板块未知"),
        ("benchmark_20d_mean", "^GSPC"),
        ("frozen_minus_index_20d_mean", "冻结减指数(非同起点)"),
        ("comparable_excess_20d_mean", "同起点可比超额"),
        ("comparable_excess_20d_n", "可比超额分母"),
        ("cluster_symbol_mean_20d", "股票等权"),
        ("small_sample", "样本不足"),
    ]
    visible_groups = [row for row in groups if row.get("sample_split") == "all" or int(row.get("n_signals") or 0)]
    parts = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>信号形态研究</title>",
        "<style>body{font-family:'WenQuanYi Micro Hei','Noto Sans CJK SC',sans-serif;margin:24px;color:#1c2833;background:#f7f5f0}",
        "h1,h2{font-weight:600} table{border-collapse:collapse;width:100%;margin:12px 0;background:#fff;font-size:13px}",
        "th,td{border:1px solid #d5d8dc;padding:4px 6px;text-align:left} th{background:#eaecee}",
        ".note{background:#fdebd0;padding:10px 12px} .answer{background:#fff;padding:12px 14px;margin:10px 0;border-left:4px solid #1a5276}",
        "img{max-width:100%;background:#fff;border:1px solid #d5d8dc}</style></head><body>",
        "<h1>正式推送的日线 / 周线形态研究</h1>",
        "<p class=\"note\">这是研究假设的描述，不是买卖条件，也不覆盖原来的 DXDX 验证器。"
        "亏损隐藏只影响邮件展示；下面的样本保留盈利和亏损。4 小时不进入统计，历史不删除。没有真实交易。</p>",
        "<h2>覆盖与出处</h2><ul>",
    ]
    for key, value in coverage.items():
        parts.append(f"<li><b>{html.escape(key)}</b>：{html.escape(str(value))}</li>")
    parts.append("</ul><h2>六个问题</h2>")
    for answer in answers:
        parts.append(
            f"<div class=\"answer\"><b>{html.escape(str(answer.get('ledger_scope') or ''))} {html.escape(answer['question'])}</b>"
            f"<p>{html.escape(answer['answer'])}</p>"
            f"<p>{html.escape(answer['rule'])}</p></div>"
        )
    parts.append("<h2>分组（全样本，以及非空的训练 / 样本外）</h2>")
    parts.append(_table(visible_groups, group_columns))
    parts.append("<h2>独立交易模拟</h2>")
    parts.append(_table(_sim_summary(sims), [
        ("cohort", "周期"), ("sample_split", "样本"), ("strategy", "策略"), ("cost_mode", "成本"),
        ("n_rows", "行数"), ("n_closed", "已平仓"), ("n_censored", "右删失"), ("n_excluded_scale", "口径排除"),
        ("n_ambiguous", "双触"), ("gross_mean_conservative", "保守毛收益"), ("gross_mean_optimistic", "乐观毛收益"),
        ("net_mean_conservative", "保守净收益"), ("excess_mean", "超额"), ("highwater_drawdown_mean", "高水位回撤"),
    ]))
    parts.append("<h2>按研究板块状态的模拟高水位回撤</h2>")
    parts.append("<p>这里的高水位回撤来自独立模拟的成交路径，不是冻结 MAE。未知板块不并入非强势。冻结收益的分母与这些成交行数不同。</p>")
    parts.append(_table(_sim_sector_summary(sims), [
        ("cohort", "周期"), ("sector_status", "板块"), ("strategy", "策略"), ("cost_mode", "成本"),
        ("n_closed", "已平仓"), ("highwater_drawdown_mean", "高水位回撤"), ("gross_mean_conservative", "保守毛收益"),
    ]))
    parts.append("<h2>案例蜡烛图</h2>")
    for chart in charts:
        parts.append(f"<h3>{html.escape(chart['title'])}</h3><p>{html.escape(chart['note'])}</p>")
        if chart.get("png"):
            encoded = base64.b64encode(chart["png"]).decode("ascii")
            parts.append(f"<img alt=\"{html.escape(chart['title'])}\" src=\"data:image/png;base64,{encoded}\">")
    parts.append("</body></html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")
