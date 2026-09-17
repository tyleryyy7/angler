# -*- coding: utf-8 -*-
"""tick_bar_builder.py - snapshot-driven realtime bar aggregator (TQ UserPY strategy).

Background: tqcenter's get_market_data is a static store (officially daily-only
intraday); minute history only reflects manual post-close downloads. This builder
polls get_pricevol (batch, one call for all codes) every POLL_SEC seconds and
aggregates today's 5m bars in memory, then derives 15m/30m/1h and atomically
rewrites the scanner cache files cache/daily_qfq/tdxq/{code}_{period}.csv
(history rows before today preserved, today's rows fully rebuilt - overwriting
TQ's frozen-price padding rows).

Approximation: intrabar extremes between polls are lost; 5m high/low are the
max/min of polled prices. Fisher decisions use the latest bars only, impact is
bounded. Ratio alignment: static cache is front-adjusted, snapshots are raw;
ratio = static last close / snapshot LastClose applied to all polled prices
(exact 1.0 on non ex-div days).

Run: add as UserPY strategy in TQ manager, start mode "run with terminal".
Master copy in git repo D:/angler; deploy copy must live in PYPlugins/user/.
"""
import csv
import os
import time
import traceback
from datetime import datetime

from tqcenter import tq

# ---------------- config ----------------
ANGLER = r'D:\angler'
CACHE_DIR = os.path.join(ANGLER, 'cache', 'daily_qfq', 'tdxq')
LOG_FILE = r'D:\qmt\tick_builder.log'
DEEP_TOP_N = 50                 # top N of pool_deep.csv by score
POLL_SEC = 20                   # snapshot poll interval
FLUSH_SEC = 30                  # min seconds between cache writes
MAX_CODES = 100                 # safety cap
# ----------------------------------------

# 5m bar end labels (tdx convention: label = bar close time);
# 09:35..11:30 (24 bars) + 13:05..15:00 (24 bars)
def _grid(h0, m0, h1, m1):
    out = []
    h, m = h0, m0
    while (h, m) <= (h1, m1):
        out.append('%02d:%02d' % (h, m))
        m += 5
        if m >= 60:
            h += 1
            m -= 60
    return out


GRID_5M = _grid(9, 35, 11, 30) + _grid(13, 5, 15, 0)
ENDS_15M = ['09:45', '10:00', '10:15', '10:30', '10:45', '11:00', '11:15',
            '11:30', '13:15', '13:30', '13:45', '14:00', '14:15', '14:30',
            '14:45', '15:00']
ENDS_30M = ['10:00', '10:30', '11:00', '11:30', '13:30', '14:00', '14:30',
            '15:00']
ENDS_60M = ['10:30', '11:30', '14:00', '15:00']

TODAY = ''                        # yyyymmdd, set at startup / day rollover
codes = []
ratio = {}                        # code -> front-adjust ratio
bars5 = {}                        # code -> list of finalized 5m bar dicts
cur = {}                          # code -> in-progress bar dict


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def suffix(code6):
    if code6.startswith(('60', '68', '51', '58')):
        return code6 + '.SH'
    return code6 + '.SZ'


def read_code_col(path):
    out = []
    try:
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                c = (row.get('code') or '').strip()
                if c:
                    out.append(c)
    except Exception:
        pass
    return out


