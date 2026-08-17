import os

import dash
import dash_bootstrap_components as dbc
from dash import html, dcc, dash_table, Input, Output, callback
import plotly.graph_objects as go
import pandas as pd

import timescaledb_model as tsdb
from etl import store_files

from loguru import logger

# Date ranges imported on startup. Override with environment variables to load
# a different slice of the dataset without touching the code.
EURONEXT_START = os.getenv("EURONEXT_START", "2020-05-01")
EURONEXT_END = os.getenv("EURONEXT_END", "2022-09-16")
BOURSO_START = os.getenv("BOURSO_START", "2020-01-01")
BOURSO_END = os.getenv("BOURSO_END", "2022-01-01")

external_stylesheets = [dbc.themes.BOOTSTRAP]
app = dash.Dash(
    __name__,
    title="EuroBoard",
    suppress_callback_exceptions=True,
    external_stylesheets=external_stylesheets,
)
db = tsdb.TimescaleStockMarketModel("bourse", "bourse", "database", "password")

# =====================================================================
# Helper functions to query the database
# =====================================================================


def get_companies():
    """Get the tradeable listings, each labelled with the market it is on.

    A company cross-listed on two exchanges has one entry per market, with its
    own order book, so the market has to be part of how it is identified.

    Dormant listings are left out. Boursorama keeps quoting a line long after it
    stops trading, repeating its last close: 349 of the 1606 listings never trade
    at all over the whole archive, and others trade a handful of times in six
    years. Plotted, both become a straight line that reads as a trend and is
    none.

    A listing is kept when it traded on at least a tenth of the days it was
    quoted. Below that a daily series is mostly interpolation between rare
    prints, and the 20-day average behind the Bollinger tab means nothing. The
    cutoff is a judgement call: it drops 41 listings, and every one of them
    traded less than one day in ten. It is deliberately a ratio and not a count,
    so a short-lived instrument that traded throughout its life -- a warrant
    quoted for 18 days and traded on all 18 -- is kept.
    """
    return db.df_query(
        """
        SELECT c.id, c.name, c.symbol, COALESCE(m.name, 'Unknown') AS market
        FROM companies c
        LEFT JOIN markets m ON m.id = c.mid
        JOIN (
            SELECT cid FROM daystocks
            GROUP BY cid
            HAVING count(*) FILTER (WHERE volume > 0) >= 0.10 * count(*)
        ) traded ON traded.cid = c.id
        ORDER BY c.name, market
        """
    )


def get_daystocks(cids, start_date, end_date):
    """Get the daily aggregates of a set of listings over a date range.

    Days without a single trade are skipped: on those Boursorama reports the
    previous close again, so open, close, high and low are all equal and the
    volume is zero. Keeping them drew flat segments across months of inactivity.
    An actively traded stock loses 2 to 4 days out of 1400 this way.
    """
    if not cids:
        return pd.DataFrame()
    query = """
        SELECT d.date, d.cid, d.open, d.close, d.high, d.low, d.volume, d.mean, d.std,
               c.name,
               c.name || ' (' || c.symbol || ') - ' || COALESCE(m.name, 'Unknown') AS label
        FROM daystocks d
        JOIN companies c ON c.id = d.cid
        LEFT JOIN markets m ON m.id = c.mid
        WHERE d.cid = ANY(%(cids)s) AND d.date >= %(start)s AND d.date <= %(end)s
          AND d.volume > 0
        ORDER BY d.date
    """
    return db.df_query(
        query,
        params={"cids": [int(c) for c in cids], "start": start_date, "end": end_date},
        parse_dates=["date"],
    )


def get_intraday(cids, day):
    """Get the intraday path of a set of listings over a single session.

    The stocks table holds both sources: Boursorama snapshots taken every ten
    minutes through the session, and the Euronext end-of-day row, written at
    midnight. Only the former describes a session, so the midnight row is left
    out -- it would otherwise hang a point eight hours before the open, and its
    volume is the exchange's own daily total, on a different basis.

    Snapshots outside 06:00-18:00 are dropped too. From July 2024 the collector
    also fires once at 19:00, long after Euronext Paris closes at 17:30, and
    since the market is shut it returns the closing price and volume unchanged:
    37 sessions carry that repeat, which drew a flat line stretching the session
    by seventy minutes.
    """
    if not cids or not day:
        return pd.DataFrame()
    query = """
        SELECT s.date, s.cid, s.value, s.volume,
               c.name || ' (' || c.symbol || ') - ' || COALESCE(m.name, 'Unknown') AS label
        FROM stocks s
        JOIN companies c ON c.id = s.cid
        LEFT JOIN markets m ON m.id = c.mid
        WHERE s.cid = ANY(%(cids)s)
          AND s.date >= %(day)s AND s.date < %(day)s::date + 1
          AND (s.date AT TIME ZONE 'UTC')::time BETWEEN '06:00' AND '18:00'
        ORDER BY s.date
    """
    return db.df_query(
        query,
        params={"cids": [int(c) for c in cids], "day": day},
        parse_dates=["date"],
    )


