# ledwetter

Collect measurements from public weather and water stations around Zurich, store them in a local
database and explore them in the browser. All timestamps are **measurement times reported by the
sources, in UTC**, never retrieval times.

## Features

- **`update`** builds a local SQLite database with all measurements available online
  (first run: the entire history) and updates it incrementally afterwards.
- **`serve`** starts a web interface: select stations, parameters, time range and aggregation
  (raw data, hourly or daily values) and plot them.
- **`export`** writes any period from all sources directly to a homogeneous CSV file (no database needed).

## Installation

```bash
git clone <repo-url> ledwetter && cd ledwetter
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Python 3.10 or newer. Dependencies: pandas, requests, PyYAML; pytest for the tests.

### With conda

Development environment (dependencies from conda-forge, the package itself as an editable install):

```bash
conda env create -f environment.yml
conda activate ledwetter
pip install -e . --no-deps --no-build-isolation
```

`--no-deps` keeps pip from replacing the conda packages. After changes to `environment.yml`, run
`conda env update -f environment.yml --prune`.

As a proper conda package (recipe in `conda/meta.yaml`, requires `conda-build`):

```bash
conda build conda/ -c conda-forge      # also runs the test suite
conda install -c local ledwetter
```

## Usage

```bash
python -m ledwetter update                 # build or update the database (data/ledwetter.db)
python -m ledwetter serve                  # web interface at http://127.0.0.1:8000
python -m ledwetter export --start 2026-10-01 --end 2026-10-02 --out oct.csv
```

More options: `-v` for diagnostic logging, `--config` for a different station file,
`--sources meteoschweiz,bafu` to select sources, `update --since 2026-01-01` to reload from a given date.
Running `update` regularly (e.g. hourly via `cron` or `launchd`) keeps the database current; for METAR
this is required because the source keeps only a few days of archive.

Stations are configured in `config/stations.yaml`. Newly added stations receive their entire history
on the next `update`.

The command-line help, log messages and the web interface are in German.

## Sources

| Source | Stations (example) | Parameters | Time reference | History | Status |
|---|---|---|---|---|---|
| MeteoSwiss SwissMetNet (OGD, data.geo.admin.ch) | SMA, KLO, REH, LAE, UEB (tower) | air, wind | 10-min mean, interval end | from 2000 (10-min files) | format verified against a real file excerpt; tower parameter names derived |
| Zurich water police (OGD CSV + Tecdottir API) | Tiefenbrunnen, Mythenquai | air, water, wind | 10 min (water: instantaneous) | from 2007 | OGD columns verified; Tecdottir JSON not checked live |
| FOEN/BAFU hydrology (data.bafu.admin.ch, GraphQL) | 2099, 2243, 2176 | water temperature | 10-min mean, interval start | per `coverageFrom` | API documentation verified; check station selection |
| City of Zurich UGZ (OGD) | Stampfenbach-, Schimmel-, Rosengartenstrasse, Heubeeribüel | air, wind | hourly mean, reference unknown | from 1992 | format verified; interval reference open |
| METAR (aviationweather.gov) | LSZH | air, wind | observation time | a few days | verified; temperature in whole degrees |

Deliberately **not** included: scraping tecson-data.ch (prohibited by its terms), NABEL Zürich-Kaserne
(no open meteorological API), the AWEL LoRa urban-climate network (monthly files only, no real-time operation).

Terms of use: MeteoSwiss data require the attribution "Source: MeteoSwiss".

## Database

**Format: SQLite.** A single file, included in the Python standard library, no server, transactional
and with upserts, so repeated `update` runs never store anything twice. It is fast enough for tens of
millions of values and per-series time aggregation.

Alternatives: DuckDB or Parquet would be faster for large analyses but add a dependency and suit
frequent small updates less well. CSV is unsuitable for a growing database (no upserts, no indexes).

Schema (compact, normalised):

```
stations(id, source, station_id, name)
series(id, station, parameter, unit, medium, time_ref, interval_min)   -- one time series
measurements(series, t, value)   PRIMARY KEY (series, t), WITHOUT ROWID; t = Unix seconds UTC
```

Expected size with the default configuration: roughly 25–30 million values, about 0.5–1 GB.
The initial build takes a while depending on the connection (large MeteoSwiss decade files,
several hundred BAFU queries); `request_delay_seconds` in the configuration spares the servers.

## Aggregation

- Hourly and daily values are computed from the stored values: mean, minimum, maximum, count.
- Bins are in **UTC** and labelled with their **start** (hourly value 14:00 = 14:00–15:00 UTC).
- Values whose time reference is the *interval end* count towards the interval in which they were
  measured (the 10-minute value at 15:00 belongs to the hour 14:00–15:00).
- Bins with insufficient coverage (default 75 % of the expected values) are left empty; for
  instantaneous values the expected count is unknown, so no filter is applied.
- For climatological analyses MeteoSwiss recommends its officially aggregated hourly and daily values
  over self-computed ones; this database is meant for exploration.

## CSV export

Long format, one row per measurement:

```
time_utc, time_ref, interval_min, source, station_id, station_name, medium, parameter, value, unit
```

`time_ref` is `interval_end`, `interval_start`, `instant` or `unknown`; units are always °C and km/h.

## Tests

```bash
pytest                      # all tests
pytest tests/unit           # unit tests
pytest -m regression        # regression tests against tests/regression/expected
UPDATE_GOLDEN=1 pytest -m regression   # rewrite the references after an intentional change
```

- **Unit tests** cover time and unit conversion (including DST transitions), file selection, pagination,
  BAFU query windows, per-station error isolation, storage, aggregation and the web API.
- **Regression tests** replay recorded responses of all sources (`tests/fixtures`) and compare the CSV
  export and the aggregates with stored references. A real MeteoSwiss file excerpt additionally guards
  the file format.
- Apart from that excerpt the fixtures are synthetic but follow the documented formats;
  `tests/fixtures/make_fixtures.py` regenerates them. The tests run without network access.
- GitHub Actions runs the tests on every push (Python 3.10 and 3.12).

## Project structure

```
src/ledwetter/
  model.py        canonical data format, time period, time and unit conversion
  http.py         HTTP client with retries
  sources/        one class per source (base class Source)
  storage.py      SQLite storage and aggregation
  ingest.py       building and updating the database
  export.py       CSV export
  server.py       local web server with JSON API
  web/index.html  user interface (Plotly)
config/stations.yaml
environment.yml   conda development environment
conda/meta.yaml   conda-build recipe
tests/unit, tests/regression, tests/fixtures
```

## Open points

Only a real run (`python -m ledwetter update -v`) will settle: the tower parameter names for UEB,
the exact JSON of the Tecdottir API, the interval reference of the UGZ data and which BAFU stations
actually measure water temperature. In these cases the log names the columns, locations or missing
stations that were actually found.
