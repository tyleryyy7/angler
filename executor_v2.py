# -*- coding: utf-8 -*-
# qmt_executor_v2.py - big-QMT built-in strategy, aligned with scanner Fisher-ESI rules v2.
#
# Entry (R1): 1h fisher cross UP and none of 30m/15m/5m is in down state
#             (fish < trigger) and no position -> buy VOLUME shares.
# Exit A    : mirrors the entry side: 1h fisher cross DOWN with magnitude
#             (trigger - fish) >= MIN_CROSS and ALL of 15m/30m/5m in down state
#             -> sell all. Hairline down-crosses are ignored; small tfs not all
#             down = fake-invalid exit -> defer + watch, void if 1h fish
#             recovers above its trigger.
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
ACCOUNT_ID = '8891080156'              # fallback only; normally the account bound in QMT client is used
WATCHLIST_FILE = r'D:\qmt\watchlist.txt'   # lines: CODE  or  CODE,VOLUME
PENDING_FILE = r'D:\qmt\pending.csv'       # persisted R2 watch list (code,signal_key)
SIM_POS_FILE = r'D:\qmt\sim_pos.csv'       # local simulated positions (code,vol,cost)
EXDEF_FILE = r'D:\qmt\exdef.csv'           # deferred exits (code,reason): latched
                                           # while T+1-frozen, executed on sellable
FALLBACK_CODES = ['600519.SH']
LENGTH = 9                   # fisher window
HIST_BARS = 120              # bars fetched per timeframe per call
VOLUME = 100                 # default shares per BUY order (per-code override in watchlist)
USE_ENTRY_GATE = True        # R1: small-timeframe resonance gate (30m/15m/5m not down)
MIN_CROSS = 0.15             # SELL-side magnitude floor only (exit, 09-29b).
MIN_CROSS_ENTRY = 0.0        # entry floor REMOVED 2026-09-30: thick-sample sweep
                             # (231 codes x 10.5mo, ESI-off base) monotone -
                             # 0.00 beats 0.15 by +127k; hairline bucket win rate
                             # lower (42% vs 46%) but expectancy +0.27%/trade.
                             # HSSR old study measured win rate only.
MAX_F60 = 2.5                # entry cap: fish60 >= 2.5 skipped (high-position
                             # bucket gross negative, 2026 alone -19k; caps
                             # 1.5/2.0/2.5 statistically tied, 2.5 matches the
                             # scanner esi_register convention)
ENTRY_TO = '14:40'           # no fresh entries after this time: 15:00-bar signals
                             # are the worst time bucket (ret10 ~-0.4~-0.7%, HSSR
                             # ~38-44%). Mirrors executor_t0 ENTRY_TO.
VERIFY_WAIT_SEC = 90         # min seconds between order submit and first fill
                             # check: handlebar is tick-driven live (~3s), but the
                             # position query lags the fill push by seconds;
                             # checking earlier caused phantom retries that
                             # double/triple-filled (2026-09-28 incident)
USE_EXIT_A = True            # Exit A: 1h fisher cross down -> sell all (normal exit)
USE_ESI_EXIT = True          # R3 ESI master switch - per-instrument since
                             # 2026-09-30: stocks (60/00) OFF (4-layer thick-sample
                             # evidence: -258k on falling-knife pool, 2026-09-29),
                             # T0 ETFs ON (+12k, trending instruments).
                             # See esi_enabled().
EXIT_CONFIRM_TFS = ('15m', '30m', '5m')   # Exit A sell gate (mirrors the entry
                             # gate): sell only when ALL these small tfs are in
                             # down state; otherwise fake-invalid exit -> defer
                             # + watch (PSELL), void if 1h fish recovers above
                             # its trigger. Down-crosses thinner than MIN_CROSS
                             # are ignored entirely (same floor as entries).
OPEN_GUARD_UNTIL = '09:35'   # no orders before this time: at the 09:30 open the
                             # forming 1h bar holds a single tick (high==low) and
                             # cross readings on it are degenerate, and the trade
                             # session's position cache is still syncing (2026-09-29:
                             # 3 positions dumped on one-tick cross-downs at
                             # 09:30:00.5 + a buy fired while a real position was
                             # invisible to the position query). Mirrors
                             # executor_t0 STALE_EXIT_AT.
