import time
import os
import bz2
import pickle
import re
import warnings
from collections import defaultdict

import pandas as pd
from pandas._libs.internals import BlockPlacement
from pandas.core.internals.blocks import new_block
from loguru import logger

import timescaledb_model as tsdb

warnings.filterwarnings("ignore", category=DeprecationWarning)

TSDB = tsdb.TimescaleStockMarketModel
DATADIR = "/mnt/data/"  # contains bourso/ and euronext/ subdirectories


def _new_block_compat(values, placement, ndim, **kwargs):
    """Rebuild a block from a pickle that stored its placement as a plain slice."""
    if not isinstance(placement, BlockPlacement):
        placement = BlockPlacement(placement)
    return new_block(values, placement, ndim=ndim, **kwargs)


class _CompatUnpickler(pickle.Unpickler):
    """Read Boursorama pickles whatever pandas wrote them.

    Two formats coexist in the archive. The oldest ones reference
    pandas.indexes, moved to pandas.core.indexes since. Those written from
    August 2023 onwards pass the block placement as a slice, which the current
    new_block refuses; wrap it back into a BlockPlacement.
    """

    def find_class(self, module, name):
        if module.startswith("pandas.indexes"):
            module = module.replace("pandas.indexes", "pandas.core.indexes")
        if module == "pandas.core.internals.blocks" and name == "new_block":
            return _new_block_compat
        return super().find_class(module, name)


# ---- Parsing helpers ----


def _to_number(series: pd.Series) -> pd.Series:
    """Parse Boursorama quotes, which are not plain numbers.

    A quote carries a one-letter status between parentheses -- "58.010(c)" is a
    closing price, "315.000(s)" a suspended one -- and thousands are separated
    by a space: "1 157.500". Reading them as-is silently dropped about 40% of
    every snapshot.

    The decimal separator is not stable either: part of 2024 is written the
    French way, "58,010(c)". No file ever mixes the two, so the comma can be
    mapped to a dot without guessing which role it plays.
    """
    return pd.to_numeric(
        series.astype(str)
        .str.replace(r"\([a-zA-Z]\)", "", regex=True)
        .str.replace(r"[\s\u00a0\u202f]", "", regex=True)
        .str.replace(",", ".", regex=False),
        errors="coerce",
    )


def _open_bourso_file(filepath: str):
    """Open a snapshot whatever the archive compressed it with.

    Snapshots are bz2-compressed pickles up to 2023 and raw pickles from 2024,
    where the .bz2 suffix was dropped too. Sniff the magic bytes rather than
    trust the name: relying on the extension silently skipped all of 2024.
    """
    with open(filepath, "rb") as probe:
        compressed = probe.read(3) == b"BZh"
    return bz2.open(filepath, "rb") if compressed else open(filepath, "rb")


def _parse_bourso_file(filepath: str):
    """Parse a boursorama snapshot. Returns a DataFrame, or None if unreadable."""
    filename = os.path.basename(filepath)
    match = re.match(r"comp[AB]\s+(.+)$", filename)
    if not match:
        return None

    dt_str = match.group(1)
    if dt_str.endswith(".bz2"):
        dt_str = dt_str[: -len(".bz2")]
    try:
        dt = pd.to_datetime(dt_str)
    except Exception:
        return None

    try:
        with _open_bourso_file(filepath) as f:
            df = _CompatUnpickler(f).load()
    except Exception:
        return None

    if df.empty:
        return None

    df = df.reset_index(drop=True)
    df["last"] = _to_number(df["last"])
    df["volume"] = _to_number(df["volume"])
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
    """Resolve a company to its id, creating it on first sight.

    ISIN is the strongest key but only Euronext carries one, so the symbol is
    matched together with the market: the same ticker can designate different
    companies on Paris and on Amsterdam.
    """
    mid = 0
    if market_alias and market_alias in db.market_id:
        mid = db.market_id[market_alias]

    if isin:
        rows = db.raw_query("SELECT id FROM companies WHERE isin = %s", (isin,))
        if rows and len(rows) > 0:
            return rows[0][0]

    rows = db.raw_query(
        "SELECT id FROM companies WHERE symbol = %s AND mid = %s", (symbol, mid)
    )
    if rows and len(rows) > 0:
        return rows[0][0]

    db.raw_query(
        "INSERT INTO companies (name, symbol, isin, mid) VALUES (%s, %s, %s, %s)",
        (name, symbol, isin, mid),
    )
    db.commit()

    rows = db.raw_query(
        "SELECT id FROM companies WHERE symbol = %s AND mid = %s", (symbol, mid)
    )
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
    df = df.drop_duplicates(subset=["date", "cid"], keep="last")
    out = df[["date", "cid", "value", "volume"]].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True)
    out["value"] = out["value"].astype("float32")
    out["volume"] = out["volume"].astype("float32")
    db.df_write(out, "stocks", commit=True)
    logger.info(f"Flushed {len(out)} stock records")


