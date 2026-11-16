# =================================================================
#
# Authors: Shane Mill <shane.mill@noaa.gov>
#
# Copyright (c) 2026 Shane Mill
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

"""Feature provider that exposes a directory of data files as items.

Each file (GRIB2, NetCDF, ...) becomes a GeoJSON Feature (null geometry)
with an ``enclosure`` link to this API's own download endpoint
(``/collections/{collectionId}/items/{itemId}/download``, see
``edr_app.py``). The storage location is never exposed to clients.

``data`` may be:
  * an http(s) directory-listing URL (e.g. NOAA RA-IV GIFS)
  * a local directory path
  * any fsspec URL (``s3://bucket/prefix/`` requires ``s3fs``)

Config example::

    providers:
        - type: feature
          name: pygeoapi.provider.file_listing.FileListingProvider
          data: https://ra4-gifs.weather.gov/data/RMTN/GRIB2/
          id_field: id
          time_field: reference_time
          file_types: [.grib2_rmtn, .grib2, .nc]   # optional filter
          options:
              media_type: application/x-grib2
              # where the bytes are read from, if different from ``data``
              # (e.g. listing from an index, files in s3)
              # storage_url: s3://bucket/prefix/
              # seconds a listing is served from memory before a background
              # refresh (default 60)
              # cache_ttl: 60
              # inspect_grib: extent  # | full | false
              # inspect_bufr: false   # true: decode BUFR for station locations
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
import tempfile
import threading
import time
import mimetypes
import os
import re
from urllib.parse import quote, unquote, urljoin, urlparse

from pygeoapi.provider.base import (BaseProvider, ProviderConnectionError,
                                    ProviderItemNotFoundError,
                                    ProviderQueryError)

LOGGER = logging.getLogger(__name__)

# e.g. 202611161200_f180.grib2_rmtn -> run time + forecast hour
NAME_RE = re.compile(r'^(?P<run>\d{12})_f(?P<fh>\d{2,3})')

MEDIA_TYPES = {
    '.grib2': 'application/x-grib2',
    '.grb2': 'application/x-grib2',
    '.nc': 'application/x-netcdf',
}


_STATES = {}
_STATES_LOCK = threading.Lock()


class _State:
    """Cache shared by all provider objects of one collection.

    pygeoapi creates a new provider object for every request, so anything
    worth keeping between requests has to live here.
    """

    def __init__(self):
        self.snapshot = None  # (loaded_at, entries_by_name, features)
        self.lock = threading.Lock()
        self.refreshing = False
        self.grib_cache = {}
        self.collection_id = None
        self.server_url = None


def _get_state(key):
    with _STATES_LOCK:
        if key not in _STATES:
            _STATES[key] = _State()
        return _STATES[key]


ROW_RE = re.compile(
    r'<tr>\s*<td>(?:(?!</td>).)*?<a\s+href="([^"]+)"[^>]*>\s*([^<]*?)\s*</a>'
    r'.*?</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>'
    r'\s*</tr>', re.S)
DATE_FORMAT = '%b %d %Y %I:%M %p'


def _rows_fast(html):
    """(href, type, date, size) per table row; fast path for large pages"""
    return [(m.group(1), m.group(3).strip(), m.group(4).strip(),
             m.group(5).strip()) for m in ROW_RE.finditer(html)]


def _rows_soup(html):
    """Slower, layout-tolerant fallback using BeautifulSoup"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    for row in soup.find_all('tr'):
        a = row.find('a', href=True)
        cells = [c.get_text(' ', strip=True) for c in row.find_all('td')]
        if a is None or len(cells) < 4:
            continue
        rows.append((a['href'], cells[1], cells[2], cells[3]))
    return rows


