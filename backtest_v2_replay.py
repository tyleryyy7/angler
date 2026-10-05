# -*- coding: utf-8 -*-
"""backtest_v2_replay.py — executor_v2.py（钓鱼，T+1 股票策略）离线回放，5m 粒度。

策略语义以 executor_v2.py REV 2026-09-29b 为准：
  进场（空仓）：形成中 1h fisher 上穿（f60 > t60 且 t60 <= 前二根），幅度
    f60 - t60 >= MIN_CROSS(0.15)；R1 闸门 = 30m/15m/5m 形成中序列无一在
    下行段（fish < trigger），否则转 R2 pending；R2 = 小周期全部离开下行
    且 f60 > t60 时激活买入，f60 < t60 时作废。时间窗 09:35~14:40。
  出场（持仓）：Exit A = 1h 下穿且幅度 t60 - f60 >= MIN_CROSS，且 15m/30m/5m
    全下行才卖，否则 PSELL 挂起（f60 > t60 作废，全下行时执行）；ESI(R3) =
    30m 下穿 + 浮亏立即卖。T+1 冻结期命中则 EXDEF 锁存，可卖首根 bar 执行。
  费用：佣金 max(5, 名义*万1)/边 + 卖出印花税 0.05%（与实际一致）。

变体：
  V0        = 当前线上规则（REV 2026-09-29b 全量）；
  A_OLD     = 旧卖出侧（下穿无幅度门槛；浮盈延迟确认/浮亏立即卖）——检验
              2026-09-29 卖出对称化改动；
  B_NOX     = 进场无幅度门槛（贴线上穿也买）；
  C_NOGATE  = 无 R1 进场闸门；
  D_NOSELLG = Exit A 无卖出小周期闸门（幅度下穿立即卖，无 PSELL）；
  E_NOESI   = 无 ESI 快割；
  F_NOXSELL = 卖出无幅度门槛（贴线下穿也走闸门）。

数据：cache/daily_qfq/tdxq 全深度缓存（2024-11 起）。宇宙 = 各股票池 ∪
watchlist ∪ holdings（60/00 主板，cache/_v2_universe.txt）。
口径近似：成交价 = 信号 bar close（未建模滑点）；R2 pending 跨日保留；
不包括 VERIFY 重报/账户熔断等执行层细节。

用法：.venv\\Scripts\\python.exe backtest_v2_replay.py
"""
import csv
import os
import sys
import time
from bisect import bisect_left
from collections import defaultdict

from backtest_t0_replay import (LENGTH, Tee, agg_stats, fisher_series,
                                fisher_series_v, forming_last, hour_win_close,
                                load_bars, win_close, win_close_5, win_close_30)

MIN_CROSS = 0.15
ENTRY_FROM = '09:35'
ENTRY_TO = '14:40'
FEE_RATE = 0.0001          # 万1
FEE_FLOOR = 5.0            # 不免5
STAMP = 0.0005             # 卖出印花税 0.05%
MIN_5M_BARS = 2000
UNIVERSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'cache', '_v2_universe.txt')
REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'results', 'backtest_v2_replay_report.txt')


def fee_buy(px, vol):
    return max(FEE_FLOOR, px * vol * FEE_RATE)


def fee_sell(px, vol):
    return max(FEE_FLOOR, px * vol * FEE_RATE) + px * vol * STAMP


def load_universe():
    base = os.path.dirname(os.path.abspath(__file__))
    codes = []
    with open(UNIVERSE_FILE, encoding='utf-8') as f:
        for ln in f:
            c = ln.strip()
            if not c:
                continue
            ok = True
            for p in ('5m', '1h', '30m', '15m'):
                if not os.path.exists(os.path.join(
                        base, 'cache', 'daily_qfq', 'tdxq',
                        '%s_%s.csv' % (c, p))):
                    ok = False
                    break
            if ok:
                codes.append(c)
    return codes


