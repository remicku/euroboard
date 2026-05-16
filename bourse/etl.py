import time
import os
import bz2
import pickle
import re
import warnings
from collections import defaultdict

import pandas as pd
from loguru import logger

import timescaledb_model as tsdb

warnings.filterwarnings("ignore", category=DeprecationWarning)

TSDB = tsdb.TimescaleStockMarketModel
DATADIR = "/mnt/data/"  # contains bourso/ and euronext/ subdirectories


class _CompatUnpickler(pickle.Unpickler):
    """Handle old pandas pickle format (pandas.indexes -> pandas.core.indexes)."""

    def find_class(self, module, name):
        if module.startswith("pandas.indexes"):
            module = module.replace("pandas.indexes", "pandas.core.indexes")
        return super().find_class(module, name)


# ---- Parsing helpers ----


def _parse_bourso_file(filepath: str):
    """Parse a boursorama bz2 pickle file. Returns (datetime, DataFrame) or None."""
    filename = os.path.basename(filepath)
    match = re.match(r"comp[AB]\s+(.+)\.bz2$", filename)
    if not match:
        return None

    dt_str = match.group(1)
    try:
        dt = pd.to_datetime(dt_str)
    except Exception:
        return None

    try:
        with bz2.open(filepath, "rb") as f:
            df = _CompatUnpickler(f).load()
    except Exception:
        return None

    if df.empty:
        return None

    df = df.reset_index(drop=True)
    df["last"] = pd.to_numeric(df["last"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["datetime"] = dt
    return df[["symbol", "name", "last", "volume", "datetime"]]


def _parse_euronext_csv(filepath: str) -> pd.DataFrame:
    df = pd.read_csv(filepath, sep="\t", header=0, skiprows=[1, 2, 3])
    df = df.rename(
        columns={
            "Name": "name", "ISIN": "isin", "Symbol": "symbol",
            "Market": "market", "Trading Currency": "currency",
            "Open": "open", "High": "high", "Low": "low", "Last": "last",
            "Last Date/Time": "last_datetime", "Time Zone": "timezone",
            "Volume": "volume", "Turnover": "turnover",
        }
    )
    return df


def _parse_euronext_xlsx(filepath: str) -> pd.DataFrame:
    df = pd.read_excel(filepath, skiprows=[1, 2, 3])
    df = df.rename(
        columns={
            "Name": "name", "ISIN": "isin", "Symbol": "symbol",
            "Market": "market", "Currency": "currency",
            "Open Price": "open", "High Price": "high", "low Price": "low",
            "last Price": "last", "last Trade MIC Time": "last_datetime",
            "Time Zone": "timezone", "Volume": "volume", "Turnover": "turnover",
        }
    )
    return df


def _extract_date_from_euronext_filename(filepath: str) -> pd.Timestamp:
    basename = os.path.basename(filepath)
    match = re.search(r"(\d{4}-\d{2}-\d{2})", basename)
    if match:
        return pd.to_datetime(match.group(1))
    return pd.NaT


# ---- DB helpers ----


def _get_or_create_company(db: TSDB, name: str, symbol: str, isin: str = None, market_alias: str = None) -> int:
    rows = db.raw_query("SELECT id FROM companies WHERE symbol = %s", (symbol,))
    if rows and len(rows) > 0:
        return rows[0][0]

    if isin:
        rows = db.raw_query("SELECT id FROM companies WHERE isin = %s", (isin,))
        if rows and len(rows) > 0:
            return rows[0][0]

    mid = 0
    if market_alias and market_alias in db.market_id:
        mid = db.market_id[market_alias]

    db.raw_query(
        "INSERT INTO companies (name, symbol, isin, mid) VALUES (%s, %s, %s, %s)",
        (name, symbol, isin, mid),
    )
    db.commit()

    rows = db.raw_query("SELECT id FROM companies WHERE symbol = %s", (symbol,))
    if rows and len(rows) > 0:
        return rows[0][0]
    return None


def _is_file_done(db: TSDB, filename: str) -> bool:
    rows = db.raw_query("SELECT name FROM file_done WHERE name = %s", (filename,))
    return rows is not None and len(rows) > 0


def _mark_file_done(db: TSDB, filename: str):
    try:
        db.raw_query("INSERT INTO file_done (name) VALUES (%s)", (filename,))
        db.commit()
    except Exception:
        db.commit()


def _flush_stocks(db: TSDB, df: pd.DataFrame):
    """Write a DataFrame to the stocks table."""
    if df.empty:
        return
    out = df[["date", "cid", "value", "volume"]].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True)
    out["value"] = out["value"].astype("float32")
    out["volume"] = out["volume"].astype("float32")
    db.df_write(out, "stocks", commit=True)
    logger.info(f"Flushed {len(out)} stock records")


def _flush_daystocks(db: TSDB, df: pd.DataFrame):
    """Write a DataFrame to the daystocks table."""
    if df.empty:
        return
    out = df[["date", "cid", "open", "close", "high", "low", "volume", "mean", "std"]].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True)
    for col in ["open", "close", "high", "low", "volume", "mean", "std"]:
        out[col] = out[col].astype("float32")
    out["cid"] = out["cid"].astype("int16")
    db.df_write(out, "daystocks", commit=True)
    logger.info(f"Flushed {len(out)} daystocks records")


# ---- Market detection ----


def _detect_market_alias_from_bourso_symbol(symbol: str) -> str:
    for mid, name, alias, bourso_prefix, sws, euronext in tsdb.initial_markets_data:
        if bourso_prefix and symbol.startswith(bourso_prefix):
            return alias
    return "paris"


def _detect_market_alias_from_euronext_market(market_str: str) -> str:
    if not market_str or pd.isna(market_str):
        return "paris"
    market_lower = str(market_str).lower()
    if "amsterdam" in market_lower:
        return "amsterdam"
    if "bruxel" in market_lower or "brussel" in market_lower:
        return "bruxelle"
    if "milan" in market_lower:
        return "milano"
    return "paris"


# ---- Decorator ----


def timer_decorator(func):
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        print(f"{func.__name__} run in {(end_time - start_time):.2f} seconds.")
        return result
    return wrapper


# ---- Public API ----


@timer_decorator
def store_files(start: str, end: str, website: str, db: TSDB):
    raise NotImplementedError("The store_files function is not implemented")
