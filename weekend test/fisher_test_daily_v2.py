# -*- coding: utf-8 -*-
# fisher_test_daily_v2.py
# Daily-bar Fisher transform test for backtest.
# Calculation + logging only, no orders. Pure ASCII, Python 3.6.
# v2: fixed off-by-one in prev_fish capture (was lagging 2 bars).

import math

TEST_CODE = '600519.SH'   # change to any code you like
LENGTH = 10               # fisher window, keep same as your external system

def fisher_calc(high, low, length):
    # Standard fisher transform on high/low series (old to new).
    # Full recompute each bar: deterministic, no state drift.
    # Returns (fish, prev_fish) or (None, None) if not enough data.
    if len(high) < length + 1:
        return None, None

    value = 0.0
    fish = 0.0
    prev_fish = None
    for i in range(length - 1, len(high)):
        hh = max(high[i - length + 1: i + 1])
        ll = min(low[i - length + 1: i + 1])
        if hh == ll:
            continue
        raw = 0.33 * 2.0 * ((high[i] - ll) / (hh - ll) - 0.5) + 0.67 * value
        value = max(min(raw, 0.999), -0.999)
        fish = 0.5 * math.log((1.0 + value) / (1.0 - value)) + 0.5 * fish
        # capture AFTER update: this is the fish of the second-to-last bar
        if i == len(high) - 2:
            prev_fish = fish

    if prev_fish is None:
        prev_fish = fish

    return fish, prev_fish

def init(ContextInfo):
    ContextInfo.strategyName = 'fisher_test_daily'
    ContextInfo.set_universe([TEST_CODE])
    print('=== fisher daily test start, code=%s len=%d ===' % (TEST_CODE, LENGTH))

def handlebar(ContextInfo):
    # Native dict API, no pandas involved.
    # get_history_data(length, period, field, stockcode) -> {code: list}
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