def _flush_daystocks(db: TSDB, df: pd.DataFrame):
    """Write a DataFrame to the daystocks table.

    A company has one aggregate per day by definition. Euronext can still emit
    two rows for it: when a security is reclassified the new ISIN trades under
    the old ticker for a while, and both lines are reported side by side. Keep
    the most recent one so the series stays single-valued.
    """
    if df.empty:
        return
    df = df.drop_duplicates(subset=["date", "cid"], keep="last")
    out = df[["date", "cid", "open", "close", "high", "low", "volume", "mean", "std"]].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True)
    for col in ["open", "close", "high", "low", "volume", "mean", "std"]:
        out[col] = out[col].astype("float32")
    out["cid"] = out["cid"].astype("int16")
    db.df_write(out, "daystocks", commit=True)
    logger.info(f"Flushed {len(out)} daystocks records")


# ---- Market detection ----


def _strip_bourso_prefix(symbol: str) -> str:
    """Remove the market code Boursorama prepends to its symbols.

    Boursorama writes "1rPSAN" where Euronext writes "SAN". Without stripping
    it the same company is created twice and its history is split in two.
    """
    for mid, name, alias, bourso_prefix, sws, euronext in tsdb.initial_markets_data:
        if bourso_prefix and symbol.startswith(bourso_prefix):
            return symbol[len(bourso_prefix):]
    return symbol


def _detect_market_alias_from_bourso_symbol(symbol: str) -> str:
    for mid, name, alias, bourso_prefix, sws, euronext in tsdb.initial_markets_data:
        if bourso_prefix and symbol.startswith(bourso_prefix):
            return alias
    return "paris"


# Needle to look for in the Euronext "Market" column, and the market it maps to.
_EURONEXT_MARKETS = (
    ("paris", "paris"),
    ("amsterdam", "amsterdam"),
    ("bruxel", "bruxelle"),
    ("brussel", "bruxelle"),
    ("milan", "milano"),
)


def _detect_market_alias_from_euronext_market(market_str: str) -> str:
    """Pick the primary market of a listing.

    A cross-listed stock reads "Euronext Paris, Amsterdam, Brussels": the
    primary market is the one named first, so match on position rather than on
    a fixed order, otherwise a Paris stock is filed under Amsterdam.
    """
    if not market_str or pd.isna(market_str):
        return "paris"
    market_lower = str(market_str).lower()

    best = None
    for needle, alias in _EURONEXT_MARKETS:
        pos = market_lower.find(needle)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, alias)
    return best[1] if best else "paris"


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


# ---- Store: Boursorama ----


