#!/usr/bin/env python3
"""Build the station tables used by ``wmo_text.py``.

Writes, into ``--out`` (default: this directory):

* ``wmo_stations.csv``  WMO block + station number -> position
* ``icao_stations.csv`` ICAO location indicator -> position
* ``tables_info.json``  when and from where they were built

Sources, in priority order:

WMO ids
    1. WMO OSCAR/Surface (WMO's official station registry): stations with a
       WIGOS id ``0-20000-0-<5 digits>``, primary ids and operational
       stations preferred. An id that OSCAR lists only as a station's
       secondary WIGOS id is dropped if another source puts that station
       more than 500 km away (e.g. ``07034``, a French index number listed
       on an Australian radar).
    2. NOAA Aviation Weather Center station list (``wmoId`` field)
    3. NWS ``nsd_bbsss.txt`` (2016)
ICAO ids
    1. NOAA Aviation Weather Center station list
    2. NWS ``nsd_cccc.txt`` (2018)

Both online sources (OSCAR/Surface and AWC) must download successfully;
otherwise nothing is written and the exit status is 1, so a failed refresh
never replaces existing tables with worse ones.

Usage: python3 refresh.py [--out DIR] [--src DIR]
"""

import argparse
import csv
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OSCAR_URL = 'https://oscar.wmo.int/surface/rest/api/search/station'
AWC_URL = 'https://aviationweather.gov/data/cache/stations.cache.json.gz'
USER_AGENT = 'ra4-pygeoapi station table refresh'

WSI_RE = re.compile(r'^0-20000-0-(\d{5})$')
ICAO_RE = re.compile(r'^[A-Z][A-Z0-9]{3}$')
DMS_RE = re.compile(r'^(\d{1,3})-(\d{1,2})(?:-(\d{1,2}))?([NSEW])$')


def log(msg):
    print(f'[station tables] {msg}', file=sys.stderr, flush=True)


def fetch(url, timeout, retries=3):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as err:
            log(f'{url}: attempt {attempt}/{retries} failed: {err}')
            if attempt < retries:
                time.sleep(10 * attempt)
    raise RuntimeError(f'could not download {url}')


def valid(lat, lon):
    return (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            and -90 <= lat <= 90 and -180 <= lon <= 180
            and not (lat == 0 and lon == 0))


# -- sources ------------------------------------------------------------------

def oscar_wmo():
    """WMO OSCAR/Surface: {wmo_id: (name, lat, lon, is_primary_id)}"""
    best = {}  # wmo_id -> (rank, name, lat, lon, is_primary_id)
    page, pages = 1, 1
    while page <= pages:
        log(f'OSCAR/Surface page {page}')
        # the API pages with `page` (50,000 stations per page)
        data = json.loads(fetch(f'{OSCAR_URL}?page={page}', timeout=900))
        pages = int(data.get('pageCount') or 1)
        for s in data.get('stationSearchResults') or []:
            lat, lon = s.get('latitude'), s.get('longitude')
            if not valid(lat, lon):
                continue
            operational = s.get('stationStatusCode') == 'operational'
            ids = [(w.get('wigosStationIdentifier'), bool(w.get('primary')))
                   for w in s.get('wigosStationIdentifiers') or []]
            for wsi, primary in ids or [(s.get('wigosId'), True)]:
                m = WSI_RE.match(wsi or '')
                if not m:
                    continue
                rank = (not primary, not operational)
                if m.group(1) not in best or rank < best[m.group(1)][0]:
                    best[m.group(1)] = (rank, (s.get('name') or '').strip(),
                                        lat, lon, primary)
        page += 1
    return {k: v[1:] for k, v in best.items()}


def awc():
    """NOAA AWC station list: ({icao: (name, lat, lon)}, {wmo: ...})"""
    log('AWC station list')
    stations = json.loads(gzip.decompress(fetch(AWC_URL, timeout=300)))
    icao, wmo = {}, {}
    for s in stations:
        lat, lon = s.get('lat'), s.get('lon')
        if not valid(lat, lon):
            continue
        entry = ((s.get('site') or '').strip(), lat, lon)
        if ICAO_RE.match(s.get('icaoId') or ''):
            icao.setdefault(s['icaoId'], entry)
        if re.fullmatch(r'\d{5}', str(s.get('wmoId') or '')):
            wmo.setdefault(str(s['wmoId']), entry)
    return icao, wmo


def _dms(value, hemispheres):
    m = DMS_RE.match(value.strip())
    if not m or m.group(4) not in hemispheres:
        return None
    deg = int(m.group(1)) + int(m.group(2)) / 60 + int(m.group(3) or 0) / 3600
    return -deg if m.group(4) in 'SW' else deg


