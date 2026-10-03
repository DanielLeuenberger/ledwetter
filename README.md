# ledwetter

Collect measurements from public weather and water stations in Switzerland (developed for the canton of Zurich), store them in a local
database and explore them in the browser. All timestamps are **measurement times reported by the
sources, in UTC**, never retrieval times.

## Features

- **`update`** builds a local SQLite database with all measurements available online
  (first run: the entire history) and updates it incrementally afterwards.
- **`serve`** starts a web interface: select stations, parameters, time range and aggregation
  (raw data, hourly or daily values) and plot them.
- **`export`** writes any period from all sources directly to a homogeneous CSV file (no database needed).
- **`stations`** lists all stations of the sources within one or more cantons (default: Zurich) and writes
  them as a configuration file. National sources work for every canton; regional networks only for theirs.

All parameters a source delivers are stored, in their original unit, together with a parameter
catalogue (description, unit, group, aggregation method).

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
python -m ledwetter stations --canton ZH            # all stations in the canton -> config/stations_zh.yaml
python -m ledwetter stations --canton ZH,AG,SH      # several cantons -> config/stations_zh_ag_sh.yaml
python -m ledwetter --config config/stations_zh.yaml update   # build the database for them
```

More options: `-v` for diagnostic logging, `--config` for a different station file,
`--sources meteoschweiz,bafu` to select sources, `update --since 2026-01-01` to reload from a given date.
Running `update` regularly (e.g. hourly via `cron` or `launchd`) keeps the database current; for METAR
this is required because the source keeps only a few days of archive.

Stations are configured in `config/stations.yaml`. Newly added stations receive their entire history
on the next `update`.

The command-line help, log messages and the web interface are in German.

## Sources

| Source | Parameters (all are extracted) | Time reference | History | Status |
|---|---|---|---|---|
| MeteoSwiss SwissMetNet (OGD, data.geo.admin.ch): weather, tower and precipitation networks | every column of the 10-minute files, e.g. temperature 2 m / 5 cm / surface, dew point, humidity, vapour pressure, QFE/QFF/QNH, 850/700 hPa geopotential, precipitation, sunshine, global/diffuse/long-wave/reflected radiation, wind direction/speed/gusts, snow depth, soil temperature, foehn index; descriptions and units from the official `*_meta_parameters.csv` | codes `…s0` instantaneous, `…z0/z1/z3` 10-min interval end | from 2000 (10-min files) | file format and parameter list verified |
| Zurich water police (OGD CSV + Tecdottir API) | air and water temperature, wind speed/gust/force/direction, windchill, QFE, precipitation, dew point, global radiation, humidity, lake level (radiation, precipitation and level at Mythenquai only) | `*_10min` interval end, others unknown | from 2007 | verified with real responses; the API lags about 2–3 hours |
| FOEN/BAFU hydrology (data.bafu.admin.ch, GraphQL) | all parameters per station, usually W (water level), Q (discharge), WT (water temperature) | 10-min mean, interval start | per `coverageFrom` | verified with real responses; 10-min means lag about 1.5 hours |
| City of Zurich UGZ (OGD) | T, Hr, p, RainDur, WD, WVs, WVv at Stampfenbach-, Schimmel- and Rosengartenstrasse (StrGlo at Stampfenbachstrasse); T, Hr, p at Heubeeribüel | hourly, timestamp = start of the hour | from 1992 | verified with real responses; time reference determined against MeteoSwiss radiation |
| METAR (aviationweather.gov) | temperature, dew point, wind direction/speed/gust, visibility (km; 10 = 10 km or more), QNH, QFF | observation time | a few days | verified with real responses; temperature in whole degrees; no direction for calm or variable wind |

MeteoSwiss publishes the m/s variants of its wind parameters (`fkl010z*`, `fk1towz0`, `fkltowz1`) in addition
to the km/h ones; the m/s duplicates are skipped when the km/h column exists. Uetliberg (UEB) appears in the
weather network (radiation and sunshine only) and in the tower network; both files carry identical radiation
and sunshine values, which are stored once.

Deliberately **not** included: scraping tecson-data.ch (prohibited by its terms), NABEL Zürich-Kaserne
(no open meteorological API).

### Further sources in canton Zurich (not yet integrated)

- **AWEL hydrometric network** (canton): over 60 stations for water level, discharge, water temperature
  and precipitation. Station metadata are open data (CSV/WFS); measurement series are published as
  yearbooks or on request, without a documented API.
- **AWEL LoRa urban-climate network** (canton): about 40–50 low-cost sensors for air temperature and
  humidity, monthly CSV files on opendata.swiss. Suited for backfills; the file format still needs checking.
- **Winterthur urban-climate measurements** (canton statistics office): yearly CSV files in UTC with a
  station table.
- **City of Zurich urban-climate network (meteoblue)**: station locations and cleaned temperature series
  as open data.

Terms of use: MeteoSwiss data require the attribution "Source: MeteoSwiss".

## Cantons and station discovery

`python -m ledwetter stations --canton ZH` builds a configuration with every station of the integrated sources
in a canton; several cantons are given comma-separated (`--canton ZH,AG`). One database can hold stations of
any number of cantons; the web interface then offers a canton filter.

| Source | Coverage | How stations are found |
|---|---|---|
| MeteoSwiss (weather, tower, precipitation networks) | Switzerland | official station lists with a canton column |
| FOEN/BAFU hydrology | Switzerland | station list of the data platform; canton from the coordinates via the federal geo service (api3.geo.admin.ch, swisstopo cantonal boundaries) |
| METAR | Switzerland | station info service of aviationweather.gov (METAR sites in Switzerland); canton from the coordinates |
| Zurich water police | ZH | fixed list |
| City of Zurich UGZ | ZH | locations in the current yearly file |

Each station entry carries `canton` and, where known, `lat`, `lon` and `height_masl`; these are stored as
station metadata. National station lists are downloaded once even for several cantons, and point lookups
are skipped for stations outside the canton's bounding box.

### Adding a regional network

A network of another canton or city is a new subclass of `sources.base.Source`:

```python
class BernCityNetwork(Source):
    name = "bern_city"          # section name in the configuration
    key = "id"                  # station key in the configuration
    regions = ("BE",)           # cantons it covers (None = all of Switzerland)
    CATALOG = {"temp": ("Lufttemperatur", "°C", "Temperatur", "mean", "air_temperature")}

    @classmethod
    def discover(cls, ctx, canton):          # station entries for 'ledwetter stations'
        return [{"id": "b1", "name": "Bärenplatz"}]

    def fetch_station(self, st, period):     # yield canonical frames via self.frame(...)
        ...
