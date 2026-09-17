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
import time

# ---------------- config ----------------
ACCOUNT_ID = ''              # fallback only; normally the account bound in QMT client is used
WATCHLIST_FILE = r'D:\qmt\watchlist.txt'   # lines: CODE  or  CODE,VOLUME
PENDING_FILE = r'D:\qmt\pending.csv'       # persisted R2 watch list (code,signal_key)
SIM_POS_FILE = r'D:\qmt\sim_pos.csv'       # local simulated positions (code,vol,cost)
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
T0_SET = set()               # codes allowed to sell same-day (T+0 instruments)
LAST_ACT = {}                # (code, action) -> fisher-state key, dedup per bar
PENDING = {}                 # code -> signal_key of fake-invalid signal (persisted to
                             # PENDING_FILE on every change, reloaded in init)
SIM_POS = {}                 # code -> [vol, cost, buy_date], simulation-mode ledger;
                             # real account positions always take precedence
LAST_PRINT = {}              # code -> last printed 1h fisher key (log throttle)
SIM_POS_PATH = SIM_POS_FILE  # backtest runs get a separate ledger (set in init)


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


def acc_id(ContextInfo):
    # account id must reach the C++ API as str; prefer the client-bound account.
    a = getattr(ContextInfo, 'accountid', '') or ACCOUNT_ID
    return str(a)


def get_position(ContextInfo, code):
    # returns (total_volume, cost_price, can_use_volume); cost 0.0 if unknown.
    # can_use < total under T+1 (shares bought today are frozen).
    try:
        positions = get_trade_detail_data(acc_id(ContextInfo), 'stock', 'position')
        plain = code.split('.')[0]
        for p in positions:
            pid = getattr(p, 'm_strInstrumentID', '')
            exch = getattr(p, 'm_strExchangeID', '')
            if pid == plain or ('%s.%s' % (pid, exch)) == code:
                vol = getattr(p, 'm_nVolume', 0)
                can_use = getattr(p, 'm_nCanUseVolume', vol)
                cost = 0.0
                for attr in ('m_dOpenPrice', 'm_dPositionCost', 'm_dCostPrice'):
                    c = getattr(p, attr, 0.0)
                    if c:
                        cost = float(c)
                        break
                return vol, cost, can_use
    except Exception as e:
        print('[WARN] position query failed: %s' % e)
    return 0, 0.0, 0


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
    # account id / price / volume must reach C++ as str / double / double.
    r = passorder(op, 1101, acc_id(ContextInfo), code, 5, -1.0, float(volume),
                  ContextInfo.strategyName, 1, '', ContextInfo)
    print('[ORDER] %s submitted op=%d %s vol=%d ret=%s'
          % (bar_dt(ContextInfo), op, code, volume, r))


def load_codes():
    """watchlist lines: CODE  or  CODE,VOLUME[,T0] (# comments, blank lines ok).
    Third field T0 marks intraday-round-trip instruments (T+0 ETFs); everything
    else is treated as T+1 (no same-day sell of shares bought today).
    Returns (codes, vol_map, t0_set)."""
    try:
        f = open(WATCHLIST_FILE, 'r', encoding='utf-8')
        lines = f.readlines()
        f.close()
        codes, vol_map, t0 = [], {}, set()
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
            if len(parts) > 2 and parts[2].strip().upper() == 'T0':
                t0.add(code)
        if codes:
            return codes, vol_map, t0
        print('[WARN] watchlist file empty, use fallback')
    except Exception as e:
        print('[WARN] watchlist read failed: %s, use fallback' % e)
    return list(FALLBACK_CODES), {}, set()


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


def simpos_load():
    out = {}
    try:
        f = open(SIM_POS_PATH, 'r', encoding='utf-8')
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                parts = ln.split(',')
                if len(parts) >= 3:
                    # 4th field = buy date (yyyymmdd); '' for legacy rows
                    buy_date = parts[3].strip() if len(parts) > 3 else ''
                    out[parts[0].strip()] = [int(parts[1]), float(parts[2]), buy_date]
        f.close()
    except Exception:
        pass
    return out