def _store_bourso_files(start: str, end: str, db: TSDB):
    """Load ALL boursorama intraday bz2 pickle files into the database.

    1. Read every snapshot file into memory with pandas
    2. Store all intraday points in `stocks`
    3. Compute daily OHLC aggregates with pandas groupby -> `daystocks`
    """
    start_dt = pd.to_datetime(start)
    end_dt = pd.to_datetime(end)
    bourso_dir = os.path.join(DATADIR, "bourso")

    if not os.path.isdir(bourso_dir):
        logger.warning(f"Bourso directory not found: {bourso_dir}")
        return

    # ---- Step 1: Collect and group files by day ----
    files_by_day = defaultdict(list)
    for year_dir in sorted(os.listdir(bourso_dir)):
        year_path = os.path.join(bourso_dir, year_dir)
        if not os.path.isdir(year_path):
            continue
        for f in os.listdir(year_path):
            match = re.match(r"comp[AB]\s+(\d{4}-\d{2}-\d{2})\s", f)
            if not match:
                continue
            day_str = match.group(1)
            day_dt = pd.to_datetime(day_str)
            if day_dt < start_dt or day_dt >= end_dt:
                continue
            files_by_day[day_str].append(os.path.join(year_path, f))

    total_days = len(files_by_day)
    total_files = sum(len(v) for v in files_by_day.values())
    logger.info(f"Bourso: {total_files} files across {total_days} days")

    if not files_by_day:
        logger.warning(f"No boursorama file in range {start} -> {end}, nothing to import")
        return

    # ---- Step 2: Create companies from the first day's data ----
    company_cache = {}

    first_day = sorted(files_by_day.keys())[0]
    for fpath in files_by_day[first_day][:2]:  # read one compA + one compB
        result = _parse_bourso_file(fpath)
        if result is not None:
            for _, row in result.iterrows():
                symbol = row["symbol"]
                name = row["name"]
                if symbol not in company_cache:
                    market_alias = _detect_market_alias_from_bourso_symbol(symbol)
                    cid = _get_or_create_company(
                        db, name, _strip_bourso_prefix(symbol), market_alias=market_alias
                    )
                    company_cache[symbol] = cid

    logger.info(f"Created/cached {len(company_cache)} companies from first day")

    # ---- Step 3: Process each day - read all files, build stocks DataFrame ----
    days_processed = 0
    days_known = 0
    days_unreadable = 0

    for day_str in sorted(files_by_day.keys()):
        day_key = f"bourso_day_{day_str}"
        if _is_file_done(db, day_key):
            days_known += 1
            continue

        day_files = files_by_day[day_str]
        day_dfs = []

        for fpath in day_files:
            result = _parse_bourso_file(fpath)
            if result is not None:
                day_dfs.append(result)

        if not day_dfs:
            days_unreadable += 1
            logger.warning(
                f"Bourso: none of the {len(day_files)} files for {day_str} could be read"
            )
            continue

        # Concatenate all snapshots for this day
        day_df = pd.concat(day_dfs, ignore_index=True)
        day_df = day_df.dropna(subset=["last"])

        # Ensure all companies exist
        new_symbols = set(day_df["symbol"].unique()) - set(company_cache.keys())
        if new_symbols:
            for _, row in day_df[day_df["symbol"].isin(new_symbols)].drop_duplicates("symbol").iterrows():
                symbol = row["symbol"]
                if symbol not in company_cache:
                    market_alias = _detect_market_alias_from_bourso_symbol(symbol)
                    cid = _get_or_create_company(
                        db, row["name"], _strip_bourso_prefix(symbol), market_alias=market_alias
                    )
                    company_cache[symbol] = cid

        # Map symbols to cids using pandas
        day_df["cid"] = day_df["symbol"].map(company_cache)
        day_df = day_df.dropna(subset=["cid"])
        day_df["cid"] = day_df["cid"].astype(int)

        # ---- Insert into stocks (all intraday points) ----
        if day_df.empty:
            days_unreadable += 1
            logger.warning(
                f"Bourso: {len(day_files)} files for {day_str} parsed but held no "
                "usable quote"
            )
            continue

        stocks_df = day_df[["datetime", "cid", "last", "volume"]].rename(
            columns={"datetime": "date", "last": "value"}
        )
        _flush_stocks(db, stocks_df)

        # ---- Compute daystocks with pandas groupby ----
        daily = day_df.groupby("cid").agg(
            open=("last", "first"),
            close=("last", "last"),
            high=("last", "max"),
            low=("last", "min"),
            volume=("volume", "last"),  # last reported volume of the day
            mean=("last", "mean"),
            std=("last", "std"),
        ).reset_index()

        daily["std"] = daily["std"].fillna(0.0)
        daily["date"] = pd.to_datetime(day_str)

        # Euronext may already have written an end-of-day row for this company
        # and day. The Boursorama aggregate is computed from the intraday points
        # of the session, so it supersedes it: drop the old row before writing.
        day_cids = [int(c) for c in daily["cid"].unique()]
        if day_cids:
            db.raw_query(
                "DELETE FROM daystocks WHERE date = %s AND cid = ANY(%s)",
                (pd.to_datetime(day_str), day_cids),
            )
            db.commit()

        _flush_daystocks(db, daily)
        _mark_file_done(db, day_key)

        days_processed += 1
        if days_processed % 50 == 0:
            logger.info(f"Bourso: {days_processed}/{total_days} days done")

    logger.info(
        f"Bourso import complete: {days_processed} days imported, "
        f"{days_known} already present, {days_unreadable} unreadable"
    )


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
        website: Source website - "bourso" or "euronext".
        db: TimescaleStockMarketModel database instance.
    """
    logger.info(f"Loading {website} data from {start} to {end}")

    if website == "bourso":
        _store_bourso_files(start, end, db)
    elif website == "euronext":
        _store_euronext_files(start, end, db)
    else:
        raise ValueError(f"Unknown website: {website}. Use 'bourso' or 'euronext'.")

    logger.info(f"Done loading {website} data")