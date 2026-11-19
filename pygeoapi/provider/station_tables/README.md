# Station tables

`wmo_text.py` turns station identifiers in text bulletins into coordinates
using:

- `wmo_stations.csv`: WMO block + station number (SYNOP, TEMP, PILOT)
- `icao_stations.csv`: ICAO location indicator (METAR, SPECI, TAF,
  satellite cloud reports)

Both have the columns `id, name, lat, lon, source`. `tables_info.json` records
when they were built and how many stations came from each source. These files
are a committed snapshot; they are not refreshed automatically.

## Sources

| Ids | Source, in priority order | Status |
|---|---|---|
| WMO | 1. **WMO OSCAR/Surface**, WMO's official registry of observing stations (`https://oscar.wmo.int/surface`). Stations with WIGOS id `0-20000-0-<5 digits>`. | Authoritative. Also the source wis2box uses for station metadata. |
| | 2. NOAA Aviation Weather Center station list (`wmoId` field) | Fills gaps |
| | 3. NWS `nsd_bbsss.txt` (last updated 2016) | Fallback for retired or unregistered stations |
| ICAO | 1. **NOAA Aviation Weather Center station list** (`https://aviationweather.gov/data/cache/stations.cache.json.gz`), updated daily | NOAA's operational METAR/TAF station list. Not an ICAO registry. |
| | 2. NWS `nsd_cccc.txt` (last updated 2018) | Fallback |

The authoritative registry for ICAO identifiers is ICAO Doc 7910 (Location
Indicators), which ICAO sells; it is not used.

The `stations` property on each item records the `source` of every station
position.

## How the tables are built

`refresh.py` downloads OSCAR/Surface (all stations, two pages of up to
50,000) and the AWC list, then merges them with the NWS files in this
directory:

- OSCAR: a station's primary WIGOS id is preferred over a secondary one, and
  operational stations over closed ones. An id that OSCAR lists only as a
  secondary WIGOS id is dropped if another source puts that station more than
  500 km away. On 2026-11-19 this dropped one id: `07034`, a French index
  number listed on an Australian radar.
- NWS positions: the rounded position (degrees and minutes) is used unless
  the precise one (seconds) is within 25 km of it, because some precise
  values are wrong.
- If either OSCAR or AWC fails to download, nothing is written.

Snapshot of 2026-11-19: 15,530 WMO stations (13,264 OSCAR, 276 AWC, 1,990
NWS) and 9,252 ICAO stations (8,755 AWC, 497 NWS).

## Refreshing

```bash
python3 pygeoapi/provider/station_tables/refresh.py
```

Takes about 30 s to 2 min, depending on OSCAR. Commit the updated
`wmo_stations.csv`, `icao_stations.csv` and `tables_info.json`, then rebuild
or restart the server. The NWS `nsd_*.txt` files are inputs to the refresh
and are not updated by it.
