"""Parser for the Greifensee private weather station, against reconstructed fixture pages
(tests/fixtures/greifensee — see each file's header for provenance and caveats)."""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from conftest import FakeHttp
from ledwetter.model import Period, concat
from ledwetter.sources import GreifenseeWetter
from ledwetter.sources.greifenseewetter import COMPASS_DEGREES, week_url

UTC = timezone.utc
FIXED = datetime(2026, 10, 3, 14, 45, tzinfo=UTC)  # 16:45 local time
FIXTURES = "tests/fixtures/greifensee"


def read(name):
    with open(f"{FIXTURES}/{name}", "rb") as f:
        return f.read()


def http_with(**routes):
    return FakeHttp([(pattern, (lambda content: (lambda m, p: content))(content)) for pattern, content in routes.items()])


def default_http():
    return http_with(**{r"/aktuell\.htm$": read("aktuell.htm"), r"/w2026_01\.htm$": read("week_2026_01.htm")})


def source(http=None, **cfg):
    cfg.setdefault("stations", [{"id": "greifensee", "name": "Greifensee"}])
    return GreifenseeWetter(http or default_http(), cfg, lambda: FIXED)


def fetch(src, period=None):
    period = period or Period(FIXED - timedelta(hours=30), FIXED)
    return concat(src.fetch(period))


class TestWeekUrl:
    def test_url_uses_iso_week_numbering_year(self):
        # 29 Dec 2025 is in ISO week 1 of 2026 -> filed under /2026/, not /2025/
        assert week_url("https://x/Wetter", 2026, 1) == "https://x/Wetter/2026/w2026_01.htm"

    def test_all_compass_points_resolve_to_distinct_degrees(self):
        assert len(COMPASS_DEGREES) == 16 and len(set(COMPASS_DEGREES.values())) == 16
        assert COMPASS_DEGREES["N"] == 0 and COMPASS_DEGREES["S"] == 180


class TestTodayPage:
    def test_summary_rows_are_excluded_and_time_range(self):
        df = fetch(source(http_with(**{r"/aktuell\.htm$": read("aktuell.htm")})),
                   Period(FIXED - timedelta(hours=24), FIXED))
        assert df["time_utc"].max() == pd.Timestamp("2026-10-03 14:45", tz="UTC")
        assert df["time_utc"].min() == pd.Timestamp("2026-10-02 15:00", tz="UTC")  # 02.10 17:00 CEST

    def test_values_units_and_degree_column_used_directly(self):
        df = fetch(source(http_with(**{r"/aktuell\.htm$": read("aktuell.htm")})),
                   Period(FIXED - timedelta(hours=24), FIXED))
        latest = df[df["time_utc"] == df["time_utc"].max()].set_index("parameter")
        assert latest.loc["air_temperature", "value"] == 22.9 and latest.loc["air_temperature", "unit"] == "°C"
        assert latest.loc["wind_direction", "value"] == 237  # exact degrees, not the compass label

    def test_wind_speed_and_beaufort_split_from_combined_cell(self):
        df = fetch(source(http_with(**{r"/aktuell\.htm$": read("aktuell.htm")})),
                   Period(FIXED - timedelta(hours=24), FIXED))
        row = df[df["time_utc"] == pd.Timestamp("2026-10-02 15:00", tz="UTC")].set_index("parameter")
        assert row.loc["wind_speed", "value"] == 5.3 and row.loc["wind_force", "value"] == 1
        assert row.loc["wind_gust", "value"] == 14.5 and row.loc["wind_gust_force", "value"] == 3


class TestWeeklyArchive:
    def test_compass_only_direction_is_converted_to_degrees(self):
        http = http_with(**{r"/w2026_01\.htm$": read("week_2026_01.htm")})
        # period starts exactly at the first row's UTC instant (29.12 00:00 CET = 28.12 23:00 UTC),
        # whose local calendar date (29.12, a Monday) is what places this in ISO week 1 of 2026
        period = Period(datetime(2025, 12, 28, 23, tzinfo=UTC), datetime(2026, 1, 5, tzinfo=UTC))
        df = fetch(source(http), period)
        row = df[df["time_utc"] == pd.Timestamp("2025-12-28 23:00", tz="UTC")].set_index("parameter")
        assert row.loc["wind_direction", "value"] == 202.5  # S-SW

    def test_year_spanning_week_keeps_all_its_days(self):
        http = http_with(**{r"/w2026_01\.htm$": read("week_2026_01.htm")})
        df = fetch(source(http), Period(datetime(2025, 12, 29, tzinfo=UTC), datetime(2026, 1, 5, tzinfo=UTC)))
        days = sorted(df["time_utc"].dt.tz_convert("Europe/Zurich").dt.date.unique())
        assert days[0].year == 2025 and days[-1].year == 2026

    def test_week_boundary_is_requested_once(self):
        http = http_with(**{r"/w2026_01\.htm$": read("week_2026_01.htm")})
        src = source(http)
        fetch(src, Period(datetime(2025, 12, 29, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)))
        assert len([c for c in http.calls if "w2026_01" in c[1]]) == 1

    def test_missing_week_is_skipped_not_fatal(self, caplog):
        http = FakeHttp()  # every URL 404s
        with caplog.at_level(logging.INFO):
            df = fetch(source(http), Period(datetime(2025, 12, 29, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)))
        assert df.empty and "not available" in caplog.text

    def test_unknown_compass_label_is_dropped_with_warning(self, caplog):
        html = (read("week_2026_01.htm").decode("iso-8859-1")
               .replace(">S-SW<", ">VAR<", 1))
        http = http_with(**{r"/w2026_01\.htm$": html.encode("iso-8859-1")})
        with caplog.at_level(logging.WARNING):
            df = fetch(source(http), Period(datetime(2025, 12, 29, tzinfo=UTC), datetime(2026, 1, 5, tzinfo=UTC)))
        assert "unknown compass label" in caplog.text
        row = df[df["time_utc"] == pd.Timestamp("2025-12-28 23:00", tz="UTC")]
        assert "wind_direction" not in set(row["parameter"])


