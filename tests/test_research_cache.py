from __future__ import annotations

import pandas as pd

from research.prices import merge_cache, read_cache


def _frame(days, closes):
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "adj_close": closes,
            "volume": 1,
            "dividends": 0,
            "stock_splits": 0,
        },
        index=days,
    )


def test_empty_download_does_not_replace_cache_and_merge_is_idempotent(tmp_path):
    path = tmp_path / "AAA.csv"
    first = _frame([pd.Timestamp("2026-01-05").date(), pd.Timestamp("2026-01-06").date()], [10.0, 11.0])
    merge_cache(path, first)
    digest = path.read_bytes()
    kept, wrote = merge_cache(path, None)
    assert wrote is False
    assert path.read_bytes() == digest
    kept_empty, wrote_empty = merge_cache(path, _frame([], []))
    assert wrote_empty is False
    assert path.read_bytes() == digest
    assert list(kept["close"]) == [10.0, 11.0]
    update = _frame([pd.Timestamp("2026-01-06").date(), pd.Timestamp("2026-01-07").date()], [12.0, 13.0])
    merged, wrote_update = merge_cache(path, update)
    assert wrote_update is True
    assert list(merged.index) == list(pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]).date)
    assert float(merged.loc[pd.Timestamp("2026-01-05").date(), "close"]) == 10
    assert float(merged.loc[pd.Timestamp("2026-01-06").date(), "close"]) == 12
    digest = path.read_bytes()
    again, wrote_again = merge_cache(path, update)
    assert wrote_again is True
    assert path.read_bytes() == digest
    assert list(again["close"]) == [10.0, 12.0, 13.0]
    assert list(read_cache(path)["close"]) == [10.0, 12.0, 13.0]
