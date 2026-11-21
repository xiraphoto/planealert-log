#!/usr/bin/env python3
"""PlaneAlert cloud logger.

Runs on GitHub Actions every few minutes. Asks the free ADS-B feeds (adsb.lol,
adsb.fi) what is around each watched airport and keeps a log of today's
arrivals and departures. The PlaneAlert app on the PC downloads this log and
merges it, so the day list is complete even while the PC was off.

Usage: python3 logger.py OUT_DIR
Reads  OUT_DIR/day-YYYY-MM-DD.json.gz (if present), writes it back updated.
Standard library only.
"""
import csv
import datetime as dt
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
UA = 'PlaneAlert-cloud-logger/1.0 (personal hobby logger)'
TZ_NAME = os.environ.get('PLANEALERT_TZ', 'Europe/Oslo')
TZ = ZoneInfo(TZ_NAME)
RADIUS_NM = 60       # how far around each watched airport to look
CONTEXT_NM = 50      # nearby airports considered so traffic is not mis-assigned
ROUTE_MAX_AGE_MIN = 120


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- geo

def dist_nm(lat1, lon1, lat2, lon2):
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angle_diff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


# ---------------------------------------------------------------- reference data

def load_airports():
    db, iata = {}, {}
    with open(os.path.join(HERE, 'airports.csv'), encoding='utf-8') as f:
        for line in f:
            p = line.rstrip('\n').split(',')
            if len(p) < 6:
                continue
            n = len(p)
            try:
                a = {'icao': p[0], 'iata': p[1], 'name': ','.join(p[2:n - 3]).strip('"'),
                     'country': p[n - 3], 'lat': float(p[n - 2]), 'lon': float(p[n - 1])}
            except ValueError:
                continue
            db[p[0]] = a
            if p[1]:
                iata[p[1]] = p[0]
    return db, iata


def load_runways(icaos):
    rw = {}
    with open(os.path.join(HERE, 'runways.csv'), encoding='utf-8') as f:
        for line in f:
            p = line.rstrip('\n').split(',')
            if len(p) < 7 or p[0] not in icaos:
                continue
            try:
                la1, lo1, la2, lo2 = float(p[2]), float(p[3]), float(p[5]), float(p[6])
            except ValueError:
                continue
            rw.setdefault(p[0], []).append({'id': p[1], 'lat': la1, 'lon': lo1, 'hdg': bearing(la1, lo1, la2, lo2)})
            rw[p[0]].append({'id': p[4], 'lat': la2, 'lon': lo2, 'hdg': bearing(la2, lo2, la1, lo1)})
    return rw


def load_watch(db, iata):
    out = []
    with open(os.path.join(HERE, 'watch.txt'), encoding='utf-8') as f:
        for line in f:
            code = line.split('#')[0].strip().upper()
            if not code:
                continue
            icao = code if code in db else iata.get(code)
            if icao and icao not in out:
                out.append(icao)
            elif not icao:
                log(f'watch.txt: unknown airport {code}')
    return out


# ---------------------------------------------------------------- http

def fetch_json(url, body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, 'Accept': 'application/json'})
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8', 'replace'))
        except urllib.error.HTTPError as e:
            log(f'HTTP {e.code} {url}')
            if e.code in (401, 403, 404):
                return None
        except Exception as e:  # network hiccup: retry once
            log(f'{type(e).__name__} {url}: {e}')
        time.sleep(2)
    return None


SOURCES = [
    ('adsbLol', 'https://api.adsb.lol/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{nm}', 0.4),
    ('adsbFi', 'https://opendata.adsb.fi/api/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{nm}', 1.1),
]