class TestCombinedFetch:
    def test_period_spanning_week_and_today_uses_both_pages(self):
        src = source()
        period = Period(datetime(2025, 12, 29, tzinfo=UTC), FIXED)
        df = fetch(src, period)
        assert df["time_utc"].min() < pd.Timestamp("2026-01-01", tz="UTC")
        assert df["time_utc"].max() == pd.Timestamp("2026-10-03 14:45", tz="UTC")

    def test_pure_past_period_does_not_fetch_today_page(self):
        http = default_http()
        fetch(source(http), Period(datetime(2025, 12, 29, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)))
        assert not any("aktuell" in c[1] for c in http.calls)


class TestRobustness:
    def test_implausible_values_are_dropped(self, caplog):
        with caplog.at_level(logging.WARNING):
            df = fetch(source())
        assert "implausible" not in caplog.text  # the fixtures have no out-of-range values
        assert df["value"].between(-30, 1080).all()

    def test_missing_table_is_handled(self, caplog):
        http = http_with(**{r"/aktuell\.htm$": b"<html><body>no data today</body></html>"})
        with caplog.at_level(logging.WARNING):
            assert fetch(source(http), Period(FIXED - timedelta(hours=2), FIXED)).empty
        assert "could not read a table" in caplog.text

    def test_unexpected_layout_is_handled(self, caplog):
        http = http_with(**{r"/aktuell\.htm$": b"<table><tr><th>A</th><th>B</th></tr>"
                                               b"<tr><td>1</td><td>2</td></tr></table>"})
        with caplog.at_level(logging.WARNING):
            assert fetch(source(http), Period(FIXED - timedelta(hours=2), FIXED)).empty
        assert "layout looks different" in caplog.text

    def test_registered_and_discoverable(self, clock):
        from ledwetter.discover import StationDiscovery
        from ledwetter.sources import REGISTRY
        assert REGISTRY["greifenseewetter"] is GreifenseeWetter
        d = StationDiscovery(default_http(), ["ZH", "BE"], clock, sources={"greifenseewetter": GreifenseeWetter})
        cfg = d.build_config()
        assert cfg["greifenseewetter"]["stations"] == [
            {"id": "greifensee", "name": "Greifensee (private, greifenseewetter.ch)", "canton": "ZH"}]

    def test_history_start_is_the_configured_guess(self):
        src = source(history_start="2021-06-01")
        assert src.history_start() == datetime(2021, 6, 1, tzinfo=UTC)


class TestRealCapture:
    """Against byte-for-byte real pages captured via 'ledwetter diagnose' on 2026-10-03
    (tests/fixtures/greifensee/REAL_README.md)."""

    def test_real_pages_parse_without_warnings(self, caplog):
        http = http_with(**{r"/w2026_40\.htm$": read("real_week_2026_40.htm"),
                            r"/aktuell\.htm$": read("real_aktuell.htm")})
        clock_at_capture = lambda: datetime(2026, 10, 3, 15, 26, 18, tzinfo=UTC)
        src = GreifenseeWetter(http, {"stations": [{"id": "greifensee", "name": "Greifensee"}]}, clock_at_capture)
        with caplog.at_level(logging.WARNING):
            df = fetch(src, Period(datetime(2026, 9, 27, tzinfo=UTC), clock_at_capture()))
        assert caplog.text == ""
        assert set(df["parameter"]) == set(GreifenseeWetter.CATALOG)
        assert df["time_utc"].min() < pd.Timestamp("2026-09-28", tz="UTC")
        assert df["time_utc"].max() == pd.Timestamp("2026-10-03 15:25:00", tz="UTC")

    def test_real_page_declares_latin1_and_is_decoded_as_such(self):
        # the captured bytes contain a non-UTF-8 '©' in the generator meta tag; decoding as UTF-8
        # would silently corrupt it (and the degree signs / umlauts in the data) instead of erroring
        raw = read("real_aktuell.htm")
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        assert "Werner Krenn" in raw.decode("iso-8859-1")

    def test_missing_html_parser_gives_an_actionable_error(self, caplog, monkeypatch):
        import pandas as pd
        def fail(*a, **k):
            raise ImportError("no parser")
        monkeypatch.setattr(pd, "read_html", fail)
        http = http_with(**{r"/aktuell\.htm$": read("real_aktuell.htm")})
        with caplog.at_level(logging.WARNING):
            df = fetch(source(http), Period(FIXED - timedelta(hours=2), FIXED))
        assert df.empty and "ensure lxml and html5lib" in caplog.text