def get_disabled_days(sessions, first, last):
    """Calendar days between first and last that hold no session."""
    if not sessions:
        return []
    have = set(sessions)
    span = pd.date_range(first, last, freq="D")
    return [str(d.date()) for d in span if str(d.date()) not in have]


def get_intraday_sessions(cids, start_date, end_date):
    """Sessions in the range for which intraday snapshots exist, most recent first.

    The archive is not continuous -- 2024 holds 143 sessions where a full year
    of trading would hold about 250 -- so the calendar disables every day that
    holds nothing rather than letting one be picked and come up empty.
    """
    if not cids:
        return []
    df = db.df_query(
        """
        SELECT DISTINCT (s.date AT TIME ZONE 'UTC')::date AS d FROM stocks s
        WHERE s.cid = ANY(%(cids)s)
          AND s.date >= %(start)s AND s.date <= %(end)s
          AND (s.date AT TIME ZONE 'UTC')::time BETWEEN '06:00' AND '18:00'
        ORDER BY d DESC
        """,
        params={"cids": [int(c) for c in cids], "start": start_date, "end": end_date},
    )
    return [] if df.empty else [str(d) for d in df["d"]]


# =====================================================================
# Layout
# =====================================================================

app.layout = dbc.Container(
    [
        dbc.Row(
            dbc.Col(html.H1("EuroBoard", className="text-center my-4")),
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
                    md=3,
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
                    html.Div(
                        [
                            html.Label("Session", className="fw-bold"),
                            html.Br(),
                            dcc.DatePickerSingle(
                                id="intraday-day",
                                display_format="YYYY-MM-DD",
                            ),
                        ],
                        id="intraday-controls",
                        # mounted visible on purpose: the picker measures its
                        # own width once, at mount, and a hidden element
                        # measures as nothing. The callback below hides it
                        # immediately for every tab but the intraday one.
                        style={},
                    ),
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
                dbc.Tab(label="Intraday", tab_id="tab-intraday"),
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
    options = [
        {
            "label": f"{row['name']} ({row['symbol']}) - {row['market']}",
            "value": row["id"],
        }
        for _, row in companies.iterrows()
    ]

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


@callback(
    Output("tab-content", "children"),
    Input("tabs", "active_tab"),
    Input("stock-selector", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
    Input("chart-type", "value"),
    Input("scale-type", "value"),
    Input("intraday-day", "date"),
)
def render_tab(
    active_tab, selected_stocks, start_date, end_date, chart_type, scale_type, intraday_day
):
    """Render the content of the active tab."""
    if not selected_stocks or not start_date or not end_date:
        return dbc.Alert("Select at least one stock and a date range.", color="info")

    cids = selected_stocks if isinstance(selected_stocks, list) else [selected_stocks]

    if active_tab == "tab-prices":
        return render_prices(cids, start_date, end_date, chart_type, scale_type)
    elif active_tab == "tab-bollinger":
        return render_bollinger(cids, start_date, end_date, scale_type)
    elif active_tab == "tab-data":
        return render_data_table(cids, start_date, end_date)
    elif active_tab == "tab-performance":
        return render_performance(cids, start_date, end_date)
    elif active_tab == "tab-intraday":
        return render_intraday(cids, intraday_day, scale_type)

    return html.Div()


# =====================================================================
# Tab renderers
# =====================================================================


def render_prices(cids, start_date, end_date, chart_type, scale_type):
    """Render the stock price chart (line or candlestick)."""
    df = get_daystocks(cids, start_date, end_date)
    if df.empty:
        return dbc.Alert("No data for this selection.", color="warning")

    fig = go.Figure()

    for name, group in df.groupby("label"):
        group = group.sort_values("date")
        if chart_type == "candlestick":
            fig.add_trace(
                go.Candlestick(
                    x=group["date"],
                    open=group["open"],
                    high=group["high"],
                    low=group["low"],
                    close=group["close"],
                    name=name,
                )
            )
        else:
            fig.add_trace(
                go.Scatter(
                    x=group["date"],
                    y=group["close"],
                    mode="lines",
                    name=name,
                )
            )

    fig.update_layout(
        title="Stock prices",
        xaxis_title="Date",
        yaxis_title="Price",
        yaxis_type=scale_type,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        xaxis_rangeslider_visible=False,
        height=600,
    )

    return dcc.Graph(figure=fig)


def render_bollinger(cids, start_date, end_date, scale_type):
    """Render Bollinger Bands for the first selected stock."""
    df = get_daystocks(cids, start_date, end_date)
    if df.empty:
        return dbc.Alert("No data for this selection.", color="warning")

    # Build a selector for which stock to show Bollinger bands
    stock_names = df[["cid", "label"]].drop_duplicates()
    tabs_content = []

    for _, stock_row in stock_names.iterrows():
        cid = stock_row["cid"]
        name = stock_row["label"]
        group = df[df["cid"] == cid].sort_values("date").copy()

        # Compute Bollinger Bands (20-day SMA, 2 std dev)
        window = 20
        group["sma"] = group["close"].rolling(window=window).mean()
        group["std_val"] = group["close"].rolling(window=window).std()
        group["upper"] = group["sma"] + 2 * group["std_val"]
        group["lower"] = group["sma"] - 2 * group["std_val"]

        fig = go.Figure()

        # Upper band
        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=group["upper"],
                mode="lines",
                line=dict(width=1, color="rgba(100,100,100,0.3)"),
                name="Upper band",
            )
        )
        # Lower band (fill between)
        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=group["lower"],
                mode="lines",
                line=dict(width=1, color="rgba(100,100,100,0.3)"),
                fill="tonexty",
                fillcolor="rgba(100,149,237,0.15)",
                name="Lower band",
            )
        )
        # SMA
        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=group["sma"],
                mode="lines",
                line=dict(width=1.5, color="orange", dash="dash"),
                name=f"SMA {window}d",
            )
        )
        # Close price
        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=group["close"],
                mode="lines",
                line=dict(width=2, color="blue"),
                name="Price",
            )
        )

        fig.update_layout(
            title=f"Bollinger bands - {name}",
            xaxis_title="Date",
            yaxis_title="Price",
            yaxis_type=scale_type,
            hovermode="x unified",
            height=500,
        )

        tabs_content.append(dbc.Tab(label=name, children=[dcc.Graph(figure=fig)]))

    if len(tabs_content) == 1:
        return tabs_content[0].children

    return dbc.Tabs(tabs_content)


