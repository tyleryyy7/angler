# -*- coding: utf-8 -*-
# qmt_executor_t0.py - T+0 intraday variant of qmt_executor_v2, for T+0 ETFs
# (cross-border / gold ETFs that allow same-day round trips).
#
# EXPERIMENT BRANCH: kept fully separate from qmt_executor_v2.py so old/new
# can be compared. v2 file and its data files are untouched.
#
#   * Runs on 1m bars (set strategy period to 1m in the QMT client).
#   * Timeframe ladder compressed for intraday: Context=15m, gate=5m/1m
#     (replaces the 1h/30m/15m/5m ladder of v2).
#   * Strictly intraday: entries only in [09:40, 14:40], force flat at
#     14:55, pending (R2) wiped at day rollover, stale sim positions from a
#     previous day are exited at 09:35, no overnight holds by design.
#     (Stale REAL positions cannot be dated -> init prints a WARN instead.)
#   * Extra exits: hard price stop vs cost; account-level daily circuit
#     breaker (max trades per code / max daily loss, then no new entries).
#
# Entry (R1): 15m fisher cross UP and neither 5m nor 1m is in down state
#             (fish < trigger) and daily filter passes (last COMPLETED day
#             bar, DAY_MODE up/above) and no position -> buy VOLUME shares.
# Exit A    : 5m fisher cross DOWN -> sell all (normal trend exit).
# ESI (R3)  : TF_ESI (default 1m) fisher cross DOWN and price < cost -> sell all
#             (failed trade); floating profit -> keep holding for Exit A / stop.
# STOP      : price < cost*(1-STOP_PCT) -> sell all immediately.
# FLAT      : bar time >= FORCE_FLAT_AT -> sell all regardless of state.
#
# Fisher: Pine/THS standard, hl2 input, window 9 - bit-identical to
# fisher_scanner.py / qmt_executor_v2.py.
# Pure ASCII, Python 3.6, save as UTF-8. All file paths must be ASCII-only.

import math
import time

# ---------------- config ----------------
ACCOUNT_ID = ''                  # fallback only; normally client-bound account
WATCHLIST_FILE = r'D:\qmt\watchlist_t0.txt'   # lines: CODE,VOLUME[,T0]
PENDING_FILE = r'D:\qmt\pending_t0.csv'       # R2 watch list, same-day only
SIM_POS_FILE = r'D:\qmt\sim_pos_t0.csv'       # sim ledger (code,vol,cost,date)
DAILY_FILE = r'D:\qmt\daily_t0.csv'           # per-day counters + pnl (circuit breaker)
FALLBACK_CODES = ['518880.SH']
LENGTH = 9
HIST_BARS = 120
VOLUME = 10000                   # MUST be sized so price*vol >= 50000 (min commission 5 CNY applies)
RUN_PERIOD = '1m'                # strategy run period in the QMT client
TF_CTX = '15m'                   # Context: primary cross
TF_MID = '5m'                    # gate + normal exit (Exit A)
TF_FAST = '1m'                   # gate (entry side)
TF_ESI = TF_FAST                 # ESI exit timeframe; '5m' = calmer failed-trade stop

STOP_PCT = 0.005                 # hard stop vs cost, 0.005 = 0.5%
ENTRY_FROM = '09:40'             # no entries before (open volatility)
ENTRY_TO = '14:40'               # no new entries after
FORCE_FLAT_AT = '14:55'          # exit everything (before 14:57 SZ closing auction)
STALE_EXIT_AT = '09:35'          # sim positions carried from yesterday -> exit
MAX_TRADES_PER_CODE = 10         # entry count limit per code per day
MAX_DAILY_LOSS = -300.0          # account-level realized pnl floor; then flat-only
FEE_MIN_NOTIONAL = 50000.0       # below this the 5-yuan commission floor dominates

