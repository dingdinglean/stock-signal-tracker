"""Independent, cached public sector metadata for formal DXDX pushes.

This module deliberately does not import or download either source project's
code.  It only uses public index constituent metadata, plus a small local map
to align common industry names with the sector labels stored in the V4 artifact.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import requests


WIKI_SOURCES = (
    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "https://en.wikipedia.org/wiki/Nasdaq-100",
)
CACHE_TTL = timedelta(days=7)
USER_AGENT = "Mozilla/5.0 (compatible; StockSignalTracker/1.0)"

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


def _column(columns: Any, choices: tuple[str, ...]) -> str | None:
    names = {str(column).strip().lower(): str(column) for column in columns}
    return next((names[name.lower()] for name in choices if name.lower() in names), None)


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


def _download_metadata(timeout: int = 20) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    for url in WIKI_SOURCES:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        response.raise_for_status()
        for table in pd.read_html(StringIO(response.text)):
            symbol_col = _column(table.columns, ("Symbol", "Ticker", "Ticker symbol"))
            if not symbol_col:
                continue
            sector_col = _column(table.columns, ("GICS Sector", "Sector", "ICB sector"))
            industry_col = _column(table.columns, ("GICS Sub-Industry", "Industry", "ICB subsector"))
            for _, row in table.iterrows():
                symbol = str(row.get(symbol_col, "")).strip().upper().replace(".", "-")
                if not symbol or len(symbol) > 10 or not symbol.replace("-", "").isalnum():
                    continue
                sector = str(row.get(sector_col, "") if sector_col else "").strip() or "其他"
                industry = str(row.get(industry_col, "") if industry_col else "").strip() or sector
                metadata[symbol] = {"sector": sector, "industry": industry, "sector_theme": SYMBOL_THEME.get(symbol, _display(industry))}
            break
    return metadata


def load_metadata(cache_path: Path) -> dict[str, dict[str, str]]:
    """Use a seven-day local cache; public metadata failures do not abort runs."""
    cached: dict[str, Any] = {}
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if cached.get("symbols") and _fresh(cached):
        return {str(key): value for key, value in cached["symbols"].items() if isinstance(value, dict)}
    try:
        symbols = _download_metadata()
        if not symbols:
            raise RuntimeError("no public index metadata")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "symbols": symbols}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return symbols
    except Exception as exc:
        print(f"::warning::Sector metadata unavailable: {exc}")
        return {str(key): value for key, value in cached.get("symbols", {}).items() if isinstance(value, dict)}


def metadata_for(symbol: str, metadata: dict[str, dict[str, str]]) -> dict[str, str]:
    row = metadata.get(symbol.upper(), {})
    industry = str(row.get("industry", "其他"))
    return {
        "industry": industry,
        "sector_theme": str(row.get("sector_theme") or SYMBOL_THEME.get(symbol.upper()) or _display(industry)),
    }