def _parse_http_listing(base_url, timeout=60):
    """Parse an HTML directory listing (table rows: name, type, date, size)"""
    import requests

    try:
        resp = requests.get(base_url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as err:
        raise ProviderConnectionError(f'Cannot list {base_url}: {err}')

    rows = _rows_fast(resp.text)
    if not rows:
        rows = _rows_soup(resp.text)

    base_path = urlparse(base_url).path.rstrip('/') + '/'
    dates = {}
    entries = []
    for raw_href, kind, date, size in rows:
        href = urljoin(base_url, raw_href)
        path = urlparse(href).path
        # only direct children of the listed directory
        if not path.startswith(base_path) or path == base_path:
            continue
        name = unquote(path[len(base_path):])
        if not name or '/' in name.strip('/'):
            continue
        if date not in dates:
            try:
                dates[date] = datetime.strptime(date, DATE_FORMAT).replace(
                    tzinfo=timezone.utc)
            except ValueError:
                dates[date] = None
        entries.append({'name': name, 'href': href,
                        'size': int(size) if size.isdigit() else None,
                        'modified': dates[date],
                        'is_dir': kind == 'DIR' or name.endswith('/')})
    return entries


def _list_fsspec(base):
    import fsspec

    try:
        fs, root = fsspec.core.url_to_fs(base)
        infos = fs.ls(root, detail=True)
    except Exception as err:
        raise ProviderConnectionError(f'Cannot list {base}: {err}')

    entries = []
    for info in infos:
        name = os.path.basename(info['name'].rstrip('/'))
        if not name:
            continue
        href = None  # set by FileListingProvider._fetch_entries()
        mtime = info.get('mtime') or info.get('LastModified')
        if isinstance(mtime, (int, float)):
            mtime = datetime.fromtimestamp(mtime, tz=timezone.utc)
        entries.append({'name': name, 'href': href,
                        'size': info.get('size'), 'modified': mtime,
                        'is_dir': info.get('type') == 'directory'})
    return entries


class FileListingProvider(BaseProvider):
    """Directory/bucket listing exposed as OGC API - Features items"""

    def __init__(self, provider_def):
        super().__init__(provider_def)
        self.id_field = self.id_field or 'id'
        self.time_field = self.time_field or 'reference_time'
        self.media_type = (self.options or {}).get('media_type')
        self.storage_url = (self.options or {}).get('storage_url')
        self.cache_ttl = float((self.options or {}).get('cache_ttl', 60))
        self._st = _get_state((self.name, self.data, self.storage_url,
                               tuple(self.file_types)))
        # open the GRIB on /items/{id} to add geometry + message summary
        # 'extent' (default): read only the first GRIB message (~tens of KB)
        # 'full': download the whole file and also list variables/levels
        # false: do not open GRIB files
        mode = (self.options or {}).get('inspect_grib', 'extent')
        self.inspect_grib = {True: 'extent', False: None}.get(mode, mode)
        # decode BUFR files on /items/{id} for station locations (opt-in;
        # BUFR bulletins have no file extension to detect them by)
        self.inspect_bufr = bool((self.options or {}).get('inspect_bufr',
                                                          False))
        self.get_fields()
        if (self.options or {}).get('prewarm', True) \
                and self._st.snapshot is None and not self._st.refreshing:
            threading.Thread(target=self._refresh_quietly,
                             daemon=True).start()

    def get_fields(self):
        if not self._fields:
            self._fields = {
                'id': {'type': 'string'},
                'reference_time': {'type': 'string', 'format': 'date-time'},
                'forecast_hour': {'type': 'integer'},
                'size': {'type': 'integer'},
                'modified': {'type': 'string', 'format': 'date-time'},
            }
        return self._fields

    # -- helpers -----------------------------------------------------------

    def _fetch_entries(self):
        if urlparse(self.data).scheme in ('http', 'https'):
            entries = _parse_http_listing(self.data)
        else:
            entries = _list_fsspec(self.data)
        entries = [e for e in entries if not e['is_dir']]
        if self.file_types:
            entries = [e for e in entries
                       if e['name'].endswith(tuple(self.file_types))]
        for e in entries:
            e['href'] = self._storage_href(e['name'])
        return entries

    def _refresh(self):
        """Fetch the listing and build the sorted feature list"""
        entries = self._fetch_entries()
        features = [self._to_feature(e) for e in entries]
        features.sort(key=lambda f: (f['properties']['modified'] or '',
                                     f['id']), reverse=True)
        snap = (time.monotonic(), {e['name']: e for e in entries}, features)
        self._st.snapshot = snap
        return snap

    def _refresh_quietly(self):
        with self._st.lock:
            self._st.refreshing = True
            try:
                snap = self._st.snapshot
                if snap is not None and \
                        time.monotonic() - snap[0] < self.cache_ttl:
                    return  # another thread just refreshed
                self._refresh()
            except Exception as err:
                LOGGER.warning(f'Listing refresh failed for {self.data}: '
                               f'{err}')
            finally:
                self._st.refreshing = False

    def _get_snapshot(self, force=False):
        """
        Listing snapshot, served from memory.

        Stale data is returned immediately while a background thread
        refreshes it. Only the very first call (or ``force``) waits.
        """
        snap = self._st.snapshot
        if snap is not None and not force:
            if time.monotonic() - snap[0] > self.cache_ttl \
                    and not self._st.refreshing:
                self._st.refreshing = True  # claim before starting
                threading.Thread(target=self._refresh_quietly,
                                 daemon=True).start()
            return snap
        with self._st.lock:  # one fetch at a time; others reuse its result
            snap = self._st.snapshot
            if snap is None or (force and time.monotonic() - snap[0] > 1):
                snap = self._refresh()
            return snap

    def _storage_href(self, name):
        """Internal location of the bytes (never shown to clients)"""
        base = (self.storage_url or self.data).rstrip('/')
        return f'{base}/{quote(name)}'

    def _get_collection_id(self):
        """Find the collection this provider is configured under"""
        if self._st.collection_id is None:
            from pygeoapi.config import get_config
            self._st.collection_id = ''
            try:
                for cid, res in get_config().get('resources', {}).items():
                    for prov in res.get('providers', []):
                        if (prov.get('name') == self.name
                                and prov.get('data') == self.data):
                            self._st.collection_id = cid
            except Exception as err:
                LOGGER.warning(f'Cannot resolve collection id: {err}')
        return self._st.collection_id

    def _download_href(self, name):
        """Public URL of the download endpoint for an item"""
        cid = self._get_collection_id()
        if not cid:
            return None
        if self._st.server_url is None:
            from pygeoapi.config import get_config
            self._st.server_url = get_config()['server']['url'].rstrip('/')
        base = self._st.server_url
        return f'{base}/collections/{cid}/items/{quote(name)}/download'

    def media_type_for(self, name):
        ext = os.path.splitext(name)[1].lower()
        if ext not in MEDIA_TYPES and '.grib2' in name.lower():
            ext = '.grib2'  # e.g. .grib2_rmtn
        return (self.media_type or MEDIA_TYPES.get(ext)
                or mimetypes.guess_type(name)[0]
                or 'application/octet-stream')

    # -- file access (used by the download endpoint) -----------------------

    def open_item(self, identifier):
        """
        Locate a single file for download.

        ``identifier`` must be a single file name (no path separators),
        so only files directly under the configured storage location can
        be reached.

        :returns: dict with name, href, size, modified, media_type
        """
        if (not identifier or identifier in ('.', '..')
                or '/' in identifier or '\\' in identifier
                or '\x00' in identifier or identifier.startswith('.')):
            raise ProviderItemNotFoundError(f'Item not found: {identifier}')
        if self.file_types and not identifier.endswith(
                tuple(self.file_types)):
            raise ProviderItemNotFoundError(f'Item not found: {identifier}')

        href = self._storage_href(identifier)
        size = modified = None
        if urlparse(href).scheme in ('http', 'https'):
            import requests
            try:
                r = requests.head(href, timeout=30, allow_redirects=True)
            except requests.RequestException as err:
                raise ProviderConnectionError(f'Storage unavailable: {err}')
            if r.status_code in (403, 404):
                raise ProviderItemNotFoundError(
                    f'Item not found: {identifier}')
            if r.status_code >= 400:
                # some servers answer 500 for a missing file: only report a
                # storage failure if the file is really in the listing
                if identifier not in self._get_snapshot(force=True)[1]:
                    raise ProviderItemNotFoundError(
                        f'Item not found: {identifier}')
                raise ProviderConnectionError(
                    f'Storage returned {r.status_code}')
            size = int(r.headers['Content-Length']) \
                if 'Content-Length' in r.headers else None
            lm = r.headers.get('Last-Modified')
            if lm:
                from email.utils import parsedate_to_datetime
                modified = parsedate_to_datetime(lm)
        else:
            import fsspec
            try:
                fs, path = fsspec.core.url_to_fs(
                    f"{(self.storage_url or self.data).rstrip('/')}/"
                    f'{identifier}')
                info = fs.info(path)
            except FileNotFoundError:
                raise ProviderItemNotFoundError(
                    f'Item not found: {identifier}')
            except Exception as err:
                raise ProviderConnectionError(f'Storage unavailable: {err}')
            if info.get('type') == 'directory':
                raise ProviderItemNotFoundError(
                    f'Item not found: {identifier}')
            size = info.get('size')
            mtime = info.get('mtime') or info.get('LastModified')
            if isinstance(mtime, (int, float)):
                mtime = datetime.fromtimestamp(mtime, tz=timezone.utc)
            modified = mtime

        return {'name': identifier, 'href': href, 'size': size,
                'modified': modified,
                'media_type': self.media_type_for(identifier)}

    @staticmethod
    def iter_bytes(item, start=0, end=None, chunk_size=1 << 20):
        """Yield the bytes of ``item`` from ``start`` to ``end`` (inclusive)"""
        href = item['href']
        if urlparse(href).scheme in ('http', 'https'):
            import requests
            headers = {}
            if start or end is not None:
                headers['Range'] = f'bytes={start}-' + \
                    (str(end) if end is not None else '')
            with requests.get(href, headers=headers, stream=True,
                              timeout=60) as r:
                r.raise_for_status()
                if headers and r.status_code != 206:
                    # upstream ignored the Range header: slice ourselves
                    pos = 0
                    for chunk in r.iter_content(chunk_size):
                        lo = max(start - pos, 0)
                        hi = len(chunk) if end is None else min(
                            len(chunk), end + 1 - pos)
                        if hi > lo:
                            yield chunk[lo:hi]
                        pos += len(chunk)
                        if end is not None and pos > end:
                            break
                    return
                yield from r.iter_content(chunk_size)
        else:
            import fsspec
            with fsspec.open(href, 'rb') as src:
                if start:
                    src.seek(start)
                remaining = None if end is None else end - start + 1
                while remaining is None or remaining > 0:
                    n = chunk_size if remaining is None else min(
                        chunk_size, remaining)
                    chunk = src.read(n)
                    if not chunk:
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                    yield chunk

    def _to_feature(self, entry):
        name = entry['name']
        props = {'id': name, 'size': entry['size'],
                 'modified': entry['modified'].isoformat()
                 if entry['modified'] else None}
        m = NAME_RE.match(name)
        if m:
            run = datetime.strptime(m.group('run'), '%Y%m%d%H%M')
            props['reference_time'] = run.replace(
                tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')
            props['forecast_hour'] = int(m.group('fh'))
        else:
            props['reference_time'] = props['modified']

        href = self._download_href(name)
        if href is None:
            LOGGER.debug('Collection not found in config; '
                         'falling back to the storage location')
            href = entry['href']
        link = {'rel': 'enclosure', 'type': self.media_type_for(name),
                'title': name, 'href': href}
        if entry['size'] is not None:
            link['length'] = entry['size']
        return {'type': 'Feature', 'id': name, 'geometry': None,
                'properties': props, 'links': [link]}

    @staticmethod
    def _in_range(value, datetime_):
        if not datetime_ or not value:
            return True
        parts = datetime_.split('/')
        v = datetime.fromisoformat(value.replace('Z', '+00:00'))

        def parse(s):
            return datetime.fromisoformat(
                s.replace('Z', '+00:00')).astimezone(timezone.utc) \
                if s not in ('..', '') else None
        try:
            start = parse(parts[0])
            end = parse(parts[1]) if len(parts) > 1 else start
        except ValueError:
            raise ProviderQueryError('Invalid datetime')
        if len(parts) == 1:
            return v == start
        return (start is None or v >= start) and (end is None or v <= end)

    def _add_grib_info(self, feature, entry):
        """Read the GRIB file and add geometry/summary (best effort)"""
        key = (entry['name'], entry['size'], self.inspect_grib)
        info = self._st.grib_cache.get(key)
        if info is None:
            try:
                info = self._read_grib(entry)
            except Exception as err:
                LOGGER.warning(f"Could not read GRIB {entry['name']}: {err}")
                return
            if len(self._st.grib_cache) > 64:
                self._st.grib_cache.clear()
            self._st.grib_cache[key] = info
        feature['geometry'] = info['geometry']
        feature['properties'].update(info['properties'])

    def _add_bufr_info(self, feature, entry):
        """Decode the BUFR file and add station locations (best effort)"""
        key = ('bufr', entry['name'], entry['size'])
        if key in self._st.grib_cache:
            info = self._st.grib_cache[key]
        else:
            try:
                info = self._read_bufr(entry)
            except Exception as err:
                LOGGER.warning(f"Could not read BUFR {entry['name']}: {err}")
                return
            if len(self._st.grib_cache) > 64:
                self._st.grib_cache.clear()
            self._st.grib_cache[key] = info  # None = not a BUFR file
        if info is None:
            return
        feature['geometry'] = info['geometry']
        feature['properties'].update(info['properties'])

    def _read_bufr(self, entry):
        """
        Station locations from every BUFR message in the file.

        Bulletin files hold a WMO header followed by one or more BUFR
        messages. Files are small (KB to a few hundred KB), so the whole
        file is read.

        :returns: dict with geometry/properties, or None if no BUFR data
        """
        data = b''.join(self.iter_bytes(entry))
        messages = []
        pos = data.find(b'BUFR')
        while pos >= 0:
            length = int.from_bytes(data[pos + 4:pos + 7], 'big')
            if length < 8 or pos + length > len(data):
                break
            messages.append(data[pos:pos + length])
            pos = data.find(b'BUFR', pos + length)
        if not messages:
            LOGGER.debug(f"No BUFR message in {entry['name']}")
            return None

        points, stations = [], []
        subsets = 0
        categories = set()
        for msg in messages:
            m_points, m_stations, n, cat = self._decode_bufr_message(msg)
            points += m_points
            stations += m_stations
            subsets += n
            categories.add(cat)

        unique = list(dict.fromkeys(points))  # drop repeats, keep order
        if not unique:
            geometry = None
        elif len(unique) == 1:
            geometry = {'type': 'Point', 'coordinates': list(unique[0])}
        else:
            geometry = {'type': 'MultiPoint',
                        'coordinates': [list(p) for p in unique]}

        props = {
            'number_of_messages': len(messages),
            'number_of_subsets': subsets,
            'data_category': sorted(categories)[0] if len(categories) == 1
            else sorted(categories),
        }
        if stations:
            props['stations'] = list(
                {(s.get('wmo_id'), s.get('name')): s
                 for s in stations}.values())
        return {'geometry': geometry, 'properties': props}

    @staticmethod
    def _decode_bufr_message(msg):
        """(points, stations, number_of_subsets, data_category)"""
        import eccodes

        def valid(lat, lon):
            return (lat is not None and lon is not None
                    and -90 <= lat <= 90 and -180 <= lon <= 180)

        def get_array(h, key):
            try:
                return list(eccodes.codes_get_array(h, key))
            except eccodes.KeyValueNotFoundError:
                return []

        h = eccodes.codes_new_from_message(msg)
        try:
            n = int(eccodes.codes_get(h, 'numberOfSubsets'))
            category = int(eccodes.codes_get(h, 'dataCategory'))
            compressed = bool(eccodes.codes_get(h, 'compressedData'))
            eccodes.codes_set(h, 'unpack', 1)

            # One position per subset. Values can repeat within a subset
            # (e.g. radiosonde drift per level): use the first valid one.
            if compressed or n == 1:
                lats, lons = get_array(h, 'latitude'), get_array(h, 'longitude')
                if n == 1:
                    pairs = [next(((la, lo) for la, lo in zip(lats, lons)
                                   if valid(la, lo)), (None, None))]
                else:  # compressed: one value per subset, or a constant
                    if len(lats) == 1:
                        lats = lats * n
                    if len(lons) == 1:
                        lons = lons * n
                    pairs = list(zip(lats, lons)) if len(lats) == len(lons) \
                        == n else []
            else:
                pairs = []
                for i in range(1, n + 1):
                    lats = get_array(h, f'/subsetNumber={i}/latitude')
                    lons = get_array(h, f'/subsetNumber={i}/longitude')
                    pairs.append(next(((la, lo) for la, lo in zip(lats, lons)
                                       if valid(la, lo)), (None, None)))

            points = [(round(float(lo), 5), round(float(la), 5))
                      for la, lo in pairs if valid(la, lo)]

            # Station ids/names, only when they line up with the subsets
            blocks = get_array(h, 'blockNumber')
            numbers = get_array(h, 'stationNumber')
            names = get_array(h, 'stationOrSiteName')
            stations = []
            if len(blocks) == len(numbers) == n:
                for i in range(n):
                    b, st = int(blocks[i]), int(numbers[i])
                    if 0 <= b <= 99 and 0 <= st <= 999:
                        s = {'wmo_id': f'{b:02d}{st:03d}'}
                        if len(names) == n and str(names[i]).strip():
                            s['name'] = str(names[i]).strip()
                        stations.append(s)
            return points, stations, n, category
        finally:
            eccodes.codes_release(h)

    @staticmethod
    def _read_first_message(href, probe=65536):
        """
        Return the bytes of the first GRIB message in a file.

        Reads only the start of the file (HTTP Range or fsspec). Bulletin
        headers before the ``GRIB`` marker are skipped.
        """
        def read(start, end):  # inclusive range
            if urlparse(href).scheme in ('http', 'https'):
                import requests
                r = requests.get(href, headers={
                    'Range': f'bytes={start}-{end}'}, stream=True, timeout=60)
                try:
                    r.raise_for_status()
                    buf = b''.join(r.iter_content(1 << 16))
                    # upstream ignored Range and sent the whole body
                    return buf[start:end + 1] if r.status_code == 200 \
                        else buf
                finally:
                    r.close()
            import fsspec
            with fsspec.open(href, 'rb') as src:
                src.seek(start)
                return src.read(end - start + 1)

        head = read(0, probe - 1)
        i = head.find(b'GRIB')
        if i < 0:
            raise ValueError('no GRIB message found')
        edition = head[i + 7]
        if edition == 2:
            length = int.from_bytes(head[i + 8:i + 16], 'big')
        elif edition == 1:
            length = int.from_bytes(head[i + 4:i + 7], 'big')
        else:
            raise ValueError(f'unsupported GRIB edition {edition}')
        if i + length > len(head):
            head = read(0, i + length - 1)
        return head[i:i + length]

    def _read_grib(self, entry):
        import pygrib

        href = entry['href']
        if self.inspect_grib != 'full':
            first = pygrib.fromstring(self._read_first_message(href))
            return self._describe_grib(first, None)

        tmp = tempfile.NamedTemporaryFile(suffix='.grib2')
        try:
            if urlparse(href).scheme in ('http', 'https'):
                import requests
                with requests.get(href, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(1 << 20):
                        tmp.write(chunk)
            else:
                import fsspec
                with fsspec.open(href, 'rb') as src:
                    while chunk := src.read(1 << 20):
                        tmp.write(chunk)
            tmp.flush()
            grbs = pygrib.open(tmp.name)
            first = grbs.message(1)
            info = self._describe_grib(first, grbs)
            grbs.close()
        finally:
            tmp.close()
        return info

    @staticmethod
    def _describe_grib(first, grbs):
        """Geometry and summary from the first message (and all, if given)"""
        lats, lons = first.latlons()
        west, east = float(lons.min()), float(lons.max())
        south, north = float(lats.min()), float(lats.max())
        if east > 180:  # grid uses 0..360 longitudes
            if west >= 180:
                west, east = west - 360, east - 360
            else:  # global, or crosses the antimeridian
                west, east = -180.0, 180.0
        geometry = {'type': 'Polygon', 'coordinates': [[
            [west, south], [east, south], [east, north],
            [west, north], [west, south]]]}

        try:
            grid_size = [int(first['Ni']), int(first['Nj'])]
            if grid_size[0] > 1 << 30:  # missing (reduced grids)
                raise KeyError
        except (KeyError, RuntimeError):
            grid_size = None
        ref = datetime.strptime(
            f"{int(first['dataDate']):08d}{int(first['dataTime']):04d}",
            '%Y%m%d%H%M')
        valid = ref + timedelta(hours=int(first['endStep']))
        props = {
            'grid_type': first.gridType,
            'grid_size': grid_size,
            'number_of_points': int(first['numberOfPoints']),
            'valid_time': valid.replace(
                tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z'),
        }
        if grbs is not None:
            variables = {}
            grbs.seek(0)
            for m in grbs:
                v = variables.setdefault(m.shortName, {
                    'name': m.name, 'levels': []})
                lvl = f'{m.level} {m.typeOfLevel}'
                if lvl not in v['levels']:
                    v['levels'].append(lvl)
            props['message_count'] = grbs.messages
            props['variables'] = variables
        return {'geometry': geometry, 'properties': props}

    # -- provider API ------------------------------------------------------

    def query(self, offset=0, limit=10, resulttype='results',
              bbox=[], datetime_=None, properties=[], sortby=[],
              select_properties=[], skip_geometry=False, q=None, **kwargs):
        features = self._get_snapshot()[2]  # newest modified first
        if datetime_:
            features = [f for f in features
                        if self._in_range(f['properties']['reference_time'],
                                          datetime_)]
        for key, val in properties:
            features = [f for f in features
                        if str(f['properties'].get(key)) == str(val)]
        if q:
            features = [f for f in features if q in f['id']]
        if sortby:
            features = list(features)
            for sb in reversed(sortby):
                features.sort(
                    key=lambda f: (f['properties'].get(sb['property'])
                                   is None,
                                   f['properties'].get(sb['property'])),
                    reverse=sb['order'] == '-')
        matched = len(features)
        if resulttype == 'hits':
            return {'type': 'FeatureCollection', 'features': [],
                    'numberMatched': matched}

        page = deepcopy(features[offset:offset + limit])
        if select_properties:
            for f in page:
                f['properties'] = {k: v for k, v in f['properties'].items()
                                   if k in select_properties}
        return {'type': 'FeatureCollection', 'features': page,
                'numberMatched': matched, 'numberReturned': len(page)}

    def get(self, identifier, **kwargs):
        entry = self._get_snapshot()[1].get(identifier)
        if entry is None:  # may be new since the last refresh
            entry = self._get_snapshot(force=True)[1].get(identifier)
        if entry is not None:
            feature = self._to_feature(entry)
            if self.inspect_grib and '.grib' in identifier.lower():
                self._add_grib_info(feature, entry)
            elif self.inspect_bufr:
                self._add_bufr_info(feature, entry)
            return feature
        raise ProviderItemNotFoundError(f'Item not found: {identifier}')

    def __repr__(self):
        return f'<FileListingProvider> {self.data}'
