"""Locations from WMO text bulletins.

Turns the reports in a text bulletin into positions, using either

* a station identifier looked up in ``station_tables/wmo_stations.csv`` and
  ``icao_stations.csv`` (built by ``station_tables/refresh.py`` from WMO
  OSCAR/Surface, the NOAA AWC station list and NWS tables), or
* a position coded in the report itself (ships, buoys, aircraft).

Supported, by the T1T2 of the abbreviated heading:

=========================  =============================================
SA, SP                     METAR / SPECI: ICAO identifier
FT, FC                     TAF: ICAO identifier
SM, SI, SN, SO, SS         SYNOP land (AAXX): WMO station;
                           SHIP / mobile (BBXX, OOXX): coded position;
                           BUOY, TESAC, BATHY (ZZYY, KKYY, JJVV, ...):
                           coded position
U* (TEMP / PILOT parts)    TT.., PP..: WMO station;
                           UU.., XX.., II..: coded position
UD, UR                     aircraft / reconnaissance: positions in
                           degrees and minutes (``2639N 12324E``)
TC                         satellite cloud reports: ICAO station per line
TP                         tropical cyclone fixes: ``C. 13.01N`` /
                           ``D. 86.78E`` lines
=========================  =============================================

Anything else returns ``None``. Only standard report layouts are parsed;
free text is never searched for station identifiers.
"""

import csv
import logging
import os
import re

LOGGER = logging.getLogger(__name__)

DEFAULT_TABLE_DIR = os.path.join(os.path.dirname(__file__), 'station_tables')

HEADING_RE = re.compile(
    r'^([A-Z]{4})(\d{2}) ([A-Z]{4}) (\d{6})(?: [A-Z]{3})?[ \t\r]*$', re.M)
ICAO_RE = re.compile(r'^[A-Z][A-Z0-9]{3}$')
WMO_RE = re.compile(r'^\d{5}$')
SHIP_POS_RE = re.compile(r'^99(\d{3})$')
QC_LON_RE = re.compile(r'^([1357])(\d{4})$')
TEMP_PART_RE = re.compile(r'^(TT|PP|UU|XX|II|QQ|EE)(AA|BB|CC|DD)$')
DM_POS_RE = re.compile(
    r'(?<![\w.])(\d{2})(\d{2})([NS])\s+(\d{3})(\d{2})([EW])(?![\w.])')
TC_LINE_RE = re.compile(r'^([A-Z][A-Z0-9]{3})\s+\d{2}/\d{4}\b', re.M)
TP_LAT_RE = re.compile(r'^\s*C\.\s*(\d{1,2}(?:\.\d+)?)\s*([NS])\s*$', re.M)
TP_LON_RE = re.compile(r'^\s*D\.\s*(\d{1,3}(?:\.\d+)?)\s*([EW])\s*$', re.M)

REPORT_KEYWORDS = {'METAR', 'SPECI', 'TAF', 'AMD', 'COR', 'CNL', 'RTD'}
TIME_RE = re.compile(r'^(\d{6}Z|NIL|\d{4}/\d{4})$')
OCEAN_KEYWORDS = {
    # keyword: (index of Qc+latitude token, index of longitude token)
    'ZZYY': (4, 5),
    'KKYY': (3, 4), 'KKXX': (3, 4),
    'JJVV': (3, 4), 'JJYY': (3, 4), 'JJXX': (3, 4),
}

_TABLES = {}


# -- station tables ---------------------------------------------------------