ETF_ENTRIES = True             # ETF (non 60/00) NEW-ENTRY switch; False = manage
                             # exits only, no fresh ETF buys. Hot-reloaded from
                             # CONFIG_FILE every handlebar (no restart needed).
CONFIG_FILE = r'D:\qmt\v2_config.txt'   # optional KEY=VALUE lines, whitelisted:
                                        #   ETF_ENTRIES=0|1
REV = '2026-09-30c'              # bumped on every repo edit; printed in the start
                                 # banner so the deployed copy's version is always
                                 # identifiable from the client log (a stale paste
                                 # once ran silently for a whole day)
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
MD2_WARNED = set()           # market_data2 empty-result warnings already printed
PSELL = {}                   # code -> key1h of deferred Exit A (sell gate not
                             # fully down; in-memory, lost on restart)
VERIFY = {}                  # code -> [side, base_pos, vol, tries, tag, submit_ts]:
                             # fill check VERIFY_WAIT_SEC after submit; retry the
                             # missing remainder once, then WeCom alert
WEBHOOK_KEY_FILE = r'D:\angler\webhook.key'
EXDEF = {}                   # code -> reason: exit signal latched while T+1-frozen
                             # (persisted; executed as soon as shares are sellable)
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
    # Primary: get_market_data2 via the pandas-free wrapper (old
    # get_history_data can return history ending yesterday intraday).
    try:
        d = ContextInfo.get_market_data_ex_ori([field], [code], period=period,
                                               count=HIST_BARS, dividend_type='none',
                                               fill_data=True, subscribe=True)
        data = d.get(code) if isinstance(d, dict) else None
        if data is None and isinstance(d, dict) and len(d) == 1:
            data = list(d.values())[0]      # key format may differ from code
        if isinstance(data, dict):
            # {field: [v, ...], 'stime': [...]} shape
            vals = data.get(field)
            if vals:
                return [float(x) for x in list(vals)[-HIST_BARS:]]
        elif data:
            # [[stime, v, ...], ...] row shape
            rows = sorted(data, key=lambda r: r[0])
            return [float(r[-1]) for r in rows[-HIST_BARS:]]
        wkey = '%s|%s' % (code, period)
        if wkey not in MD2_WARNED:
            MD2_WARNED.add(wkey)
            print('[WARN] %s %s market_data2 empty, keys=%s -> fallback'
                  % (code, period, list(d.keys())[:3] if isinstance(d, dict) else type(d)))
    except Exception as e:
        print('[WARN] %s %s market_data2 failed, fallback: %s' % (code, period, e))
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
        c = get_hist(ContextInfo, code, '5m', 'close')[-2:]
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