def ac_record(x, src):
    hex_ = str(x.get('hex') or '').strip().lower()
    if not hex_ or hex_.startswith('~'):
        return None
    lat, lon, seen = x.get('lat'), x.get('lon'), x.get('seen_pos')
    if lat is None and isinstance(x.get('lastPosition'), dict):
        lp = x['lastPosition']
        lat, lon, seen = lp.get('lat'), lp.get('lon'), lp.get('seen_pos')
    if lat is None or lon is None:
        return None
    if seen is None:
        seen = x.get('seen') or 0
    if float(seen) > 300:
        return None
    cat = str(x.get('category') or '')
    reg = str(x.get('r') or '').strip()
    typ = str(x.get('t') or '').strip().upper()
    cs = str(x.get('flight') or '').strip().upper()
    if cat.startswith('C'):
        return None
    if typ in ('TWR', 'GND', 'TOWER', 'GROUND', 'SERV') or cs in ('TWR', 'GND', 'TOWER') or reg == 'TWR':
        return None
    gnd, alt = False, None
    ab = x.get('alt_baro')
    if isinstance(ab, str):
        if ab == 'ground':
            gnd, alt = True, 0.0
    elif ab is not None:
        alt = float(ab)
    elif x.get('alt_geom') is not None:
        alt = float(x['alt_geom'])
    vs = x.get('baro_rate', x.get('geom_rate'))
    trk = x.get('track')
    if trk is None:
        trk = x.get('true_heading', x.get('mag_heading'))
    return {'hex': hex_, 'reg': reg, 'type': typ, 'desc': str(x.get('desc') or ''), 'op': str(x.get('ownOp') or ''),
            'year': str(x.get('year') or ''), 'cs': cs, 'lat': float(lat), 'lon': float(lon), 'alt': alt, 'gnd': gnd,
            'gs': float(x['gs']) if x.get('gs') is not None else None,
            'vs': float(vs) if vs is not None else None,
            'trk': float(trk) if trk is not None else None,
            'flags': int(x.get('dbFlags') or 0), 'cat': cat, 'seen': float(seen), 'src': {src},
            'orig': '', 'dest': '', 'legs': 0, 'sure': False}


def merge_ac(all_, rec):
    e = all_.get(rec['hex'])
    if not e:
        all_[rec['hex']] = rec
        return
    for k in ('reg', 'type', 'desc', 'op', 'year', 'cs', 'cat'):
        if not e[k] and rec[k]:
            e[k] = rec[k]
    if not e['flags'] and rec['flags']:
        e['flags'] = rec['flags']
    if rec['seen'] < e['seen']:
        for k in ('lat', 'lon', 'alt', 'gnd', 'seen', 'gs', 'vs', 'trk'):
            if rec[k] is not None:
                e[k] = rec[k]
    for k in ('gs', 'vs', 'trk', 'alt'):
        if e[k] is None and rec[k] is not None:
            e[k] = rec[k]
    e['src'] |= rec['src']


def scan(watch_apts):
    all_, calls_ok = {}, 0
    for name, tpl, pause in SOURCES:
        for p in watch_apts:
            r = fetch_json(tpl.format(lat=p['lat'], lon=p['lon'], nm=RADIUS_NM))
            if r:
                calls_ok += 1
                for x in (r.get('ac') or r.get('aircraft') or []):
                    rec = ac_record(x, name)
                    if rec:
                        merge_ac(all_, rec)
            time.sleep(pause)
    return all_, calls_ok


# ---------------------------------------------------------------- routes (adsb.lol routeset)

def set_routes(acs, day, now):
    cache = day.setdefault('routes', {})
    need = []
    for a in acs:
        if not re.match(r'^[A-Z]{3}\d', a['cs']):
            continue
        c = cache.get(a['cs'])
        if c and (now.timestamp() - c['t']) / 60 < ROUTE_MAX_AGE_MIN:
            continue
        need.append(a)
    for i in range(0, len(need), 40):
        batch = need[i:i + 40]
        planes = [{'callsign': a['cs'], 'lat': round(a['lat'], 4), 'lng': round(a['lon'], 4)} for a in batch]
        res = fetch_json('https://api.adsb.lol/api/0/routeset', {'planes': planes})
        if not isinstance(res, list):
            continue
        for r in res:
            cs = str(r.get('callsign') or '').strip().upper()
            if not cs:
                continue
            ent = {'t': now.timestamp(), 'orig': '', 'dest': '', 'legs': 0, 'sure': False}
            codes = str(r.get('airport_codes') or '')
            if codes and codes != 'unknown' and '-' in codes:
                parts = codes.upper().split('-')
                ent.update(orig=parts[0], dest=parts[-1], legs=len(parts), sure=r.get('plausible') in (True, 1))
            cache[cs] = ent
        time.sleep(0.3)
    for a in acs:
        c = cache.get(a['cs'])
        if c and c['dest']:
            a.update(orig=c['orig'], dest=c['dest'], legs=c['legs'], sure=c['sure'])
    # forget routes not used for a while
    for cs in [k for k, v in cache.items() if (now.timestamp() - v['t']) > 6 * 3600]:
        del cache[cs]


