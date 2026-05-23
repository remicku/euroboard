import os

import dash
import dash_bootstrap_components as dbc
from dash import html, dcc, dash_table, Input, Output, callback
import plotly.graph_objects as go
import pandas as pd

import timescaledb_model as tsdb
from etl import store_files

from loguru import logger

external_stylesheets = [dbc.themes.BOOTSTRAP]
app = dash.Dash(
    __name__,
    title="Bourse Dashboard",
    suppress_callback_exceptions=True,
    external_stylesheets=external_stylesheets,
)
db = tsdb.TimescaleStockMarketModel("bourse", "bourse", "database", "password")

# =====================================================================
# Helper functions to query the database
# =====================================================================


def get_companies():
    """Get list of companies from the database."""
    df = db.df_query("SELECT id, name, symbol FROM companies ORDER BY name")
    return df


def get_daystocks(cids, start_date, end_date):
    """Get daily stock data for given company ids and date range."""
    if not cids:
        return pd.DataFrame()
    cid_list = ",".join(str(c) for c in cids)
    query = """
        SELECT d.date, d.cid, d.open, d.close, d.high, d.low, d.volume, d.mean, d.std, c.name
        FROM daystocks d
        JOIN companies c ON c.id = d.cid
        WHERE d.cid IN (%s) AND d.date >= '%s' AND d.date <= '%s'
        ORDER BY d.date
    """ % (cid_list, start_date, end_date)
    df = db.df_query(query, parse_dates=["date"])
    return df


def get_stocks_intraday(cids, start_date, end_date):
    """Get intraday stock data."""
    if not cids:
        return pd.DataFrame()
    cid_list = ",".join(str(c) for c in cids)
    query = """
        SELECT s.date, s.cid, s.value, s.volume, c.name
        FROM stocks s
        JOIN companies c ON c.id = s.cid
        WHERE s.cid IN (%s) AND s.date >= '%s' AND s.date <= '%s'
        ORDER BY s.date
    """ % (cid_list, start_date, end_date)
    df = db.df_query(query, parse_dates=["date"])
    return df


# =====================================================================
# Layout
# =====================================================================

app.layout = dbc.Container(
    [
        dbc.Row(
            dbc.Col(html.H1("Bourse Dashboard", className="text-center my-4")),
        ),
        # Controls row
        dbc.Row(
            [
                dbc.Col(
                    [
                        html.Label("Stocks", className="fw-bold"),
                        dcc.Dropdown(
                            id="stock-selector",
                            multi=True,
                            placeholder="Select one or more stocks...",
                        ),
                    ],
                    md=4,
                ),
                dbc.Col(
                    [
                        html.Label("Date range", className="fw-bold"),
                        dcc.DatePickerRange(
                            id="date-range",
                            display_format="YYYY-MM-DD",
                            start_date_placeholder_text="Start",
                            end_date_placeholder_text="End",
                        ),
                    ],
                    md=3,
                ),
                dbc.Col(
                    [
                        html.Label("Chart type", className="fw-bold"),
                        dbc.RadioItems(
                            id="chart-type",
                            options=[
                                {"label": "Line", "value": "line"},
                                {"label": "Candlestick", "value": "candlestick"},
                            ],
                            value="line",
                            inline=True,
                        ),
                    ],
                    md=2,
                ),
                dbc.Col(
                    [
                        html.Label("Scale", className="fw-bold"),
                        dbc.RadioItems(
                            id="scale-type",
                            options=[
                                {"label": "Linear", "value": "linear"},
                                {"label": "Logarithmic", "value": "log"},
                            ],
                            value="log",
                            inline=True,
                        ),
                    ],
                    md=2,
                ),
            ],
            className="mb-4",
        ),
        # Tabs
        dbc.Tabs(
            [
                dbc.Tab(label="Prices", tab_id="tab-prices"),
                dbc.Tab(label="Bollinger", tab_id="tab-bollinger"),
                dbc.Tab(label="Raw data", tab_id="tab-data"),
                dbc.Tab(label="Performance comparison", tab_id="tab-performance"),
            ],
            id="tabs",
            active_tab="tab-prices",
            className="mb-3",
        ),
        html.Div(id="tab-content"),
    ],
    fluid=True,
)


# =====================================================================
# Callbacks
# =====================================================================


@callback(
    Output("stock-selector", "options"),
    Output("date-range", "min_date_allowed"),
    Output("date-range", "max_date_allowed"),
    Output("date-range", "start_date"),
    Output("date-range", "end_date"),
    Input("stock-selector", "id"),  # fires once on load
)
def init_controls(_):
    """Populate the stock selector and date range on page load."""
    companies = get_companies()
    logger.info(f"Found {len(companies)} companies in database")
    options = [{"label": row["name"], "value": row["id"]} for _, row in companies.iterrows()]

    # Try daystocks first, fallback to stocks
    dates = db.df_query("SELECT MIN(date) as min_d, MAX(date) as max_d FROM daystocks")
    if dates.empty or pd.isna(dates["min_d"].iloc[0]):
        dates = db.df_query("SELECT MIN(date) as min_d, MAX(date) as max_d FROM stocks")

    if dates.empty or pd.isna(dates["min_d"].iloc[0]):
        logger.warning("No date data found in daystocks or stocks")
        return options, None, None, None, None

    min_d = pd.to_datetime(dates["min_d"].iloc[0]).date()
    max_d = pd.to_datetime(dates["max_d"].iloc[0]).date()
    logger.info(f"Date range: {min_d} to {max_d}")
    return options, str(min_d), str(max_d), str(min_d), str(max_d)


# =====================================================================
# Main
# =====================================================================

if __name__ == "__main__":
    logger.info("Importing data into the database")
    store_files("2020-05-01", "2022-09-16", "euronext", db)
    store_files("2020-01-01", "2022-01-01", "bourso", db)
    logger.info("Import done")
    logger.info("Starting dashboard server")
    app.run(host="0.0.0.0", port=8050, debug=os.getenv("DASH_DEBUG", "0") == "1")
