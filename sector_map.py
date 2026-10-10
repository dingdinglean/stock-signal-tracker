"""Independent, cached public sector metadata for formal DXDX pushes.

This module deliberately does not import or download either source project's
code.  It only uses public index constituent metadata, plus a small local map
to align common industry names with the sector labels stored in the V4 artifact.

Constituent lists are read from the committed cache.  A download runs only when
that cache is stale or incomplete, and again from the weekly refresh workflow.
Implausible or failed downloads leave the committed cache untouched.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd
import requests


class IndexSource(NamedTuple):
    name: str
    url: str
    minimum: int
    maximum: int
    anchors: frozenset[str]


# The Nasdaq-100 article no longer contains the constituent table; the list page does.
INDEX_SOURCES = (
    IndexSource("sp500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 450, 550, frozenset({"AAPL", "MSFT", "XOM", "JPM"})),
    IndexSource("nasdaq100", "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies", 90, 130, frozenset({"AAPL", "MSFT", "NVDA", "AMZN"})),
)
WIKI_SOURCES = tuple(source.url for source in INDEX_SOURCES)
CACHE_TTL = timedelta(days=7)
DEFAULT_CACHE = Path("data/sector_metadata_cache.json")
USER_AGENT = "Mozilla/5.0 (compatible; StockSignalTracker/1.0)"
_MIN_SYMBOLS = 450
_MAX_LISTED = 700
_MAX_SYMBOLS = 1000

# Local classification aid, not a dependency on the strength-radar repository.
SYMBOL_THEME = {
    "NVDA": "半导体", "AMD": "半导体", "AVGO": "半导体", "TSM": "半导体", "ASML": "半导体", "AMAT": "半导体", "LRCX": "半导体", "MU": "半导体", "ARM": "半导体",
    "SMCI": "AI基础设施", "PLTR": "AI基础设施",
    "MSFT": "云软件", "ORCL": "云软件", "CRM": "云软件", "NOW": "云软件", "SNOW": "云软件", "DDOG": "云软件", "NET": "云软件",
    "CRWD": "网络安全", "PANW": "网络安全", "ZS": "网络安全", "FTNT": "网络安全",
    "JPM": "银行", "BAC": "银行", "WFC": "银行", "GS": "金融", "MS": "金融",
    "LLY": "生物医药", "NVO": "生物医药", "MRK": "生物医药", "VRTX": "生物医药", "REGN": "生物医药",
    "NEM": "黄金矿业", "AEM": "黄金矿业", "GOLD": "黄金矿业", "FCX": "金属/矿业",
    "XOM": "能源", "CVX": "能源", "COP": "能源", "SLB": "能源", "LNG": "能源",
    "RTX": "航空航天", "LMT": "航空航天", "NOC": "航空航天", "GE": "工业", "CAT": "工业",
    "COST": "消费", "WMT": "消费", "HD": "消费", "LOW": "消费", "AMZN": "消费",
    "AAPL": "大型科技", "META": "大型科技", "GOOGL": "大型科技", "NFLX": "大型科技", "TSLA": "大型科技",
}
INDUSTRY_TRANSLATIONS = {
    "Oil & Gas Refining & Marketing": "石油炼化", "Semiconductors": "半导体", "Cloud Software": "云软件",
    "Biotechnology": "生物科技", "Health Care Equipment": "医疗设备", "Life Sciences Tools & Services": "生命科学工具",
    "Agricultural & Farm Machinery": "农业机械", "Application Software": "应用软件", "Systems Software": "系统软件",
    "Aerospace & Defense": "航空航天", "Regional Banks": "区域银行", "Diversified Banks": "银行",
    "Metals & Mining": "金属矿业", "Gold": "黄金矿业", "Energy": "能源", "Industrials": "工业",
}


def _normalize_header(column: Any) -> str:
    text = re.sub(r"\[[^\]]*\]", "", str(column))
    return re.sub(r"\s+", " ", text).strip().lower()


def _column(columns: Any, choices: tuple[str, ...]) -> str | None:
    names = {_normalize_header(column): str(column) for column in columns}
    return next((names[_normalize_header(name)] for name in choices if _normalize_header(name) in names), None)


def _display(value: str) -> str:
    value = (value or "其他").strip()
    if any("\u4e00" <= char <= "\u9fff" for char in value):
        return value
    return INDUSTRY_TRANSLATIONS.get(value, f"其他（{value}）")


def _fresh(payload: dict[str, Any]) -> bool:
    try:
        then = datetime.fromisoformat(str(payload.get("updated_at", "")).replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - then <= CACHE_TTL
    except ValueError:
        return False


def _parse_constituent_table(table: pd.DataFrame) -> dict[str, dict[str, str]]:
    symbol_col = _column(table.columns, ("Symbol", "Ticker", "Ticker symbol"))
    if not symbol_col:
        return {}
    sector_col = _column(table.columns, ("GICS Sector", "Sector", "ICB sector", "ICB Industry"))
    industry_col = _column(table.columns, ("GICS Sub-Industry", "Industry", "ICB subsector", "ICB Subsector"))
    metadata: dict[str, dict[str, str]] = {}
    for _, row in table.iterrows():
        symbol = str(row.get(symbol_col, "")).strip().upper().replace(".", "-")
        if not symbol or len(symbol) > 10 or not symbol.replace("-", "").isalnum():
            continue
        sector = str(row.get(sector_col, "") if sector_col else "").strip() or "其他"
        industry = str(row.get(industry_col, "") if industry_col else "").strip() or sector
        metadata[symbol] = {"sector": sector, "industry": industry, "sector_theme": SYMBOL_THEME.get(symbol, _display(industry))}
    return metadata


def _select_constituents(tables: list[pd.DataFrame], source: IndexSource) -> dict[str, dict[str, str]] | None:
    """Pick the constituent table whose size and anchor symbols look like this index."""
    matches = []
    for table in tables:
        parsed = _parse_constituent_table(table)
        symbols = set(parsed)
        if source.minimum <= len(parsed) <= source.maximum and source.anchors <= symbols:
            matches.append(parsed)
    if not matches:
        return None
    return max(matches, key=len)


def _symbol_map(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, dict):
        return {}
    return {str(key): value for key, value in symbols.items() if isinstance(value, dict)}


def _read_cache(cache_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cache(cache_path: Path, payload: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _strictly_plausible(payload: dict[str, Any]) -> bool:
    """True only when both committed constituent lists look like the real indexes."""
    if not isinstance(payload, dict):
        return False
    symbols = payload.get("symbols")
    constituents = payload.get("constituents")
    if not isinstance(symbols, dict) or not isinstance(constituents, dict):
        return False
    if any(not isinstance(value, dict) for value in symbols.values()):
        return False
    union: set[str] = set()
    for source in INDEX_SOURCES:
        listed = constituents.get(source.name)
        if not isinstance(listed, list):
            return False
        cleaned = [str(item).strip().upper() for item in listed]
        if len(cleaned) != len(set(cleaned)) or not (source.minimum <= len(cleaned) <= source.maximum):
            return False
        if not source.anchors <= set(cleaned):
            return False
        union.update(cleaned)
    # Constituent lists are the current indexes. Previously cached symbols may
    # remain in the lookup map after they leave an index.
    return union <= set(symbols) and _MIN_SYMBOLS <= len(union) <= _MAX_LISTED and len(symbols) <= _MAX_SYMBOLS


def _download_metadata(timeout: int = 20) -> dict[str, Any]:
    merged: dict[str, dict[str, str]] = {}
    constituents: dict[str, list[str]] = {}
    for source in INDEX_SOURCES:
        response = requests.get(source.url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        response.raise_for_status()
        parsed = _select_constituents(pd.read_html(StringIO(response.text)), source)
        if parsed is None:
            raise RuntimeError(f"{source.name} constituent list missing or implausible")
        constituents[source.name] = sorted(parsed)
        for symbol, row in parsed.items():
            # Keep S&P GICS labels when a symbol is also in the Nasdaq-100.
            merged.setdefault(symbol, row)
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "constituents": constituents, "symbols": merged}
    if not _strictly_plausible(payload):
        raise RuntimeError("combined constituent metadata failed plausibility checks")
    return payload


def refresh_metadata(cache_path: Path) -> bool:
    """Download both constituent lists. Leave the committed cache unchanged on failure."""
    cached = _read_cache(cache_path)
    try:
        payload = _download_metadata()
    except Exception as exc:
        print(f"::warning::Sector metadata unavailable: {exc}; committed cache left unchanged")
        return False
    for symbol, row in _symbol_map(cached).items():
        payload["symbols"].setdefault(symbol, row)
    if not _strictly_plausible(payload):
        print("::warning::Sector metadata unavailable: implausible constituent list; committed cache left unchanged")
        return False
    _write_cache(cache_path, payload)
    return True


def load_metadata(cache_path: Path) -> dict[str, dict[str, str]]:
    """Use the committed cache. Refresh only when it is stale or incomplete.

    Network failures and implausible Wikipedia results do not abort the tracker
    and do not overwrite the cache.
    """
    cached = _read_cache(cache_path)
    if cached.get("symbols") and _fresh(cached) and _strictly_plausible(cached):
        return _symbol_map(cached)
    if refresh_metadata(cache_path):
        return _symbol_map(_read_cache(cache_path))
    return _symbol_map(cached)


def metadata_for(symbol: str, metadata: dict[str, dict[str, str]]) -> dict[str, str]:
    row = metadata.get(symbol.upper(), {})
    industry = str(row.get("industry", "其他"))
    return {
        "industry": industry,
        "sector_theme": str(row.get("sector_theme") or SYMBOL_THEME.get(symbol.upper()) or _display(industry)),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Refresh cached S&P 500 and Nasdaq-100 constituents")
    parser.add_argument("--refresh", action="store_true", help="download constituent lists and update the committed cache")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    args = parser.parse_args(argv)
    if not args.refresh:
        parser.error("--refresh is required")
    if not refresh_metadata(Path(args.cache)):
        raise SystemExit(1)
    print(f"Refreshed {args.cache}")


if __name__ == "__main__":
    main()