USE_HARD_STOP = True
USE_ENTRY_GATE = True            # 5m/1m not-in-down gate on entry
USE_EXIT_A = True                # 5m fisher cross down -> sell all
USE_ESI_EXIT = True              # TF_ESI cross down + floating loss -> sell all
USE_DAY_FILTER = True            # daily fisher filter on entries
DAY_MODE = 'up'                  # 'up' = daily fish rising; 'above' = not in down state

# Optional A/B backtest overrides, KEY=VALUE per line, e.g.:
#   USE_ESI_EXIT=0
#   TF_ESI=5m
#   USE_DAY_FILTER=0
#   DAY_MODE=above
# Whitelisted keys only; unknown keys are ignored with a WARN.
CONFIG_FILE = r'D:\qmt\t0_config.txt'
# ----------------------------------------

CODES = []
VOL_MAP = {}
T0_SET = set()
PENDING = {}
SIM_POS = {}
TRADES_TODAY = {}                # code -> entry count today
CODE_PNL = {}                    # code -> realized pnl today (sim or known cost)
DAY_DATE = ''
DAY_PNL = 0.0
DAY_STOPPED = False
LAST_ACT = {}
LAST_PRINT = {}
DAY_STATE = {}                   # (code, yyyymmdd) -> bool, daily filter cache
SIM_POS_PATH = SIM_POS_FILE
DAILY_PATH = DAILY_FILE


def fisher_series(high, low, length):
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
    # (fish_last, trig_last, is_down); None if data insufficient
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


def tf_series(ContextInfo, code, period):
    high = get_hist(ContextInfo, code, period, 'high')
    low = get_hist(ContextInfo, code, period, 'low')
    if len(high) < 3:
        return []
    return fisher_series(high, low, LENGTH)


def day_ok(ContextInfo, code):
    # daily fisher filter on the last COMPLETED day bar (today's bar is
    # dropped: it flickers intraday). None if data insufficient.
    ck = (code, bar_date(ContextInfo))
    if ck in DAY_STATE:
        return DAY_STATE[ck]
    try:
        f = tf_series(ContextInfo, code, '1d')
    except Exception as e:
        print('[ERR] %s 1d history failed: %s' % (code, e))
        return None
    if len(f) < 4:
        return None
    done = f[:-1]
    ok = done[-1] > done[-2] if DAY_MODE == 'up' else done[-1] >= done[-2]
    DAY_STATE[ck] = ok
    print('[DAY] %s daily fisher %.3f -> %.3f mode=%s pass=%s'
          % (code, done[-2], done[-1], DAY_MODE, ok))
    return ok


def day_pass(ContextInfo, code, tag):
    # True = allowed; False/None = blocked (None = data unknown)
    if not USE_DAY_FILTER:
        return True
    ok = day_ok(ContextInfo, code)
    if ok is None:
        print('[WARN] %s daily filter data unknown, skip %s' % (code, tag))
        return False
    if not ok:
        print('[SKIP] %s %s blocked by daily filter (mode=%s)'
              % (bar_dt(ContextInfo), code, DAY_MODE))
        return False
    return True


def acc_id(ContextInfo):
    a = getattr(ContextInfo, 'accountid', '') or ACCOUNT_ID
    return str(a)


def get_position(ContextInfo, code):
    # (total_volume, cost_price, can_use_volume); cost 0.0 if unknown
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
        d = ContextInfo.get_history_data(2, RUN_PERIOD, 'close', code)
        c = list(d[code])
        if c:
            return float(c[-1])
    except Exception:
        pass
    return 0.0


def do_order(ContextInfo, code, op, volume):
    # op: 23=buy 24=sell. priceType 5=market. Verified 2026-09-12 on big-QMT.
    r = passorder(op, 1101, acc_id(ContextInfo), code, 5, -1.0, float(volume),
                  ContextInfo.strategyName, 1, '', ContextInfo)
    print('[ORDER] %s submitted op=%d %s vol=%d ret=%s'
          % (bar_dt(ContextInfo), op, code, volume, r))


