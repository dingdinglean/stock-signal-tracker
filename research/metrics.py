"""Group statistics. Unknowns stay in their own rows and are not folded into the 'no' group."""
from __future__ import annotations

import math
import statistics
from collections import Counter

from research.hypothesis import STUDY_PARAMS, StudyParams
from research.market import parse_day, session_offset


def number(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _values(rows: list[dict], field: str) -> list[float]:
    return [value for row in rows if (value := number(row.get(field))) is not None]


def _mean(values: list[float]) -> str:
    return f"{statistics.fmean(values):.6f}" if values else ""


def _median(values: list[float]) -> str:
    return f"{statistics.median(values):.6f}" if values else ""


def _share(hits: int, total: int) -> str:
    return f"{hits / total:.6f}" if total else ""


def _se(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    return f"{statistics.pstdev(values) / math.sqrt(len(values)):.6f}"


def assign_splits(rows: list[dict], calendar, params: StudyParams | None = None) -> None:
    """Purge any training push whose 20-session outcome window reaches the cut.

    Parameters are not refit on either side. A short history can leave the
    training side empty; that result is kept.
    """
    params = params or STUDY_PARAMS
    for row in rows:
        row["sample_split"] = "not_applicable"
        row["split_cut"] = ""
    for cohort in ("daily", "weekly"):
        members = [row for row in rows if row.get("cohort") == cohort and parse_day(row.get("push_date"))]
        members.sort(key=lambda row: str(row.get("push_date")))
        if len(members) < 2:
            continue
        cut_at = int(round(len(members) * params.train_fraction))
        cut_at = min(max(cut_at, 1), len(members) - 1)
        cut = parse_day(members[cut_at]["push_date"])
        assert cut is not None
        for row in members:
            push = parse_day(row["push_date"])
            assert push is not None
            row["split_cut"] = cut.isoformat()
            if push >= cut:
                row["sample_split"] = "oos"
                continue
            window_end = session_offset(calendar, push, params.embargo_sessions)
            if window_end is None or window_end >= cut:
                row["sample_split"] = "purged"
            else:
                row["sample_split"] = "train"


def summarize(rows: list[dict], *, cohort: str, split: str, name: str, role: str, params: StudyParams | None = None) -> dict[str, str]:
    params = params or STUDY_PARAMS
    symbols = {str(row.get("symbol") or "") for row in rows if row.get("symbol")}
    returns20 = _values(rows, "return_20d")
    returns10 = _values(rows, "return_10d")
    latest = _values(rows, "latest_return")
    mfe = _values(rows, "mfe_20d")
    mae = _values(rows, "mae_20d")
    paired = []
    benchmarks = []
    for row in rows:
        left = number(row.get("return_20d"))
        right = number(row.get("benchmark_20d"))
        if left is None or right is None:
            continue
        paired.append(left - right)
        benchmarks.append(right)
    mature = [row for row in rows if str(row.get("effectiveness") or "") in {"有效", "无效", "中性", "无法判定"}]
    pending = [row for row in rows if str(row.get("effectiveness") or "") in {"", "pending"}]
    cluster: list[float] = []
    for symbol in symbols:
        values = _values([row for row in rows if row.get("symbol") == symbol], "return_20d")
        if values:
            cluster.append(statistics.fmean(values))
    immature20 = sum(1 for row in rows if number(row.get("return_20d")) is None)
    record = {
        "cohort": cohort,
        "sample_split": split,
        "group": name,
        "role": role,
        "n_signals": str(len(rows)),
        "n_symbols": str(len(symbols)),
        "n_hidden": str(sum(1 for row in rows if row.get("display_state") == "hidden")),
        "return_20d_n": str(len(returns20)),
        "return_20d_mean": _mean(returns20),
        "return_20d_median": _median(returns20),
        "return_20d_win_rate": _share(sum(value > 0 for value in returns20), len(returns20)),
        "return_20d_se_independent": _se(returns20),
        "return_10d_n": str(len(returns10)),
        "return_10d_mean": _mean(returns10),
        "return_10d_median": _median(returns10),
        "return_10d_positive_share": _share(sum(value > 0 for value in returns10), len(returns10)),
        "latest_return_n": str(len(latest)),
        "latest_return_mean": _mean(latest),
        "latest_return_median": _median(latest),
        "latest_return_win_rate": _share(sum(value > 0 for value in latest), len(latest)),
        "effectiveness_n": str(len(mature)),
        "first_plus_5_share": _share(sum(row.get("effectiveness") == "有效" for row in mature), len(mature)),
        "first_minus_5_share": _share(sum(row.get("effectiveness") == "无效" for row in mature), len(mature)),
        "ambiguous_share": _share(sum(row.get("effectiveness") == "无法判定" for row in mature), len(mature)),
        "neutral_share": _share(sum(row.get("effectiveness") == "中性" for row in mature), len(mature)),
        "pending_n": str(len(pending)),
        "mfe_20d_n": str(len(mfe)),
        "mfe_20d_mean": _mean(mfe),
        "mae_20d_n": str(len(mae)),
        "mae_20d_mean": _mean(mae),
        "mae_20d_worst": f"{min(mae):.6f}" if mae else "",
        "benchmark_20d_n": str(len(benchmarks)),
        "benchmark_20d_mean": _mean(benchmarks),
        "excess_20d_n": str(len(paired)),
        "excess_20d_mean": _mean(paired),
        "cluster_symbol_mean_20d": _mean(cluster),
        "immature_20d": str(immature20),
        "missing_quote": str(sum(1 for row in rows if row.get("quote_status") not in {"ok", ""})),
        "small_sample": "yes" if len(rows) < params.min_conclusion or len(symbols) < params.min_symbols or len(returns20) < params.min_conclusion else "no",
        "denominator_note": "收益分母是该字段有限数值的推送数，不是全组 N；未成熟、停牌和无法判定不计入对应分子。重复信号的标准误按独立样本计算，会低估不确定度，所以同时给出股票等权均值。",
    }
    return record


GROUPS = (
    ("all", "entry", lambda row: True),
    ("double_bottom", "entry", lambda row: row.get("pattern_status") == "double_bottom"),
    ("not_double_bottom", "entry", lambda row: row.get("pattern_status") in {"platform", "sharp_stabilize", "other"}),
    ("pattern_unknown", "entry", lambda row: row.get("pattern_status") == "unknown"),
    ("breakout_yes", "entry", lambda row: row.get("breakout_state") == "yes"),
    ("breakout_no", "entry", lambda row: row.get("breakout_state") == "no"),
    ("strong", "entry", lambda row: row.get("sector_status") == "strong"),
    ("not_strong", "entry", lambda row: row.get("sector_status") == "not_strong"),
    ("sector_unknown", "entry", lambda row: row.get("sector_status") == "unknown"),
    ("double_and_strong", "entry", lambda row: row.get("pattern_status") == "double_bottom" and row.get("sector_status") == "strong"),
    ("posterior_turned_strong", "posterior", lambda row: row.get("posterior_sector_entered") == "yes"),
)


def build_groups(rows: list[dict], params: StudyParams | None = None) -> list[dict[str, str]]:
    records = []
    for cohort in ("daily", "weekly"):
        cohort_rows = [row for row in rows if row.get("cohort") == cohort]
        for split, subset in (
            ("all", cohort_rows),
            ("train", [row for row in cohort_rows if row.get("sample_split") == "train"]),
            ("oos", [row for row in cohort_rows if row.get("sample_split") == "oos"]),
        ):
            for name, role, predicate in GROUPS:
                chosen = [row for row in subset if predicate(row)]
                records.append(summarize(chosen, cohort=cohort, split=split, name=name, role=role, params=params))
    return records


def _fmt_pct(text: str) -> str:
    value = number(text)
    if value is None:
        return "无"
    return f"{value * 100:.2f}%"


def _fmt_num(text: str) -> str:
    value = number(text)
    if value is None:
        return "无"
    return f"{value:.2f}"


def _find(groups: list[dict], cohort: str, split: str, name: str) -> dict:
    for row in groups:
        if row["cohort"] == cohort and row["sample_split"] == split and row["group"] == name:
            return row
    return {}


def _mode_line(rows: list[dict], field: str) -> str:
    counts = Counter(str(row.get(field) or "unknown") for row in rows)
    if not counts:
        return "无记录"
    ranked = counts.most_common()
    detail = "、".join(f"{key} {value}" for key, value in ranked)
    leaders = [key for key, value in ranked if value == ranked[0][1]]
    if len(leaders) > 1:
        return f"没有单一多数，{'、'.join(leaders)} 并列（各 {ranked[0][1]}/{len(rows)}；分布 {detail}）"
    return f"最多的是 {leaders[0]}（{ranked[0][1]}/{len(rows)}；分布 {detail}）"


def _compare(label: str, left: dict, right: dict, baseline: dict) -> str:
    if not left or not right:
        return f"{label}：分组缺失。"
    small = left.get("small_sample") == "yes" or right.get("small_sample") == "yes"
    text = (
        f"{label}。前组 N={left.get('n_signals')}（股票 {left.get('n_symbols')}，20日收益分母 {left.get('return_20d_n')}，"
        f"盈利率 {_fmt_pct(left.get('return_20d_win_rate', ''))}，均值 {_fmt_num(left.get('return_20d_mean', ''))}，"
        f"MAE均值 {_fmt_num(left.get('mae_20d_mean', ''))}）；"
        f"后组 N={right.get('n_signals')}（股票 {right.get('n_symbols')}，分母 {right.get('return_20d_n')}，"
        f"盈利率 {_fmt_pct(right.get('return_20d_win_rate', ''))}，均值 {_fmt_num(right.get('return_20d_mean', ''))}，"
        f"MAE均值 {_fmt_num(right.get('mae_20d_mean', ''))}）。"
        f"全样本基线 N={baseline.get('n_signals')}，20日分母 {baseline.get('return_20d_n')}，盈利率 {_fmt_pct(baseline.get('return_20d_win_rate', ''))}。"
    )
    if small:
        return text + "未达到 N>=20 且股票数>=8 的门槛，不能据此说成功率更高或更低。"
    return text + "两组都过了计数门槛，仍只是同一历史里的描述，不是因果。"


def six_answers(groups: list[dict], rows: list[dict], params: StudyParams | None = None) -> list[dict[str, str]]:
    """Six questions. Small cells are described and then explicitly not concluded."""
    params = params or STUDY_PARAMS
    answers = []

    def add(key: str, question: str, text: str, cohort: str) -> None:
        answers.append({"question_id": key, "cohort": cohort, "question": question, "answer": text, "rule": (
            f"描述门槛 N>={params.min_descriptive}；结论门槛 N>={params.min_conclusion} 且股票>={params.min_symbols}。"
            "样本外必须是清除 20 日窗口重叠后的后段，参数没有在全样本上重估。"
        )})

    for cohort, key, question in (
        ("daily", "q1_daily_commonality", "日线盈利最明显的共性是什么？"),
        ("weekly", "q2_weekly_commonality", "周线盈利最明显的共性是什么？"),
    ):
        sample = [row for row in rows if row.get("cohort") == cohort]
        winners = [row for row in sample if (value := number(row.get("return_20d"))) is not None and value > 0]
        losers = [row for row in sample if (value := number(row.get("return_20d"))) is not None and value <= 0]
        immature = sum(1 for row in sample if number(row.get("return_20d")) is None)
        dates = sorted(str(row.get("push_date") or "") for row in sample if row.get("push_date"))
        span = f"{dates[0]} 至 {dates[-1]}" if dates else "无推送日"
        text = (
            f"研究样本 {len(sample)} 条正式推送、{len({row.get('symbol') for row in sample})} 只股票，"
            f"推送日 {span}。20 日收益已成熟的盈利 {len(winners)}、亏损或持平 {len(losers)}，未成熟 {immature}。"
            f"盈利记录的形态{_mode_line(winners, 'pattern_status')}；板块状态{_mode_line(winners, 'sector_status')}；"
            f"信号时突破{_mode_line(winners, 'breakout_state')}。"
            f"亏损或持平记录的形态{_mode_line(losers, 'pattern_status')}。"
        )
        winner_patterns = Counter(str(row.get("pattern_status") or "unknown") for row in winners)
        top = winner_patterns.most_common(1)
        tied = len(top) == 0 or sum(value == top[0][1] for value in winner_patterns.values()) > 1
        majority = bool(top) and top[0][1] * 2 > len(winners)
        if len(winners) < params.min_descriptive:
            text += "盈利样本少于 8。即便某个标签在这几条里重复出现，也不是可以称为共性的结果，这里不下结论。"
        elif tied or not majority:
            text += "盈利样本没有一个形态占到明显多数，这里不下结论。"
        else:
            text += "即使某一标签占多数，也只说明这批已成熟盈利记录里它较常见，不能外推成筛选条件。"
        add(key, question, text, cohort)

    for cohort, key, question, left_name, right_name, title in (
        ("daily", "q3_double_bottom", "双底是否提高成功率？", "double_bottom", "not_double_bottom", "日线双底对照非双底"),
        ("weekly", "q3_double_bottom", "双底是否提高成功率？", "double_bottom", "not_double_bottom", "周线双底对照非双底"),
        ("daily", "q4_strong_sector", "信号时强势板块是否优于非强势？", "strong", "not_strong", "日线强势对照非强势"),
        ("weekly", "q4_strong_sector", "信号时强势板块是否优于非强势？", "strong", "not_strong", "周线强势对照非强势"),
    ):
        left = _find(groups, cohort, "all", left_name)
        right = _find(groups, cohort, "all", right_name)
        base = _find(groups, cohort, "all", "all")
        train = _find(groups, cohort, "train", left_name)
        oos = _find(groups, cohort, "oos", left_name)
        text = _compare(title, left, right, base)
        text += (
            f" 未知组不并入对照：形态未知 N={_find(groups, cohort, 'all', 'pattern_unknown').get('n_signals', '0')}，"
            f"板块未知 N={_find(groups, cohort, 'all', 'sector_unknown').get('n_signals', '0')}。"
            f" 训练段该组 N={train.get('n_signals', '0')}，样本外 N={oos.get('n_signals', '0')}，"
            f"样本外盈利率 {_fmt_pct(oos.get('return_20d_win_rate', ''))}。"
        )
        if number(oos.get("n_signals")) is not None and int(float(oos.get("n_signals") or 0)) < params.min_descriptive:
            text += " 样本外不足，全样本里看到的差异不能当成样本外结果。"
        add(key, question, text, cohort)

    condition_lines = []
    for cohort in ("daily", "weekly"):
        base = _find(groups, cohort, "all", "all")
        base_mean = number(base.get("return_20d_mean"))
        base_mae = number(base.get("mae_20d_mean"))
        for name in ("double_bottom", "breakout_yes", "strong", "double_and_strong"):
            group = _find(groups, cohort, "all", name)
            oos = _find(groups, cohort, "oos", name)
            mean = number(group.get("return_20d_mean"))
            mae = number(group.get("mae_20d_mean"))
            passed = group.get("small_sample") == "no" and oos.get("small_sample") == "no"
            better = (
                passed
                and mean is not None
                and base_mean is not None
                and mae is not None
                and base_mae is not None
                and mean > base_mean
                and mae > base_mae
                and (number(oos.get("return_20d_mean")) or -1) > (number(_find(groups, cohort, "oos", "all").get("return_20d_mean")) or 0)
            )
            condition_lines.append(
                f"{cohort}/{name}：N={group.get('n_signals')}，股票 {group.get('n_symbols')}，"
                f"20日均值 {_fmt_num(group.get('return_20d_mean', ''))}，MAE均值 {_fmt_num(group.get('mae_20d_mean', ''))}，"
                f"样本外 N={oos.get('n_signals', '0')}。"
                + ("方向在预设门槛内同时更好。" if better else "没有同时满足更高收益、更小不利波动、足够样本和样本外同向。")
            )
        post = _find(groups, cohort, "all", "posterior_turned_strong")
        condition_lines.append(
            f"{cohort}/推送后转入强势：N={post.get('n_signals')}，这是后验描述。"
            "信号之后才知道板块是否转强，用它筛入场会有 immortal-time 偏差，因此不把它当成提高收益的入场条件。"
        )
    add(
        "q5_conditions",
        "哪些条件提高收益并降低回撤？",
        "只检查事先写死的四组入场标签（双底、信号时已突破、信号时强势、双底且强势），没有在全样本里搜索其他组合。"
        + "".join(condition_lines),
        "daily+weekly",
    )

    thin = []
    for row in groups:
        if row["sample_split"] != "all":
            continue
        if row["group"] == "all":
            thin.append(f"{row['cohort']} 全样本 N={row['n_signals']}，股票 {row['n_symbols']}，未成熟20日 {row['immature_20d']}，小样本={row['small_sample']}")
        elif int(row["n_signals"] or 0) and row["small_sample"] == "yes":
            thin.append(f"{row['cohort']}/{row['group']} N={row['n_signals']}，股票 {row['n_symbols']}")
    train_empty = [
        f"{cohort} 训练段 N={_find(groups, cohort, 'train', 'all').get('n_signals', '0')}，清除重叠后样本外 N={_find(groups, cohort, 'oos', 'all').get('n_signals', '0')}"
        for cohort in ("daily", "weekly")
    ]
    add(
        "q6_insufficient",
        "哪些样本不足？",
        "4小时和月线不进入这轮日/周统计，但没有从历史文件删除。以下格子没有达到结论门槛，或训练/样本外被 20 日禁运抽空："
        + "；".join(thin + train_empty),
        "daily+weekly",
    )
    return answers
