# RA-IV RMTN file collections on pygeoapi

OGC API - Features collections for the files under
`https://ra4-gifs.weather.gov/data/RMTN/`, one collection per folder. Each file
is an item, and each item links to a download endpoint on this API. Clients
never see where the files are stored, so storage can change without changing
the API.

## Additions to pygeoapi

No pygeoapi file is modified.

| File | Purpose |
|---|---|
| `pygeoapi/provider/file_listing.py` | `FileListingProvider`: lists files, reads their bytes, extracts GRIB, BUFR and text-bulletin locations. |
| `pygeoapi/provider/wmo_text.py` | Locations from WMO text bulletins. |
| `pygeoapi/provider/station_tables/` | Station tables used by `wmo_text.py`, the script that builds them, and a README on sources. |
| `edr_app.py` | WSGI entry point. Imports pygeoapi's Flask `APP` and adds the `/download` route. |
| `example-config.yaml` | The `rmtn-*` collections, and `server.templates.path: rmtn_templates`. |
| `rmtn_templates/collections/items/` | Copies of pygeoapi's `index.html` and `item.html`. When there is no geometry, the map shows the whole world, and the item page also shows a "no geometry" notice. Otherwise they behave as upstream. All other pages use pygeoapi's built-in templates. |
| `start_local.sh` | Installs the extra packages and runs `gunicorn edr_app:APP`, locally or with `--docker`. |
| `Dockerfile.edr` | Image used by `--docker`. The upstream `Dockerfile` is unchanged. |

The provider sits in `pygeoapi/provider/` but can live in any package. Only
the `name:` line in the config would change.

### `edr_app.py`

- Adds `GET` and `HEAD` on `/collections/{collectionId}/items/{itemId}/download`.
  All pygeoapi endpoints are unchanged.
- Must be started as `gunicorn edr_app:APP`, not `pygeoapi.flask_app:APP`.
- At startup, loads each collection's first feature provider so listings are
  fetched before the first request. This includes providers other than
  `FileListingProvider`, so merging this config with database-backed
  collections would also create those at startup.
- `/download` is not in the generated OpenAPI document.

## How it works

```
Request                       Handled by                          Reads from source
GET /items                    FileListingProvider.query()         nothing: listing is in memory,
                                                                  refreshed in the background
GET /items/{id}               FileListingProvider.get()           GRIB: first message (~64 KB range)
                                                                  BUFR: whole file
GET /items/{id}/download      edr_app.download_item()             the file, streamed
```

### `/items`

- Item id = file name. Geometry is `null`.
- Properties: `size` and `modified` from the listing. Names like
  `202611161200_f180.grib2_rmtn` also give `reference_time` and
  `forecast_hour`; otherwise `reference_time` is the modified time.
- Sorted by most recently modified. Supports `sortby`, `datetime` (on
  `reference_time`), property filters, `q` (substring of the file name) and
  paging. Not `bbox`.
- Each item has an `enclosure` link to its `/download` URL, with media type
  and size.

### `/items/{id}`

**GRIB** (file name contains `.grib`): reads the first GRIB message and adds
`geometry` (grid bounding polygon), `grid_type`, `grid_size`,
`number_of_points` and `valid_time`. With `inspect_grib: full`, it downloads
the whole file and also adds `message_count` and `variables` (each with its
levels).

**BUFR** (collections with `inspect_bufr: true`, only `rmtn-bufr`): decodes
every BUFR message in the file and adds:

- `geometry`: `Point`, or `MultiPoint` with one point per station
- `number_of_messages`, `number_of_subsets`, `data_category`
- `stations`: WMO id and name, when present

Each subset contributes its first valid position, which for radiosondes is the
launch position. BUFR files have no extension, so this is enabled per
collection.

**Text bulletins** (collections with `inspect_text: true`: `rmtn-surface`,
`rmtn-fcast`, `rmtn-ua`, `rmtn-sat`): parses the standard report layouts and
adds:

- `geometry`: `Point` or `MultiPoint` of the report locations
- `number_of_reports`, `location_source` (`station table` and/or
  `reported position`)
- `stations` (ICAO or WMO id and name) and `unlocated_stations` (ids not in
  the tables)

| Heading (T1T2) | Report type | Location from |
|---|---|---|
| `SA`, `SP` | METAR, SPECI | ICAO id |
| `FT`, `FC` | TAF | ICAO id |
| `SM`, `SI`, `SN`, `SO`, `SS` | SYNOP land (`AAXX`) | WMO station number |
| | SHIP, mobile (`BBXX`, `OOXX`); buoy, TESAC, BATHY (`ZZYY`, `KKYY`, `JJVV`…) | Position coded in the report |
| `U*` | TEMP/PILOT land (`TT..`, `PP..`) | WMO station number |
| | TEMP ship, mobile, dropsonde (`UU..`, `II..`, `XX..`) | Position coded in the report |
| `UD`, `UR` | AMDAR, reconnaissance | Positions in the report (`2639N 12324E`) |
| `TC` | Satellite cloud reports | ICAO id per line |
| `TP` | Tropical cyclone fixes | `C.`/`D.` latitude and longitude lines |

Other types, such as US zone forecasts (`FP`, `FZ`) and binary files (`SX`,
`SH`), keep `geometry: null`.

Station positions come from `station_tables/`: WMO OSCAR/Surface for WMO
ids and the NOAA Aviation Weather Center station list for ICAO ids, with old
NWS tables as fallback. The tables are a committed snapshot; refresh them with
`station_tables/refresh.py` (see `station_tables/README.md`). Ids not found
are listed in `unlocated_stations`. In samples, 0–9% of station ids were
unlocated depending on the report type: none for METAR, SPECI and TAF, up to
9% for some synoptic bulletins.