def wecom_alert(msg):
    # best-effort WeCom robot push, stdlib only (QMT builtin env has no requests)
    try:
        f = open(WEBHOOK_KEY_FILE, 'r')
        key = f.read().strip()
        f.close()
        import json
        import urllib.request
        body = json.dumps({'msgtype': 'text',
                           'text': {'content': msg}}).encode('utf-8')
        req = urllib.request.Request(
            'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=' + key,
            data=body, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print('[WARN] wecom alert failed: %s' % e)


def is_etf(code):
    return not code.split('.')[0].startswith(('60', '00'))


def esi_enabled(code):
    # per-instrument ESI switch (thick-sample 2026-09-29/30): harmful on
    # falling-knife stock pools (-258k), positive on trending T0 ETFs (+12k)
    if not USE_ESI_EXIT:
        return False
    return is_etf(code)


def config_override():
    # optional KEY=VALUE lines, whitelisted keys only; re-read every handlebar
    # so ETF_ENTRIES flips take effect intraday without a strategy restart
    global ETF_ENTRIES
    try:
        f = open(CONFIG_FILE, 'r', encoding='utf-8')
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith('#') or '=' not in ln:
                continue
            k, v = ln.split('=', 1)
            k, v = k.strip().upper(), v.strip()
            if k == 'ETF_ENTRIES':
                new = v not in ('0', 'false', 'False', 'no')
                if new != ETF_ENTRIES:
                    print('[CONFIG] ETF_ENTRIES %s -> %s' % (ETF_ENTRIES, new))
                    ETF_ENTRIES = new
        f.close()
    except Exception:
        pass


def strict_position(ContextInfo, code):
    # position volume only; None when the query itself failed (untrustworthy).
    # Fill verification must never act on a failed query (get_position swallows
    # exceptions and returns 0, which would cause bogus retries / double orders).
    try:
        positions = get_trade_detail_data(acc_id(ContextInfo), 'stock', 'position')
        plain = code.split('.')[0]
        for p in positions:
            pid = getattr(p, 'm_strInstrumentID', '')
            exch = getattr(p, 'm_strExchangeID', '')
            if pid == plain or ('%s.%s' % (pid, exch)) == code:
                return getattr(p, 'm_nVolume', 0)
        return 0
    except Exception as e:
        print('[WARN] %s verify position query failed: %s' % (code, e))
        return None


def verify_fills(ContextInfo):
    # handlebar is tick-driven live (~3s L1 snapshots), NOT per bar: the first
    # check would run ~3s after submit, long before the fill reaches the
    # position query (2026-09-28: retry fired 2.9s after submit, fill push
    # arrived 0.1s later -> double fill; another name triple-filled). Wait
    # VERIFY_WAIT_SEC before the first check, then: unfilled -> retry the
    # missing remainder once -> drop phantom + WeCom alert.
    for code in list(VERIFY.keys()):
        rec = VERIFY[code]
        side, base, vol, tries, tag = rec[:5]
        if time.time() - rec[5] < VERIFY_WAIT_SEC:
            continue
        pos = strict_position(ContextInfo, code)
        if pos is None:
            continue                       # unknown, check again next tick
        done = pos >= base + vol if side == 'BUY' else pos <= base - vol
        if done:
            print('[VERIFY] %s %s x%d filled (pos %d->%d)'
                  % (code, side, vol, base, pos))
            del VERIFY[code]
            continue
        if tries == 0:
            rec[3] = 1
            rec[5] = time.time()           # re-arm the wait for the retry
            filled = (pos - base) if side == 'BUY' else (base - pos)
            miss = vol - max(0, filled)    # retry only the missing remainder
            print('[VERIFY] %s %s x%d NOT filled (pos %d, base %d), retry x%d'
                  % (code, side, vol, pos, base, miss))
            do_order(ContextInfo, code, 23 if side == 'BUY' else 24, miss)
            continue
        del VERIFY[code]
        if side == 'BUY' and pos <= base and code in SIM_POS:
            simpos_on_sell(code)           # phantom entry, never really bought
        msg = ('QMT FILL FAIL: %s %s x%d not filled after retry '
               '(pos %d, base %d), manual check needed'
               % (side, code, vol, pos, base))
        print('[VERIFY] !!! %s' % msg)
        wecom_alert(msg)


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


def exdef_load():
    out = {}
    try:
        f = open(EXDEF_FILE, 'r', encoding='utf-8')
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                parts = ln.split(',')
                code = parts[0].strip()
                reason = parts[1].strip() if len(parts) > 1 else 'DEFER'
                rev = parts[2].strip() if len(parts) > 2 else ''
                if rev != REV:
                    # latch written by another ruleset: drop it. handlebar
                    # re-evaluates exits every bar - if the exit still
                    # qualifies under the CURRENT rules it re-latches on the
                    # spot, so dropping costs nothing (2026-09-30: a thin-cross
                    # latch from REV 09-28a auction-executed after the 09-29b
                    # deploy)
                    print('[EXDEF] %s latch from rev=%s dropped (current %s)'
                          % (code, rev or '?', REV))
                    continue
                out[code] = reason
        f.close()
    except Exception:
        pass
    return out


def exdef_save():
    try:
        f = open(EXDEF_FILE, 'w', encoding='utf-8')
        for code, reason in EXDEF.items():
            f.write('%s,%s,%s\n' % (code, reason, REV))
        f.close()
    except Exception as e:
        print('[WARN] exdef save failed: %s' % e)


def esi_cross_down(ContextInfo, code):
    # 30m-series cross-down test (shared by the ESI exit and the frozen latch)
    try:
        h30 = get_hist(ContextInfo, code, '30m', 'high')
        l30 = get_hist(ContextInfo, code, '30m', 'low')
        f30 = fisher_series(h30, l30, LENGTH)
    except Exception:
        return False
    return len(f30) >= 3 and f30[-1] < f30[-2] and f30[-2] >= f30[-3]


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


def slot1h(ContextInfo):
    # stable identity of the 1h bar currently forming (A=09:30-10:30,
    # B=10:30-11:30, C=13:00-14:00, D=14:00-15:00), used as the dedup key
    # component. Derived from the bar clock, so it is identical for every tick
    # inside the same 1h bar - unlike live fisher values, which drift per tick.
    dt = bar_dt(ContextInfo)
    hm = dt[11:16]
    slot = ('A' if hm <= '10:30' else
            'B' if hm <= '11:30' else
            'C' if hm <= '14:00' else 'D')
    return '%s %s' % (dt[:10], slot)


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
    global CODES, VOL_MAP, T0_SET, PENDING, SIM_POS, SIM_POS_PATH, EXDEF
    ContextInfo.strategyName = 'qmt_executor_v2'
    if not getattr(ContextInfo, 'accountid', ''):
        ContextInfo.accountid = ACCOUNT_ID
    if getattr(ContextInfo, 'do_back_test', False):
        # backtest: separate ledger so the live/sim file is never clobbered
        SIM_POS_PATH = SIM_POS_FILE.replace('.csv', '_backtest.csv')
    CODES, VOL_MAP, T0_SET = load_codes()
    PENDING = pending_load()
    EXDEF.update(exdef_load())
    SIM_POS = simpos_load()
    ContextInfo.set_universe(CODES)
    print('=== qmt_executor_v2 rev=%s start, account=%s codes=%d vol=%d gate=%s exit_a=%s esi_exit=%s sell_gate=%s etf_entries=%s t0=%d backtest=%s pending=%s sim_pos=%s ==='
          % (REV, acc_id(ContextInfo), len(CODES), VOLUME, USE_ENTRY_GATE, USE_EXIT_A,
             (USE_ESI_EXIT and 'stock-off/etf-on' or 'off'), '/'.join(EXIT_CONFIRM_TFS),
             ETF_ENTRIES,
             len(T0_SET), getattr(ContextInfo, 'do_back_test', False),
             list(PENDING.keys()), SIM_POS))


def handlebar(ContextInfo):
    # live/simulation: client replays all history bars through handlebar on
    # startup; only the latest bar is a real tick - skip the rest or stale
    # signals fire orders. Backtest mode must process EVERY bar, so exempt it.
    if (not getattr(ContextInfo, 'do_back_test', False)
            and hasattr(ContextInfo, 'is_last_bar')
            and not ContextInfo.is_last_bar()):
        return
    config_override()
    if not getattr(ContextInfo, 'do_back_test', False):
        # pre-open auction ticks (09:15-09:29) pollute the forming bar and
        # produce phantom signals; orders then also land in a non-trading
        # window and are dropped. Skip everything until the 09:30 open.
        # The first minutes after the open are guarded too: the forming 1h bar
        # is degenerate (single tick) and the position cache is still syncing.
        _now = time.localtime()
        if (_now.tm_hour, _now.tm_min) < (9, 30):
            return
        if time.strftime('%H:%M') < OPEN_GUARD_UNTIL:
            return
    bar_key = '%s %s' % (bar_dt(ContextInfo), ContextInfo.barpos)
    hb = int(time.time() // 300)
    if LAST_PRINT.get('_hb') != hb:
        # heartbeat every 5 min so log silence is distinguishable from a stall
        LAST_PRINT['_hb'] = hb
        print('[HB] %s alive codes=%d' % (bar_key, len(CODES)))
    t_start = time.time()
    if not getattr(ContextInfo, 'do_back_test', False):
        verify_fills(ContextInfo)
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
        if cross_down and t60 - f60 < MIN_CROSS:
            # magnitude floor, mirrored from the entry side: hairline
            # down-crosses whipsaw deep-negative names at every small gap
            # (2026-09-29 open dump); ignore them entirely (also blocks the
            # T+1 EXDEF latch below)
            cross_down = False
            print('[SKIP] %s %s down-cross too thin %.3f < %.2f (hairline), ignored'
                  % (bar_key, code, t60 - f60, MIN_CROSS))
        # dedup key = identity of the 1h bar currently forming. Do NOT use the
        # fisher values themselves: live handlebar is tick-driven (~3s L1
        # snapshots) and the forming bar's fisher drifts every tick, so a
        # value-keyed LAST_ACT never matches and the same signal can re-fire
        # (2026-09-28: third order after VERIFY drop, 002185 triple-filled)
        key1h = slot1h(ContextInfo)

        pkey = '%.3f|%.3f|%d' % (f60, t60, pos)
        if LAST_PRINT.get(code) != pkey:
            LAST_PRINT[code] = pkey
            print('[%s] %s fish60=%.3f trig=%.3f pos=%d%s'
                  % (bar_key, code, f60, t60, pos, is_sim and '(sim)' or ''))

        if code in VERIFY:
            # an order for this code is awaiting fill verification; no new
            # orders until it resolves (prevents order pileup)
            continue

        if pos == 0:
            if code in EXDEF:
                # position gone (sold/manually closed) - drop stale deferred exit
                EXDEF.pop(code, None)
                exdef_save()
            if is_etf(code) and not ETF_ENTRIES:
                continue            # ETF entry switch off: exits still managed
            if bar_dt(ContextInfo)[11:16] >= ENTRY_TO:
                # late-day entries blocked (see ENTRY_TO comment); throttle log
                lk = (code, 'LATE')
                if LAST_PRINT.get(lk) != bar_key:
                    LAST_PRINT[lk] = bar_key
                    print('[SKIP] %s %s entry window closed (after %s)'
                          % (bar_key, code, ENTRY_TO))
                continue
            if cross_up and f60 - t60 < MIN_CROSS_ENTRY:
                cross_up = False
                print('[SKIP] %s %s cross too thin %.3f < %.2f (hairline), ignored'
                      % (bar_key, code, f60 - t60, MIN_CROSS_ENTRY))
            if cross_up and f60 >= MAX_F60:
                cross_up = False
                print('[SKIP] %s %s high-position entry fish60=%.3f >= %.2f, ignored'
                      % (bar_key, code, f60, MAX_F60))
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
                VERIFY[code] = ['BUY', pos, vol, 0, 'BUY', time.time()]
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
                VERIFY[code] = ['BUY', pos, vol, 0, 'REACTIVATED', time.time()]
                LAST_ACT[(code, 'BUY')] = key1h
                pending_remove(code)
                simpos_on_buy(ContextInfo, code, vol, get_last_price(ContextInfo, code))
                print('>>> BUY %s %s %d shares (REACTIVATED after fake-invalid), '
                      'fish60=%.3f' % (bar_dt(ContextInfo), code, vol, f60))

        else:
            if sellable <= 0:
                # T+1: shares bought today are frozen. Exits must NOT be dropped:
                # evaluate them anyway and latch into EXDEF; executed the moment
                # the shares become sellable (2026-09-23 600498 incident - the
                # old code discarded both exit signals fired during the freeze,
                # then no fresh cross existed after解冻).
                reason = None
                if USE_EXIT_A and cross_down:
                    reason = 'DEFER_EXIT_A'
                elif esi_enabled(code) and cost > 0.0:
                    price0 = get_last_price(ContextInfo, code)
                    if price0 > 0.0 and price0 < cost \
                            and esi_cross_down(ContextInfo, code):
                        reason = 'DEFER_ESI'
                if reason:
                    if EXDEF.get(code) != reason:
                        EXDEF[code] = reason
                        exdef_save()
                    pkey2 = '%s|T1X' % pkey
                    if LAST_PRINT.get((code, 'T1X')) != pkey2:
                        LAST_PRINT[(code, 'T1X')] = pkey2
                        print('[T+1] %s %s frozen but %s fired - latched, will exit when sellable'
                              % (bar_dt(ContextInfo), code, reason))
                    continue
                pkey2 = '%s|T1' % pkey
                if LAST_PRINT.get((code, 'T1')) != pkey2:
                    LAST_PRINT[(code, 'T1')] = pkey2
                    print('[T+1] %s %s holding %d but 0 sellable (bought today), exits deferred'
                          % (bar_dt(ContextInfo), code, pos))
                continue
            if code in EXDEF:
                # deferred exit latched during T+1 freeze. Re-validate the
                # rationale against the CURRENT bar before executing: a latch
                # whose reason evaporated overnight (gap-up recovery) must not
                # fire (mirrors the PSELL void rule)
                reason = EXDEF[code]
                if reason == 'DEFER_EXIT_A':
                    ok = f60 < t60
                elif reason == 'DEFER_ESI':
                    px0 = get_last_price(ContextInfo, code)
                    ok = not (cost > 0.0 and px0 > 0.0 and px0 >= cost)
                else:
                    ok = True
                if not ok:
                    EXDEF.pop(code)
                    exdef_save()
                    print('[CANCEL] %s %s deferred exit dropped (%s no longer holds)'
                          % (bar_dt(ContextInfo), code, reason))
                    continue
                EXDEF.pop(code)
                exdef_save()
                do_order(ContextInfo, code, 24, sellable)
                VERIFY[code] = ['SELL', pos, sellable, 0, reason, time.time()]
                LAST_ACT[(code, 'SELL')] = key1h
                simpos_on_sell(code)
                print('>>> SELL %s %s %d shares (%s, T+1 freeze deferred)'
                      % (bar_dt(ContextInfo), code, sellable, reason))
                continue
            # deferred Exit A: 1h crossed down but the small-tf sell gate was
            # not fully down (fake-invalid exit); sell once 15m/30m/5m are ALL
            # in down state; void if 1h fish recovers above its trigger
            if code in PSELL:
                if f60 > t60:
                    PSELL.pop(code, None)
                    print('[CANCEL] %s %s deferred sell void (1h fish back above trigger)'
                          % (bar_dt(ContextInfo), code))
                else:
                    downs = []
                    unknown = False
                    for tf in EXIT_CONFIRM_TFS:
                        st = tf_state(ContextInfo, code, tf)
                        if st is None:
                            unknown = True
                            break
                        if st[2]:
                            downs.append(tf)
                    if unknown:
                        print('[WARN] %s confirm state unknown, defer kept' % code)
                        continue
                    if len(downs) == len(EXIT_CONFIRM_TFS):
                        do_order(ContextInfo, code, 24, sellable)
                        VERIFY[code] = ['SELL', pos, sellable, 0, 'EXIT_A_C', time.time()]
                        LAST_ACT[(code, 'SELL')] = PSELL.pop(code)
                        simpos_on_sell(code)
                        print('>>> SELL %s %s %d shares (1h cross down + %s confirm)'
                              % (bar_dt(ContextInfo), code, sellable,
                                 '/'.join(EXIT_CONFIRM_TFS)))
                    continue
            if cross_down and USE_EXIT_A:
                if LAST_ACT.get((code, 'SELL')) == key1h:
                    continue
                # sell gate mirrors the entry gate: the 1h down-cross is acted
                # on only when 15m/30m/5m are ALL in down state; any still up
                # = fake-invalid exit -> defer + watch (PSELL)
                downs = []
                unknown = False
                for tf in EXIT_CONFIRM_TFS:
                    st = tf_state(ContextInfo, code, tf)
                    if st is None:
                        unknown = True
                        break
                    if st[2]:
                        downs.append(tf)
                if unknown:
                    print('[WARN] %s %s sell gate state unknown, skip bar'
                          % (bar_dt(ContextInfo), code))
                    continue
                if len(downs) < len(EXIT_CONFIRM_TFS):
                    PSELL[code] = key1h
                    print('[DEFER] %s %s 1h cross down but %s not all down '
                          '(fake-invalid exit, watching)'
                          % (bar_dt(ContextInfo), code, '/'.join(EXIT_CONFIRM_TFS)))
                    continue
                do_order(ContextInfo, code, 24, sellable)
                VERIFY[code] = ['SELL', pos, sellable, 0, 'EXIT_A', time.time()]
                LAST_ACT[(code, 'SELL')] = key1h
                simpos_on_sell(code)
                print('>>> SELL %s %s %d shares (1h cross down + %s all down), fish60=%.3f'
                      % (bar_dt(ContextInfo), code, sellable,
                         '/'.join(EXIT_CONFIRM_TFS), f60))
                continue
            if esi_enabled(code):
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
                    VERIFY[code] = ['SELL', pos, sellable, 0, 'ESI', time.time()]
                    LAST_ACT[(code, 'ESI')] = key30
                    simpos_on_sell(code)
                    print('>>> ESI-SELL %s %s %d shares (30m down, price %.2f < cost %.2f)'
                          % (bar_dt(ContextInfo), code, sellable, price, cost))
                # floating profit: keep holding for Exit A
    elapsed = time.time() - t_start
    if elapsed > 30:
        print('[WARN] handlebar took %.1fs for %d codes' % (elapsed, len(CODES)))
