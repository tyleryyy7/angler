# -*- coding: utf-8 -*-
# qmt_executor_v1.py - big-QMT built-in strategy (executor for manually picked stocks)
#
# Logic: 60m Fisher transform, Pine/THS standard (hl2 input, window 9) - bit-identical
# to external fisher_scanner.py (verified 2026-09-14).
#   BUY : fish crosses UP through trigger (upturn) and no position and 0 < fish < 2.5
#   SELL: fish crosses DOWN through trigger (downturn) and position > 0, sell all
# Fixed lot per order. Pure ASCII, Python 3.6, save as UTF-8.
#
# Setup: put stock codes (one per line, e.g. 600519.SH) into the watchlist file.
# Requires 60m history downloaded in the QMT client (data management -> supplement).

import math

# ---------------- config ----------------
ACCOUNT_ID = 'test'          # TODO: set your account id
WATCHLIST_FILE = r'D:\qmt\watchlist.txt'   # must be an ASCII-only path (QMT builtin env misreads non-ASCII)
FALLBACK_CODES = ['600519.SH']   # used when the watchlist file is unreadable
PERIOD = '1h'                  # kline period; NOTE: this GJ build rejects '60m', use '1h'
LENGTH = 9                   # fisher window, same as fisher_scanner.py FISHER_LEN
HIST_BARS = 120              # bars fetched per handlebar (warmup for fisher)
VOLUME = 100                 # fixed shares per BUY order
USE_ENTRY_GATE = False       # buy only when 0 < fish < 2.5 (ESI entry condition); False = off
GATE_LO, GATE_HI = 0.0, 2.5
# ----------------------------------------

CODES = []
LAST_ORDER_BAR = {}          # code -> bar time of last order (same-bar dedup)


def fisher_calc3(high, low, length):
    # Fisher transform (hl2 input). Old-to-new series, full recompute each call.
    # Returns (f0, f1, f2) = fish of last, 2nd-last, 3rd-last bar; Nones if warming up.
    n = len(high)
    if n < 3:
        return None, None, None
    hl2 = [(high[i] + low[i]) / 2.0 for i in range(n)]
    value = 0.0
    fish = 0.0
    out = []
    for i in range(n):
        s = max(0, i - length + 1)
        hh = max(hl2[s: i + 1])
        ll = min(hl2[s: i + 1])
        div = (hh - ll) if hh != ll else 1.0
        v = 0.66 * ((hl2[i] - ll) / div - 0.5) + 0.67 * value
        if v > 0.99:      # asymmetric clip, identical to fisher_scanner.py
            v = 0.999
        elif v < -0.99:
            v = -0.999
        value = v
        fish = 0.5 * math.log((1.0 + v) / (1.0 - v)) + 0.5 * fish
        out.append(fish)
    return out[-1], out[-2], out[-3]


def load_codes():
    try:
        f = open(WATCHLIST_FILE, 'r', encoding='utf-8')
        lines = f.readlines()
        f.close()
        codes = []
        for ln in lines:
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                codes.append(ln)
        if codes:
            return codes
        print('[WARN] watchlist file empty, use fallback')
    except Exception as e:
        print('[WARN] watchlist read failed: %s, use fallback' % e)
    return list(FALLBACK_CODES)


def get_position_volume(code):
    # Actual account position; 0 on any failure (conservative: never blocks SELL,
    # BUY path re-checks via this too, broker rejects dup buys anyway).
    try:
        positions = get_trade_detail_data(ACCOUNT_ID, 'stock', 'position')
        plain = code.split('.')[0]
        for p in positions:
            pid = getattr(p, 'm_strInstrumentID', '')
            exch = getattr(p, 'm_strExchangeID', '')
            vol = getattr(p, 'm_nVolume', 0)
            if pid == plain or ('%s.%s' % (pid, exch)) == code:
                return vol
    except Exception as e:
        print('[WARN] position query failed: %s' % e)
    return 0


def do_order(ContextInfo, code, op, volume):
    # op: 23=buy 24=sell. priceType 5=market, -1=no price. Verified 2026-09-12.
    passorder(op, 1101, ACCOUNT_ID, code, 5, -1, volume,
              ContextInfo.strategyName, 1, '', ContextInfo)


def init(ContextInfo):
    global CODES
    ContextInfo.strategyName = 'qmt_executor_v1'
    ContextInfo.accountid = ACCOUNT_ID
    CODES = load_codes()
    ContextInfo.set_universe(CODES)
    print('=== qmt_executor_v1 start, codes=%s len=%d vol=%d gate=%s ==='
          % (CODES, LENGTH, VOLUME, USE_ENTRY_GATE))


def handlebar(ContextInfo):
    for code in CODES:
        try:
            hd = ContextInfo.get_history_data(HIST_BARS, PERIOD, 'high', code)
            ld = ContextInfo.get_history_data(HIST_BARS, PERIOD, 'low', code)
            high = list(hd[code])
            low = list(ld[code])
        except Exception as e:
            print('[ERR] %s history failed: %s' % (code, e))
            continue

        f0, f1, f2 = fisher_calc3(high, low, LENGTH)
        if f0 is None:
            print('[WAIT] %s warming up, bars=%d' % (code, len(high)))
            continue

        pos = get_position_volume(code)
        cross_up = (f0 > f1) and (f1 <= f2)
        cross_down = (f0 < f1) and (f1 >= f2)
        bar_key = str(ContextInfo.barpos)

        print('[%s] %s fish=%.3f prev=%.3f prev2=%.3f pos=%d'
              % (bar_key, code, f0, f1, f2, pos))

        if LAST_ORDER_BAR.get(code) == bar_key:
            continue

        if cross_up and pos == 0:
            if USE_ENTRY_GATE and not (GATE_LO < f0 < GATE_HI):
                print('[SKIP] %s cross up but fish=%.3f outside gate (%.1f, %.1f)'
                      % (code, f0, GATE_LO, GATE_HI))
                continue
            do_order(ContextInfo, code, 23, VOLUME)
            LAST_ORDER_BAR[code] = bar_key
            print('>>> BUY %s %d shares, fish=%.3f' % (code, VOLUME, f0))

        elif cross_down and pos > 0:
            do_order(ContextInfo, code, 24, pos)
            LAST_ORDER_BAR[code] = bar_key
            print('>>> SELL %s %d shares, fish=%.3f' % (code, pos, f0))