# ---------------------------------------------------------------- movement (same rules as the PC app)

def runway_end(a, p, mode, runways):
    ends = runways.get(p['icao'])
    if not ends or a['trk'] is None:
        return None
    best, best_x = None, 99.0
    for e in ends:
        if angle_diff(a['trk'], e['hdg']) > 15:
            continue
        d = dist_nm(a['lat'], a['lon'], e['lat'], e['lon'])
        if mode == 'arr':
            if d > 16:
                continue
            brg, lim = bearing(a['lat'], a['lon'], e['lat'], e['lon']), 30
        else:
            if d > 12:
                continue
            brg, lim = bearing(e['lat'], e['lon'], a['lat'], a['lon']), 40
        off = angle_diff(brg, e['hdg'])
        if d > 0.5 and off > lim:
            continue
        x = abs(d * math.sin(math.radians(off)))
        if x <= 0.8 and x < best_x:
            best_x, best = x, {'id': e['id'], 'dt': d}
    return best


def movement(a, p, d, now, runways):
    alt = a['alt'] if a['alt'] is not None else 99999
    gs = a['gs']
    if d <= 3 and (a['gnd'] or (gs is not None and gs < 45 and alt < 2000)):
        return {'kind': 'ground', 'score': 60, 'strong': True, 'rwy': '', 'eta': None}
    brg_to = bearing(a['lat'], a['lon'], p['lat'], p['lon'])
    toward = away = False
    if a['trk'] is not None:
        toward = angle_diff(a['trk'], brg_to) <= 35
        away = angle_diff(a['trk'], (brg_to + 180) % 360) <= 50
    desc = a['vs'] is not None and a['vs'] <= -300
    climb = a['vs'] is not None and a['vs'] >= 300
    route_to, route_from = a['dest'] == p['icao'], a['orig'] == p['icao']
    m = None
    if not climb and alt <= 5500 and d <= 18:
        rw = runway_end(a, p, 'arr', runways)
        if rw:
            m = {'kind': 'landing', 'score': 52, 'strong': True, 'rwy': rw['id'], 'eta': None}
            if gs and gs > 60:
                m['eta'] = now + dt.timedelta(minutes=rw['dt'] / gs * 60)
    if not m and climb and alt <= 9000 and d <= 14:
        rw = runway_end(a, p, 'dep', runways)
        if rw:
            m = {'kind': 'departed', 'score': 25, 'strong': True, 'rwy': rw['id'], 'eta': None}
    if not m:
        if d <= 10 and alt <= 4000 and not climb and (toward or d <= 4):
            m = {'kind': 'landing', 'score': 50, 'strong': True}
        elif d <= 40 and alt <= 12000 and desc and toward:
            m = {'kind': 'inbound', 'score': 40, 'strong': True}
        elif route_to and not route_from and not a['gnd'] and d <= 320:
            m = {'kind': 'inbound', 'score': 35, 'strong': bool(a['sure'])}
        elif d <= 60 and alt <= 18000 and desc and toward:
            m = {'kind': 'inbound', 'score': 20, 'strong': False}
        elif d <= 25 and alt <= 12000 and climb and away and not route_to:
            m = {'kind': 'departed', 'score': 20 if route_from else 15, 'strong': d <= 15}
        elif d <= 15 and alt <= 5000:
            m = {'kind': 'nearby', 'score': 5, 'strong': False}
    if not m:
        return None
    m.setdefault('rwy', '')
    if 'eta' not in m:
        m['eta'] = None
        if m['kind'] in ('inbound', 'landing') and gs and gs > 60:
            m['eta'] = now + dt.timedelta(minutes=d / gs * 60 + 2)
    return m


def best_movement(a, apts, now, runways):
    best = None
    for p in apts:
        if abs(p['lat'] - a['lat']) > 2:
            continue
        d = dist_nm(a['lat'], a['lon'], p['lat'], p['lon'])
        if d > RADIUS_NM + 10:
            continue
        m = movement(a, p, d, now, runways)
        if not m:
            continue
        m['sc'] = m['score'] - d / 1000.0
        m['apt'] = p['icao']
        m['d'] = d
        if not best or m['sc'] > best['sc']:
            best = m
    return best