def simpos_save():
    try:
        f = open(SIM_POS_PATH, 'w', encoding='utf-8')
        for code, (vol, cost, buy_date) in SIM_POS.items():
            f.write('%s,%d,%.4f,%s\n' % (code, vol, cost, buy_date))
        f.close()
    except Exception as e:
        print('[WARN] sim position save failed: %s' % e)


def bar_date(ContextInfo):
    # current bar's yyyymmdd. Uses the bar's own timetag so T+1 logic works in
    # backtests (system clock would mark every historical bar as "today").
    try:
        tt = ContextInfo.get_bar_timetag(ContextInfo.barpos)
        return time.strftime('%Y%m%d', time.localtime(tt / 1000))
    except Exception:
        return time.strftime('%Y%m%d')


def bar_dt(ContextInfo):
    # current bar as 'YYYY-MM-DD HH:MM' for log lines (backtest logs would
    # otherwise only show the wall-clock run time)
    try:
        tt = ContextInfo.get_bar_timetag(ContextInfo.barpos)
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(tt / 1000))
    except Exception:
        return time.strftime('%Y-%m-%d %H:%M')


def eff_position(ContextInfo, code):
    # returns (volume, cost, sellable, is_sim); real account position wins over
    # sim ledger. sellable respects T+1: sim rows bought on the current bar's
    # date and real rows with can_use=0 cannot be sold (except T0_SET codes).
    pos, cost, can_use = get_position(ContextInfo, code)
    if pos > 0:
        if code in SIM_POS:
            SIM_POS.pop(code, None)
            simpos_save()
        sellable = pos if code in T0_SET else min(can_use, pos)
        return pos, cost, sellable, False
    s = SIM_POS.get(code)
    if s:
        sellable = s[0] if (code in T0_SET or s[2] != bar_date(ContextInfo)) else 0
        return s[0], s[1], sellable, True
    return 0, 0.0, 0, False


def simpos_on_buy(ContextInfo, code, vol, price):
    SIM_POS[code] = [vol, price, bar_date(ContextInfo)]
    simpos_save()


def simpos_on_sell(code):
    if code in SIM_POS:
        SIM_POS.pop(code, None)
        simpos_save()


def init(ContextInfo):
    global CODES, VOL_MAP, T0_SET, PENDING, SIM_POS, SIM_POS_PATH
    ContextInfo.strategyName = 'qmt_executor_v2'
    if not getattr(ContextInfo, 'accountid', ''):
        ContextInfo.accountid = ACCOUNT_ID
    if getattr(ContextInfo, 'do_back_test', False):
        # backtest: separate ledger so the live/sim file is never clobbered
        SIM_POS_PATH = SIM_POS_FILE.replace('.csv', '_backtest.csv')
    CODES, VOL_MAP, T0_SET = load_codes()
    PENDING = pending_load()
    SIM_POS = simpos_load()
    ContextInfo.set_universe(CODES)
    print('=== qmt_executor_v2 start, account=%s codes=%d vol=%d gate=%s exit_a=%s esi_exit=%s t0=%d backtest=%s pending=%s sim_pos=%s ==='
          % (acc_id(ContextInfo), len(CODES), VOLUME, USE_ENTRY_GATE, USE_EXIT_A,
             USE_ESI_EXIT, len(T0_SET), getattr(ContextInfo, 'do_back_test', False),
             list(PENDING.keys()), SIM_POS))


