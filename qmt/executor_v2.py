# -*- coding: utf-8 -*-
# qmt_executor_v2.py - big-QMT built-in strategy, aligned with scanner Fisher-ESI rules v2.
#
# Entry (R1): 1h fisher cross UP and none of 30m/15m/5m is in down state
#             (fish < trigger) and no position -> buy VOLUME shares.
# Exit A    : 1h fisher cross DOWN and position > 0 -> sell all (normal exit).
# Exit B(R3): latest completed 30m bar cross DOWN and current price < cost -> sell all
#             immediately (failed trade); floating profit -> keep holding for Exit A.
#
# Fisher: Pine/THS standard, hl2 input, window 9 - bit-identical to fisher_scanner.py.
# Pure ASCII, Python 3.6, save as UTF-8. All file paths must be ASCII-only.
#
# Setup: run on 5m bars (frequent checks); history via get_history_data per timeframe.
# Requires 1h/30m/15m/5m history downloaded in the client for watched codes.

import math

# ---------------- config ----------------
ACCOUNT_ID = 'test'          # TODO: set your account id
WATCHLIST_FILE = r'D:\qmt\watchlist.txt'   # lines: CODE  or  CODE,VOLUME
PENDING_FILE = r'D:\qmt\pending.csv'       # persisted R2 watch list (code,signal_key)
FALLBACK_CODES = ['600519.SH']
LENGTH = 9                   # fisher window
HIST_BARS = 120              # bars fetched per timeframe per call
VOLUME = 100                 # default shares per BUY order (per-code override in watchlist)
USE_ENTRY_GATE = True        # R1: small-timeframe resonance gate (30m/15m/5m not down)
USE_EXIT_A = True            # Exit A: 1h fisher cross down -> sell all (normal exit)
USE_ESI_EXIT = True          # R3: 30m cross-down + floating loss -> sell immediately
# ----------------------------------------

CODES = []
VOL_MAP = {}                 # code -> per-stock buy volume
LAST_ACT = {}                # (code, action) -> fisher-state key, dedup per bar
PENDING = {}                 # code -> signal_key of fake-invalid signal (persisted to
                             # PENDING_FILE on every change, reloaded in init)


def fisher_series(high, low, length):
    # Fisher transform (hl2 input). Old-to-new lists. Returns list of fish values.
    n = len(high)
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
        if v > 0.99:
            v = 0.999
        elif v < -0.99:
            v = -0.999
        value = v
        fish = 0.5 * math.log((1.0 + v) / (1.0 - v)) + 0.5 * fish
        out.append(fish)
    return out


def get_hist(ContextInfo, code, period, field):
    d = ContextInfo.get_history_data(HIST_BARS, period, field, code)
    return list(d[code])


def tf_state(ContextInfo, code, period):
    # returns (fish_last, trig_last, is_down); None if data insufficient
    try:
        high = get_hist(ContextInfo, code, period, 'high')
        low = get_hist(ContextInfo, code, period, 'low')
    except Exception as e:
        print('[ERR] %s %s history failed: %s' % (code, period, e))
        return None
    if len(high) < 3:
        return None
    f = fisher_series(high, low, LENGTH)
    return f[-1], f[-2], f[-1] < f[-2]


def get_position(code):
    # returns (volume, cost_price); cost 0.0 if unknown
    try:
        positions = get_trade_detail_data(ACCOUNT_ID, 'stock', 'position')
        plain = code.split('.')[0]
        for p in positions:
            pid = getattr(p, 'm_strInstrumentID', '')
            exch = getattr(p, 'm_strExchangeID', '')
            if pid == plain or ('%s.%s' % (pid, exch)) == code:
                vol = getattr(p, 'm_nVolume', 0)
                cost = 0.0
                for attr in ('m_dOpenPrice', 'm_dPositionCost', 'm_dCostPrice'):
                    c = getattr(p, attr, 0.0)
                    if c:
                        cost = float(c)
                        break
                return vol, cost
    except Exception as e:
        print('[WARN] position query failed: %s' % e)
    return 0, 0.0