def load_tables(table_dir=DEFAULT_TABLE_DIR):
    """
    Load ``wmo_stations.csv`` and ``icao_stations.csv`` (cached per directory).

    :returns: dict with ``icao`` and ``wmo`` lookups. Each maps an
              identifier to ``{'name', 'pos': (lon, lat), 'source'}``.
    """
    if table_dir in _TABLES:
        return _TABLES[table_dir]

    def read(name, id_field):
        out = {}
        with open(os.path.join(table_dir, name), newline='',
                  encoding='utf-8') as f:
            for row in csv.DictReader(f):
                out[row[id_field]] = {
                    'name': row['name'],
                    'pos': (round(float(row['lon']), 5),
                            round(float(row['lat']), 5)),
                    'source': row['source']}
        return out

    tables = {'icao': read('icao_stations.csv', 'icao_id'),
              'wmo': read('wmo_stations.csv', 'wmo_id')}
    LOGGER.debug(f"Station tables: {len(tables['icao'])} ICAO, "
                 f"{len(tables['wmo'])} WMO")
    _TABLES[table_dir] = tables
    return tables


# -- helpers ----------------------------------------------------------------

def _bulletins(text):
    """Yield (T1T2, body) for each bulletin in the text"""
    heads = list(HEADING_RE.finditer(text))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        yield h.group(1)[:2], text[h.end():end]


def _is_binary(body):
    sample = body[:2000]
    if not sample:
        return False
    bad = sum(1 for c in sample if ord(c) < 32 and c not in '\r\n\t\x03')
    return bad / len(sample) > 0.05


def _reports(body):
    """Reports in a bulletin body: token lists split on '='"""
    flat = body.replace('\r', ' ').replace('\n', ' ').replace('\x03', ' ')
    return [r.split() for r in flat.split('=') if r.split()]


def _coded_position(lat_tok, lon_tok):
    """``99LaLaLa`` + ``QcLoLoLoLo`` (SYNOP SHIP, TEMP SHIP) -> (lon, lat)"""
    m1, m2 = SHIP_POS_RE.match(lat_tok), QC_LON_RE.match(lon_tok)
    if not (m1 and m2):
        return None
    lat, lon = int(m1.group(1)) / 10, int(m2.group(2)) / 10
    return _quadrant(m2.group(1), lat, lon)


def _quadrant(qc, lat, lon):
    if lat > 90 or lon > 180:
        return None
    if qc in '35':
        lat = -lat
    if qc in '57':
        lon = -lon
    return (round(lon, 4), round(lat, 4))


def _ocean_position(lat_tok, lon_tok):
    """``QcLaLaLaLa[La]`` + ``LoLoLoLoLo[Lo]`` (FM 18/63/64) -> (lon, lat)"""
    if not (re.fullmatch(r'[1357]\d{4,5}', lat_tok)
            and re.fullmatch(r'\d{5,6}', lon_tok)):
        return None
    lat_scale = 1000 if len(lat_tok) == 6 else 100
    lon_scale = 1000 if len(lon_tok) == 6 else 100
    return _quadrant(lat_tok[0], int(lat_tok[1:]) / lat_scale,
                     int(lon_tok) / lon_scale)


class _Result:
    def __init__(self):
        self.points = []
        self.stations = {}
        self.unlocated = []
        self.sources = set()
        self.reports = 0

    def station(self, table, kind, ident):
        e = table.get(ident)
        if e is None:
            if ident not in self.unlocated:
                self.unlocated.append(ident)
            return
        self.points.append(e['pos'])
        key = 'icao_id' if kind == 'icao' else 'wmo_id'
        self.stations.setdefault(ident, {key: ident, 'name': e['name'],
                                         'source': e['source']})
        self.sources.add('station table')

    def position(self, pos):
        if pos:
            self.points.append(pos)
            self.sources.add('reported position')


# -- parsers by product -----------------------------------------------------

def _icao_reports(body, tables, res):
    for r in _reports(body):
        while r and r[0] in REPORT_KEYWORDS:
            r = r[1:]
        if len(r) >= 2 and ICAO_RE.match(r[0]) and TIME_RE.match(r[1]):
            res.reports += 1
            res.station(tables['icao'], 'icao', r[0])


