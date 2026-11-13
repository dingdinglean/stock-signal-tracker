"""Research hypotheses. These constants are not buy rules and are not fit on the sample."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatternParams:
    """Chart-pattern hypotheses applied only inside the as-of window."""

    pivot_left: int = 3
    pivot_right: int = 3
    min_low_gap: int = 5
    max_low_gap: int = 40
    max_low_price_diff: float = 0.03
    min_mid_rebound: float = 0.05
    # Second low must sit inside this many bars of the as-of bar.
    # An older W is not treated as the pattern in force at the push.
    max_second_low_age: int = 30
    platform_window: int = 20
    platform_low_tolerance: float = 0.04
    platform_contraction: float = 0.70
    platform_pivot: int = 2
    drop_lookback: int = 10
    drop_threshold: float = -0.08
    stabilize_bars: int = 5
    stabilize_undercut: float = 0.005
    rebound_from_low: float = 0.02
    min_bars: int = 40


DAILY_PARAMS = PatternParams()
WEEKLY_PARAMS = PatternParams(
    pivot_left=2,
    pivot_right=2,
    min_low_gap=3,
    max_low_gap=12,
    platform_window=8,
    platform_pivot=1,
    max_second_low_age=8,
    drop_lookback=6,
    stabilize_bars=3,
    min_bars=16,
)


@dataclass(frozen=True)
class SectorParams:
    """Top-3 windows. Missing snapshots stay unknown; they are not coded as non-strong."""

    prior_short: int = 5
    prior_long: int = 10
    sustain_hits: int = 7
    posterior_short: int = 5
    posterior_long: int = 10


SECTOR_PARAMS = SectorParams()


@dataclass(frozen=True)
class StudyParams:
    """Fixed before looking at outcomes. The 20-session embargo matches the longest hold."""

    min_descriptive: int = 8
    min_conclusion: int = 20
    min_symbols: int = 8
    embargo_sessions: int = 20
    train_fraction: float = 0.6
    take_profit: float = 0.05
    stop_loss: float = 0.05
    fee_bps: float = 5.0
    slippage_bps: float = 5.0
    close_buffer_minutes: int = 20
    chart_lookback: int = 60
    price_start: str = "2024-06-01"


STUDY_PARAMS = StudyParams()

# Below this many signals, or this many distinct symbols, a difference is reported
# as a count and is not phrased as a finding.
CONCLUSION_RULE = (
    "结论门槛：同一组 N>=20 且独立股票数>=8，并且清除 20 个交易日重叠后的样本外方向一致。"
    "未达到时只报告计数，不解释成因果关系。"
)