```

Register it in `sources.REGISTRY`; station discovery, the database, the CSV export and the web interface
pick it up without further changes. Setting `quantity` in the catalogue lets its parameters appear together
with the equivalent parameters of other sources.

## Diagnostics

To check that every source is imported correctly on your machine:

```bash
python -m ledwetter diagnose                  # writes diagnose/ and diagnose.zip
python -m ledwetter diagnose --since "2026-10-03 00:00" --canton ZH,AG
```

This discovers the stations of the canton(s), runs a short update (default: since midnight local time) into a
separate database `diagnose/diagnose.db` and writes:

- `index.jsonl` plus one file per response of every source (CSV files above 2 MB are cut at a line boundary),
- `summary.csv` with every stored series: count, time range, value range, unit, aggregation method,
- `diagnose.log`, `stations_zh.yaml` and `run_info.json` (command, versions, configuration).

The general options `--record DIR` and `--replay DIR` work with every command. While recording, the clock is
frozen at the start time so that time-dependent requests can be replayed; requests that still differ only in
their timestamps are matched without them. `--replay` answers all requests
from a recording without network access and sets the clock to the time of the recording, so a run can be
reproduced exactly elsewhere, for example `python -m ledwetter --replay diagnose diagnose --out check`.
Recordings contain only public data and no credentials.

## Database

**Format: SQLite.** A single file, included in the Python standard library, no server, transactional
and with upserts, so repeated `update` runs never store anything twice. It is fast enough for tens of
millions of values and per-series time aggregation.

Alternatives: DuckDB or Parquet would be faster for large analyses but add a dependency and suit
frequent small updates less well. CSV is unsuitable for a growing database (no upserts, no indexes).

Schema (compact, normalised, version 3):

```
stations(id, source, station_id, name, canton, lat, lon, height_masl)
parameters(source, code, description, unit, grp, agg, quantity)   -- parameter catalogue
series(id, station, parameter, unit, time_ref, interval_min)      -- one time series
measurements(series, t, value)   PRIMARY KEY (series, t), WITHOUT ROWID; t = Unix seconds UTC
```

Values are stored in the unit delivered by the source. `quantity` links equivalent parameters of different
sources (e.g. `tre200s0`, `air_temperature`, `T` and `temp` are all `air_temperature`). A version 2 database
is upgraded automatically (station metadata columns are added); a version 1 database is rejected, delete it
and run `update` again.

Expected size with the default configuration and all parameters: a few hundred million values, several GB.
Restrict parameters per source with `parameters.include` / `parameters.exclude` if needed.
The initial build takes a while depending on the connection (large MeteoSwiss decade files,
several hundred BAFU queries); `request_delay_seconds` in the configuration spares the servers.

## Aggregation

- Hourly and daily values are computed with each parameter's method from the catalogue:
  **mean** (with minimum/maximum band), **sum** (precipitation, sunshine and rain duration),
  **max** (gusts, foehn index), **min**, or **dir** (vector mean for wind directions, so 350° and 10° give 0°).
- The method comes from the official description (MeteoSwiss) or the source catalogue; without a description
  it is derived from the code (e.g. `rre…` sum, `dkl…` direction, `…z1` maximum).
- Bins are in **UTC** and labelled with their **start** (hourly value 14:00 = 14:00–15:00 UTC).
- Values whose time reference is the *interval end* count towards the interval in which they were
  measured (the 10-minute value at 15:00 belongs to the hour 14:00–15:00).
- Bins with insufficient coverage (default 75 % of the expected values) are left empty; for
  instantaneous values the expected count is unknown, so no filter is applied.
- The web interface shows wind speeds in km/h for comparison across sources; all other values are shown
  in their original unit.
- For climatological analyses MeteoSwiss recommends its officially aggregated hourly and daily values
  over self-computed ones; this database is meant for exploration.

## CSV export

Long format, one row per measurement:

```
time_utc, time_ref, interval_min, source, station_id, station_name, parameter, value, unit, quantity, description
```

`time_ref` is `interval_end`, `interval_start`, `instant` or `unknown`; `unit` is the original unit of the source.

## Tests

```bash
pytest                      # all tests
pytest tests/unit           # unit tests
pytest -m regression        # regression tests against tests/regression/expected
UPDATE_GOLDEN=1 pytest -m regression   # rewrite the references after an intentional change
```

- **Unit tests** cover time conversion (including DST transitions), file selection, pagination, BAFU query
  windows, parameter extraction and catalogues, per-station error isolation, storage, all aggregation methods,
  station discovery and the web API.
- **Regression tests** replay recorded responses of all sources (`tests/fixtures`) and compare the CSV
  export and the aggregates with stored references. A real MeteoSwiss file excerpt additionally guards
  the file format.
- `tests/regression/test_real_data.py` runs the parsers on real responses of all sources, recorded in a
  diagnostic run (`tests/fixtures/real`). The remaining fixtures are synthetic but follow the real formats;
  `tests/fixtures/make_fixtures.py` regenerates them. The tests run without network access.
- GitHub Actions runs the tests on every push (Python 3.10 and 3.12).

## Project structure

```
src/ledwetter/
  model.py        canonical data format, time period, time and unit conversion
  http.py         HTTP client with retries, recording and replay
  diagnose.py     one-command diagnostics run
  sources/        one class per source (base class Source)
  storage.py      SQLite storage, parameter catalogue and aggregation
  geo.py          cantons and canton lookups via the federal geo service
  discover.py     station discovery for one or more cantons
  ingest.py       building and updating the database
  export.py       CSV export
  server.py       local web server with JSON API
  web/index.html  user interface (Plotly)
config/stations.yaml
environment.yml   conda development environment
conda/meta.yaml   conda-build recipe
tests/unit, tests/regression, tests/fixtures
```

## Verification status

A diagnostic run on 3 October 2026 (canton ZH: 37 stations, 241 series, about 17,000 values) confirmed the
formats of all sources and the geo and METAR station services. Cross-checks: the lake level of the water
police (405.26–405.27 m) matches the FOEN station Zürichsee (405.25 m); hourly global radiation of UGZ
Stampfenbachstrasse matches MeteoSwiss Fluntern within 9 W/m² RMS when the UGZ timestamp is taken as the start
of the hour. Still open: whether the water police's non-10-minute parameters are instantaneous values or means.
