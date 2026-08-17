"""Tests for the parts of the pipeline that read the archive.

Every case here stands for a real defect the archive produced: the source
format drifts over the six years it spans, and a parser that silently returns
nothing looks exactly like a day the market was closed. These lock in what each
variation looks like.
"""

import bz2
import pickle
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bourse"))

import etl  # noqa: E402


class TestToNumber:
    """Quotes are not plain numbers."""

    def test_plain_value(self):
        assert etl._to_number(pd.Series(["58.010"]))[0] == pytest.approx(58.01)

    def test_closing_marker(self):
        # "(c)" marks a closing price: dropping these cost ~40% of every snapshot
        assert etl._to_number(pd.Series(["58.010(c)"]))[0] == pytest.approx(58.01)

    def test_suspended_marker(self):
        assert etl._to_number(pd.Series(["315.000(s)"]))[0] == pytest.approx(315.0)

    def test_space_as_thousands_separator(self):
        assert etl._to_number(pd.Series(["1 157.500"]))[0] == pytest.approx(1157.5)

    def test_comma_as_decimal_separator(self):
        # 42 sessions of 2024 are written the French way
        assert etl._to_number(pd.Series(["58,010(c)"]))[0] == pytest.approx(58.01)

    def test_comma_and_space_together(self):
        assert etl._to_number(pd.Series(["1 157,500"]))[0] == pytest.approx(1157.5)

    def test_unparseable_becomes_nan(self):
        assert pd.isna(etl._to_number(pd.Series(["n/a"]))[0])

    def test_whole_series(self):
        got = etl._to_number(pd.Series(["58.010(c)", "38,220", "1 157.500", ""]))
        assert list(got[:3]) == pytest.approx([58.01, 38.22, 1157.5])
        assert pd.isna(got[3])


class TestStripBoursoPrefix:
    """Boursorama prefixes symbols with a market code, Euronext does not."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1rPSAN", "SAN"),      # Paris
            ("1rAENX", "ENX"),      # Amsterdam
            ("FF11_ABC", "ABC"),    # Brussels
            ("1gXYZ", "XYZ"),       # Milan
            ("SAN", "SAN"),         # already bare
        ],
    )
    def test_known_prefixes(self, raw, expected):
        assert etl._strip_bourso_prefix(raw) == expected

    def test_keeps_distinct_instruments_apart(self):
        # MCNV is a separate line from MC and must not collapse into it
        assert etl._strip_bourso_prefix("1rPMCNV") == "MCNV"
        assert etl._strip_bourso_prefix("1rPMC") == "MC"


class TestMarketDetection:
    """A cross-listed stock names several markets; the first one is its own."""

    def test_primary_market_is_the_one_named_first(self):
        got = etl._detect_market_alias_from_euronext_market(
            "Euronext Paris, Amsterdam, Brussels"
        )
        assert got == "paris"

    def test_order_is_read_not_assumed(self):
        got = etl._detect_market_alias_from_euronext_market(
            "Euronext Amsterdam, Brussels, Paris"
        )
        assert got == "amsterdam"

    @pytest.mark.parametrize(
        "market,expected",
        [
            ("Euronext Paris", "paris"),
            ("Euronext Brussels", "bruxelle"),
            ("Euronext Milan", "milano"),
            ("", "paris"),
        ],
    )
    def test_single_market(self, market, expected):
        assert etl._detect_market_alias_from_euronext_market(market) == expected

    def test_bourso_market_from_symbol(self):
        assert etl._detect_market_alias_from_bourso_symbol("1rPSAN") == "paris"
        assert etl._detect_market_alias_from_bourso_symbol("1rAENX") == "amsterdam"


class TestOpenBoursoFile:
    """Snapshots are bz2 until 2023 and raw pickles after, name unchanged."""

    @staticmethod
    def _frame():
        return pd.DataFrame(
            {
                "last": ["58.010(c)", "38.220"],
                "volume": [0, 684970],
                "symbol": ["1rPABBV", "1rPAC"],
                "name": ["ABBVIE", "SRDACCOR"],
            },
            index=["1rPABBV", "1rPAC"],
        )

    def test_reads_bz2(self, tmp_path):
        p = tmp_path / "compA 2023-01-02 09:02:01.123456.bz2"
        with bz2.open(p, "wb") as f:
            pickle.dump(self._frame(), f)
        with etl._open_bourso_file(str(p)) as f:
            assert pickle.load(f).shape == (2, 4)

    def test_reads_raw_pickle(self, tmp_path):
        p = tmp_path / "compA 2024-01-15 09:02:01.771435"
        with open(p, "wb") as f:
            pickle.dump(self._frame(), f)
        with etl._open_bourso_file(str(p)) as f:
            assert pickle.load(f).shape == (2, 4)

    def test_format_is_sniffed_not_taken_from_the_name(self, tmp_path):
        # a raw pickle still carrying the .bz2 suffix must still be read
        p = tmp_path / "compA 2024-01-15 09:02:01.771435.bz2"
        with open(p, "wb") as f:
            pickle.dump(self._frame(), f)
        with etl._open_bourso_file(str(p)) as f:
            assert pickle.load(f).shape == (2, 4)


class TestParseBoursoFile:
    """End to end on a snapshot, both container formats."""

    @staticmethod
    def _write(path, compressed):
        df = pd.DataFrame(
            {
                "last": ["58.010(c)", "1 157,500", "0.000"],
                "volume": [0, 684970, 0],
                "symbol": ["1rPABBV", "1rPAC", "1rPXX"],
                "name": ["ABBVIE", "SRDACCOR", "XX"],
            },
            index=["1rPABBV", "1rPAC", "1rPXX"],
        )
        opener = bz2.open if compressed else open
        with opener(path, "wb") as f:
            pickle.dump(df, f)

    def test_parses_and_dates_from_the_filename(self, tmp_path):
        p = tmp_path / "compA 2024-01-15 09:02:01.771435"
        self._write(p, compressed=False)
        got = etl._parse_bourso_file(str(p))
        assert got is not None
        assert len(got) == 3
        assert got["last"].notna().all()
        assert got["datetime"].iloc[0] == pd.Timestamp("2024-01-15 09:02:01.771435")

    def test_parses_bz2_with_suffix(self, tmp_path):
        p = tmp_path / "compB 2023-07-31 17:52:01.425808.bz2"
        self._write(p, compressed=True)
        got = etl._parse_bourso_file(str(p))
        assert got is not None
        assert got["datetime"].iloc[0] == pd.Timestamp("2023-07-31 17:52:01.425808")

    def test_rejects_a_name_that_is_not_a_snapshot(self, tmp_path):
        p = tmp_path / "notes.txt"
        p.write_bytes(b"hello")
        assert etl._parse_bourso_file(str(p)) is None

    def test_unreadable_file_returns_none_rather_than_raising(self, tmp_path):
        p = tmp_path / "compA 2024-01-15 09:02:01.771435"
        p.write_bytes(b"not a pickle at all")
        assert etl._parse_bourso_file(str(p)) is None


class TestEuronextFilenameDate:
    def test_extracts_the_date(self):
        got = etl._extract_date_from_euronext_filename(
            "/data/euronext/Euronext_Equities_2022-10-20.xlsx"
        )
        assert got == pd.Timestamp("2022-10-20")

    def test_missing_date_is_not_a_time(self):
        assert pd.isna(etl._extract_date_from_euronext_filename("Euronext.csv"))
