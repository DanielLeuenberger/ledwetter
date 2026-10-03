"""Cantons of Switzerland and canton lookups via the federal geo service (api3.geo.admin.ch).

- canton_of(lat, lon): canton code of a point (swisstopo cantonal boundaries, identify service)
- bbox(canton): bounding box of a canton (find service), used to skip needless point lookups
Results are cached per run. All coordinates are WGS84.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

CANTONS = {
    "AG": "Aargau", "AI": "Appenzell Innerrhoden", "AR": "Appenzell Ausserrhoden", "BE": "Bern",
    "BL": "Basel-Landschaft", "BS": "Basel-Stadt", "FR": "Freiburg", "GE": "Genf", "GL": "Glarus",
    "GR": "Graubünden", "JU": "Jura", "LU": "Luzern", "NE": "Neuenburg", "NW": "Nidwalden", "OW": "Obwalden",
    "SG": "St. Gallen", "SH": "Schaffhausen", "SO": "Solothurn", "SZ": "Schwyz", "TG": "Thurgau", "TI": "Tessin",
    "UR": "Uri", "VD": "Waadt", "VS": "Wallis", "ZG": "Zug", "ZH": "Zürich",
}
SWITZERLAND_BBOX = (45.80, 47.85, 5.90, 10.55)   # lat_min, lat_max, lon_min, lon_max
LAYER = "ch.swisstopo.swissboundaries3d-kanton-flaeche.fill"
BASE = "https://api3.geo.admin.ch/rest/services/api/MapServer"


def parse_cantons(text: str) -> list:
    """'zh, be' -> ['ZH', 'BE']; unknown codes raise ValueError."""
    codes = [c.strip().upper() for c in text.split(",") if c.strip()]
    unknown = [c for c in codes if c not in CANTONS]
    if unknown or not codes:
        raise ValueError(f"Unknown canton codes: {unknown or text!r}. Valid: {', '.join(sorted(CANTONS))}")
    return list(dict.fromkeys(codes))


class CantonGeo:
    def __init__(self, http):
        self.http = http
        self._points: dict = {}
        self._boxes: dict = {}

    def bbox(self, canton: str) -> Optional[tuple]:
        """(lat_min, lat_max, lon_min, lon_max) of a canton, or None if the service gives no box."""
        if canton not in self._boxes:
            box = None
            try:
                data = self.http.get(f"{BASE}/find", layer=LAYER, searchText=canton, searchField="ak",
                                     contains="false", returnGeometry="true", sr=4326).json()
                boxes = [r["bbox"] for r in data.get("results", []) if len(r.get("bbox") or []) == 4]
                if boxes:  # bbox = [lon_min, lat_min, lon_max, lat_max]
                    box = (min(b[1] for b in boxes), max(b[3] for b in boxes),
                           min(b[0] for b in boxes), max(b[2] for b in boxes))
            except Exception as e:
                log.debug("Canton %s: bounding box not available (%s)", canton, e)
            self._boxes[canton] = box
        return self._boxes[canton]

    def canton_of(self, lat: float, lon: float) -> Optional[str]:
        key = (round(lat, 5), round(lon, 5))
        if key not in self._points:
            data = self.http.get(f"{BASE}/identify", geometry=f"{lon},{lat}", geometryType="esriGeometryPoint",
                                 sr=4326, layers=f"all:{LAYER}", tolerance=0, returnGeometry="false").json()
            found = None
            for r in data.get("results", []):
                attrs = r.get("attributes", {})
                values = [attrs.get("ak")] + list(attrs.values())
                found = next((str(v).upper() for v in values if str(v).upper() in CANTONS), None)
                if found:
                    break
            self._points[key] = found
        return self._points[key]

    def contains(self, canton: str, lat: float, lon: float) -> bool:
        box = self.bbox(canton)
        if box and not (box[0] <= lat <= box[1] and box[2] <= lon <= box[3]):
            return False
        return self.canton_of(lat, lon) == canton