def load_codes():
    """top DEEP_TOP_N of pool_deep by score + holdings + watchlist, dedup."""
    picked = []
    rows = []
    try:
        with open(os.path.join(ANGLER, 'pool_deep.csv'),
                  'r', encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                c = (row.get('code') or '').strip()
                if not c:
                    continue
                try:
                    sc = float(row.get('score') or 0)
                except ValueError:
                    sc = 0.0
                rows.append((sc, c))
    except Exception as e:
        log('[WARN] read pool_deep failed: %s' % e)
    rows.sort(key=lambda x: -x[0])
    picked.extend(c for _, c in rows[:DEEP_TOP_N])
    for extra in ('holdings.csv', 'watchlist.csv'):
        picked.extend(read_code_col(os.path.join(ANGLER, extra)))
    try:
        with open(os.path.join(ANGLER, 'cache', 'esi_pending.csv'),
                  'r', encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                if (row.get('status') or '').strip() == 'pending':
                    c = (row.get('code') or '').strip()
                    if c:
                        picked.append(c)
    except Exception:
        pass
    seen = {}
    for c in picked:
        seen.setdefault(suffix(c), True)
    out = sorted(seen)
    log('codes: %d (deep top%d + holdings + watchlist + esi pending)'
        % (len(out), DEEP_TOP_N))
    return out[:MAX_CODES]


def bucket5(now):
    """current 5m bar end label; None during pre-open/lunch."""
    t = now.strftime('%H:%M')
    if t < '09:30' or '11:30' < t < '13:00':
        return None
    if t >= '15:00':
        return '15:00'
    for label in GRID_5M:
        if t < label:
            return label
    return '15:00'


def aggregate(day_bars, ends):
    """aggregate today's 5m bars into period bars ending at `ends`."""
    out = []
    prev = '00:00'
    for e in ends:
        grp = [b for b in day_bars if prev < b['label'] <= e]
        prev = e
        if not grp:
            continue
        out.append({'label': e, 'o': grp[0]['o'],
                    'h': max(b['h'] for b in grp),
                    'l': min(b['l'] for b in grp), 'c': grp[-1]['c'],
                    'v': sum(b['v'] for b in grp)})
    return out


def strip_padding(rows):
    """drop trailing rows that are TQ frozen-price padding
    (consecutive rows with identical OHLC)."""
    while len(rows) >= 2:
        a, b = rows[-2], rows[-1]
        if a[1:5] == b[1:5]:
            rows.pop()
        else:
            break
    return rows


def atomic_write(path, rows):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['time', 'open', 'high', 'low', 'close', 'volume', 'amount'])
        w.writerows(rows)
    os.replace(tmp, path)


def load_history(path):
    """cache rows before today + today's real static rows (padding stripped)."""
    rows = []
    if not os.path.exists(path):
        return rows, []
    prefix = '%s-%s-%s' % (TODAY[:4], TODAY[4:6], TODAY[6:])
    try:
        with open(path, 'r', encoding='utf-8') as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                if row:
                    rows.append(row)
    except Exception:
        return [], []
    hist = [r for r in rows if not r[0].startswith(prefix)]
    today = strip_padding([r for r in rows if r[0].startswith(prefix)])
    return hist, today


def bar_rows(day_bars):
    prefix = '%s-%s-%s' % (TODAY[:4], TODAY[4:6], TODAY[6:])
    return [['%s %s:00' % (prefix, b['label']), '%.3f' % b['o'],
             '%.3f' % b['h'], '%.3f' % b['l'], '%.3f' % b['c'],
             '%.0f' % b['v'], '%.2f' % (b['v'] * b['c'])]
            for b in day_bars]


def rebuild_and_write(code):
    """rewrite 4 period cache files: history + today rebuilt from bars5+cur."""
    plain = code.split('.')[0]
    day5 = list(bars5.get(code, []))
    b = cur.get(code)
    if b is not None:
        day5 = day5 + [b]
    if not day5:
        return
    for period, ends in (('5m', None), ('15m', ENDS_15M),
                         ('30m', ENDS_30M), ('1h', ENDS_60M)):
        path = os.path.join(CACHE_DIR, '%s_%s.csv' % (plain, period))
        hist, static_today = load_history(path)
        rows = hist + static_today     # midday-start compensation: keep real
        if period == '5m':             # static rows; live bars appended after
            rows = hist + bar_rows(day5) if not static_today else \
                hist + merge_static(static_today, bar_rows(day5))
        else:
            agg = aggregate(day5, ends)
            rows = hist + bar_rows(agg)
        try:
            atomic_write(path, rows)
        except Exception as e:
            log('[ERR] write %s %s failed: %s' % (code, period, e))


def merge_static(static_rows, live_rows):
    """midday start: keep static today rows before the first live bar."""
    if not live_rows:
        return static_rows
    first_live_time = live_rows[0][0]
    kept = [r for r in static_rows if r[0] < first_live_time]
    return kept + live_rows


def on_prices(data):
    """one poll: data = {code: {LastClose, Now, Volume(lots)}}"""
    label = bucket5(datetime.now())
    if label is None:
        return
    for code in codes:
        d = data.get(code)
        if not d:
            continue
        try:
            px = float(d['Now']) * ratio.get(code, 1.0)
            vol = float(d.get('Volume') or 0) * 100   # lots -> shares
        except (ValueError, TypeError):
            continue
        if px <= 0:
            continue
        b = cur.get(code)
        if b is None or b['label'] != label:
            if b is not None:                        # finalize previous bar
                bars5.setdefault(code, []).append(b)
                log('bar5 %s %s c=%.2f' % (code, b['label'], b['c']))
            cur[code] = {'label': label, 'o': px, 'h': px, 'l': px,
                         'c': px, 'v': 0.0, 'volcum': vol}
        else:
            b['h'] = max(b['h'], px)
            b['l'] = min(b['l'], px)
            b['c'] = px
            if vol >= b.get('volcum', 0):
                b['v'] += vol - b.get('volcum', 0)
                b['volcum'] = vol


def flush_all():
    for code in codes:
        rebuild_and_write(code)


def init_ratio():
    """ratio = static last close (front-adj) / snapshot LastClose (raw)."""
    try:
        data = tq.get_pricevol(stock_list=codes)
    except Exception as e:
        log('[WARN] init pricevol failed: %s' % e)
        return
    for code in codes:
        r = 1.0
        try:
            p1h = os.path.join(CACHE_DIR, '%s_1h.csv' % code.split('.')[0])
            with open(p1h, 'r', encoding='utf-8') as f:
                rows = list(csv.reader(f))
            static_close = float(rows[-1][4])
            raw_close = float(data[code]['LastClose'])
            if raw_close > 0:
                r = static_close / raw_close
        except Exception:
            pass
        ratio[code] = r


def main():
    global TODAY, codes
    TODAY = datetime.now().strftime('%Y%m%d')
    tq.initialize(__file__)
    codes = load_codes()
    if not codes:
        log('[ERR] no codes, exit')
        return
    init_ratio()
    log('builder start, %d codes, poll %ds' % (len(codes), POLL_SEC))
    last_flush = 0.0
    closed_flushed = False
    while True:
        try:
            now = datetime.now()
            hm = now.strftime('%H:%M')
            if now.strftime('%Y%m%d') != TODAY:      # new day: reset state
                TODAY = now.strftime('%Y%m%d')
                bars5.clear(); cur.clear(); ratio.clear()
                codes = load_codes()
                init_ratio()
                closed_flushed = False
                log('new day, reloaded %d codes' % len(codes))
            if '09:25' <= hm <= '15:05' and now.weekday() < 5:
                data = tq.get_pricevol(stock_list=codes)
                if data:
                    on_prices(data)
                if time.time() - last_flush >= FLUSH_SEC:
                    flush_all()
                    last_flush = time.time()
                time.sleep(POLL_SEC)
            else:
                if hm > '15:05' and not closed_flushed:
                    flush_all()
                    closed_flushed = True
                    log('market closed, final flush done')
                time.sleep(60)
        except Exception:
            log('[ERR] main loop:\n' + traceback.format_exc())
            time.sleep(60)


main()