def _synop(body, tables, res):
    mode = None
    for r in _reports(body):
        if r[0] in OCEAN_KEYWORDS:
            lat_i, lon_i = OCEAN_KEYWORDS[r[0]]
            if len(r) > lon_i:
                res.reports += 1
                res.position(_ocean_position(r[lat_i], r[lon_i]))
            continue
        if r[0] == 'AAXX':
            mode, r = 'land', r[2:]  # drop AAXX and YYGGiw
        elif r[0] in ('BBXX', 'OOXX'):
            mode, r = 'ship', r[1:]
        if not r or r[0] == 'NIL' or (len(r) > 1 and r[1] == 'NIL'):
            continue
        if mode == 'land' and WMO_RE.match(r[0]):
            res.reports += 1
            res.station(tables['wmo'], 'wmo', r[0])
        elif mode == 'ship' and len(r) >= 4:
            # call sign, YYGGiw, 99LaLaLa, QcLoLoLoLo
            res.reports += 1
            res.position(_coded_position(r[2], r[3]))


def _temp(body, tables, res):
    for r in _reports(body):
        # some centres prefix each report with the station number:
        # "78866 TTDD 6912/ 78866 ..."
        if len(r) > 1 and WMO_RE.match(r[0]) and TEMP_PART_RE.match(r[1]):
            r = r[1:]
        m = TEMP_PART_RE.match(r[0])
        if not m or len(r) < 3 or 'NIL' in r[:4]:
            continue
        res.reports += 1
        if m.group(1) in ('TT', 'PP'):
            if WMO_RE.match(r[2]):
                res.station(tables['wmo'], 'wmo', r[2])
        elif len(r) >= 4:
            res.position(_coded_position(r[2], r[3]))


def _aircraft(body, tables, res):
    for m in DM_POS_RE.finditer(body):
        la_d, la_m, ns, lo_d, lo_m, ew = m.groups()
        if int(la_m) >= 60 or int(lo_m) >= 60:
            continue
        lat = int(la_d) + int(la_m) / 60
        lon = int(lo_d) + int(lo_m) / 60
        if lat > 90 or lon > 180:
            continue
        res.reports += 1
        res.position((round(-lon if ew == 'W' else lon, 4),
                      round(-lat if ns == 'S' else lat, 4)))


def _sat_cloud(body, tables, res):
    for m in TC_LINE_RE.finditer(body):
        res.reports += 1
        res.station(tables['icao'], 'icao', m.group(1))


def _tc_fix(body, tables, res):
    for la, lo in zip(TP_LAT_RE.finditer(body), TP_LON_RE.finditer(body)):
        lat, lon = float(la.group(1)), float(lo.group(1))
        if lat > 90 or lon > 180:
            continue
        res.reports += 1
        res.position((round(-lon if lo.group(2) == 'W' else lon, 4),
                      round(-lat if la.group(2) == 'S' else lat, 4)))


def _parser(t1t2):
    if t1t2 in ('SA', 'SP', 'FT', 'FC'):
        return _icao_reports
    if t1t2 in ('SM', 'SI', 'SN', 'SO', 'SS'):
        return _synop
    if t1t2 in ('UD', 'UR'):
        return _aircraft
    if t1t2[0] == 'U':
        return _temp
    if t1t2 == 'TC':
        return _sat_cloud
    if t1t2 == 'TP':
        return _tc_fix
    return None


# -- entry point -------------------------------------------------------------

def locate(text, table_dir=DEFAULT_TABLE_DIR):
    """
    Locations of the reports in a WMO text bulletin file.

    :param text: file contents (one or more bulletins)
    :returns: ``None`` if no supported bulletin is found, otherwise a dict:
              ``points`` (unique (lon, lat) tuples), ``stations``,
              ``unlocated_stations``, ``location_source``,
              ``number_of_reports``
    """
    res = None
    for t1t2, body in _bulletins(text):
        parse = _parser(t1t2)
        if parse is None or _is_binary(body):
            continue
        if res is None:
            res = _Result()
        parse(body, load_tables(table_dir), res)
    if res is None:
        return None
    return {
        'points': list(dict.fromkeys(res.points)),
        'stations': list(res.stations.values()),
        'unlocated_stations': res.unlocated,
        'location_source': sorted(res.sources),
        'number_of_reports': res.reports,
    }