def render_data_table(cids, start_date, end_date):
    """Render raw data table with daily stats: min, max, open, close, mean, std."""
    df = get_daystocks(cids, start_date, end_date)
    if df.empty:
        return dbc.Alert("No data for this selection.", color="warning")

    # Build the table: one row per day per stock
    df = df.sort_values(["date", "name"])
    table_df = pd.DataFrame(
        {
            "Date": df["date"].dt.strftime("%Y-%m-%d"),
            "Stock": df["label"],
            "Open": df["open"].round(4),
            "Close": df["close"].round(4),
            "Min": df["low"].round(4),
            "Max": df["high"].round(4),
            "Mean": df["mean"].round(4),
            "Std dev": df["std"].round(4),
            "Volume": df["volume"].astype(int),
        }
    )

    return dash_table.DataTable(
        data=table_df.to_dict("records"),
        columns=[{"name": c, "id": c} for c in table_df.columns],
        page_size=25,
        sort_action="native",
        filter_action="native",
        style_table={"overflowX": "auto"},
        style_cell={"textAlign": "center", "padding": "8px", "fontSize": "14px"},
        style_header={"backgroundColor": "#f8f9fa", "fontWeight": "bold"},
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "#f2f2f2"}
        ],
        export_format="csv",
    )


def render_performance(cids, start_date, end_date):
    """Custom feature: normalized performance comparison (% change from start)."""
    df = get_daystocks(cids, start_date, end_date)
    if df.empty:
        return dbc.Alert("No data for this selection.", color="warning")

    fig = go.Figure()

    for name, group in df.groupby("label"):
        group = group.sort_values("date")
        first_close = group["close"].iloc[0]
        if first_close == 0 or pd.isna(first_close):
            continue
        perf = ((group["close"] / first_close) - 1) * 100

        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=perf,
                mode="lines",
                name=name,
            )
        )

    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)

    fig.update_layout(
        title="Performance comparison (% change from start of period)",
        xaxis_title="Date",
        yaxis_title="Change (%)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=600,
    )

    # Also show a volume subplot
    vol_fig = go.Figure()
    for name, group in df.groupby("label"):
        group = group.sort_values("date")
        vol_fig.add_trace(
            go.Bar(
                x=group["date"],
                y=group["volume"],
                name=name,
                opacity=0.7,
            )
        )

    vol_fig.update_layout(
        title="Traded volume",
        xaxis_title="Date",
        yaxis_title="Volume",
        barmode="group",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=400,
    )

    return html.Div([dcc.Graph(figure=fig), dcc.Graph(figure=vol_fig)])


