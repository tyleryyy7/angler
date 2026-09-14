# -*- coding: utf-8 -*-
# fisher_trade_test_v1.py
# Daily backtest WITH orders. Buy 100 shares on fisher cross up,
# exit all on fisher cross down. TEST ONLY - exit rule is a placeholder,
# NOT the real invalidation logic.
# Pure ASCII, Python 3.6.

import math

TEST_CODE = '600519.SH'   # change to any code you like
LENGTH = 10               # fisher window, keep same as your external system
VOLUME = 100              # test lot size

POS = {'volume': 0}       # current position, in shares

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
        if i == len(high) - 2:
            prev_fish = fish

    if prev_fish is None:
        prev_fish = fish

    return fish, prev_fish

def init(ContextInfo):
    ContextInfo.strategyName = 'fisher_trade_test'
    ContextInfo.set_universe([TEST_CODE])
    ContextInfo.accountid = 'test'
    print('=== fisher trade test start, code=%s len=%d vol=%d ===' % (TEST_CODE, LENGTH, VOLUME))

def do_order(ContextInfo, op, volume):
    # op: 23=buy 24=sell. priceType 5=market, -1=no price.
    # NOTE: verify passorder signature against your XtQuant docs;
    # enum values may differ across broker builds.
    passorder(op, 1101, ContextInfo.accountid, TEST_CODE,
              5, -1, volume, ContextInfo.strategyName,
              1, '', ContextInfo)

def handlebar(ContextInfo):
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
        return

    fish, prev_fish = fisher_calc(high, low, LENGTH)
    if fish is None:
        return

    price = close[-1]
    crossed_up = (prev_fish <= 0.0 and fish > 0.0)
    crossed_down = (prev_fish >= 0.0 and fish < 0.0)

    print('[%s] %s px=%.2f fish=%.3f prev=%.3f pos=%d' % (
        ContextInfo.barpos, TEST_CODE, price, fish, prev_fish, POS['volume']
    ))

    if crossed_up and POS['volume'] == 0:
        do_order(ContextInfo, 23, VOLUME)
        POS['volume'] = VOLUME
        print('>>> BUY %s %d shares @ px=%.2f fish=%.3f' % (TEST_CODE, VOLUME, price, fish))

    elif crossed_down and POS['volume'] > 0:
        do_order(ContextInfo, 24, POS['volume'])
        print('>>> SELL %s %d shares @ px=%.2f fish=%.3f' % (TEST_CODE, POS['volume'], price, fish))
        POS['volume'] = 0