def config_override():
    # optional KEY=VALUE lines for A/B backtests; whitelisted keys only
    global USE_HARD_STOP, USE_ENTRY_GATE, USE_EXIT_A, USE_ESI_EXIT
    global USE_DAY_FILTER, DAY_MODE
    global TF_ESI, STOP_PCT, MAX_DAILY_LOSS, MAX_TRADES_PER_CODE
    bools = {'USE_HARD_STOP': 'USE_HARD_STOP', 'USE_ENTRY_GATE': 'USE_ENTRY_GATE',
             'USE_EXIT_A': 'USE_EXIT_A', 'USE_ESI_EXIT': 'USE_ESI_EXIT',
             'USE_DAY_FILTER': 'USE_DAY_FILTER'}
    try:
        f = open(CONFIG_FILE, 'r', encoding='utf-8')
        lines = f.readlines()
        f.close()
    except Exception:
        return
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith('#') or '=' not in ln:
            continue
        k, v = ln.split('=', 1)
        k, v = k.strip().upper(), v.strip()
        if k in bools:
            globals()[bools[k]] = v not in ('0', 'false', 'False', 'no')
        elif k == 'TF_ESI':
            TF_ESI = v
        elif k == 'DAY_MODE':
            if v in ('up', 'above'):
                DAY_MODE = v
            else:
                print('[WARN] bad DAY_MODE %s, ignored' % v)
                continue
        elif k == 'STOP_PCT':
            STOP_PCT = float(v)
        elif k == 'MAX_DAILY_LOSS':
            MAX_DAILY_LOSS = float(v)
        elif k == 'MAX_TRADES_PER_CODE':
            MAX_TRADES_PER_CODE = int(v)
        else:
            print('[WARN] config override ignored: %s' % ln)
            continue
        print('[CFG] override %s = %s' % (k, v))


def load_codes():
    """watchlist lines: CODE,VOLUME[,T0]. Third field T0 marks T+0 ETFs;
    codes without it are treated as T+1 and cannot be sold same-day."""
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


def daily_load():
    # returns (trades_dict, code_pnl_dict, pnl_sum, stopped) for DAY_DATE
    trades, code_pnl, pnl, stopped = {}, {}, 0.0, False
    try:
        f = open(DAILY_PATH, 'r', encoding='utf-8')
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith('#'):
                continue
            p = ln.split(',')
            if len(p) >= 5 and p[0] == DAY_DATE:
                trades[p[1]] = int(p[2])
                code_pnl[p[1]] = float(p[3])
                pnl += float(p[3])
                if p[4] == '1':
                    stopped = True
        f.close()
    except Exception:
        pass
    return trades, code_pnl, pnl, stopped


def daily_save():
    try:
        f = open(DAILY_PATH, 'w', encoding='utf-8')
        for code in CODES:
            f.write('%s,%s,%d,%.2f,%d\n' % (DAY_DATE, code,
                    TRADES_TODAY.get(code, 0), CODE_PNL.get(code, 0.0),
                    1 if DAY_STOPPED else 0))
        f.close()
    except Exception as e:
        print('[WARN] daily save failed: %s' % e)


def bar_date(ContextInfo):
    try:
        tt = ContextInfo.get_bar_timetag(ContextInfo.barpos)
        return time.strftime('%Y%m%d', time.localtime(tt / 1000))
    except Exception:
        return time.strftime('%Y%m%d')


def bar_dt(ContextInfo):
    try:
        tt = ContextInfo.get_bar_timetag(ContextInfo.barpos)
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(tt / 1000))
    except Exception:
        return time.strftime('%Y-%m-%d %H:%M')


def bar_hhmm(ContextInfo):
    try:
        tt = ContextInfo.get_bar_timetag(ContextInfo.barpos)
        return time.strftime('%H:%M', time.localtime(tt / 1000))
    except Exception:
        return time.strftime('%H:%M')