def render_intraday(cids, day, scale_type):
    """Draw the price path of one session.

    Rendered straight from render_tab rather than filled by a second callback:
    a dcc.Graph and a dcc.Dropdown sitting together inside dynamically created
    tab content leave the Dash 4 client unable to re-render, and the tab empties
    on the next click anywhere on the page. The session picker therefore lives
    in the control bar, where inputs and graphs already coexist.
    """
    if not day:
        return dbc.Alert(
            "No intraday snapshot for this selection. Boursorama covers 2019 to "
            "2024; outside that only the Euronext daily close is stored.",
            color="warning",
        )

    df = get_intraday(cids, day)
    if df.empty:
        return dbc.Alert(f"No intraday snapshot on {day}.", color="warning")

    # Intraday moves are fractions of a percent. Several stocks quoted at 85 and
    # at 650 share no readable axis, so compare them on their move since the
    # open; a single one is more useful at its actual price.
    compare = df["label"].nunique() > 1

    fig = go.Figure()
    for label, group in df.groupby("label"):
        group = group.sort_values("date")
        if compare:
            first = group["value"].iloc[0]
            y = ((group["value"] / first) - 1) * 100 if first else group["value"]
            hover = "%{x|%H:%M} — %{y:+.2f}%<extra></extra>"
        else:
            y = group["value"]
            hover = "%{x|%H:%M} — %{y:.2f}<extra></extra>"
        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=y,
                mode="lines+markers",
                marker=dict(size=4),
                name=label,
                hovertemplate=hover,
            )
        )

    if compare:
        fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)

    fig.update_layout(
        title=(
            f"Intraday change since the open — {day}"
            if compare
            else f"Intraday prices — {day}"
        ),
        xaxis_title="Time",
        yaxis_title="Change (%)" if compare else "Price",
        yaxis_type="linear" if compare else (scale_type or "linear"),
        xaxis_tickformat="%H:%M",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=600,
    )

    spans = df.groupby("label")["date"].agg(["min", "max", "count"])
    note = "  ·  ".join(
        f"{label}: {int(row['count'])} snapshots, "
        f"{row['min'].strftime('%H:%M')} to {row['max'].strftime('%H:%M')}"
        for label, row in spans.iterrows()
    )
    return html.Div([dcc.Graph(figure=fig), html.Small(note, className="text-muted")])


@callback(
    Output("intraday-controls", "style"),
    Input("tabs", "active_tab"),
)
def toggle_intraday_controls(active_tab):
    """The session picker only means anything on the intraday tab."""
    return {} if active_tab == "tab-intraday" else {"display": "none"}


@callback(
    Output("intraday-day", "date"),
    Output("intraday-day", "min_date_allowed"),
    Output("intraday-day", "max_date_allowed"),
    Output("intraday-day", "disabled_days"),
    Input("stock-selector", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
)
def set_intraday_sessions(selected_stocks, start_date, end_date):
    """Restrict the calendar to the sessions that actually hold snapshots."""
    if not selected_stocks or not start_date or not end_date:
        return None, None, None, []

    cids = selected_stocks if isinstance(selected_stocks, list) else [selected_stocks]
    sessions = get_intraday_sessions(cids, start_date, end_date)
    if not sessions:
        return None, None, None, []

    first, last = sessions[-1], sessions[0]
    return last, first, last, get_disabled_days(sessions, first, last)


# =====================================================================
# Main
# =====================================================================

if __name__ == "__main__":
    logger.info("Importing data into the database")
    store_files(EURONEXT_START, EURONEXT_END, "euronext", db)
    store_files(BOURSO_START, BOURSO_END, "bourso", db)
    logger.info("Import done")
    logger.info("Starting dashboard server")
    app.run(host="0.0.0.0", port=8050, debug=os.getenv("DASH_DEBUG", "0") == "1")
