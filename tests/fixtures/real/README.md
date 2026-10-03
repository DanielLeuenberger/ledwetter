# Real source responses

Excerpts of a diagnostic run (`ledwetter diagnose`, canton ZH, 3 October 2026, 12:17 UTC).
They guard the parsers against the formats the sources actually deliver.

- MeteoSwiss: complete parameter lists; station lists reduced to ZH (plus a few others); the first 20 lines of
  `t_now` files (weather, tower, precipitation networks).
- Water police (Tecdottir): the first 6 measurements of Tiefenbrunnen.
- Geo services: an identify answer; a find answer with the geometry removed (bounding box kept).
- METAR: station info for Switzerland plus two foreign examples (one with an empty siteType); the METARs of LSZH and LSMD.
- BAFU: one 10-minute data query (from 09:00 UTC).
- UGZ: the first 40 data lines of the 2026 yearly file.
