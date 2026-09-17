# -*- coding: utf-8 -*-
"""tdx_postclose_download.py - automated post-close TQ data download.

Replaces the manual "盘后数据下载" ritual: refreshes 5m + 1d kline caches for
the union of all pools + holdings + watchlist, then refresh_cache, then
verifies today's 15:00 bar is real. Run after market close (scheduled 16:00).
--midday: morning-session top-up run (scheduled 11:35), refreshes 5m ONLY
(the 1d bar is still forming midday; refreshing it would poison the daily
caches with a half-finished bar) and verifies only the bar date.

Master copy in git repo D:/angler; deploy copy must live in PYPlugins/user/
(TQ scripts must run from there). Run: .venv python tdx_postclose_download.py
"""
import csv
import os
import sys
import time
import traceback
from datetime import datetime

from tqcenter import tq

ANGLER = r'D:\angler'
LOG_FILE = r'D:\qmt\postclose_download.log'
BATCH = 100
POOL_FILES = ['pool_right.csv', 'pool_left.csv', 'pool_deep.csv',
              'pool_t0.csv', 'pool_t1.csv', 'watchlist.csv', 'holdings.csv']


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


def load_codes():
    seen = {}
    for name in POOL_FILES:
        path = os.path.join(ANGLER, name)
        try:
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                for row in csv.DictReader(f):
                    c = (row.get('code') or '').strip()
                    if c:
                        seen.setdefault(suffix(c), True)
        except FileNotFoundError:
            pass
        except Exception as e:
            log('[WARN] read %s failed: %s' % (name, e))
    return sorted(seen)


def refresh_all(codes, period):
    ok = True
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        try:
            r = tq.refresh_kline(stock_list=chunk, period=period)
            log('refresh_kline %s batch %d-%d: %s'
                % (period, i, i + len(chunk), str(r)[:120]))
        except Exception as e:
            ok = False
            log('[ERR] refresh_kline %s batch %d failed: %s' % (period, i, e))
    return ok


def verify(codes, midday=False):
    """sample 3 codes: latest 5m bar must be today; post-close also requires
    the 14:55/15:00 bar, midday only requires the date (morning session)."""
    today = datetime.now().strftime('%Y-%m-%d')
    bad = []
    for code in codes[:3]:
        try:
            df = tq.get_market_data(stock_list=[code], period='5m', count=5,
                                    dividend_type='front')
            v = list(df.values())[0]
            last_time = str(v.index[-1] if hasattr(v, 'index') else v[-1])
            if not last_time.startswith(today):
                bad.append((code, last_time))
            elif not midday and last_time[11:16] not in ('14:55', '15:00'):
                bad.append((code, last_time))
        except Exception as e:
            bad.append((code, str(e)))
    return bad


def main():
    midday = '--midday' in sys.argv
    if datetime.now().weekday() >= 5:
        log('weekend, skip')
        return
    tq.initialize(__file__)
    codes = load_codes()
    log('start, %d codes%s' % (len(codes), ' (midday: 5m only)' if midday else ''))
    if not codes:
        log('[ERR] no codes, exit')
        return
    refresh_all(codes, '5m')
    if not midday:
        # 1d 日K盘中未完结，午间刷新会让缓存混入半成品日K，只在盘后刷
        refresh_all(codes, '1d')
    try:
        log('refresh_cache: %s' % str(tq.refresh_cache(market='AG', force=True))[:120])
    except Exception as e:
        log('[WARN] refresh_cache failed: %s' % e)
    for attempt in (1, 2):
        bad = verify(codes, midday)
        if not bad:
            log('VERIFY OK: today bar present in samples')
            return
        log('[WARN] verify failed (attempt %d): %s' % (attempt, bad))
        if attempt == 1:
            log('server-side delay? retry in 10 min')
            time.sleep(600)
            refresh_all(codes[:BATCH], '5m')   # light re-pull before re-verify
    log('[ERR] verify failed twice - today data may be incomplete')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log('[ERR] fatal:\n' + traceback.format_exc())
        sys.exit(1)
