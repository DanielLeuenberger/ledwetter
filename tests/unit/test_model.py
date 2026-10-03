from datetime import datetime, timezone

import pandas as pd
import pytest

from ledwetter.model import (COLUMNS, ParameterFilter, Period, guess_aggregation, make_frame, parse_time,
                             parse_time_series, to_float)

UTC = timezone.utc


class TestParseTime:
    def test_iso_with_offset(self):
        assert parse_time("2026-10-03T07:00+0200") == datetime(2026, 10, 3, 5, tzinfo=UTC)

    def test_naive_is_local_summer_time(self):
        assert parse_time("03.10.2026 07:00:00", "Europe/Zurich") == datetime(2026, 10, 3, 5, tzinfo=UTC)

    def test_naive_is_local_winter_time(self):
        assert parse_time("2026-01-15 07:00", "Europe/Zurich") == datetime(2026, 1, 15, 6, tzinfo=UTC)

    def test_unix_seconds(self):
        assert parse_time(1791007800) == datetime(2026, 10, 3, 6, 10, tzinfo=UTC)

    def test_ambiguous_dst_hour_is_rejected(self):
        # 25.10.2026 02:30 occurs twice in Zurich (end of DST) -> ambiguous
        assert parse_time("2026-10-25 02:30", "Europe/Zurich") is None

    @pytest.mark.parametrize("value", [None, "", "kein Datum", float("nan")])
    def test_invalid(self, value):
        assert parse_time(value) is None

    def test_series_mixed_offsets_and_naive(self):
        s = pd.Series(["2026-10-03T07:00+0200", "2026-10-03 07:00"])
        out = parse_time_series(s, "Europe/Zurich")
        assert list(out) == [pd.Timestamp("2026-10-03 05:00", tz="UTC")] * 2


class TestValuesAndParameters:
    def test_to_float(self):
        assert to_float("3,5") == 3.5 and to_float("-") is None and to_float("10+") == 10.0

    @pytest.mark.parametrize("code,desc,agg", [
        ("rre150z0", "Precipitation; ten minutes total", "sum"),
        ("fu3010z1", "Gust peak (one second); maximum in km/h", "max"),
        ("dkl010z0", "Wind direction; ten minutes mean", "dir"),
        ("tre200s0", "Air temperature 2 m above ground; current value", "mean"),
        ("RainDur", "Niederschlagsdauer", "sum"),
        ("wcc006s0", "Foehn index", "max"),
    ])
    def test_guess_aggregation(self, code, desc, agg):
        assert guess_aggregation(code, desc) == agg

    @pytest.mark.parametrize("code,agg", [("rre150z0", "sum"), ("sre000z0", "sum"), ("dkl010z0", "dir"),
                                          ("dv1towz0", "dir"), ("fu3010z1", "max"), ("fu3towz3", "max"),
                                          ("tre200s0", "mean"), ("WD", "dir"), ("RainDur", "sum")])
    def test_aggregation_from_code_only(self, code, agg):
        assert guess_aggregation(code) == agg

    def test_parameter_filter(self):
        f = ParameterFilter(include=["tre*", "fu3*"], exclude=["fu3010z3"])
        assert f("tre200s0") and f("fu3010z0") and not f("fu3010z3") and not f("ure200s0")
        assert ParameterFilter()("anything")


class TestPeriod:
    def test_date_only_end_is_inclusive(self):
        p = Period.from_args("2026-10-01", "2026-10-01", now=datetime(2026, 10, 3, tzinfo=UTC))
        assert p.start == datetime(2026, 9, 30, 22, tzinfo=UTC)
        assert p.end == datetime(2026, 10, 1, 22, tzinfo=UTC)

    def test_default_is_last_24h_capped_at_now(self):
        now = datetime(2026, 10, 3, 6, tzinfo=UTC)
        p = Period.from_args(None, "2030-01-01", now=now)
        assert p.end == now and (p.end - p.start).total_seconds() == 86400

    def test_start_after_end_raises(self):
        with pytest.raises(ValueError):
            Period(datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))

    def test_overlaps(self):
        p = Period(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
        assert p.overlaps(datetime(2025, 12, 31, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
        assert not p.overlaps(datetime(2026, 1, 3, tzinfo=UTC), datetime(2026, 1, 4, tzinfo=UTC))


class TestMakeFrame:
    def test_drops_missing_and_sets_metadata(self):
        t = pd.to_datetime(["2026-10-03T05:00Z", "2026-10-03T05:10Z"], utc=True)
        df = make_frame(t, [1.0, None], source="x", station_id="A", station_name="Alpha", parameter="p1",
                        unit="°C", time_ref="interval_end", interval_min=10)
        assert list(df.columns) == COLUMNS and len(df) == 1 and df["unit"].iloc[0] == "°C"

    def test_rejects_unknown_time_ref(self):
        with pytest.raises(ValueError):
            make_frame([], [], source="x", station_id="A", station_name="A", parameter="p", unit="",
                       time_ref="end", interval_min=10)
