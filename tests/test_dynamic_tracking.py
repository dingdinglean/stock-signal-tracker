from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

import tracker
import dynamic_tracking
from dynamic_tracking import (display_allowed, dynamic_quote, latest_completed_session,
                              mark_visibility, market_calendar, month_anniversary,
                              strategy_statistics, trading_days)
from notifications import build_body, changes_between, prepare_email_data
from publish_changes import merge_history_rows

NOW = datetime(2026, 10, 16, 5, tzinfo=timezone.utc)
CUTOFF = date(2026, 10, 15)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    # Scheduled CI must remain deterministic after these sample signal dates.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(dynamic_tracking, "datetime", Clock)


def row(**updates):
    result = {f: "" for f in tracker.HISTORY_FIELDS}
    result.update(signal_id="one", symbol="META", signal_date="2026-09-04", push_date="2026-09-09",
                  signal_price="616.77", push_price="100", source_timeframe="weekly", daily_dxdx="True",
                  quote_status="ok", price_as_of=latest_completed_session().isoformat(), latest_return="-1", return_20d="20", sessions_observed="20")
    result.update(updates)
    return result


def frame(start="2026-09-09", end="2026-10-15", price=110):
    dates = market_calendar(int(start[:4]), int(end[:4])).sessions_in_range(start, end)
    closes = [100.] + [float(price)] * (len(dates) - 1)
    return pd.DataFrame({"close": closes, "adj close": closes, "high": closes, "low": closes,
                         "stock splits": [0.] * len(dates)}, index=dates)


@pytest.mark.parametrize("signal_day,value,visible", [
    ("2026-09-17", "-10", True), ("2026-09-16", "-10", False),
    ("2026-09-16", "1", True), ("2026-09-16", "0", True),
    ("2024-01-01", "12", True), ("2024-01-01", "-0.000001", False),
])
def test_natural_month_boundary_and_unlimited_retention(signal_day, value, visible):
    assert display_allowed(row(signal_date=signal_day, latest_return=value), date(2026, 10, 16)) is visible


@pytest.mark.parametrize("start,expected", [(date(2026,1,31), date(2026,2,28)),
    (date(2028,1,31), date(2028,2,29)), (date(2026,8,31), date(2026,9,30)),
    (date(2026,12,31), date(2027,1,31))])
def test_month_end_clamps_to_calendar_month(start, expected):
    assert month_anniversary(start) == expected


def test_hidden_recovers_and_can_hide_again_with_durable_transition_log():
    r = row()
    for value in ("-1", "0", "2", "-3"):
        r["latest_return"] = value
        mark_visibility(r, date(2026,10,16), NOW.isoformat())
    assert [x["state"] for x in json.loads(r["visibility_events"])] == ["hidden", "visible", "hidden"]
    before = r["visibility_events"]
    mark_visibility(r, date(2026,10,16), NOW.isoformat())
    assert r["visibility_events"] == before


@pytest.mark.parametrize("state,value,expected", [("visible","-50",True), ("hidden","50",False), ("","",True)])
def test_missing_quote_preserves_previous_visibility(state, value, expected):
    r = row(display_state=state, quote_status="pending", latest_return=value)
    assert display_allowed(r, date(2026,10,16)) is expected


def test_calendar_weekend_holiday_early_close_and_open_bar():
    assert trading_days(date(2026,9,4), date(2026,9,8)) == 1  # Labor Day
    assert latest_completed_session(datetime(2026,9,7,23,tzinfo=timezone.utc)) == date(2026,9,4)
    assert latest_completed_session(datetime(2026,11,27,18,19,tzinfo=timezone.utc)) == date(2026,11,25)
    assert latest_completed_session(datetime(2026,11,27,18,20,tzinfo=timezone.utc)) == date(2026,11,27)
    assert latest_completed_session(datetime(2026,10,15,19,tzinfo=timezone.utc)) == date(2026,10,14)


