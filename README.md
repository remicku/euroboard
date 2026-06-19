# EuroBoard

Interactive dashboard for European stock market data, backed by a TimescaleDB
hypertable store and an ETL pipeline that ingests raw exchange data files.

## Features

- **ETL pipeline**: ingests Boursorama intraday snapshots (bz2 pickles, one
  directory per year) and Euronext end-of-day exports (CSV/XLSX), resolves
  companies and markets, and writes to TimescaleDB in batches. Files already
  processed are recorded and skipped, so imports are resumable.
- **Price charts**: line or candlestick, on a linear or logarithmic scale.
- **Bollinger bands**: 20-period simple moving average with a ±2σ envelope.
- **Raw data table**: daily open, close, min, max, mean, standard deviation and
  volume; sortable, filterable, exportable to CSV.
- **Performance comparison**: several stocks normalised to percent change from
  the start of the selected period, with a traded-volume chart alongside.

## Stack

Python 3.14 · Dash 4 · Plotly · pandas · SQLAlchemy · TimescaleDB (PostgreSQL 17) · Docker Compose · uv

## Running it

```bash
docker compose up --build
```

The dashboard is served at <http://localhost:8050>.

Data files are read from `./data`, mounted into the container at `/mnt/data`:

```
data/
├── bourso/            # Boursorama intraday snapshots
│   └── 2020/          #   one directory per year, *.bz2 pickles
└── euronext/          # Euronext end-of-day exports, *.csv and *.xlsx
```

On startup the application loads the configured date ranges into the database
before serving. The first run takes a while; later runs skip files already
marked as imported.

### Local development

`compose.override.yml` is picked up automatically by Docker Compose. It mounts
`./bourse` into the container and enables Dash debug mode, so the application
reloads on save without rebuilding the image.

## Layout

```
bourse/
├── app.py                 # Dash application: layout, callbacks, tab renderers
├── etl.py                 # extract, transform and load pipeline
└── timescaledb_model.py   # schema definition and database access layer
```
