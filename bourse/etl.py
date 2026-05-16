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


# ---- Store: Euronext ----


def _store_euronext_files(start: str, end: str, db: TSDB):
    """Load Euronext CSV/XLSX files. They contain daily OHLC so we insert
    into both stocks and daystocks directly."""
    start_dt = pd.to_datetime(start)
    end_dt = pd.to_datetime(end)
    euronext_dir = os.path.join(DATADIR, "euronext")

    if not os.path.isdir(euronext_dir):
        logger.warning(f"Euronext directory not found: {euronext_dir}")
        return

    all_files = sorted(
        os.path.join(euronext_dir, f)
        for f in os.listdir(euronext_dir)
        if f.endswith(".csv") or f.endswith(".xlsx")
    )
    logger.info(f"Found {len(all_files)} euronext files")

    company_cache = {}
    stocks_rows = []
    daystocks_rows = []

    for fpath in all_files:
        basename = os.path.basename(fpath)
        file_date = _extract_date_from_euronext_filename(fpath)
        if pd.isna(file_date) or file_date < start_dt or file_date >= end_dt:
            continue

        if _is_file_done(db, basename):
            continue

        logger.info(f"Processing {basename}")

        try:
            if fpath.endswith(".csv"):
                df = _parse_euronext_csv(fpath)
            else:
                df = _parse_euronext_xlsx(fpath)
        except Exception as e:
            logger.error(f"Error reading {fpath}: {e}")
            _mark_file_done(db, basename)
            continue

        if df.empty:
            _mark_file_done(db, basename)
            continue

        for col in ["open", "high", "low", "last", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["last"])
        df = df[df["last"] > 0]

        for _, row in df.iterrows():
            symbol = str(row.get("symbol", ""))
            name = str(row.get("name", ""))
            isin = str(row.get("isin", "")) if pd.notna(row.get("isin")) else None
            market_str = str(row.get("market", "")) if pd.notna(row.get("market")) else ""

            cache_key = isin or symbol
            if cache_key not in company_cache:
                market_alias = _detect_market_alias_from_euronext_market(market_str)
                cid = _get_or_create_company(db, name, symbol, isin=isin, market_alias=market_alias)
                company_cache[cache_key] = cid

            cid = company_cache[cache_key]
            if cid is None:
                continue

            last_val = float(row["last"])
            vol = float(row["volume"]) if pd.notna(row.get("volume")) else 0.0
            open_val = float(row["open"]) if pd.notna(row.get("open")) else last_val
            high_val = float(row["high"]) if pd.notna(row.get("high")) else last_val
            low_val = float(row["low"]) if pd.notna(row.get("low")) else last_val

            stocks_rows.append({"date": file_date, "cid": cid, "value": last_val, "volume": vol})
            daystocks_rows.append({
                "date": file_date, "cid": cid,
                "open": open_val, "close": last_val, "high": high_val, "low": low_val,
                "volume": vol, "mean": (open_val + high_val + low_val + last_val) / 4.0, "std": 0.0,
            })

        _mark_file_done(db, basename)

        if len(stocks_rows) >= 5000:
            _flush_stocks(db, pd.DataFrame(stocks_rows))
            _flush_daystocks(db, pd.DataFrame(daystocks_rows))
            stocks_rows = []
            daystocks_rows = []

    if stocks_rows:
        _flush_stocks(db, pd.DataFrame(stocks_rows))
    if daystocks_rows:
        _flush_daystocks(db, pd.DataFrame(daystocks_rows))


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
    """Extract, transform and load stock data files into the database.

    Args:
        start: Start date (inclusive) in YYYY-MM-DD format.
        end: End date (exclusive) in YYYY-MM-DD format.
        website: Source website - "euronext".
        db: TimescaleStockMarketModel database instance.
    """
    logger.info(f"Loading {website} data from {start} to {end}")

    if website == "euronext":
        _store_euronext_files(start, end, db)
    else:
        raise ValueError(f"Unknown website: {website}. Use 'euronext'.")

    logger.info(f"Done loading {website} data")