@pytest.mark.parametrize("start", ["2026-09-09", "2026-07-01", "2026-01-02", "2024-01-02"])
def test_all_history_keeps_updating_past_20_60_100_and_one_year(monkeypatch, start):
    r = row(signal_date=start, push_date=start, display_state="hidden", return_1d="1.23")
    immutable = {k:r[k] for k in ("signal_id","signal_date","signal_price","push_date","push_price","return_1d","return_20d")}
    prices = frame(start=start)
    calls=[]
    monkeypatch.setattr(tracker, "fetch_history", lambda symbol, first: calls.append((symbol, first)) or prices)
    tracker.update_performance([r], now=NOW)
    assert len(calls)==1
    assert r["price_as_of"]=="2026-10-15" and float(r["latest_return"])==pytest.approx(10)
    assert r["tracking_sessions"]==len(prices)-1 and r["display_state"]=="visible"
    assert {k:r[k] for k in immutable} == immutable
    previous=copy.deepcopy(r)
    tracker.update_performance([r], now=NOW)
    assert changes_between([previous],[r]) == []
    assert r["visibility_events"] == previous["visibility_events"]


def test_retry_failure_keeps_prices_baseline_milestones_and_state(monkeypatch):
    r=row(latest_price="99",price_as_of="2026-10-14",display_state="visible",latest_return="-1")
    old=copy.deepcopy(r);calls=[]
    monkeypatch.setattr(tracker,"fetch_history",lambda *_: calls.append(1) or pd.DataFrame())
    monkeypatch.setattr(tracker.time,"sleep",lambda _:None)
    tracker.update_performance([r],now=NOW)
    assert len(calls)==3 and r["quote_status"]=="pending"
    for k in ("latest_price","latest_return","price_as_of","return_20d","signal_price","push_price","display_state"):
        assert r[k]==old[k]
    assert r["last_updated"]==old["last_updated"]


def test_retry_can_recover_and_all_signals_for_a_symbol_are_refreshed(monkeypatch):
    attempts=[pd.DataFrame(),frame(price=99)]
    monkeypatch.setattr(tracker,"fetch_history",lambda *_:attempts.pop(0))
    monkeypatch.setattr(tracker.time,"sleep",lambda _:None)
    rows=[row(),row(signal_id="two",signal_date="2026-09-08",display_state="hidden")]
    tracker.update_performance(rows,now=NOW)
    assert all(r["quote_status"]=="ok" for r in rows)
    assert all(r["display_state"]=="hidden" for r in rows)
    assert len({r["signal_id"] for r in rows})==2


def test_pending_migration_preserves_legacy_hidden_state(monkeypatch):
    r=row(return_20d="-1",quote_status="",latest_return="")
    monkeypatch.setattr(tracker,"fetch_history",lambda *_:pd.DataFrame())
    monkeypatch.setattr(tracker.time,"sleep",lambda _:None)
    tracker.update_performance([r],now=NOW)
    assert r["display_state"]=="hidden" and r["latest_return"]==""


def test_split_and_dividend_adjustment_does_not_rewrite_source_price():
    prices=frame(price=26.25)
    prices.iloc[0,prices.columns.get_loc("close")]=25
    prices.iloc[0,prices.columns.get_loc("adj close")]=23.75
    prices.iloc[1,prices.columns.get_loc("stock splits")]=4
    r=row()
    result=dynamic_quote(prices,r,CUTOFF)
    assert result["latest_price"]=="26.25"
    assert float(result["comparable_price"])==pytest.approx(110.526315789)
    assert float(result["latest_return"])==pytest.approx(10.526315789)
    assert r["push_price"]=="100" and r["signal_price"]=="616.77"


@pytest.mark.parametrize("kind",["stale","no_adjusted","nan","currency","wrong_scale","outlier","duplicate"])
def test_abnormal_quotes_are_rejected(kind):
    prices=frame()
    if kind=="stale":prices=prices.iloc[:-1]
    if kind=="no_adjusted":prices=prices.drop(columns=["adj close"])
    if kind=="nan":prices.iloc[-1,prices.columns.get_loc("close")]=float("nan")
    if kind=="currency":prices.attrs["currency"]="HKD"
    if kind=="wrong_scale":prices.iloc[0,prices.columns.get_loc("close")]=1
    if kind=="outlier":prices.iloc[-1,prices.columns.get_loc("adj close")]=99999
    if kind=="duplicate":prices=pd.concat([prices,prices.tail(1)])
    with pytest.raises(ValueError):dynamic_quote(prices,row(),CUTOFF)


