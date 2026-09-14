# -*- coding: utf-8 -*-
# fisher_test_daily_v3.py
# Daily-bar Fisher transform test for QMT backtest. Signal + logging only, no orders.
# Pure ASCII, Python 3.6, save as UTF-8.
#
# v3 changes (2026-09-14, aligned with external fisher_scanner.py, Pine/THS standard):
#   - input price: hl2 = (high+low)/2 (was: high)
#   - hh/ll window: over hl2 series (was: max(high)/min(low) separately)
#   - LENGTH = 9 (was: 10)
#   - clip: asymmetric like scanner (v>0.99 -> 0.999, v<-0.99 -> -0.999)
# keeps the v2 prev off-by-one fix (capture AFTER update at second-to-last bar).

import math

TEST_CODE = '600519.SH'   # change to any code you like
LENGTH = 9                # fisher window, same as fisher_scanner.py FISHER_LEN


def fisher_calc(high, low, length):
    # Fisher transform, Pine/THS standard (hl2 input). Old-to-new series.
    # Full recompute each bar: deterministic, no state drift.
    # Returns (fish, prev_fish) or (None, None) if not enough data.
    if len(high) < length + 1:
        return None, None

    hl2 = [(high[i] + low[i]) / 2.0 for i in range(len(high))]

    value = 0.0
    fish = 0.0
    prev_fish = None
    for i in range(len(hl2)):
        s = max(0, i - length + 1)
        hh = max(hl2[s: i + 1])
        ll = min(hl2[s: i + 1])
        div = (hh - ll) if hh != ll else 1.0
        v = 0.66 * ((hl2[i] - ll) / div - 0.5) + 0.67 * value
        # asymmetric clip, identical to fisher_scanner.py
        if v > 0.99:
            v = 0.999
        elif v < -0.99:
            v = -0.999
        value = v
        fish = 0.5 * math.log((1.0 + v) / (1.0 - v)) + 0.5 * fish
        # capture AFTER update: this is the fish of the second-to-last bar
        if i == len(hl2) - 2:
            prev_fish = fish

    if prev_fish is None:
        prev_fish = fish

    return fish, prev_fish


def init(ContextInfo):
    ContextInfo.strategyName = 'fisher_test_daily_v3'
    ContextInfo.set_universe([TEST_CODE])
    print('=== fisher daily test v3 (Pine-aligned), code=%s len=%d ===' % (TEST_CODE, LENGTH))


def handlebar(ContextInfo):
    # Native dict API, no pandas involved.
    try:
        hd = ContextInfo.get_history_data(60, '1d', 'high', TEST_CODE)
        ld = ContextInfo.get_history_data(60, '1d', 'low', TEST_CODE)
        cd = ContextInfo.get_history_data(60, '1d', 'close', TEST_CODE)
        high = list(hd[TEST_CODE])
        low = list(ld[TEST_CODE])
        close = list(cd[TEST_CODE])
    except Exception as e:
        print('[ERR] history data failed:', e)
        return

    if len(close) < 2:
        print('[WAIT] not enough bars: %d' % len(close))
        return

    fish, prev_fish = fisher_calc(high, low, LENGTH)
    if fish is None:
        print('[WAIT] warming up, bars=%d need=%d' % (len(close), LENGTH + 1))
        return

    price = close[-1]
    crossed = (prev_fish <= 0.0 and fish > 0.0)

    print('[%s] %s px=%.2f fish=%.3f prev=%.3f %s' % (
        ContextInfo.barpos,
        TEST_CODE,
        price,
        fish,
        prev_fish,
        '<<< CROSS UP' if crossed else ''
    ))

    if crossed:
        print('>>> SIGNAL: fisher cross up, %s px=%.2f fish=%.3f' % (TEST_CODE, price, fish))