def eff_position(ContextInfo, code):
    # real account position wins; sellable respects T+1 except T0_SET codes
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
    global CODES, VOL_MAP, T0_SET, PENDING, SIM_POS, SIM_POS_PATH, DAILY_PATH
    global DAY_DATE, DAY_PNL, DAY_STOPPED
    ContextInfo.strategyName = 'qmt_executor_t0'
    if not getattr(ContextInfo, 'accountid', ''):
        ContextInfo.accountid = ACCOUNT_ID
    config_override()
    if getattr(ContextInfo, 'do_back_test', False):
        SIM_POS_PATH = SIM_POS_FILE.replace('.csv', '_backtest.csv')
        DAILY_PATH = DAILY_FILE.replace('.csv', '_backtest.csv')
    CODES, VOL_MAP, T0_SET = load_codes()
    PENDING = pending_load()
    SIM_POS = simpos_load()
    DAY_DATE = time.strftime('%Y%m%d')
    if getattr(ContextInfo, 'do_back_test', False):
        TRADES_TODAY.clear(); CODE_PNL.clear()
        DAY_PNL, DAY_STOPPED = 0.0, False
    else:
        t, cp, p, s = daily_load()
        TRADES_TODAY.clear(); TRADES_TODAY.update(t)
        CODE_PNL.clear(); CODE_PNL.update(cp)
        DAY_PNL, DAY_STOPPED = p, s
    ContextInfo.set_universe(CODES)
    for code in CODES:
        pos, _, _ = get_position(ContextInfo, code)
        if pos > 0 and code not in SIM_POS:
            print('[WARN] %s real position %d without sim ledger entry: if carried '
                  'overnight, STALE exit does not apply, manual check needed'
                  % (code, pos))
    print('=== qmt_executor_t0 start, account=%s codes=%d vol=%d period=%s stop=%.3f '
          'gate=%s exit_a=%s esi=%s esi_tf=%s day_filter=%s/%s flat=%s max_trades=%d max_loss=%.0f backtest=%s '
          'pending=%s trades=%s pnl=%.2f stopped=%s t0=%d ==='
          % (acc_id(ContextInfo), len(CODES), VOLUME, RUN_PERIOD, STOP_PCT,
             USE_ENTRY_GATE, USE_EXIT_A, USE_ESI_EXIT, TF_ESI, USE_DAY_FILTER, DAY_MODE, FORCE_FLAT_AT,
             MAX_TRADES_PER_CODE, MAX_DAILY_LOSS,
             getattr(ContextInfo, 'do_back_test', False),
             list(PENDING.keys()), TRADES_TODAY, DAY_PNL, DAY_STOPPED, len(T0_SET)))


def on_sell(ContextInfo, code, sellable, cost, reason, key, is_sim):
    # shared sell path: order, ledger, pnl, circuit breaker
    global DAY_PNL, DAY_STOPPED
    price = get_last_price(ContextInfo, code)
    do_order(ContextInfo, code, 24, sellable)
    LAST_ACT[(code, reason)] = key
    simpos_on_sell(code)
    if price > 0.0 and cost > 0.0:
        d = (price - cost) * sellable
        CODE_PNL[code] = CODE_PNL.get(code, 0.0) + d
        DAY_PNL += d
    daily_save()
    print('>>> SELL %s %s %d shares (%s), pnl_day=%.2f'
          % (bar_dt(ContextInfo), code, sellable, reason, DAY_PNL))
    if DAY_PNL <= MAX_DAILY_LOSS and not DAY_STOPPED:
        DAY_STOPPED = True
        daily_save()
        print('!!! CIRCUIT BREAKER: day pnl %.2f <= %.2f, no new entries today'
              % (DAY_PNL, MAX_DAILY_LOSS))