If a file can't be decoded (not GRIB/BUFR, or `pygrib`/`eccodes` missing),
the item is returned with `geometry: null` and a warning is logged.

### `/items/{id}/download`

- Streams the file as an attachment with `Content-Type`, `Content-Length`, and
  `Last-Modified` and `ETag` when the source provides a modified time.
- Supports `HEAD` and single byte ranges (`bytes=a-b`, `bytes=-n`); 416 for an
  unsatisfiable range.
- The id must be a plain file name (no `/`, `\` or leading dot, and matching
  `file_types` if set). It is appended to the storage location, so clients
  can't reach other paths or hosts.
- 404 for a missing file, 502 for a storage error. No authentication.

### Caching

- Listings are cached per collection in each gunicorn worker. After
  `cache_ttl` seconds (default 60), requests still get the cached copy while
  a background thread refreshes it, so new files appear within about a minute.
- Only a request that arrives before the startup fetch finishes has to wait.
  The largest folder, `SURFACE` (about 11,800 files), takes about 10 s to
  fetch.
- GRIB, BUFR and text results are cached per file (name and size), up to 64
  per collection.

## Configuration

```yaml
providers:
    - type: feature
      name: pygeoapi.provider.file_listing.FileListingProvider
      data: https://ra4-gifs.weather.gov/data/RMTN/GRIB2/   # listing source
      id_field: id
      time_field: reference_time
      # file_types: [.grib2_rmtn]          # only list these suffixes
      options:
          # media_type: application/x-grib2  # default: guessed from the name
          # storage_url: s3://bucket/prefix/ # where bytes are read, if not `data`
          # cache_ttl: 60                    # seconds
          # inspect_grib: extent             # extent | full | false
          # inspect_bufr: false              # true: station locations from BUFR
          # inspect_text: false              # true: locations from WMO text bulletins
          # prewarm: true                    # fetch the listing at startup
```

`server.url` must be the public URL of the API; it is used in every link.

## Running

```bash
./start_local.sh                     # current Python env, gunicorn on :5000
./start_local.sh --docker            # background container on :5000
./start_local.sh --docker --base-url https://edr-api-c.mdl.nws.noaa.gov/ra-iv
```

`--base-url` sets `server.url` in `example-config.yaml`, changing only that
line. It is saved in the file, so later runs keep it. The server still answers
at `/`, so with a path like `/ra-iv` the reverse proxy must strip the path
before forwarding.

`--docker` builds `Dockerfile.edr` (image `edr-rmtn-pygeoapi`, includes
`pygrib` and `eccodes`) and starts the container in the background with
`--restart unless-stopped`. The script waits until the API answers, then
exits. If it doesn't start, or port 5000 is in use, the script prints the
error and removes the container. Each run rebuilds the image and replaces
the container.

The config is mounted read-only, so config edits need only a restart.
Templates are copied into the image, so template edits need a rebuild
(rerun the script). The
container runs as the host user so an owner-only config file is readable.

```bash
docker logs -f edr-rmtn-pygeoapi     # logs
docker restart edr-rmtn-pygeoapi     # after editing example-config.yaml
docker rm -f edr-rmtn-pygeoapi       # stop and remove
```

## Dependencies

Beyond pygeoapi's requirements (installed by `start_local.sh` and
`Dockerfile.edr`, except `s3fs`):

| Package | Needed for |
|---|---|
| `fsspec` | Local and S3 storage |
| `s3fs` | S3 storage (not installed by default) |
| `pygrib` | GRIB geometry. Outside Docker, conda-forge is the reliable install: `mamba install -c conda-forge pygrib` |
| `eccodes` | BUFR geometry. The pip wheel includes the ecCodes library. |
| `beautifulsoup4` | Fallback HTML listing parser |
| `gunicorn`, `gevent` | Server |

## Moving off RA4-GIFS

The provider needs:

1. a listing of file names in one flat directory (size and modified time if
   available), and
2. files readable at `<base>/<file name>`.

Item ids are file names and clients only see `/download` URLs, so neither
changes as long as file names stay the same.

### Config-only changes

Local disk or mounted volume:

```yaml
data: /data/RMTN/GRIB2/
```

S3 (install `s3fs`; credentials from the standard AWS environment; not yet
tested):

```yaml
data: s3://my-bucket/RMTN/GRIB2/
```

Listing from one place, files from another (same file names in both):

```yaml
data: https://ra4-gifs.weather.gov/data/RMTN/GRIB2/
options:
    storage_url: s3://my-bucket/RMTN/GRIB2/
```

### Changes that need code

| Source | Problem | Needed |
|---|---|---|
| Another HTTP listing format (e.g. Apache/nginx autoindex) | The HTTP parser expects the RA4-GIFS table: name, type, date, size. | A parser for that format in `_parse_http_listing`. |
| Index of per-file links (OpenSearch, database, catalog) | The provider assumes one base location plus a file name. | A listing backend that keeps each file's own location. |
| Nested folders | Only one level is listed. | Recursive listing, ids including the subfolder. |
| Duplicate file names in a collection | The id is the file name. | A different id scheme. |

### Also update

- Each collection's `rel: related` link and its title and description point
  at or name the RA4-GIFS folders.
- GRIB files are recognised by `.grib` in the name; `file_types`, if set,
  must match the new names.
- Each collection needs its own `data` value: the provider finds its
  collection id by matching `name` and `data` in the config.
- `server.url` must be the deployment's public URL.