def _km(a, b):
    dx = (a[1] - b[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, a[0] - b[0]) * 111.2


def _nws_position(f):
    """
    Fields 7/8 are degrees and minutes; 9/10 are more precise but wrong in
    some rows, so they are used only within 25 km of 7/8.
    """
    def pos(i, j):
        if len(f) <= j:
            return None
        lat, lon = _dms(f[i], 'NS'), _dms(f[j], 'EW')
        return (lat, lon) if lat is not None and lon is not None \
            and valid(lat, lon) else None
    coarse, fine = pos(7, 8), pos(9, 10)
    if coarse and fine:
        return fine if _km(coarse, fine) <= 25 else coarse
    return coarse or fine


def nws(src):
    """NWS nsd tables: ({icao: (name, lat, lon)}, {wmo: ...})"""
    def rows(name):
        with open(os.path.join(src, name), encoding='latin-1') as f:
            for line in f:
                f_ = [x.strip() for x in line.rstrip('\r\n').split(';')]
                if len(f_) >= 9:
                    yield f_
    icao, wmo = {}, {}
    for f in rows('nsd_cccc.txt'):
        p = _nws_position(f)
        if p and ICAO_RE.match(f[0]):
            icao.setdefault(f[0], (f[3], *p))
    for f in rows('nsd_bbsss.txt'):
        p = _nws_position(f)
        if p and re.fullmatch(r'\d{5}', f[0] + f[1]):
            wmo.setdefault(f[0] + f[1], (f[3], *p))
    return icao, wmo


# -- build --------------------------------------------------------------------

def merge(*sources):
    """[(source name, {id: (name, lat, lon)}), ...] in priority order"""
    out = {}
    for label, table in sources:
        for ident, (name, lat, lon) in table.items():
            out.setdefault(ident, (name, round(lat, 5), round(lon, 5), label))
    return out


def write_csv(path, id_field, table):
    tmp = path + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([id_field, 'name', 'lat', 'lon', 'source'])
        for ident in sorted(table):
            w.writerow([ident, *table[ident]])
    os.replace(tmp, path)  # never leave a half-written table


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', default=HERE)
    ap.add_argument('--src', default=HERE,
                    help='directory with nsd_cccc.txt and nsd_bbsss.txt')
    args = ap.parse_args()

    status = {}
    try:
        oscar = oscar_wmo()
        status['OSCAR/Surface'] = f'ok, {len(oscar)} WMO ids'
    except Exception as err:
        oscar, status['OSCAR/Surface'] = {}, f'failed: {err}'
    try:
        awc_icao, awc_wmo = awc()
        status['AWC'] = f'ok, {len(awc_icao)} ICAO ids, {len(awc_wmo)} WMO ids'
    except Exception as err:
        awc_icao, awc_wmo, status['AWC'] = {}, {}, f'failed: {err}'
    nws_icao, nws_wmo = nws(args.src)
    status['NWS'] = f'{len(nws_icao)} ICAO ids, {len(nws_wmo)} WMO ids'
    for k, v in status.items():
        log(f'{k}: {v}')

    if not oscar or not awc_icao:
        log('an online source is unavailable; tables left unchanged')
        return 1

    # drop secondary-id OSCAR matches that another source contradicts
    dropped = []
    for ident, (name, lat, lon, primary) in list(oscar.items()):
        other = awc_wmo.get(ident) or nws_wmo.get(ident)
        if not primary and other and \
                _km((lat, lon), (other[1], other[2])) > 500:
            dropped.append(f'{ident} ({name})')
            del oscar[ident]
    if dropped:
        log(f'OSCAR secondary ids dropped (>500 km from other sources): '
            f'{", ".join(dropped)}')
    status['OSCAR/Surface'] += f', {len(dropped)} secondary ids dropped'
    oscar = {k: v[:3] for k, v in oscar.items()}

    wmo = merge(('WMO OSCAR/Surface', oscar), ('NOAA AWC', awc_wmo),
                ('NWS nsd_bbsss', nws_wmo))
    icao = merge(('NOAA AWC', awc_icao), ('NWS nsd_cccc', nws_icao))
    os.makedirs(args.out, exist_ok=True)
    write_csv(os.path.join(args.out, 'wmo_stations.csv'), 'wmo_id', wmo)
    write_csv(os.path.join(args.out, 'icao_stations.csv'), 'icao_id', icao)

    def counts(table):
        c = {}
        for v in table.values():
            c[v[3]] = c.get(v[3], 0) + 1
        return c
    info = {
        'built': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'sources': status,
        'wmo_stations': {'total': len(wmo), 'by_source': counts(wmo)},
        'icao_stations': {'total': len(icao), 'by_source': counts(icao)},
    }
    with open(os.path.join(args.out, 'tables_info.json'), 'w') as f:
        json.dump(info, f, indent=2)
    log(f"wrote {len(wmo)} WMO and {len(icao)} ICAO stations to {args.out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