def get_last_price(ContextInfo, code):
    try:
        d = ContextInfo.get_history_data(2, '5m', 'close', code)
        c = list(d[code])
        if c:
            return float(c[-1])
    except Exception:
        pass
    return 0.0


def do_order(ContextInfo, code, op, volume):
    # op: 23=buy 24=sell. priceType 5=market. Verified 2026-09-12.
    passorder(op, 1101, ACCOUNT_ID, code, 5, -1, volume,
              ContextInfo.strategyName, 1, '', ContextInfo)


def load_codes():
    """watchlist lines: CODE  or  CODE,VOLUME (# comments, blank lines ok).
    Returns (codes, vol_map)."""
    try:
        f = open(WATCHLIST_FILE, 'r', encoding='utf-8')
        lines = f.readlines()
        f.close()
        codes, vol_map = [], {}
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith('#'):
                continue
            parts = ln.split(',')
            code = parts[0].strip()
            if not code:
                continue
            codes.append(code)
            if len(parts) > 1 and parts[1].strip():
                try:
                    vol_map[code] = int(parts[1].strip())
                except ValueError:
                    print('[WARN] bad volume in line "%s", use default' % ln)
        if codes:
            return codes, vol_map
        print('[WARN] watchlist file empty, use fallback')
    except Exception as e:
        print('[WARN] watchlist read failed: %s, use fallback' % e)
    return list(FALLBACK_CODES), {}


def pending_load():
    out = {}
    try:
        f = open(PENDING_FILE, 'r', encoding='utf-8')
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                parts = ln.split(',')
                out[parts[0].strip()] = parts[1].strip() if len(parts) > 1 else ''
        f.close()
    except Exception:
        pass
    return out


def pending_save():
    try:
        f = open(PENDING_FILE, 'w', encoding='utf-8')
        for code, key in PENDING.items():
            f.write('%s,%s\n' % (code, key))
        f.close()
    except Exception as e:
        print('[WARN] pending save failed: %s' % e)


def pending_add(code, key):
    PENDING[code] = key
    pending_save()


def pending_remove(code):
    if code in PENDING:
        PENDING.pop(code, None)
        pending_save()


def init(ContextInfo):
    global CODES, VOL_MAP, PENDING
    ContextInfo.strategyName = 'qmt_executor_v2'
    ContextInfo.accountid = ACCOUNT_ID
    CODES, VOL_MAP = load_codes()
    PENDING = pending_load()
    ContextInfo.set_universe(CODES)
    print('=== qmt_executor_v2 start, codes=%s vol_map=%s gate=%s exit_a=%s esi_exit=%s vol=%d pending=%s ==='
          % (CODES, VOL_MAP, USE_ENTRY_GATE, USE_EXIT_A, USE_ESI_EXIT, VOLUME,
             list(PENDING.keys())))


