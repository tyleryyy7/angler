# -*- coding: utf-8 -*-
# build_qmt_watchlist.py - merge all pools + watchlist.csv into QMT executor watchlist.
# Output: qmt-live/watchlist.txt (junction to D:\qmt\watchlist.txt), CODE.VOL per line.
import csv
import os
import shutil
import time

POOL_FILES = ['pool_right.csv', 'pool_left.csv', 'pool_deep.csv',
              'pool_t0.csv', 'pool_t1.csv']
EXTRA_FILES = ['watchlist.csv']
T0_FILE = 'pool_t0.csv'      # codes from this pool get a T0 marker (intraday sellable)
OUT_FILE = os.path.join('qmt-live', 'watchlist.txt')
VOLUME = 100


def suffix(code):
    if code.startswith(('60', '68', '51', '58')):
        return code + '.SH'
    if code.startswith(('00', '30', '15')):
        return code + '.SZ'
    return None


def read_codes(path):
    codes = []
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            c = (row.get('code') or '').strip()
            if c:
                codes.append(c)
    return codes


def main():
    seen = {}
    stats = {}
    for path in POOL_FILES + EXTRA_FILES:
        if not os.path.exists(path):
            print('[WARN] %s missing, skipped' % path)
            continue
        n = 0
        for c in read_codes(path):
            sc = suffix(c)
            if sc is None:
                print('[WARN] unknown exchange prefix: %s (%s)' % (c, path))
                continue
            if sc not in seen:
                seen[sc] = path
                n += 1
        stats[path] = n

    t0_codes = set()
    if os.path.exists(T0_FILE):
        for c in read_codes(T0_FILE):
            sc = suffix(c)
            if sc:
                t0_codes.add(sc)

    if os.path.exists(OUT_FILE):
        shutil.copy2(OUT_FILE, OUT_FILE + '.bak')

    with open(OUT_FILE, 'w', encoding='utf-8', newline='\n') as f:
        f.write('# generated %s from %s\n'
                % (time.strftime('%Y-%m-%d %H:%M:%S'),
                   ', '.join('%s:%d' % (k, v) for k, v in stats.items())))
        f.write('# format: CODE,VOLUME[,T0]  (T0 = intraday round-trip allowed)\n')
        for sc in seen:
            f.write('%s,%d%s\n' % (sc, VOLUME, ',T0' if sc in t0_codes else ''))

    for k, v in stats.items():
        print('%-16s +%d' % (k, v))
    print('total %d (T0: %d) -> %s' % (len(seen), len(t0_codes), OUT_FILE))


if __name__ == '__main__':
    main()