def handlebar(ContextInfo):
    # live/simulation: client replays all history bars through handlebar on
    # startup; only the latest bar is a real tick - skip the rest or stale
    # signals fire orders. Backtest mode must process EVERY bar, so exempt it.
    if (not getattr(ContextInfo, 'do_back_test', False)
            and hasattr(ContextInfo, 'is_last_bar')
            and not ContextInfo.is_last_bar()):
        return
    if not getattr(ContextInfo, 'do_back_test', False):
        # pre-open auction ticks (09:15-09:29) pollute the forming bar and
        # produce phantom signals; orders then also land in a non-trading
        # window and are dropped. Skip everything until the 09:30 open.
        _now = time.localtime()
        if (_now.tm_hour, _now.tm_min) < (9, 30):
            return
    bar_key = '%s %s' % (bar_dt(ContextInfo), ContextInfo.barpos)
    t_start = time.time()
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
        pos, cost, sellable, is_sim = eff_position(ContextInfo, code)
        cross_up = len(f) >= 3 and f[-1] > f[-2] and f[-2] <= f[-3]
        cross_down = len(f) >= 3 and f[-1] < f[-2] and f[-2] >= f[-3]
        # dedup key = 1h fisher state (identifies the 1h bar; barpos changes every
        # 5m tick when strategy runs on 5m period, so it cannot be the key)
        key1h = '%.4f|%.4f|%.4f' % (f[-1], f[-2], f[-3])

        pkey = '%.3f|%.3f|%d' % (f60, t60, pos)
        if LAST_PRINT.get(code) != pkey:
            LAST_PRINT[code] = pkey
            print('[%s] %s fish60=%.3f trig=%.3f pos=%d%s'
                  % (bar_key, code, f60, t60, pos, is_sim and '(sim)' or ''))

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
                        print('[SKIP] %s %s cross up but %s in down state '
                              '(fake-invalid, watching)' % (bar_dt(ContextInfo), code, '/'.join(downs)))
                        continue
                vol = VOL_MAP.get(code, VOLUME)
                do_order(ContextInfo, code, 23, vol)
                LAST_ACT[(code, 'BUY')] = key1h
                pending_remove(code)
                simpos_on_buy(ContextInfo, code, vol, get_last_price(ContextInfo, code))
                print('>>> BUY %s %s %d shares, fish60=%.3f' % (bar_dt(ContextInfo), code, vol, f60))
                continue

            # R2 reactivation watch: pending code, no fresh 1h cross needed
            if code not in PENDING:
                continue
            if f60 < t60:
                pending_remove(code)              # 1h trend broken -> void
                print('[VOID] %s %s fake-invalid signal voided (1h fish below trigger)'
                      % (bar_dt(ContextInfo), code))
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
                simpos_on_buy(ContextInfo, code, vol, get_last_price(ContextInfo, code))
                print('>>> BUY %s %s %d shares (REACTIVATED after fake-invalid), '
                      'fish60=%.3f' % (bar_dt(ContextInfo), code, vol, f60))

        else:
            if sellable <= 0:
                # T+1: shares bought today are frozen; log once per state change
                pkey2 = '%s|T1' % pkey
                if LAST_PRINT.get((code, 'T1')) != pkey2:
                    LAST_PRINT[(code, 'T1')] = pkey2
                    print('[T+1] %s %s holding %d but 0 sellable (bought today), exits deferred'
                          % (bar_dt(ContextInfo), code, pos))
                continue
            if cross_down and USE_EXIT_A:
                if LAST_ACT.get((code, 'SELL')) == key1h:
                    continue
                do_order(ContextInfo, code, 24, sellable)
                LAST_ACT[(code, 'SELL')] = key1h
                simpos_on_sell(code)
                print('>>> SELL %s %s %d shares (1h cross down), fish60=%.3f'
                      % (bar_dt(ContextInfo), code, sellable, f60))
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
                    do_order(ContextInfo, code, 24, sellable)
                    LAST_ACT[(code, 'ESI')] = key30
                    simpos_on_sell(code)
                    print('>>> ESI-SELL %s %s %d shares (30m down, price %.2f < cost %.2f)'
                          % (bar_dt(ContextInfo), code, sellable, price, cost))
                # floating profit: keep holding for Exit A
    elapsed = time.time() - t_start
    if elapsed > 30:
        print('[WARN] handlebar took %.1fs for %d codes' % (elapsed, len(CODES)))