def ema_series(vals, n):
    k = 2.0 / (n + 1)
    out = []
    e = vals[0]
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def build_regime(d1, mode):
    """日线 regime 状态机（迟滞：切换需连续 2 根同向）。
    ma20/ma10: raw = 收盘>MA 且 MA 连续 2 日上行；
    dif:       raw = MACD DIF(12,26) 连续 2 日上行。
    返回 (dates, regime)，判定用「进场日之前的完结日线」（date < day）。"""
    d1_c = [x['c'] for x in d1]
    n = len(d1_c)
    raw = [False] * n
    if mode in ('ma20', 'ma10'):
        w = 20 if mode == 'ma20' else 10
        ma = [None] * n
        for i in range(w - 1, n):
            ma[i] = sum(d1_c[i - w + 1:i + 1]) / w
        for i in range(w + 1, n):
            raw[i] = (ma[i] is not None and d1_c[i] > ma[i]
                      and ma[i] > ma[i - 1] and ma[i - 1] > ma[i - 2])
    else:  # dif
        e12 = ema_series(d1_c, 12)
        e26 = ema_series(d1_c, 26)
        dif = [a - b for a, b in zip(e12, e26)]
        for i in range(2, n):
            raw[i] = dif[i] > dif[i - 1] and dif[i - 1] > dif[i - 2]
    regime = ['FALL'] * n
    cur = 'FALL'
    for i in range(1, n):
        if raw[i] and raw[i - 1]:
            cur = 'TREND'
        elif not raw[i] and not raw[i - 1]:
            cur = 'FALL'
        regime[i] = cur
    return [x['time'][:10] for x in d1], regime


class TF(object):
    """一个周期的完结序列 + 前缀 fisher（forming_last 的食材）。"""

    def __init__(self, bars):
        self.h = [x['h'] for x in bars]
        self.l = [x['l'] for x in bars]
        self.t = [x['time'] for x in bars]
        self.hl2 = [(self.h[j] + self.l[j]) / 2.0 for j in range(len(bars))]
        self.f_pre, self.v_pre = fisher_series_v(self.h, self.l)

    def last(self, bars, i, day, m, winc):
        return forming_last(self.t, self.hl2, self.f_pre, self.v_pre,
                            bars, i, day, m, winc)


def small_tf_down(tf, bars, i, day, m, winc):
    """该周期形成中序列是否处于下行段（fish < trigger）。数据不足返回 None。"""
    _, fl, fm1, _ = tf.last(bars, i, day, m, winc)
    if fm1 is None:
        return None
    return fl < fm1