def handlebar(ContextInfo):
    global DAY_DATE, DAY_PNL, DAY_STOPPED
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
    today = bar_date(ContextInfo)
    if today != DAY_DATE:
        # day rollover: T0 branch never carries pending or counters overnight
        DAY_DATE = today
        TRADES_TODAY.clear()
        CODE_PNL.clear()
        DAY_STATE.clear()
        DAY_PNL = 0.0
        DAY_STOPPED = False
        if PENDING:
            PENDING.clear()
            pending_save()
        daily_save()
        print('=== new day %s, counters and pending reset ===' % today)
    bar_key = '%s %s' % (bar_dt(ContextInfo), ContextInfo.barpos)
    hhmm = bar_hhmm(ContextInfo)
    t_start = time.time()
    for code in CODES:
        try:
            f = tf_series(ContextInfo, code, TF_CTX)
        except Exception as e:
            print('[ERR] %s %s history failed: %s' % (code, TF_CTX, e))
            continue
        if len(f) < 3:
            print('[WAIT] %s %s warming up' % (code, TF_CTX))
            continue
        f60, t60 = f[-1], f[-2]
        key_ctx = '%.4f|%.4f|%.4f' % (f[-1], f[-2], f[-3])
        cross_up = f[-1] > f[-2] and f[-2] <= f[-3]
        pos, cost, sellable, is_sim = eff_position(ContextInfo, code)
        price = 0.0

        pkey = '%.3f|%.3f|%d' % (f60, t60, pos)
        if LAST_PRINT.get(code) != pkey:
            LAST_PRINT[code] = pkey
            print('[%s] %s fish15=%.3f trig=%.3f pos=%d pnl_day=%.2f%s'
                  % (bar_key, code, f60, t60, pos, DAY_PNL, is_sim and '(sim)' or ''))

        if pos > 0:
            if sellable <= 0:
                pkey2 = '%s|T1' % pkey
                if LAST_PRINT.get((code, 'T1')) != pkey2:
                    LAST_PRINT[(code, 'T1')] = pkey2
                    print('[T+1] %s %s holding %d but 0 sellable, exits deferred'
                          % (bar_dt(ContextInfo), code, pos))
                continue
            s = SIM_POS.get(code)
            is_stale = bool(s) and s[2] != today
            if is_stale and hhmm >= STALE_EXIT_AT:
                on_sell(ContextInfo, code, sellable, cost, 'STALE', key_ctx, is_sim)
                continue
            if hhmm >= FORCE_FLAT_AT:
                on_sell(ContextInfo, code, sellable, cost, 'FLAT', key_ctx, is_sim)
                continue
            price = get_last_price(ContextInfo, code)
            if USE_HARD_STOP and cost > 0.0 and price > 0.0 \
                    and price < cost * (1.0 - STOP_PCT):
                on_sell(ContextInfo, code, sellable, cost, 'STOP',
                        '%s|%.4f' % (key_ctx, price), is_sim)
                continue
            # structure exits: ESI on TF_ESI, normal exit on 5m
            cd_mid = False
            cd_esi = False
            if USE_EXIT_A or USE_ESI_EXIT:
                try:
                    if USE_EXIT_A:
                        f_mid = tf_series(ContextInfo, code, TF_MID)
                        cd_mid = len(f_mid) >= 3 and f_mid[-1] < f_mid[-2] \
                            and f_mid[-2] >= f_mid[-3]
                    if USE_ESI_EXIT:
                        f_esi = tf_series(ContextInfo, code, TF_ESI)
                        cd_esi = len(f_esi) >= 3 and f_esi[-1] < f_esi[-2] \
                            and f_esi[-2] >= f_esi[-3]
                except Exception:
                    continue
            if USE_ESI_EXIT and cd_esi and cost > 0.0 and price > 0.0 \
                    and price < cost:
                key_esi = '%.4f|%.4f' % (f_esi[-2], f_esi[-3])
                if LAST_ACT.get((code, 'ESI')) != key_esi:
                    on_sell(ContextInfo, code, sellable, cost, 'ESI', key_esi, is_sim)
                    continue
                # floating profit: keep holding for Exit A / stop / flat
            if USE_EXIT_A and cd_mid:
                key_mid = '%.4f|%.4f' % (f_mid[-2], f_mid[-3])
                if LAST_ACT.get((code, 'EXIT_A')) != key_mid:
                    on_sell(ContextInfo, code, sellable, cost, 'EXIT_A', key_mid, is_sim)
                    continue
            continue

        # ---- pos == 0: entry side ----
        # VOID is signal cancellation, not an entry: runs regardless of the
        # entry time window / circuit breaker.
        if code in PENDING and f60 < t60:
            pending_remove(code)
            print('[VOID] %s %s fake-invalid voided (15m fish below trigger)'
                  % (bar_dt(ContextInfo), code))
            continue
        if DAY_STOPPED:
            pkey3 = '%s|CB' % pkey
            if LAST_PRINT.get((code, 'CB')) != pkey3:
                LAST_PRINT[(code, 'CB')] = pkey3
                print('[CB] %s %s day pnl %.2f hit breaker, entries blocked'
                      % (bar_dt(ContextInfo), code, DAY_PNL))
            continue
        if hhmm < ENTRY_FROM or hhmm > ENTRY_TO:
            continue
        if TRADES_TODAY.get(code, 0) >= MAX_TRADES_PER_CODE:
            continue

        def gates_clear():
            # gate: 5m and 1m both not in down state; None -> data unknown
            for tf in (TF_MID, TF_FAST):
                st = tf_state(ContextInfo, code, tf)
                if st is None:
                    return None
                if st[2]:
                    return False
            return True

        if cross_up and LAST_ACT.get((code, 'BUY')) != key_ctx:
            g = gates_clear() if USE_ENTRY_GATE else True
            if g is None:
                print('[WARN] %s gate data unknown, skip bar' % code)
                continue
            if g is False:
                pending_add(code, key_ctx)
                print('[SKIP] %s %s 15m cross up but gate down '
                      '(fake-invalid, watching)' % (bar_dt(ContextInfo), code))
                continue
            if not day_pass(ContextInfo, code, 'BUY'):
                continue
            buy(ContextInfo, code, f60, key_ctx, 'BUY')
            continue

        # R2 reactivation watch (same-day only; wiped at day rollover)
        if code not in PENDING:
            continue
        g = gates_clear() if USE_ENTRY_GATE else True
        if g:
            if LAST_ACT.get((code, 'BUY')) == key_ctx:
                continue
            if not day_pass(ContextInfo, code, 'REACTIVATED'):
                continue
            buy(ContextInfo, code, f60, key_ctx, 'REACTIVATED')
    elapsed = time.time() - t_start
    if elapsed > 30:
        print('[WARN] handlebar took %.1fs for %d codes' % (elapsed, len(CODES)))


def buy(ContextInfo, code, f_ctx, key_ctx, tag):
    global DAY_PNL
    vol = VOL_MAP.get(code, VOLUME)
    price = get_last_price(ContextInfo, code)
    if price > 0.0 and price * vol < FEE_MIN_NOTIONAL:
        print('[WARN] %s notional %.0f < %.0f: 5-yuan commission floor eats the '
              'edge, check volume' % (code, price * vol, FEE_MIN_NOTIONAL))
    do_order(ContextInfo, code, 23, vol)
    LAST_ACT[(code, 'BUY')] = key_ctx
    pending_remove(code)
    TRADES_TODAY[code] = TRADES_TODAY.get(code, 0) + 1
    simpos_on_buy(ContextInfo, code, vol, price)
    daily_save()
    print('>>> BUY %s %s %d shares (%s), fish15=%.3f trades_today=%d'
          % (bar_dt(ContextInfo), code, vol, tag, f_ctx, TRADES_TODAY[code]))