# ---------------------------------------------------------------- day log

def iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def update_entry(day, a, m, now):
    key = f"{a['hex']}|{m['apt']}"
    e = day['entries'].get(key)
    if not e:
        e = {'hex': a['hex'], 'apt': m['apt'], 'first': iso(now), 'reg': '', 'type': '', 'desc': '', 'op': '', 'year': '',
             'cs': '', 'cat': '', 'flags': 0, 'orig': '', 'dest': '', 'legs': 0, 'sure': False, 'status': '',
             'arr': '', 'arrEst': False, 'dep': '', 'depEst': False, 'eta': '', 'rwyArr': '', 'rwyDep': ''}
        day['entries'][key] = e
    for k in ('reg', 'type', 'desc', 'op', 'year', 'cs', 'cat'):
        if a[k]:
            e[k] = a[k]
    if a['flags']:
        e['flags'] = a['flags']
    if a['dest']:
        e.update(orig=a['orig'], dest=a['dest'], legs=a['legs'], sure=a['sure'])
    e['src'] = '+'.join(sorted(a['src']))
    e['last'] = iso(now)
    k = m['kind']
    if k == 'inbound':
        if not e['arr']:
            e['status'] = 'Inbound'
            if m['eta']:
                e['eta'] = iso(m['eta'])
    elif k == 'landing':
        if not e['arr']:
            e['arr'] = iso(m['eta'] or now)
            e['arrEst'] = False
        if m['rwy']:
            e['rwyArr'] = m['rwy']
        e['status'] = 'Landed'
    elif k == 'ground':
        if not e['arr'] and e['status'] == 'Inbound':
            e['arr'] = e['eta'] or iso(now)          # landed between two runs
            e['arrEst'] = True
        if e['status'] != 'Departed':
            e['status'] = 'On ground'
    elif k == 'departed':
        if not e['dep']:
            back = 0.0
            if a['gs'] and a['gs'] > 60 and m['d'] > 3:
                back = m['d'] / a['gs'] * 60          # minutes since take-off, roughly
            e['dep'] = iso(now - dt.timedelta(minutes=back))
            e['depEst'] = back > 0
        if m['rwy']:
            e['rwyDep'] = m['rwy']
        e['status'] = 'Departed'


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else 'out'
    os.makedirs(out_dir, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(TZ).strftime('%Y-%m-%d')
    path = os.path.join(out_dir, f'day-{today}.json.gz')

    db, iata = load_airports()
    watch = load_watch(db, iata)
    if not watch:
        log('No airports in watch.txt')
        return 1
    watch_apts = [db[i] for i in watch]
    ctx = {i: db[i] for i in watch}
    for w in watch_apts:
        for x in db.values():
            if x['iata'] and abs(x['lat'] - w['lat']) <= 1.0 and abs(x['lon'] - w['lon']) <= 2.0 \
                    and dist_nm(x['lat'], x['lon'], w['lat'], w['lon']) <= CONTEXT_NM:
                ctx[x['icao']] = x
    runways = load_runways(set(ctx))

    day = None
    if os.path.exists(path):
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                day = json.load(f)
        except Exception as e:
            log(f'Could not read old log: {e}')
    if not isinstance(day, dict) or day.get('date') != today:
        day = {'date': today, 'tz': TZ_NAME, 'entries': {}, 'routes': {}, 'runs': 0}
    day['airports'] = watch

    all_, ok = scan(watch_apts)
    if ok == 0:
        log('No data from any source this run')
        return 0
    acs = list(all_.values())
    set_routes(acs, day, now)
    n = 0
    for a in acs:
        m = best_movement(a, list(ctx.values()), now, runways)
        if not m or m['apt'] not in watch:
            continue
        if m['kind'] == 'nearby' or (m['kind'] == 'inbound' and not m['strong']):
            continue
        update_entry(day, a, m, now)
        n += 1
    day['runs'] = int(day.get('runs', 0)) + 1
    day['updated'] = iso(now)
    with gzip.open(path, 'wt', encoding='utf-8') as f:
        json.dump(day, f, separators=(',', ':'))
    log(f'{today}: {len(acs)} aircraft seen, {n} movements updated, {len(day["entries"])} in log, {ok} source calls ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