def handlebar(ContextInfo):
    bar_key = str(ContextInfo.barpos)
    for code in CODES:
        try:
            high = get_hist(ContextInfo, code, '1h', 'high')
            low = get_hist(ContextInfo, code, '1h', 'low')
        except Exception as e:
            print('[ERR] %s 1h history failed: %s' % (code, e))
            continue
        if len(high) < 3:
            print('[WAIT] %s 1h warming up' % code)
            continue
        f = fisher_series(high, low, LENGTH)
        f60, t60 = f[-1], f[-2]
        pos, cost = get_position(code)
        cross_up = len(f) >= 3 and f[-1] > f[-2] and f[-2] <= f[-3]
        cross_down = len(f) >= 3 and f[-1] < f[-2] and f[-2] >= f[-3]
        # dedup key = 1h fisher state (identifies the 1h bar; barpos changes every
        # 5m tick when strategy runs on 5m period, so it cannot be the key)
        key1h = '%.4f|%.4f|%.4f' % (f[-1], f[-2], f[-3])

        print('[%s] %s fish60=%.3f trig=%.3f pos=%d' % (bar_key, code, f60, t60, pos))

        if pos == 0:
            if cross_up and LAST_ACT.get((code, 'BUY')) != key1h:
                if USE_ENTRY_GATE:
                    downs = []
                    for tf in ('30m', '15m', '5m'):
                        st = tf_state(ContextInfo, code, tf)
                        if st is None:
                            downs = None
                            print('[WARN] %s %s state unknown, skip bar' % (code, tf))
                            break
                        if st[2]:
                            downs.append(tf)
                    if downs is None:
                        continue
                    if downs:
                        pending_add(code, key1h)   # R2: fake-invalid -> persisted watch list
                        print('[SKIP] %s cross up but %s in down state '
                              '(fake-invalid, watching)' % (code, '/'.join(downs)))
                        continue
                vol = VOL_MAP.get(code, VOLUME)
                do_order(ContextInfo, code, 23, vol)
                LAST_ACT[(code, 'BUY')] = key1h
                pending_remove(code)
                print('>>> BUY %s %d shares, fish60=%.3f' % (code, vol, f60))
                continue

            # R2 reactivation watch: pending code, no fresh 1h cross needed
            if code not in PENDING:
                continue
            if f60 < t60:
                pending_remove(code)              # 1h trend broken -> void
                print('[VOID] %s fake-invalid signal voided (1h fish below trigger)'
                      % code)
                continue
            ups = True
            for tf in ('30m', '15m', '5m'):
                st = tf_state(ContextInfo, code, tf)
                if st is None:
                    ups = None
                    break
                if st[2]:                          # still in down state
                    ups = False
                    break
            if ups:
                if LAST_ACT.get((code, 'BUY')) == key1h:
                    continue
                vol = VOL_MAP.get(code, VOLUME)
                do_order(ContextInfo, code, 23, vol)
                LAST_ACT[(code, 'BUY')] = key1h
                pending_remove(code)
                print('>>> BUY %s %d shares (REACTIVATED after fake-invalid), '
                      'fish60=%.3f' % (code, vol, f60))

        else:
            if cross_down and USE_EXIT_A:
                if LAST_ACT.get((code, 'SELL')) == key1h:
                    continue
                do_order(ContextInfo, code, 24, pos)
                LAST_ACT[(code, 'SELL')] = key1h
                print('>>> SELL %s %d shares (1h cross down), fish60=%.3f'
                      % (code, pos, f60))
                continue
            if USE_ESI_EXIT:
                st30 = tf_state(ContextInfo, code, '30m')
                if st30 is None:
                    continue
                # 30m cross-down detection on the 30m series
                try:
                    h30 = get_hist(ContextInfo, code, '30m', 'high')
                    l30 = get_hist(ContextInfo, code, '30m', 'low')
                    f30 = fisher_series(h30, l30, LENGTH)
                except Exception:
                    continue
                cd30 = len(f30) >= 3 and f30[-1] < f30[-2] and f30[-2] >= f30[-3]
                if not cd30:
                    continue
                price = get_last_price(ContextInfo, code)
                if cost <= 0.0 or price <= 0.0:
                    print('[WARN] %s cost/price unknown (%s/%s), ESI exit skipped'
                          % (code, cost, price))
                    continue
                if price < cost:
                    key30 = '%.4f|%.4f' % (f30[-2], f30[-3])
                    if LAST_ACT.get((code, 'ESI')) == key30:
                        continue
                    do_order(ContextInfo, code, 24, pos)
                    LAST_ACT[(code, 'ESI')] = key30
                    print('>>> ESI-SELL %s %d shares (30m down, price %.2f < cost %.2f)'
                          % (code, pos, price, cost))
                # floating profit: keep holding for Exit A
