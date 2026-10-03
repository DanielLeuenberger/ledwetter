# Real captures

`real_aktuell.htm` and `real_week_2026_40.htm` are genuine, byte-for-byte copies of
`https://www.greifenseewetter.ch/Wetter/aktuell.htm` and `.../Wetter/2026/w2026_40.htm`, captured by
the user via `ledwetter diagnose` on 2026-10-03 (`run_info.json` in that diagnostic run: started
2026-10-03T15:26:18Z). Encoding is `iso-8859-1` as declared by the page's own `<meta charset>`.

`aktuell.htm` and `week_2026_01.htm` in this same directory are hand-reconstructed approximations
(see their own headers) made before any real capture existed; they're kept for the edge cases they
cover (year-spanning ISO week, a trimmed row set) that the real captures don't happen to exercise.
