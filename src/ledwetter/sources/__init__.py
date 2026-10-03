"""Registry of data sources."""
from __future__ import annotations

from ..http import HttpClient
from ..model import Clock, utcnow
from .bafu import BafuHydro
from .base import Source
from .meteoswiss import MeteoSwissSMN
from .metar import MetarAWC
from .ugz import UgzMeteo
from .wapo import WaPo

REGISTRY = {cls.name: cls for cls in (MeteoSwissSMN, WaPo, BafuHydro, UgzMeteo, MetarAWC)}


def build_sources(cfg: dict, http: HttpClient, names=None, clock: Clock = utcnow) -> list:
    names = list(names or REGISTRY)
    unknown = set(names) - set(REGISTRY)
    if unknown:
        raise ValueError(f"Unbekannte Quellen: {sorted(unknown)}")
    return [REGISTRY[n](http, cfg.get(n, {}), clock=clock) for n in names]


__all__ = ["REGISTRY", "Source", "build_sources", "MeteoSwissSMN", "WaPo", "BafuHydro", "UgzMeteo", "MetarAWC"]