def replay(code, variant='V0', nominal=10000.0, regime_mode='ma20',
           esi_min_loss=0.0, min_cross=MIN_CROSS, max_f60=999.0,
           min_cross_sell=None, add_pct=None, max_adds=2, add_regime=None,
           t0=False, entry_regime=None):
    # add_pct: 顺势加仓开关（验证用近似口径）。持仓浮盈 >= add_pct 且盘中再出
    # 1h 上穿（同一 1h 窗口不重复）→ 加 1 单位，最多 max_adds 次；加仓笔随主仓
    # 同价离场、单独记账（reason 带 |ADD），T+1 对加仓笔当日不可卖的细节忽略。
    # add_regime: 加仓额外要求日线 regime=TREND（'ma20'/'ma10'/'dif'）。
    # entry_regime: 主仓进场额外要求日线 regime=TREND（动态强弱闸门）。
    # t0=True: T+0 标的（QDII/债券/商品 ETF），当日买入当日可卖（无冻结锁存）。
    sell_mc = MIN_CROSS if min_cross_sell is None else min_cross_sell
    b5 = load_bars(code, '5m')
    if len(b5) < MIN_5M_BARS:
        return []
    tf1h = TF(load_bars(code, '1h'))
    tf30 = TF(load_bars(code, '30m'))
    tf15 = TF(load_bars(code, '15m'))
    tf5 = TF(b5)
    if (variant == 'G_REGIME_ESI' or add_regime is not None
            or entry_regime is not None):
        rg_dates, rg_arr = build_regime(
            load_bars(code, '1d'),
            regime_mode if variant == 'G_REGIME_ESI'
            else (add_regime or entry_regime))
    by_day = defaultdict(list)
    for g, b in enumerate(b5):
        by_day[b['time'][:10]].append((g, b))
    trades = []
    pos = None          # [entry_g, cost, vol, entry_day, entry_f60, entry_hhmm]
    lots = []           # 加仓笔 [g, price, vol, day, f60, hhmm, delta]
    pending = None      # R2：假性失效信号（key=1h 窗口）
    psell = None        # Exit A 挂起（key=1h 窗口）
    exdef = None        # T+1 冻结期锁存的出场原因
    last_buy_key = None
    for day in sorted(by_day):
        bars = [(g, b) for g, b in by_day[day]
                if '09:30' <= b['time'][11:16] <= '15:00']
        if not bars:
            continue
        rg_T = False
        if (variant == 'G_REGIME_ESI' or add_regime is not None
                or entry_regime is not None):
            # regime 判定用「进场日之前的完结日线」（date < day，无前视）
            di = bisect_left(rg_dates, day) - 1
            rg_T = di >= 0 and rg_arr[di] == 'TREND'
        esi_off = variant == 'G_REGIME_ESI' and rg_T
        for i, (g, b) in enumerate(bars):
            hhmm = b['time'][11:16]
            m = int(hhmm[:2]) * 60 + int(hhmm[3:])
            price = b['c']
            q1h, f60, t60, f60_m2 = tf1h.last(bars, i, day, m, hour_win_close)
            key1h = '%s|%02d:%02d' % (day, q1h // 60, q1h % 60)
            cross_up = f60_m2 is not None and f60 > t60 and t60 <= f60_m2
            cross_down = f60_m2 is not None and f60 < t60 and t60 >= f60_m2

            if pos is not None:
                sellable = t0 or day != pos[3]
                reason = None
                _, f30l, f30m1, f30m2 = tf30.last(bars, i, day, m, win_close_30)
                cd30 = f30m2 is not None and f30l < f30m1 and f30m1 >= f30m2
                if variant == 'A_OLD':
                    cd60 = cross_down          # 旧口径：下穿无幅度门槛
                else:
                    cd60 = cross_down and (t60 - f60) >= sell_mc
                    if variant == 'F_NOXSELL':
                        cd60 = cross_down
                if not sellable:
                    # T+1 冻结：命中则锁存，次日可卖首根 bar 执行
                    if cd60 and exdef is None:
                        exdef = 'EXIT_A'
                    elif (variant != 'E_NOESI' and not esi_off
                            and cd30 and price < pos[1] * (1.0 - esi_min_loss)
                            and exdef is None):
                        exdef = 'ESI'
                    continue
                if exdef is not None:
                    # 执行前按当前 bar 复核锁存理由（与 executor_v2 REV
                    # 2026-09-30b 一致）：隔夜恢复则作废，不再无条件执行
                    r0 = exdef
                    exdef = None
                    ok = (f60 < t60) if r0 == 'EXIT_A' else (price < pos[1])
                    if ok:
                        reason = r0
                elif psell is not None:
                    if f60 > t60:
                        psell = None           # 挂起作废（趋势恢复）
                    else:
                        d15 = small_tf_down(tf15, bars, i, day, m, win_close)
                        d30 = small_tf_down(tf30, bars, i, day, m, win_close_30)
                        d5 = small_tf_down(tf5, bars, i, day, m, win_close_5)
                        if None in (d15, d30, d5):
                            continue
                        if d15 and d30 and d5:
                            reason = 'EXIT_A_C'
                elif cd60:
                    if variant in ('A_OLD', 'D_NOSELLG'):
                        if variant == 'A_OLD' and price >= pos[1]:
                            psell = key1h       # 旧口径：浮盈延迟确认
                            continue
                        reason = 'EXIT_A'        # D：幅度/贴线下穿立即卖
                    else:
                        d15 = small_tf_down(tf15, bars, i, day, m, win_close)
                        d30 = small_tf_down(tf30, bars, i, day, m, win_close_30)
                        d5 = small_tf_down(tf5, bars, i, day, m, win_close_5)
                        if None in (d15, d30, d5):
                            continue
                        if d15 and d30 and d5:
                            reason = 'EXIT_A'
                        else:
                            psell = key1h       # 假失效卖出，挂起观察
                            continue
                elif (variant != 'E_NOESI' and not esi_off
                        and cd30 and price < pos[1] * (1.0 - esi_min_loss)):
                    reason = 'ESI'
                if reason:
                    gross = (price - pos[1]) * pos[2]
                    fee = fee_buy(pos[1], pos[2]) + fee_sell(price, pos[2])
                    trades.append({
                        'code': code, 'day': day,
                        'entry': pos[5], 'exit': hhmm,
                        'buy': pos[1], 'sell': price,
                        'gross': gross, 'fee': fee, 'net': gross - fee,
                        'reason': reason, 'vol': pos[2],
                        'bars': g - pos[0], 'g_exit': g,
                        'hold_days': (day != pos[3]),
                        'f60': pos[4],
                        'branch': pos[6] if len(pos) > 6 else None,
                        'delta': pos[7] if len(pos) > 7 else None,
                        'path': pos[8] if len(pos) > 8 else None,
                        'eday': pos[3]})
                    for lt in lots:
                        lg = (price - lt[1]) * lt[2]
                        lf = fee_buy(lt[1], lt[2]) + fee_sell(price, lt[2])
                        trades.append({
                            'code': code, 'day': day, 'entry': lt[5],
                            'exit': hhmm, 'buy': lt[1], 'sell': price,
                            'gross': lg, 'fee': lf, 'net': lg - lf,
                            'reason': reason + '|ADD', 'vol': lt[2],
                            'bars': g - lt[0], 'g_exit': g,
                            'hold_days': (day != lt[3]), 'f60': lt[4],
                            'branch': None, 'delta': lt[6], 'path': 'ADD',
                            'eday': lt[3]})
                    pos = None
                    psell = None
                    lots = []
                elif (add_pct is not None and len(lots) < max_adds
                        and ENTRY_FROM <= hhmm <= ENTRY_TO
                        and price >= pos[1] * (1.0 + add_pct)
                        and (add_regime is None or rg_T)
                        and cross_up and f60 < max_f60
                        and key1h != last_buy_key):
                    avol = max(100, int(nominal / price / 100.0) * 100)
                    lots.append([g, price, avol, day, f60, hhmm, f60 - t60])
                    last_buy_key = key1h
                continue

            # ---- 空仓进场侧 ----
            if hhmm < ENTRY_FROM or hhmm > ENTRY_TO:
                continue
            # R2 激活监控
            if pending is not None:
                if f60 < t60 or (entry_regime is not None and not rg_T):
                    pending = None               # 1h 趋势破坏 / 转入非 TREND，作废
                else:
                    d15 = small_tf_down(tf15, bars, i, day, m, win_close)
                    d30 = small_tf_down(tf30, bars, i, day, m, win_close_30)
                    d5 = small_tf_down(tf5, bars, i, day, m, win_close_5)
                    if None in (d15, d30, d5):
                        continue
                    if not (d15 or d30 or d5):
                        vol = max(100, int(nominal / price / 100.0) * 100)
                        pos = [g, price, vol, day, f60, hhmm,
                               (('TREND' if rg_T else 'FALL')
                                if variant == 'G_REGIME_ESI' else None),
                               f60 - t60, 'R2']
                        pending = None
                        lots = []
                        last_buy_key = key1h
                        continue
            if not cross_up or last_buy_key == key1h:
                continue
            if entry_regime is not None and not rg_T:
                continue                           # 动态强弱闸门：非 TREND 日不进场
            if variant != 'B_NOX' and (f60 - t60) < min_cross:
                continue                           # 穿越幅度不足，忽略
            if f60 >= max_f60:
                continue                           # 高位进场上限
            if variant != 'C_NOGATE':
                d15 = small_tf_down(tf15, bars, i, day, m, win_close)
                d30 = small_tf_down(tf30, bars, i, day, m, win_close_30)
                d5 = small_tf_down(tf5, bars, i, day, m, win_close_5)
                if None in (d15, d30, d5):
                    continue
                if d15 or d30 or d5:
                    pending = key1h                # R1 假性失效 → R2 观察
                    continue
            vol = max(100, int(nominal / price / 100.0) * 100)
            pos = [g, price, vol, day, f60, hhmm,
                   (('TREND' if rg_T else 'FALL')
                    if variant == 'G_REGIME_ESI' else None),
                   f60 - t60, 'DIRECT']
            lots = []
            last_buy_key = key1h
    # 数据末尾仍持仓：盯市平仓
    if pos is not None:
        price = b5[-1]['c']
        gross = (price - pos[1]) * pos[2]
        fee = fee_buy(pos[1], pos[2]) + fee_sell(price, pos[2])
        trades.append({
            'code': code, 'day': by_day[sorted(by_day)[-1]][-1][1]['time'][:10],
            'entry': pos[5], 'exit': b5[-1]['time'][11:16],
            'buy': pos[1], 'sell': price,
            'gross': gross, 'fee': fee, 'net': gross - fee,
            'reason': 'END', 'vol': pos[2], 'bars': len(b5) - 1 - pos[0],
            'g_exit': len(b5) - 1,
            'branch': pos[6] if len(pos) > 6 else None,
            'delta': pos[7] if len(pos) > 7 else None,
            'path': pos[8] if len(pos) > 8 else None,
            'hold_days': True,
            'f60': pos[4], 'eday': pos[3]})
        for lt in lots:
            lg = (price - lt[1]) * lt[2]
            lf = fee_buy(lt[1], lt[2]) + fee_sell(price, lt[2])
            trades.append({
                'code': code, 'day': by_day[sorted(by_day)[-1]][-1][1]['time'][:10],
                'entry': lt[5], 'exit': b5[-1]['time'][11:16],
                'buy': lt[1], 'sell': price,
                'gross': lg, 'fee': lf, 'net': lg - lf,
                'reason': 'END|ADD', 'vol': lt[2], 'bars': len(b5) - 1 - lt[0],
                'g_exit': len(b5) - 1,
                'branch': None, 'delta': lt[6], 'path': 'ADD',
                'hold_days': True, 'f60': lt[4], 'eday': lt[3]})
    return trades


class CodeCtx(object):
    """单票回放上下文：驱动序列 + 四周期 TF（尸检前推用，每票只建一次）。"""

    def __init__(self, code):
        self.b5 = load_bars(code, '5m')
        self.tf1h = TF(load_bars(code, '1h'))
        self.tf30 = TF(load_bars(code, '30m'))
        self.tf15 = TF(load_bars(code, '15m'))
        self.tf5 = TF(self.b5)
        self.by_day = defaultdict(list)
        for g, b in enumerate(self.b5):
            self.by_day[b['time'][:10]].append((g, b))
        self.days = sorted(self.by_day)


def forward_exit(cx, g0, day0):
    """V0 口径（关掉 ESI）从全局 bar g0 之后前推持仓：EXIT_A = 1h 幅度下穿
    + 小周期全下行；闸门未全下行则 PSELL 挂起，f60>t60 作废等下一个。
    返回 (exit_price, reason, day)。"""
    psell = False
    for day in cx.days:
        if day < day0:
            continue
        bars = [(g, b) for g, b in cx.by_day[day]
                if '09:30' <= b['time'][11:16] <= '15:00']
        for i, (g, b) in enumerate(bars):
            if g <= g0:
                continue
            hhmm = b['time'][11:16]
            m = int(hhmm[:2]) * 60 + int(hhmm[3:])
            _, f60, t60, f60_m2 = cx.tf1h.last(bars, i, day, m, hour_win_close)
            cross_down = f60_m2 is not None and f60 < t60 and t60 >= f60_m2
            cd60 = cross_down and (t60 - f60) >= MIN_CROSS
            if psell:
                if f60 > t60:
                    psell = False
                    continue
                d15 = small_tf_down(cx.tf15, bars, i, day, m, win_close)
                d30 = small_tf_down(cx.tf30, bars, i, day, m, win_close_30)
                d5 = small_tf_down(cx.tf5, bars, i, day, m, win_close_5)
                if None not in (d15, d30, d5) and d15 and d30 and d5:
                    return b['c'], 'EXIT_A_C', day
            elif cd60:
                d15 = small_tf_down(cx.tf15, bars, i, day, m, win_close)
                d30 = small_tf_down(cx.tf30, bars, i, day, m, win_close_30)
                d5 = small_tf_down(cx.tf5, bars, i, day, m, win_close_5)
                if None not in (d15, d30, d5) and d15 and d30 and d5:
                    return b['c'], 'EXIT_A', day
                psell = True
    return cx.b5[-1]['c'], 'END', cx.days[-1]


def run_esi_autopsy(universe):
    """对 V0 的每一笔 ESI 出场：若不割、按 V0 扛到 Exit A/数据末，
    价差（相对实际 ESI 卖价）分布如何。费用结构相同（同名义双边），
    价差≈净差。"""
    recs = []
    for code in universe:
        esis = [t for t in replay(code, 'V0') if t['reason'] == 'ESI']
        if not esis:
            continue
        cx = CodeCtx(code)
        for t in esis:
            px, r, d = forward_exit(cx, t['g_exit'], t['day'])
            recs.append({'code': code, 'pct': (px - t['sell']) / t['sell'] * 100.0,
                         'yuan': (px - t['sell']) * t['vol'],
                         'eday': t['eday'], 'r': r})
    return recs


def report(label, ts):
    s = agg_stats(ts)
    mg = (sum(t['gross'] / (t['buy'] * t['vol']) * 100.0 for t in ts) / len(ts)
          if ts else 0.0)
    fees = sum(t['fee'] for t in ts)
    hd = sum(1 for t in ts if t['hold_days']) * 100.0 / max(len(ts), 1)
    print('%-12s %7d %+13.2f %+9.2f %6.1f%% %+9.3f%% %11.2f %5.0f%%'
          % (label, s[0], s[1], s[2], s[3], mg, fees, hd))
    return s


def main():
    t0 = time.time()
    universe = load_universe()
    print('宇宙 %d 只（60/00 主板，5m/1h/30m/15m 缓存齐备）' % len(universe))
    vt = {}
    for variant in ('V0', 'A_OLD', 'B_NOX', 'C_NOGATE', 'D_NOSELLG',
                    'E_NOESI', 'F_NOXSELL'):
        ts = []
        for code in universe:
            ts.extend(replay(code, variant))
        vt[variant] = ts
        print('  [%s done: %d trades, %.0fs]' % (variant, len(ts),
                                                time.time() - t0))
    for label, mode in (('G_MA20', 'ma20'), ('G_MA10', 'ma10'),
                        ('G_DIF', 'dif')):
        ts = []
        for code in universe:
            ts.extend(replay(code, 'G_REGIME_ESI', regime_mode=mode))
        vt[label] = ts
        print('  [%s done: %d trades, %.0fs]' % (label, len(ts),
                                                time.time() - t0))

    print('\n' + '=' * 78)
    print('executor_v2 变体对比（费用=万1不免5+卖出印花税0.05%，名义 1 万/笔）')
    print('=' * 78)
    print('%-12s %7s %13s %9s %7s %10s %11s %6s'
          % ('变体', '笔数', '总净额', '均笔', '胜率', '均毛利%/笔',
             '费用合计', '隔夜%'))
    names = {'V0': 'V0 现行', 'A_OLD': 'A 旧卖出侧', 'B_NOX': 'B 无进场幅度',
             'C_NOGATE': 'C 无R1闸门', 'D_NOSELLG': 'D 无卖出闸门',
             'E_NOESI': 'E 无ESI', 'F_NOXSELL': 'F 无卖出幅度'}
    for variant in ('V0', 'A_OLD', 'B_NOX', 'C_NOGATE', 'D_NOSELLG',
                    'E_NOESI', 'F_NOXSELL'):
        report(names[variant], vt[variant])

    # ---- G_REGIME_ESI 判定段：TREND 日关 ESI / FALL 日开 ----
    print('\n-- G_REGIME_ESI（ESI 按行情态路由；通过标准：同时赢 V0 与 '
          'E_NOESI，且 valid 窗口同样成立） --')
    print('%-14s %7s %13s %9s %7s %12s %12s %12s %12s'
          % ('组合', '笔数', '总净额', '均笔', '胜率', 'build净额',
             'valid净额', 'TREND分支', 'FALL分支'))
    BUILD_END = '2025-08-31'
    for label in ('G_MA20', 'G_MA10', 'G_DIF'):
        ts = vt[label]
        s = agg_stats(ts)
        bb = [t for t in ts if t['eday'] <= BUILD_END]
        vv = [t for t in ts if t['eday'] > BUILD_END]
        tr = [t for t in ts if t.get('branch') == 'TREND']
        fa = [t for t in ts if t.get('branch') == 'FALL']
        print('%-14s %7d %+13.2f %+9.2f %6.1f%% %+12.2f %+12.2f %+12.2f %+12.2f'
              % (label, s[0], s[1], s[2], s[3],
                 sum(t['net'] for t in bb), sum(t['net'] for t in vv),
                 sum(t['net'] for t in tr), sum(t['net'] for t in fa)))
    v0n = agg_stats(vt['V0'])[1]
    en = agg_stats(vt['E_NOESI'])[1]
    print('对照：V0 %+.2f / E_NOESI %+.2f（valid：V0 %+.2f / E_NOESI %+.2f）'
          % (v0n, en,
             sum(t['net'] for t in vt['V0'] if t['eday'] > BUILD_END),
             sum(t['net'] for t in vt['E_NOESI'] if t['eday'] > BUILD_END)))
    best_g = max((agg_stats(vt[l])[1], l) for l in ('G_MA20', 'G_MA10', 'G_DIF'))
    gv = [t for t in vt[best_g[1]] if t['eday'] > BUILD_END]
    gv_n = sum(t['net'] for t in gv)
    gv0 = sum(t['net'] for t in vt['V0'] if t['eday'] > BUILD_END)
    gve = sum(t['net'] for t in vt['E_NOESI'] if t['eday'] > BUILD_END)
    verdict = (best_g[0] > v0n and best_g[0] > en
               and gv_n > gv0 and gv_n > gve)
    print('最优指标 %s：全窗口 %+.2f，valid %+.2f —— 判定：%s'
          % (best_g[1], best_g[0], gv_n,
             '通过（同时赢 V0 与 E_NOESI，可谈实装）' if verdict
             else '未通过（不如直接关 ESI 或保留现状）'))

    # 出场原因分解（V0）
    print('\n-- V0 出场原因分解 --')
    print('%-10s %7s %13s %9s' % ('reason', '笔数', '总净额', '均笔'))
    for r in ('EXIT_A', 'EXIT_A_C', 'ESI', 'END'):
        g = [t for t in vt['V0'] if t['reason'] == r]
        if g:
            s = agg_stats(g)
            print('%-10s %7d %+13.2f %+9.2f' % (r, s[0], s[1], s[2]))

    # ---- ESI 反事实尸检（证据增强段） ----
    print('\n-- ESI 反事实尸检：每笔 ESI 若不割、按 V0 扛到 Exit A --')
    recs = run_esi_autopsy(universe)
    n = len(recs)
    if n:
        pcts = sorted(r['pct'] for r in recs)
        mean_pct = sum(pcts) / n
        median_pct = pcts[n // 2]
        win = sum(1 for r in recs if r['pct'] > 0) * 100.0 / n
        yuan = sum(r['yuan'] for r in recs)
        print('ESI 出场单 %d 笔：' % n)
        print('  扛到 Exit A 平均价差 %+.3f%%（中位 %+.3f%%）'
              % (mean_pct, median_pct))
        print('  扛住优于快割的比例 %.1f%%；合计价差 %+.2f 元（费用结构相同，'
              '价差≈净差）' % (win, yuan))
        print('  按年份：' + '；'.join(
            '%s n=%d %+.3f%%' % (y, len([r for r in recs if r['eday'][:4] == y]),
                                 sum(r['pct'] for r in recs if r['eday'][:4] == y)
                                 / max(len([r for r in recs if r['eday'][:4] == y]), 1))
            for y in ('2024', '2025', '2026')
            if any(r['eday'][:4] == y for r in recs)))
        for rr in ('EXIT_A', 'EXIT_A_C', 'END'):
            g = [r for r in recs if r['r'] == rr]
            if g:
                print('  反事实出口 %-9s n=%5d 平均价差 %+.3f%% 合计 %+.2f 元'
                      % (rr, len(g), sum(r['pct'] for r in g) / len(g),
                         sum(r['yuan'] for r in g)))

    # 毛利分层尸检（V0）
    def _gp(t):
        return t['gross'] / (t['buy'] * t['vol']) * 100.0

    def _buckets(title, keyf, order=None):
        groups = defaultdict(list)
        for t in vt['V0']:
            groups[keyf(t)].append(t)
        print('\n-- %s --' % title)
        print('%-10s %7s %11s %13s' % ('分层', '笔数', '均毛利%/笔', '总毛利'))
        keys = order or sorted(groups, key=lambda k:
                               -sum(_gp(t) for t in groups[k]) / len(groups[k]))
        for k in keys:
            g = groups.get(k)
            if not g:
                continue
            mg = sum(_gp(t) for t in g) / len(g)
            print('%-10s %7d %+10.3f%% %+13.2f'
                  % (k, len(g), mg, sum(t['gross'] for t in g)))

    _buckets('按年份', lambda t: t['eday'][:4])
    _buckets('按进场 fish60 位置', lambda t: (
        '<-2' if t['f60'] < -2 else
        '-2~-1' if t['f60'] < -1 else
        '-1~0' if t['f60'] < 0 else
        '0~1' if t['f60'] < 1 else
        '1~2.5' if t['f60'] < 2.5 else '>=2.5'),
        order=['<-2', '-2~-1', '-1~0', '0~1', '1~2.5', '>=2.5'])
    _buckets('按进场小时', lambda t: t['entry'][:2],
        order=['09', '10', '11', '13', '14'])
    _buckets('按票 TOP/BOTTOM', lambda t: t['code'])
    print('\n[elapsed %.0fs]' % (time.time() - t0))


if __name__ == '__main__':
    sys.stdout = Tee(REPORT_FILE)
    main()
    print('\n[report saved] %s' % REPORT_FILE)
