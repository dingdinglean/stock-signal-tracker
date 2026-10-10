from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

import sector_map
from sector_map import IndexSource


def _symbols(prefix: str, count: int, anchors: list[str]) -> list[str]:
    needed = count - len(anchors)
    return anchors + [f"{prefix}{index:03d}" for index in range(needed)]


def _frame(symbols: list[str], sector: str, industry: str, symbol_column: str = "Symbol", sector_column: str = "GICS Sector", industry_column: str = "GICS Sub-Industry") -> pd.DataFrame:
    return pd.DataFrame({symbol_column: symbols, sector_column: [sector] * len(symbols), industry_column: [industry] * len(symbols)})


def _plausible_payload(updated_at: str | None = None) -> dict:
    sp500 = _symbols("S", 450, ["AAPL", "MSFT", "XOM", "JPM"])
    nasdaq = _symbols("N", 90, ["AAPL", "MSFT", "NVDA", "AMZN"])
    rows = {
        symbol: {"sector": "Information Technology", "industry": "Semiconductors", "sector_theme": "半导体"}
        for symbol in set(sp500) | set(nasdaq)
    }
    payload = {
        "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
        "constituents": {"sp500": sp500, "nasdaq100": nasdaq},
        "symbols": rows,
    }
    assert sector_map._strictly_plausible(payload)
    return payload


def test_footnote_headers_map_to_sector_and_industry():
    source = next(item for item in sector_map.INDEX_SOURCES if item.name == "nasdaq100")
    symbols = _symbols("N", 90, ["AAPL", "MSFT", "NVDA", "AMZN"])
    table = _frame(symbols, "Technology", "Semiconductors", symbol_column="Ticker", sector_column="ICB Industry[1]", industry_column="ICB Subsector[1]")
    parsed = sector_map._select_constituents([table], source)
    assert parsed is not None
    assert parsed["NVDA"]["sector"] == "Technology"
    assert parsed["NVDA"]["industry"] == "Semiconductors"
    assert parsed["NVDA"]["sector_theme"] == "半导体"


def test_select_constituents_rejects_implausible_table_and_keeps_the_real_one():
    source = next(item for item in sector_map.INDEX_SOURCES if item.name == "sp500")
    symbols = _symbols("S", 450, ["AAPL", "MSFT", "XOM", "JPM"])
    decoy = _frame(["AAPL", "MSFT"], "Other", "Other")
    real = _frame(symbols, "Energy", "Integrated Oil & Gas")
    parsed = sector_map._select_constituents([decoy, real], source)
    assert parsed is not None
    assert len(parsed) == 450
    assert parsed["XOM"]["sector"] == "Energy"
    assert sector_map._select_constituents([decoy], source) is None


def test_download_keeps_sp500_labels_and_adds_nasdaq_only_symbols(monkeypatch):
    sp_symbols = _symbols("S", 450, ["AAPL", "MSFT", "XOM", "JPM"])
    nq_symbols = _symbols("N", 90, ["AAPL", "MSFT", "NVDA", "AMZN", "ASML"])
    tables = {
        sector_map.INDEX_SOURCES[0].url: [_frame(sp_symbols, "Information Technology", "Technology Hardware")],
        sector_map.INDEX_SOURCES[1].url: [_frame(nq_symbols, "Technology", "Semiconductors", symbol_column="Ticker", sector_column="ICB Industry[1]", industry_column="ICB Subsector[1]")],
    }

    class Response:
        def __init__(self, url: str):
            self.url = url
            self.text = url

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(sector_map.requests, "get", lambda url, headers, timeout: Response(url))

    def fake_read_html(source):
        text = source.getvalue() if hasattr(source, "getvalue") else str(source)
        return tables[text]

    monkeypatch.setattr(sector_map.pd, "read_html", fake_read_html)

    payload = sector_map._download_metadata()

    assert payload["symbols"]["AAPL"]["sector"] == "Information Technology"
    assert payload["symbols"]["ASML"]["sector"] == "Technology"
    assert "ASML" in payload["constituents"]["nasdaq100"]
    assert "ASML" not in payload["constituents"]["sp500"]
    assert sector_map._strictly_plausible(payload)


def test_fresh_plausible_cache_does_not_download(tmp_path, monkeypatch):
    cache = tmp_path / "sector_metadata_cache.json"
    payload = _plausible_payload()
    cache.write_text(json.dumps(payload), encoding="utf-8")

    def fail_download(*_args, **_kwargs):
        raise AssertionError("network download must not run for a fresh cache")

    monkeypatch.setattr(sector_map, "_download_metadata", fail_download)
    loaded = sector_map.load_metadata(cache)
    assert loaded["AAPL"]["sector"] == "Information Technology"
    assert json.loads(cache.read_text(encoding="utf-8"))["updated_at"] == payload["updated_at"]