def test_email_stats_and_true_strategy_stats_use_separate_populations():
    rows=[row(signal_id=str(i),latest_return=v) for i,v in enumerate(["10","0","-20"])]
    data=prepare_email_data([],rows)
    assert len(data.display_history)==2 and data.eliminated_count==1
    assert strategy_statistics(list(data.display_history))["average_return"]==5
    stats=strategy_statistics(list(data.all_history))
    assert stats["total"]==3 and stats["loss"]==1 and stats["win_rate"]==pytest.approx(100/3)
    assert stats["average_return"]==pytest.approx(-10/3)
    assert strategy_statistics([row(latest_return="")])["average_return"] is None


def test_quotes_update_without_increasing_email_frequency():
    old=row(return_1d="1",effectiveness="有效")
    new=dict(old,latest_return="15",latest_price="115",display_state="visible")
    assert changes_between([old],[new])==[]
    assert prepare_email_data([], [new]).has_changes is False


def test_publish_merges_dynamic_snapshot_as_bundle_and_preserves_all_transitions():
    base=row(latest_price="100",latest_return="0",quote_checked_at="2026-10-13T22:00:00+00:00",price_as_of="2026-10-13")
    remote=dict(base,latest_price="110",latest_return="10",quote_checked_at="2026-10-14T22:00:00+00:00",price_as_of="2026-10-14",sessions_observed="25",visibility_events='[{"state":"visible","checked_at":"2026-10-14"}]')
    local=dict(base,latest_price="95",latest_return="-5",quote_checked_at="2026-10-15T22:00:00+00:00",price_as_of="2026-10-15",sessions_observed="26",visibility_events='[{"state":"hidden","checked_at":"2026-10-15"}]')
    merged=merge_history_rows([base],[remote],[local])[0]
    assert (merged["latest_price"],merged["latest_return"],merged["price_as_of"])==("95","-5","2026-10-15")
    assert len(json.loads(merged["visibility_events"]))==2 and merged["push_price"]=="100"


def test_email_orders_dynamic_returns_and_shows_quote_date_and_colors():
    rows=[row(signal_id="a",symbol="LOW",latest_return="-2",signal_date="2026-10-01"),
          row(signal_id="b",symbol="HIGH",latest_return="5",price_as_of="2026-10-15")]
    body=build_body(prepare_email_data([],rows))
    assert body.index("HIGH")<body.index("LOW")
    assert "2026-10-15" in body and "#16803c" in body and "#c62828" in body
    assert all(f">T+{n}</th>" not in body for n in (1,3,5,10,20))


def test_stale_saved_quote_cannot_change_visibility_when_a_month_has_elapsed():
    r=row(price_as_of="2020-01-01",display_state="visible",latest_return="-20")
    assert display_allowed(r,date(2026,10,16)) is True
    body=build_body(prepare_email_data([], [r]))
    assert "待更新" in body and "META" in body


def test_missing_session_does_not_shift_milestone_dates():
    prices=frame().drop(frame().index[3])
    with pytest.raises(ValueError,match="交易日行情不完整"):
        dynamic_quote(prices,row(),CUTOFF)


def test_yfinance_repair_optional_dependency_is_installed():
    from scipy import stats
    assert stats.norm.cdf(0) == 0.5


@pytest.mark.parametrize("rows,expected", [([], 0), ([row(quote_status="pending")], None),
                                        ([row(quote_status="pending"), row()], 1)])
def test_refresh_health_rejects_all_failed_quotes_without_modifying_history(rows, expected):
    from dynamic_tracking import verify_refresh_health
    before = copy.deepcopy(rows)
    if expected is None:
        with pytest.raises(RuntimeError, match="No historical quotes refreshed"):
            verify_refresh_health(rows)
    else:
        assert verify_refresh_health(rows) == expected
    assert rows == before