def test_stale_cache_is_refreshed_when_download_is_plausible(tmp_path, monkeypatch):
    cache = tmp_path / "sector_metadata_cache.json"
    stale = _plausible_payload("2020-01-01T00:00:00+00:00")
    cache.write_text(json.dumps(stale), encoding="utf-8")
    refreshed = _plausible_payload()
    monkeypatch.setattr(sector_map, "_download_metadata", lambda timeout=20: refreshed)

    loaded = sector_map.load_metadata(cache)

    assert loaded == refreshed["symbols"]
    assert json.loads(cache.read_text(encoding="utf-8"))["updated_at"] == refreshed["updated_at"]


@pytest.mark.parametrize("error", [requests.ConnectionError("offline"), RuntimeError("sp500 constituent list missing or implausible")])
def test_failed_or_implausible_download_keeps_committed_cache(tmp_path, monkeypatch, error):
    cache = tmp_path / "sector_metadata_cache.json"
    stale = _plausible_payload("2020-01-01T00:00:00+00:00")
    cache.write_text(json.dumps(stale, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    original = cache.read_text(encoding="utf-8")

    def fail_download(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(sector_map, "_download_metadata", fail_download)
    loaded = sector_map.load_metadata(cache)

    assert set(loaded) == set(stale["symbols"])
    assert cache.read_text(encoding="utf-8") == original


def test_implausible_payload_is_not_written(tmp_path, monkeypatch):
    cache = tmp_path / "sector_metadata_cache.json"
    cache.write_text('{"updated_at": "2020-01-01T00:00:00+00:00", "symbols": {"AAPL": {"sector": "Information Technology", "industry": "Hardware", "sector_theme": "大型科技"}}}', encoding="utf-8")
    original = cache.read_text(encoding="utf-8")
    monkeypatch.setattr(sector_map, "_download_metadata", lambda timeout=20: {"updated_at": datetime.now(timezone.utc).isoformat(), "constituents": {"sp500": ["AAPL"], "nasdaq100": ["AAPL"]}, "symbols": {"AAPL": {"sector": "X", "industry": "Y", "sector_theme": "Z"}}})

    assert sector_map.refresh_metadata(cache) is False
    assert cache.read_text(encoding="utf-8") == original
    assert sector_map.load_metadata(cache)["AAPL"]["sector_theme"] == "大型科技"


def test_refresh_command_fails_without_changing_cache(tmp_path, monkeypatch):
    cache = tmp_path / "sector_metadata_cache.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sector_map, "_download_metadata", lambda timeout=20: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(SystemExit):
        sector_map.main(["--refresh", "--cache", str(cache)])
    assert cache.read_text(encoding="utf-8") == "{}"


def test_cache_older_than_ttl_is_not_fresh():
    payload = {"updated_at": (datetime.now(timezone.utc) - sector_map.CACHE_TTL - timedelta(seconds=1)).isoformat()}
    assert sector_map._fresh(payload) is False


def test_refresh_keeps_lookup_rows_that_left_the_index(tmp_path, monkeypatch):
    cache = tmp_path / "sector_metadata_cache.json"
    existing = _plausible_payload("2020-01-01T00:00:00+00:00")
    existing["symbols"]["PSKY"] = {"sector": "Communication Services", "industry": "Movies & Entertainment", "sector_theme": "其他（Movies & Entertainment）"}
    cache.write_text(json.dumps(existing), encoding="utf-8")
    monkeypatch.setattr(sector_map, "_download_metadata", lambda timeout=20: _plausible_payload())

    assert sector_map.refresh_metadata(cache) is True
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["symbols"]["PSKY"]["industry"] == "Movies & Entertainment"
    assert "PSKY" not in saved["constituents"]["sp500"]
    assert "PSKY" not in saved["constituents"]["nasdaq100"]
    assert sector_map._strictly_plausible(saved)


def test_committed_constituent_cache_includes_both_indexes():
    payload = json.loads((sector_map.DEFAULT_CACHE).read_text(encoding="utf-8"))
    assert sector_map._strictly_plausible(payload)
    sp500 = set(payload["constituents"]["sp500"])
    nasdaq = set(payload["constituents"]["nasdaq100"])
    assert nasdaq - sp500
    assert payload["symbols"]["AAPL"]["sector_theme"] == "大型科技"
    assert payload["symbols"]["ASML"]["sector"]


def test_index_source_urls_point_at_constituent_lists():
    urls = {source.name: source.url for source in sector_map.INDEX_SOURCES}
    assert urls["sp500"].endswith("List_of_S%26P_500_companies")
    assert urls["nasdaq100"].endswith("List_of_NASDAQ-100_companies")
    assert isinstance(sector_map.INDEX_SOURCES[0], IndexSource)
