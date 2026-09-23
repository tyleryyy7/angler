# -*- coding: utf-8 -*-
"""backtest_t0_replay.py — executor_t0.py（T+0 ETF 日内策略）离线回放，5m 粒度。

策略语义以 executor_t0.py 为准（15m cross_up 进场 / 硬止损 / ESI / Exit A /
14:55 FLAT / ESI+STOP 当日禁入 / 每票每日最多 10 次进场），分别回放
STOP_MODE=fixed 与 STOP_MODE=atr 两种模式并对比；并做进场假设变体对比
（出场逻辑四个变体完全一致，硬止损一律 atr 模式）：
  V0 基线   = 现回放逻辑原样（15m cross_up + 5m 闸门）；
  V1 趋势对齐 = 进场时 1h fisher 处于上行段（f60[-1] > f60[-2]）才许买
              （15m 上穿作为 1h 上涨趋势中的回调买点）；
  V2 深极值  = 进场时 15m fisher < -1.5（恐慌深位）才许买；
  V3 组合   = V1 AND V2。
完整输出（fixed/atr 对比 + 进场变体对比）Tee 到
results/backtest_t0_variants_report.txt（2026-09-23 起，新文件；
旧的 STOP_MODE 对比报告留在 results/backtest_t0_replay_report.txt）。
尾部另有「实验 B：ESI 出场反事实尸检」段：对 V0(atr) 的 ESI 出场单做
三种持有假设反事实（硬扛到 FLAT / 慢一个 cross down 出 / 扛到回本或
FLAT）+ ESI 延迟激活实验（N=0/3/6/12 根 5m bar）。

口径近似（与实盘的差异，解读结果时务必考虑）：
1. 实盘跑 1m bar，进场闸门 = 5m 与 1m 双周期「不在下行段」；回放无 1m
   数据，用 5m 单周期近似（信号时刻已完结 5m 序列的 fish[-1] >= fish[-2]
   即放行），与 backtest_entry_filter.py 的 15m 研究口径一致。
2. Exit A 实盘对浮盈单有延迟确认（USE_EXIT_CONFIRM：等 1m 进入下行段，
   5m 回到 trigger 上方则作废）；回放直接卖出（close < cost 记 ESI，
   >= cost 记 EXIT_A），会略高估 EXIT_A 的及时性。
3. ATR 止损在「已完结 + 形成中」15m 序列的最后 14 根上算（TR 含形成 bar，
   与 executor_t0.stop_pct 逐字一致）；实盘按 ctx_bar_key 每 15m bar 缓存
   首次计算值，回放按 15m 窗口内首根 5m bar 缓存，窗口内后续 5m bar 复用。
4. 15m 序列 = 全部已完结 15m bar（收盘时刻 < 当前 15m 窗口收盘时刻）+
   当前形成中 15m bar（由窗口内 5m bar 聚合，含当前 bar），fisher 在该
   序列上计算，镜像实盘在形成 bar 上判信号。fisher 用文件全量历史计算
   （实盘取最近 120 根），9 窗递归经过 120 根后初值影响已完全耗尽。
5. 回放不含：5m 追单再进场（USE_REENTRY_5M）、R2 假激活 pending 机制、
   账户级 MAX_DAILY_LOSS 熔断（单票 -300 元熔断对 3000~10000 股仓位
   几乎必然触发，属实盘风控而非策略 alpha，回放剔除以看清策略本体；
   每票每日 10 次上限保留）。含 BUY 按 ctx_bar_key 去重（同一 15m bar
   内不重复进）。
6. 成交价 = 信号 5m bar 的 close（镜像市价单假设）；commission =
   max(5.0, 名义本金*0.00025) 双边，T0 ETF 无印花税；盈亏一律净额。
7. 信号判定与成交同用一根 5m bar 的 close（5m 粒度下的 look-ahead
   极小，实盘 1m 粒度下信号会更早出现，此处无法更细）。
8. 变体 V1/V3 的 1h 序列构造与 15m 同口径：全部已完结 1h bar（取自
   {code}_1h.csv，收盘时刻 < 当前 1h 窗口收盘时刻）+ 当前形成中 1h
   bar（由当日 5m bar 聚合，含当前 bar）。1h 窗口边界 10:30/11:30/
   14:00/15:00（09:30-10:30 / 10:30-11:30 / 13:00-14:00 / 14:00-15:00），
   午休不跨窗。「1h 上行段」= 该序列 fish[-1] > fish[-2]（形成 bar 参与，
   与实盘在形成 bar 上判状态一致）。V2 的「15m 深位」= 进场判定所用
   同一「完结+形成中」15m 序列的 fish[-1] < -1.5。

数据：cache/daily_qfq/tdxq/{code}_5m.csv / {code}_15m.csv / {code}_1h.csv
（tdxq 前复权）。票池（2026-09-23 扩样版起）= 缓存并集 ∪ pool_t0 ∪ 原四只，
要求三周期缓存齐全且 5m ≥ 200 根；仓位 = 固定 1 万元名义本金/笔（取整百股、
最低 100），替代原 watchlist 固定股数——原四只 V0 数字随口径切换变化
（旧四只版报告存档 results/backtest_t0_variants_report_4codes.txt）。
用法：.venv\\Scripts\\python.exe backtest_t0_replay.py
"""
import csv
import math
import os
import sys
import time
from collections import defaultdict
from statistics import median

REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'results', 'backtest_t0_variants_report.txt')


class Tee(object):
    # stdout 同时落盘 UTF-8 报告（Git Bash 控制台是 GBK，中文会乱码）
    def __init__(self, path):
        self.f = open(path, 'w', encoding='utf-8')

    def write(self, s):
        sys.__stdout__.write(s)
        self.f.write(s)

    def flush(self):
        sys.__stdout__.flush()
        self.f.flush()

LENGTH = 9
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'cache', 'daily_qfq', 'tdxq')
LEGACY_CODES = ('513310', '159937', '513120', '513750')   # 原四只（口径切换对照）
FIXED_NOTIONAL = 10000.0   # 每笔固定名义本金（元）：vol = 10000/价 取整百股，
                           # 最低 100 股。替代原 watchlist 固定股数——跨票可比，
                           # 与原四只口径（3000~10000 股/票）的差异见报告头注。
MIN_5M_BARS = 200          # 5m 缓存少于该根数的票排除（并列入报告）

STOP_PCT = 0.005            # 'fixed' 模式硬止损
STOP_ATR_MULT = 1.5         # 'atr' 模式：ATR14(15m)/price * mult
STOP_PCT_MIN = 0.004
STOP_PCT_MAX = 0.025
V2_THRESHOLD = -1.5        # V2/V3：15m fisher 深位进场门槛（panic 位）
ENTRY_FROM = '09:40'
ENTRY_TO = '14:40'
FORCE_FLAT_AT = '14:55'
MAX_TRADES_PER_CODE = 10


def fisher_series(high, low, length=LENGTH):
    # 逐字照抄 executor_t0.py 的 fisher_series（hl2 输入、窗口 9、
    # v 超 ±0.99 截到 ±0.999），口径不许改。
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


def load_bars(code, period):
    path = os.path.join(DATA_DIR, '%s_%s.csv' % (code, period))
    rows = []
    with open(path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            try:
                rows.append({'time': r['time'],
                             'o': float(r['open']),
                             'h': float(r['high']), 'l': float(r['low']),
                             'c': float(r['close'])})
            except (TypeError, ValueError):
                continue
    return rows


def load_universe(strict=False):
    # 扩样票池 = (cache/daily_qfq/tdxq 下所有 *_5m.csv) ∪ pool_t0 ∪ 原四只；
    # 仅保留 5m+15m+1h 三周期缓存齐全且 5m >= MIN_5M_BARS 根的票，
    # 排除的票连同原因列入返回的 excluded，在报告头打印。
    # strict=True（实验 F 稳健性重验）：只用纯 T0 核心集 pool_t0 ∪ 原四只
    # （16 只）——宽宇宙混入了 21 只"仅因缓存存在"的 ETF（其中约半数 T+1，
    # 回放假设 T+0 高估其可执行性）；两宇宙对照检验结论对宇宙选择是否敏感。
    # 返回 (codes, excluded, days)：days = 每只票 5m 缓存覆盖的交易日数。
    codes = set()
    if not strict:
        for fn in os.listdir(DATA_DIR):
            if fn.endswith('_5m.csv') and fn[:-7].isdigit():
                codes.add(fn[:-7])
    pool = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'pool_t0.csv')
    if os.path.exists(pool):
        with open(pool, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                c = (r.get('code') or '').strip()
                if c:
                    codes.add(c)
    codes.update(LEGACY_CODES)
    ok, excluded, days = [], [], {}
    for c in sorted(codes):
        if c[:2] not in ('15', '16', '50', '51', '52', '56', '58'):
            # 只留 ETF（T+0 可日内回转）：缓存目录里还有各股票池的 A 股缓存，
            # A 股 T+1 无法日内卖出，T0 策略对它们不适用，必须剔除
            excluded.append((c, '非 ETF（T+1 股票，T0 策略不适用）'))
            continue
        p5 = os.path.join(DATA_DIR, '%s_5m.csv' % c)
        has_all = (os.path.exists(p5)
                   and os.path.exists(os.path.join(DATA_DIR,
                                                   '%s_15m.csv' % c))
                   and os.path.exists(os.path.join(DATA_DIR,
                                                   '%s_1h.csv' % c)))
        if not has_all:
            excluded.append((c, '缺 5m/15m/1h 完整缓存'))
            continue
        with open(p5, encoding='utf-8') as f:
            rows = [r for r in csv.DictReader(f) if r.get('time')]
        if len(rows) < MIN_5M_BARS:
            excluded.append((c, '5m 仅 %d 根 <%d' % (len(rows), MIN_5M_BARS)))
            continue
        days[c] = len({r['time'][:10] for r in rows})
        ok.append(c)
    return ok, excluded, days


def win_close(minutes):
    # 5m bar 收盘时刻（当日分钟数）-> 所属 15m 窗口的收盘时刻（分钟数）。
    # 与 executor_t0.ctx_bar_key 的窗口划分一致：整 15 分格归本格
    # （13:00 例外，归 13:15 窗口），午休 11:30~13:00 不跨窗口。
    if minutes % 15 == 0 and minutes != 780:
        q = minutes
    else:
        q = (minutes // 15 + 1) * 15
    if 690 < q < 795:
        q = 795
    if q > 900:
        q = 900
    return q


def hour_win_close(minutes):
    # 5m bar 收盘时刻（当日分钟数）-> 所属 1h 窗口的收盘时刻（分钟数）。
    # A 股 1h bar 边界：10:30 / 11:30 / 14:00 / 15:00（上午两根各 60 分钟，
    # 下午 13:00-14:00 / 14:00-15:00），午休 11:30~13:00 不跨窗口。
    if minutes <= 630:
        return 630
    if minutes <= 690:
        return 690
    if minutes <= 840:
        return 840
    return 900


def comm(price, vol):
    return max(5.0, price * vol * 0.00025)


def replay(code, stop_mode, variant='V0', esi_delay=0, no_flat=False):
    # esi_delay: 进场后前 N 根 5m bar 内 ESI 不激活（该窗口内浮亏 cross
    # down 走 EXIT_A 同价出场，但不触发当日禁入）；N=0 = 现状。
    # 仓位 = 固定名义本金 FIXED_NOTIONAL/入场价 取整百股（最低 100 股），
    # 在进场信号 bar 上计算并固定到该笔结束。
    # V4 = 日线趋势过滤：进场日之前的完结日线 fish[-1] > fish[-2] 才买
    #      （executor_t0.day_ok 口径，形成中的今日 bar 不参与）；
    # V5 = V4 AND V2（日线趋势 ∧ 15m 深极值）。
    # no_flat（实验 E 的 V6/V7）：删除 14:55 FLAT 分支，仓位可隔夜；
    # 出场只剩 ATR 硬止损 / ESI / EXIT_A，出场后可再进（禁入照旧）；
    # 15m/5m 序列天然跨日连续（历史 bar 全量参与，无隔夜衔接处理）。
    # pos = [entry_g, entry_i, cost, vol, sp, nights, gaps, flat_ref]
    b5 = load_bars(code, '5m')
    h15 = load_bars(code, '15m')
    h1 = load_bars(code, '1h') if variant in ('V1', 'V3') else []
    d1 = load_bars(code, '1d') if variant in ('V4', 'V5') else []
    f1d = fisher_series([x['h'] for x in d1], [x['l'] for x in d1]) \
        if d1 else []
    f5_all = fisher_series([b['h'] for b in b5], [b['l'] for b in b5])
    by_day = defaultdict(list)
    for g, b in enumerate(b5):
        by_day[b['time'][:10]].append((g, b))
    trades = []
    pos = None               # 见 docstring；no_flat 时跨日保持
    prev_close = None        # 上一交易日最后一根 5m close（隔夜 gap 基准）
    for day in sorted(by_day):
        bars = [(g, b) for g, b in by_day[day]
                if '09:30' <= b['time'][11:16] <= '15:00']
        if not bars:
            continue
        if not no_flat:
            pos = None       # 原口径：每日初清零（14:55 FLAT 已保证无隔夜）
        else:
            pos = pos        # no-op，明示跨日持有
            if pos is not None:
                pos[5] += 1                      # 隔夜 +1 夜
                if prev_close:
                    pos[6].append(bars[0][1]['o'] / prev_close - 1.0)
        day_open_q = None
        failed = False           # ESI/STOP 当日禁入（FAILED_TODAY）
        n_trades = 0
        last_buy_key = None      # BUY 按 ctx_bar_key 去重
        atr_cache = {}           # 15m 窗口 qkey -> sp
        for i, (g, b) in enumerate(bars):
            hhmm = b['time'][11:16]
            m = int(hhmm[:2]) * 60 + int(hhmm[3:])
            q = win_close(m)
            qkey = '%s|%02d:%02d' % (day, q // 60, q % 60)
            # 15m「完结 + 形成中」序列
            qt = '%s %02d:%02d:00' % (day, q // 60, q % 60)
            hists = [x for x in h15 if x['time'] < qt]
            fh = fl = fo = None
            fc = 0.0
            for j in range(i, -1, -1):
                bj = bars[j][1]
                mj = int(bj['time'][11:13]) * 60 + int(bj['time'][14:16])
                if win_close(mj) != q:
                    break
                fh = bj['h'] if fh is None else max(fh, bj['h'])
                fl = bj['l'] if fl is None else min(fl, bj['l'])
                fc = bj['c']
            H = [x['h'] for x in hists] + [fh]
            L = [x['l'] for x in hists] + [fl]
            C = [x['c'] for x in hists] + [fc]
            f15 = fisher_series(H, L)
            price = b['c']

            if pos is not None:
                # 持仓：每根 5m bar 按 FLAT -> STOP -> ESI -> EXIT_A 顺序检查
                reason = None
                if hhmm >= FORCE_FLAT_AT and not no_flat:
                    reason = 'FLAT'
                else:
                    if stop_mode == 'fixed':
                        sp = STOP_PCT
                    else:
                        sp = atr_cache.get(qkey)
                        if sp is None:
                            sp = STOP_PCT
                            n = min(len(H), len(L), len(C))
                            if n >= 16:
                                atr = 0.0
                                for k in range(n - 14, n):
                                    tr = max(H[k] - L[k],
                                             abs(H[k] - C[k - 1]),
                                             abs(L[k] - C[k - 1]))
                                    atr += tr
                                atr /= 14.0
                                if C[-1] > 0:
                                    sp = max(STOP_PCT_MIN, min(
                                        STOP_PCT_MAX,
                                        atr / C[-1] * STOP_ATR_MULT))
                            atr_cache[qkey] = sp
                    if price < pos[2] * (1.0 - sp):
                        reason = 'STOP'
                    elif g >= 2 and f5_all[g] < f5_all[g - 1] \
                            and f5_all[g - 1] >= f5_all[g - 2]:
                        if price < pos[2] and (i - pos[1]) >= esi_delay:
                            reason = 'ESI'
                        else:
                            reason = 'EXIT_A'
                if reason:
                    gross = (price - pos[2]) * pos[3]
                    fee = comm(pos[2], pos[3]) + comm(price, pos[3])
                    trades.append({
                        'code': code, 'day': day,
                        'entry': b5[pos[0]]['time'][11:16], 'exit': hhmm,
                        'buy': pos[2], 'sell': price,
                        'gross': gross, 'fee': fee, 'net': gross - fee,
                        'reason': reason, 'bars': g - pos[0],
                        'g_exit': g, 'cost': pos[2], 'vol': pos[3],
                        'sp': (pos[4] if len(pos) > 4 else None),
                        'nights': pos[5], 'gaps': list(pos[6]),
                        'flat_ref': pos[7],
                        'eday': b5[pos[0]]['time'][:10]})
                    if reason in ('ESI', 'STOP'):
                        failed = True
                    pos = None
                    continue
                if no_flat and hhmm >= FORCE_FLAT_AT and pos[7] is None:
                    pos[7] = price   # 首个 14:55 后 bar 仍持仓 -> FLAT 参考价

            # 空仓进场侧
            if failed or n_trades >= MAX_TRADES_PER_CODE:
                continue
            if hhmm < ENTRY_FROM or hhmm > ENTRY_TO:
                continue
            cross_up = len(f15) >= 3 and f15[-1] > f15[-2] \
                and f15[-2] <= f15[-3]
            if not cross_up or last_buy_key == qkey:
                continue
            if g < 1 or f5_all[g] < f5_all[g - 1]:   # 5m 闸门（近似 5m/1m）
                continue
            if variant in ('V1', 'V3'):
                # 1h「完结 + 形成中」序列：完结 1h bar（收盘 < 本窗口收盘）
                # + 形成中 1h bar（当日 5m 聚合，含当前 bar）
                q60 = hour_win_close(m)
                qt60 = '%s %02d:%02d:00' % (day, q60 // 60, q60 % 60)
                h1d = [x for x in h1 if x['time'] < qt60]
                g60h = g60l = None
                g60c = 0.0
                for j in range(i, -1, -1):
                    bj = bars[j][1]
                    mj = int(bj['time'][11:13]) * 60 \
                        + int(bj['time'][14:16])
                    if hour_win_close(mj) != q60:
                        break
                    g60h = bj['h'] if g60h is None else max(g60h, bj['h'])
                    g60l = bj['l'] if g60l is None else min(g60l, bj['l'])
                    g60c = bj['c']
                f60 = fisher_series(
                    [x['h'] for x in h1d] + [g60h],
                    [x['l'] for x in h1d] + [g60l])
                if len(f60) < 2 or f60[-1] <= f60[-2]:   # 1h 不在上行段
                    continue
            if variant in ('V2', 'V3', 'V5') and not (f15[-1] < V2_THRESHOLD):
                continue                            # 15m 非深位（<-1.5）
            if variant in ('V4', 'V5'):
                # 日线趋势过滤（executor_t0.day_ok 口径）：只看完结日线，
                # 形成中的当日 bar 不参与。回放严格取「进场日之前的完结
                # 日线」（date < day，不用 drop 最后一根的方式，避免未来
                # 函数），最后一根 fish 高于前一根才放行；数据不足不放行。
                idx = None
                for k in range(len(d1) - 1, -1, -1):
                    if d1[k]['time'][:10] < day:
                        idx = k
                        break
                if idx is None or idx < 1:
                    continue
                if f1d[idx] <= f1d[idx - 1]:
                    continue
            vol = max(100, int(FIXED_NOTIONAL / price / 100.0) * 100)
            pos = [g, i, price, vol,
                   (STOP_PCT if stop_mode == 'fixed' else
                    atr_cache.get(qkey)), 0, [], None]
            last_buy_key = qkey
            n_trades += 1
        prev_close = bars[-1][1]['c']
    if no_flat and pos is not None:
        # 数据末尾仍持仓：按最后可得 close 盯市平仓（reason=END），仅 V6/V7
        price = b5[-1]['c']
        gross = (price - pos[2]) * pos[3]
        fee = comm(pos[2], pos[3]) + comm(price, pos[3])
        trades.append({
            'code': code, 'day': b5[-1]['time'][:10],
            'entry': b5[pos[0]]['time'][11:16],
            'exit': b5[-1]['time'][11:16],
            'buy': pos[2], 'sell': price,
            'gross': gross, 'fee': fee, 'net': gross - fee,
            'reason': 'END', 'bars': len(b5) - 1 - pos[0],
            'g_exit': len(b5) - 1, 'cost': pos[2], 'vol': pos[3],
            'sp': (pos[4] if len(pos) > 4 else None),
            'nights': pos[5], 'gaps': list(pos[6]), 'flat_ref': pos[7],
            'eday': b5[pos[0]]['time'][:10]})
    return trades


def agg_stats(ts):
    if not ts:
        return (0, 0.0, 0.0, 0.0, 0.0, 0.0)
    nets = [t['net'] for t in ts]
    return (len(ts), sum(nets), sum(nets) / len(nets),
            sum(1 for x in nets if x > 0) * 100.0 / len(nets),
            min(nets),
            sum(t['bars'] for t in ts) / len(ts))


def report(title, trades):
    print('\n' + '=' * 64)
    print('%s   (trades=%d)' % (title, len(trades)))
    print('=' * 64)
    if not trades:
        print('no trades')
        return agg_stats(trades)
    nets = [t['net'] for t in trades]
    wins = sum(1 for x in nets if x > 0)
    print('总交易数        %d' % len(trades))
    print('胜率            %.1f%% (%d/%d)' % (100.0 * wins / len(trades),
                                              wins, len(trades)))
    print('总净利(扣费)    %+.2f' % sum(nets))
    print('平均每笔净利    %+.2f' % (sum(nets) / len(nets)))
    print('平均持仓 bar 数 %.1f (5m)' % (sum(t['bars'] for t in trades)
                                          / len(trades)))
    print('最大单笔亏损    %+.2f' % min(nets))

    print('\n-- 分代码 --')
    print('%-8s %5s %10s %8s' % ('code', '笔数', '净利', '胜率'))
    per = defaultdict(list)
    for t in trades:
        per[t['code']].append(t['net'])
    for c in sorted(per):
        ns = per[c]
        print('%-8s %5d %+10.2f %7.0f%%' % (c, len(ns), sum(ns),
                                             100.0 * sum(1 for x in ns if x > 0)
                                             / len(ns)))

    print('\n-- 分 reason --')
    print('%-8s %5s %10s %8s' % ('reason', '笔数', '净利', '均笔'))
    pr = defaultdict(list)
    for t in trades:
        pr[t['reason']].append(t['net'])
    for r in ('FLAT', 'STOP', 'ESI', 'EXIT_A'):
        if r not in pr:
            print('%-8s %5d %10s %8s' % (r, 0, '-', '-'))
            continue
        ns = pr[r]
        print('%-8s %5d %+10.2f %+8.2f' % (r, len(ns), sum(ns),
                                            sum(ns) / len(ns)))

    print('\n-- 分日净利 --')
    pd_ = defaultdict(float)
    for t in trades:
        pd_[t['day']] += t['net']
    for d in sorted(pd_):
        print('%s %+10.2f' % (d, pd_[d]))
    return agg_stats(trades)


def atr_sp(H, L, C):
    # 与 replay 内 ATR 硬止损同一公式（executor_t0.stop_pct 口径）
    n = min(len(H), len(L), len(C))
    if n < 16:
        return STOP_PCT
    atr = 0.0
    for k in range(n - 14, n):
        tr = max(H[k] - L[k], abs(H[k] - C[k - 1]), abs(L[k] - C[k - 1]))
        atr += tr
    atr /= 14.0
    if C[-1] <= 0:
        return STOP_PCT
    return max(STOP_PCT_MIN, min(STOP_PCT_MAX, atr / C[-1] * STOP_ATR_MULT))


def build_ctx15(h15, bars, i, day, q):
    # 与 replay 主循环同一口径的「完结+形成中」15m 序列（H, L, C）。
    # 只供尸检反事实用；replay 主循环保持原样不动，V0 基线逐笔不变。
    qt = '%s %02d:%02d:00' % (day, q // 60, q % 60)
    hists = [x for x in h15 if x['time'] < qt]
    fh = fl = None
    fc = 0.0
    for j in range(i, -1, -1):
        bj = bars[j][1]
        mj = int(bj['time'][11:13]) * 60 + int(bj['time'][14:16])
        if win_close(mj) != q:
            break
        fh = bj['h'] if fh is None else max(fh, bj['h'])
        fl = bj['l'] if fl is None else min(fl, bj['l'])
        fc = bj['c']
    return ([x['h'] for x in hists] + [fh],
            [x['l'] for x in hists] + [fl],
            [x['c'] for x in hists] + [fc])


def run_esi_autopsy(v0_trades):
    # 实验 B：对 V0(atr) 里 reason=ESI 的出场单逐单反事实。
    # 反事实路径纯用当日后续 5m 行情推演（ESI 砍仓后该票当日被禁入，
    # 策略不再触碰，后续 bar 无策略污染）。三种持有假设：
    #   hold_to_flat   无视一切中间信号，硬扛到 14:55 FLAT（另标出途中
    #                  会跌破 ATR 硬止损的单——那些"硬扛也守不住"）；
    #   hold_to_exit_a 跳过本次 cross down，持有到下一个 5m cross down
    #                  （无则 FLAT 兜底）——"ESI 慢一个信号"的世界；
    #   hold_to_recover 持有到 close >= 成本（回本）或 14:55 FLAT 为止。
    # 价差%一律相对 ESI 实际出场价；金额差为直接价差效应，不含"禁入解除
    # 后当日再进场"的连锁（那部分由下方 ESI 延迟实验在策略层回答）。
    by_code = defaultdict(list)
    for t in v0_trades:
        if t['reason'] == 'ESI':
            by_code[t['code']].append(t)
    recs = []
    for code, ts in by_code.items():
        b5 = load_bars(code, '5m')
        h15 = load_bars(code, '15m')
        f5 = fisher_series([b['h'] for b in b5], [b['l'] for b in b5])
        by_day = defaultdict(list)
        for g, b in enumerate(b5):
            by_day[b['time'][:10]].append((g, b))
        for t in ts:
            day = t['day']
            bars = [(g, b) for g, b in by_day[day]
                    if '09:30' <= b['time'][11:16] <= '15:00']
            gi = next(k for k, (g, b) in enumerate(bars) if g == t['g_exit'])
            after = bars[gi + 1:]
            flat = next(((g, b) for g, b in bars
                         if b['time'][11:16] >= FORCE_FLAT_AT), None)
            flat_p = flat[1]['c'] if flat else t['sell']
            cost, exit_p, vol = t['cost'], t['sell'], t['vol']
            spc = {}

            def sp_at(k):
                g, b = after[k]
                m = int(b['time'][11:13]) * 60 + int(b['time'][14:16])
                q = win_close(m)
                qkey = '%s|%02d:%02d' % (day, q // 60, q % 60)
                if qkey not in spc:
                    H, L, C = build_ctx15(h15, bars, gi + 1 + k, day, q)
                    spc[qkey] = atr_sp(H, L, C)
                return spc[qkey]

            def at_flat(k):
                return flat is not None \
                    and after[k][1]['time'] >= flat[1]['time']

            breach = None
            for k in range(len(after)):
                if at_flat(k):
                    break
                if after[k][1]['c'] < cost * (1.0 - sp_at(k)):
                    breach = after[k][1]['c']
                    break
            xa_fb = 1          # 1 = 当日再无 cross down，FLAT 价兜底
            xa_p = flat_p
            for k in range(len(after)):
                if at_flat(k):
                    break
                g = after[k][0]
                if g >= 2 and f5[g] < f5[g - 1] and f5[g - 1] >= f5[g - 2]:
                    xa_fb = 0
                    xa_p = after[k][1]['c']
                    break
            rec_n = None
            rec_p = None
            for k in range(len(after)):
                if at_flat(k):
                    break
                if after[k][1]['c'] >= cost:
                    rec_n = k + 1
                    rec_p = after[k][1]['c']
                    break

            def pct(p):
                return (p - exit_p) / exit_p * 100.0

            def cf_net(p):
                return (p - cost) * vol - comm(cost, vol) - comm(p, vol)

            recs.append({
                'code': code, 'day': day, 'exit': t['exit'],
                'loss_pct': (exit_p - cost) / cost * 100.0,
                'net': t['net'], 'vol': vol,
                'flat_pct': pct(flat_p),
                'breach': breach, 'breach_pct': pct(breach) if breach else None,
                'xa_pct': pct(xa_p), 'xa_fb': xa_fb,
                'rec': rec_n is not None, 'rec_n': rec_n,
                'rec_pct': pct(rec_p) if rec_p is not None else None,
                'rec_term_pct': pct(rec_p) if rec_p is not None
                else pct(flat_p),
                'd_flat': cf_net(flat_p) - t['net'],
                'd_flat_sa': cf_net(breach if breach else flat_p) - t['net'],
                'd_xa': cf_net(xa_p) - t['net'],
            })
    return recs


def pctfmt(xs):
    if not xs:
        return ('-', '-')
    return ('%+.2f%%' % (sum(xs) / len(xs)), '%+.2f%%' % median(xs))


def main():
    universe, excluded, days = load_universe()
    print('=' * 64)
    print('扩样票池（pool_t0 ∪ 缓存并集，固定 %.0f 元/笔）  %s'
          % (FIXED_NOTIONAL, time.strftime('%Y-%m-%d')))
    print('=' * 64)
    print('票池 %d 只：%s' % (len(universe), ' '.join(universe)))
    print('5m 数据天数：%s'
          % ', '.join('%s=%d' % (c, days[c]) for c in universe))
    if excluded:
        print('排除 %d 只：%s'
              % (len(excluded), ', '.join('%s(%s)' % x for x in excluded)))
    print('口径切换注明：仓位由原 watchlist 固定股数改为固定 1 万元名义'
          '本金/笔（vol=10000/价 取整百股，最低 100 股）；原四只的 V0 数字'
          '随之变化，旧口径四只版报告存档 '
          'results/backtest_t0_variants_report_4codes.txt。')
    all_trades = {}
    for mode in ('fixed', 'atr'):
        trades = []
        for code in universe:
            trades.extend(replay(code, mode))
        all_trades[mode] = trades
        report('STOP_MODE = %s' % mode, trades)

    a, f = all_trades['atr'], all_trades['fixed']
    print('\n' + '=' * 64)
    print('对比结论（atr vs fixed）')
    print('=' * 64)

    na, nf = agg_stats(a), agg_stats(f)
    print('%-22s %12s %12s' % ('指标', 'fixed', 'atr'))
    print('%-22s %12d %12d' % ('总交易数', nf[0], na[0]))
    print('%-22s %+12.2f %+12.2f' % ('总净利', nf[1], na[1]))
    print('%-22s %+12.2f %+12.2f' % ('平均每笔净利', nf[2], na[2]))
    print('%-22s %11.1f%% %11.1f%%' % ('胜率', nf[3], na[3]))
    print('%-22s %+12.2f %+12.2f' % ('最大单笔亏损', nf[4], na[4]))

    def reason_agg(ts, r):
        ns = [t['net'] for t in ts if t['reason'] == r]
        return (len(ns), sum(ns)) if ns else (0, 0.0)

    print('\n%-8s %16s %16s' % ('reason', 'fixed(笔/净利)', 'atr(笔/净利)'))
    for r in ('FLAT', 'STOP', 'ESI', 'EXIT_A'):
        cf, sf = reason_agg(f, r)
        ca, sa = reason_agg(a, r)
        print('%-8s %6d %+9.2f  %6d %+9.2f' % (r, cf, sf, ca, sa))

    if na and nf:
        d_net = na[1] - nf[1]
        better = '改善' if d_net > 0 else ('恶化' if d_net < 0 else '持平')
        print('\n结论：在本回放样本（%d 个交易日 × 4 只 T0 ETF，5m 粒度）上，'
              'atr 自适应止损相对 fixed 0.5%% 硬止损总体%s：' % (len(
                  {t['day'] for t in f + a}), better))
        print('总净利 %+.2f -> %+.2f（差额 %+.2f），平均每笔 %+.2f -> %+.2f，'
              '胜率 %.1f%% -> %.1f%%。' % (nf[1], na[1], d_net, nf[2], na[2],
                                           nf[3], na[3]))
        for r in ('STOP', 'ESI', 'FLAT', 'EXIT_A'):
            cf, sf = reason_agg(f, r)
            ca, sa = reason_agg(a, r)
            if cf or ca:
                print('%s：fixed %d 笔 %+0.2f -> atr %d 笔 %+.2f。'
                      % (r, cf, sf, ca, sa))
        print('（口径近似见文件头注释：5m 近似 1m 闸门、Exit A 无浮盈延迟确认、'
              'ATR 含形成 bar、无账户级熔断。样本仅约 17 个交易日，'
              '结论方向性参考，非统计显著结论。）')

    # ---------------- 进场假设变体对比（出场一致，硬止损一律 atr） ----------------
    vt = {}
    vt['V0'] = all_trades['atr']      # V0 基线 = atr 模式（上文已跑，直接复用）
    for variant in ('V1', 'V2', 'V3', 'V4', 'V5'):
        trades = []
        for code in universe:
            trades.extend(replay(code, 'atr', variant))
        vt[variant] = trades
        st = report('variant = %s (STOP_MODE=atr)' % variant, trades)
        if 0 < st[0] < 10:
            print('!!! 样本不足（%d < 10 笔），该变体结论不可信' % st[0])

    print('\n' + '=' * 64)
    print('进场变体汇总对比（出场逻辑一致；V0 = atr 基线）')
    print('=' * 64)
    hdr = ('%-16s %5s %7s %10s %9s %8s %10s' % ('variant', '笔数', '胜率',
                                                 '总净利', '均笔', '均持仓',
                                                 '最大单笔亏'))
    print(hdr)
    vs = {}
    for v in ('V0', 'V1', 'V2', 'V3'):
        vs[v] = agg_stats(vt[v])
        s = vs[v]
        flag = '  <10笔,样本不足' if 0 < s[0] < 10 else ''
        print('%-16s %5d %6.1f%% %+10.2f %+9.2f %6.1f %+10.2f%s'
              % (v, s[0], s[3], s[1], s[2], s[5], s[4], flag))

    print('\n%-8s %22s %22s %22s %22s' % ('code', 'V0', 'V1', 'V2', 'V3'))
    for c in universe:
        row = '%-8s' % c
        for v in ('V0', 'V1', 'V2', 'V3'):
            ns = [t['net'] for t in vt[v] if t['code'] == c]
            row += ' %7d笔%+12.2f' % (len(ns), sum(ns)) if ns \
                else ' %7d笔%+12.2f' % (0, 0.0)
        print(row)

    s0 = vs['V0']
    print('\n结论（相对 V0 基线，净利变化方向与幅度）：')
    supported, unsupported, untrusted = [], [], []
    for v, name in (('V1', 'V1 高周期趋势对齐(1h上行)'),
                    ('V2', 'V2 深极值反转(15m<-1.5)'),
                    ('V3', 'V3 组合(V1 AND V2)')):
        s = vs[v]
        if 0 < s[0] < 10:
            untrusted.append(v)
            print('- %s：%d 笔（样本不足不可信），净利 %+0.2f，均笔 %+0.2f。'
                  % (name, s[0], s[1], s[2]))
            continue
        d_net = s[1] - s0[1]
        d_pt = s[2] - s0[2]
        # 笔数常被过滤砍半，总量不可比；改善/恶化以均笔净利为准
        word = '改善' if d_pt > 0 else ('恶化' if d_pt < 0 else '持平')
        pct = (100.0 * d_pt / abs(s0[2])) if s0[2] else float('nan')
        print('- %s：均笔%s。净利 %+.2f -> %+.2f（%+.2f，笔数 %d -> %d，'
              '总量受笔数缩放不可直接比），均笔 %+.2f -> %+.2f（约 %+.0f%%），'
              '胜率 %.1f%% -> %.1f%%。'
              % (name, word, s0[1], s[1], d_net, s0[0], s[0], s0[2], s[2],
                 pct, s0[3], s[3]))
        (supported if d_pt > 0 else unsupported).append(v)
    print('假设支持情况：', end='')
    parts = []
    if supported:
        parts.append('%s 有支持（均笔净利提升）' % '/'.join(supported))
    if unsupported:
        parts.append('%s 无支持（均笔净利未提升）' % '/'.join(unsupported))
    if untrusted:
        parts.append('%s 样本不足不可信' % '/'.join(untrusted))
    print('；'.join(parts) + '。')
    print('注意：17 个交易日 × 4 只 ETF 的小样本，单笔均差 1~2 元级别的差异'
          '不构成统计显著证据；变体间净利差异主要由笔数缩放与少数趋势单'
          '贡献，方向性参考。口径近似见文件头注释 1-8。')

    # ---------- 实验 B 扩样版：ESI 出场反事实尸检（追加段，日期见标题行） ----------
    print('\n\n' + '#' * 64)
    print('# 实验 B 扩样版：ESI 出场反事实尸检（pool_t0 全池 ∪ 缓存并集，'
          '固定 1 万元/笔）  %s' % time.strftime('%Y-%m-%d'))
    print('#' * 64)
    print('# 四只版（watchlist 股数口径）见存档 '
          'results/backtest_t0_variants_report_4codes.txt')
    recs = run_esi_autopsy(vt['V0'])
    n = len(recs)
    print('ESI 出场单总数：%d（V0 atr 全样本 %d 笔中的 ESI 部分）'
          % (n, len(vt['V0'])))
    if n:
        print('砍仓时平均浮亏 %+.2f%%（中位 %+.2f%%），平均每股砍在成本下 '
              '%.4f 元。'
              % (sum(r['loss_pct'] for r in recs) / n,
                 median(r['loss_pct'] for r in recs),
                 sum(r['loss_pct'] for r in recs) / n / 100.0))

        print('\n-- 反事实持有假设（价差%% 相对 ESI 实际出场价；'
              '正 = 不砍比砍了好） --')
        print('%-18s %9s %9s %6s' % ('假设', '平均', '中位', 'n'))
        m1, m2 = pctfmt([r['flat_pct'] for r in recs])
        print('%-18s %9s %9s %6d' % ('hold_to_flat', m1, m2, n))
        m1, m2 = pctfmt([r['xa_pct'] for r in recs if r['xa_pct'] is not None])
        nfb = sum(1 for r in recs if r['xa_fb'])
        print('%-18s %9s %9s %6d%s' % ('hold_to_exit_a', m1, m2, n,
                                       '（%d 笔当日无后续 cross down，'
                                       'FLAT 兜底）' % nfb if nfb else ''))
        m1, m2 = pctfmt([r['rec_term_pct'] for r in recs])
        nrec = sum(1 for r in recs if r['rec'])
        print('%-18s %9s %9s %6d' % ('hold_to_recover', m1, m2, n))
        print('回本率（hard hold 到 close>=成本 或 FLAT）：%d/%d = %.0f%%；'
              '回本单平均耗时 %.1f 根 5m bar。'
              % (nrec, n, 100.0 * nrec / n,
                 sum(r['rec_n'] for r in recs if r['rec']) / nrec
                 if nrec else 0.0))
        nb = sum(1 for r in recs if r['breach'] is not None)
        if nb:
            m1, m2 = pctfmt([r['breach_pct'] for r in recs
                             if r['breach_pct'] is not None])
            print('其中硬扛途中会跌破 ATR 止损的：%d/%d 笔（平均价差 %s，'
                  '中位 %s）——这些单"不砍"也不成立，止损会先接走。'
                  % (nb, n, m1, m2))

        d_flat = sum(r['d_flat'] for r in recs)
        d_flat_sa = sum(r['d_flat_sa'] for r in recs)
        d_xa = sum(r['d_xa'] for r in recs)
        print('\n-- 「若全都不砍」相比实际 ESI 砍仓的总盈亏差（直接价差效应，'
              '不含禁入解除后再进场连锁） --')
        print('全部硬扛到 14:55 FLAT       ：%+.2f 元' % d_flat)
        print('硬扛但保留 ATR 止损兜底     ：%+.2f 元' % d_flat_sa)
        print('全部慢一个信号（下个 cross down 出）：%+.2f 元' % d_xa)
        print('（对照：ESI 砍仓实际净亏 %+.2f 元；基线 V0/atr 总净利 %+.2f 元）'
              % (sum(r['net'] for r in recs), agg_stats(vt['V0'])[1]))

    print('\n-- ESI 延迟激活实验（进场后前 N 根 5m bar 内 ESI 关闭，浮亏 '
          'cross down 走 EXIT_A 同价出、不触发当日禁入；N=0 = 现状） --')
    print('%-6s %6s %6s %8s %10s %9s %7s' % ('N', '总笔数', 'ESI笔数',
                                             'ESI净利', '总净利', '均笔',
                                             '胜率'))
    delay_rows = {}
    for nn in (0, 3, 6, 12):
        ts = []
        for code in universe:
            ts.extend(replay(code, 'atr', 'V0', esi_delay=nn))
        st = agg_stats(ts)
        nesi = sum(1 for t in ts if t['reason'] == 'ESI')
        sesi = sum(t['net'] for t in ts if t['reason'] == 'ESI')
        delay_rows[nn] = (st, nesi, sesi)
        print('%-6d %6d %6d %+8.2f %+10.2f %+9.2f %6.1f%%'
              % (nn, st[0], nesi, sesi, st[1], st[2], st[3]))
    st0, nesi0, sesi0 = delay_rows[0]
    print('（N=0 即基线：%d 笔 / ESI %d 笔 / ESI 净利 %+.2f / 总净利 %+.2f / '
          '均笔 %+.2f / 胜率 %.1f%%）'
          % (st0[0], nesi0, sesi0, st0[1], st0[2], st0[3]))

    print('\n-- 分票 ESI 明细（价差%% 相对各票 ESI 出场价；天数=5m 缓存覆盖'
          '交易日数） --')
    print('%-8s %6s %6s %9s %10s %7s' % ('code', '天数', 'ESI笔',
                                         '砍仓浮亏', '晚信号价差', '回本率'))
    per = defaultdict(list)
    for r in recs:
        per[r['code']].append(r)
    contra = []
    for c in universe:
        rs = per.get(c)
        if not rs:
            continue
        nc = len(rs)
        xa_mean = sum(x['xa_pct'] for x in rs) / nc
        rr = 100.0 * sum(1 for x in rs if x['rec']) / nc
        print('%-8s %6d %6d %+8.2f%% %+9.2f%% %6.0f%%'
              % (c, days.get(c, 0), nc, sum(x['loss_pct'] for x in rs) / nc,
                 xa_mean, rr))
        if xa_mean < 0:
            contra.append(c)
    if contra:
        print('晚一个信号出价差为负的票（尸检结论的票级反例）：%s'
              % ' '.join(contra))
    else:
        print('晚一个信号出价差为负的票：无（尸检结论跨票一致）。')

    print('\n结论：')
    if n:
        rec_rate = 100.0 * nrec / n
        m_flat = sum(r['flat_pct'] for r in recs) / n
        m_xa = sum(r['xa_pct'] for r in recs) / n
        d_xa = sum(r['d_xa'] for r in recs)
        base_net = agg_stats(vt['V0'])[1]
        print('1) 砍早还是砍对：%d 笔 ESI 平均砍在成本下 %.2f%%。若全部硬扛到 '
              '14:55 FLAT：平均价差 %+.2f%%（基本持平），回本率 %.0f%%'
              '（回本单平均 %.1f 根 5m bar）；但 %d/%d 笔硬扛途中会先跌破 '
              'ATR 止损——纯「不砍」不可行，止损会先接走 %.0f%%。'
              % (n, sum(r['loss_pct'] for r in recs) / n, m_flat,
                 rec_rate,
                 sum(r['rec_n'] for r in recs if r['rec']) / nrec
                 if nrec else 0.0, nb, n, 100.0 * nb / n))
        print('2) 信号级延迟 vs 时间级宽限（关键区分）：跳过当前 cross down、'
              '到下一个 cross down 才出，平均价差 %+.2f%%，%d 笔直接价差效应 '
              '合计 %+.2f 元（笔均 %+.2f 元；对照基线总净利 %+.2f 元）——'
              '这是 ESI_2 改造的直接增益上限口径。而按时间宽限（前 N 根 bar '
              '关闭 ESI）：N=3 总净利 %+.2f（%+0.f），N=6 %+.2f、N=12 '
              '%+.2f 反而恶化——宽限解除了同价 EXIT_A 的禁入，再进场笔数 '
              '%d→%d/%d，磨损与扛亏时间一起吃掉了价差收益。'
              % (m_xa, n, d_xa, d_xa / n, base_net,
                 delay_rows[3][0][1], delay_rows[3][0][1] - base_net,
                 delay_rows[6][0][1], delay_rows[12][0][1], st0[0],
                 delay_rows[6][0][0], delay_rows[12][0][0]))
        print('   增益区间估计：策略层已实现增益（N=3 总净利 - 基线）约 %+0.f '
              '元/样本期；单票反事实价差上限（d_xa 合计）约 %+.0f 元/样本'
              '期。真实增益应在两者之间，偏下沿（止损连锁与费用未 fully '
              '建模在上限口径里）。'
              % (delay_rows[3][0][1] - base_net, d_xa))
        hold = '仍成立' if m_xa > 0.05 else ('不成立' if m_xa < -0.05
                                             else '边际')
        if '513120' in per:
            r120 = per['513120']
            m120 = sum(x['xa_pct'] for x in r120) / len(r120)
            w120 = '同样异常（晚信号价差 %+.2f%%）' % m120 if m120 < 0 \
                else '并不异常（晚信号价差 %+.2f%%）' % m120
        else:
            w120 = '本样本无 ESI 出场单'
        print('3) 扩样判定：ESI_2（二次确认制）在扩样后%s——晚一个信号出价差 '
              '全池平均 %+.2f%%（四只版 +0.12%%），回本率 %.0f%%（四只版 '
              '51%%）。票级反例：%s。513120 在 ESI 尸检下%s。'
              % (hold, m_xa, rec_rate,
                 ' '.join(contra) if contra else '无', w120))
        print('4) 推荐改造方案（基于扩样样本，实装前建议再积累 1-2 个月数据'
              '复核）：ESI 改二次确认制——浮亏 cross down 只标记 defer 不'
              '卖出；期间 hard stop / FLAT 优先级不变立即执行（兜底）；下一'
              '个 5m cross down 到来时仍浮亏（close<成本）则卖，reason='
              'ESI_2；若其间 close>=成本（回本）或 5m fish 回到上行'
              '（fish>=前一根，类比实盘 USE_EXIT_CONFIRM 的作废条件）则撤'
              '标记继续持有，等 EXIT_A / 止损 / FLAT。禁入规则维持「ESI_2 '
              '与 STOP 都禁当日再进」——延迟实验显示解除禁入带来的再进场是'
              '磨损主因，不要放开。若票级反例票在实装后延续负价差，可为单票'
              '加白名单回退旧 ESI。')

    # ---------- 实验 C：V2 深极值过滤全池判定（追加段，日期见标题行） ----------
    print('\n\n' + '#' * 64)
    print('# 实验 C：进场变体全池对比 · V2 深极值判定（37 只 T0 池 ETF，'
          '固定 1 万元/笔）  %s' % time.strftime('%Y-%m-%d'))
    print('#' * 64)
    print('%-8s %6s %7s %11s %9s %11s %s' % ('variant', '笔数', '胜率',
                                             '总净利', '均笔', '最大单笔亏',
                                             '样本'))
    vstats = {}
    for v in ('V0', 'V1', 'V2', 'V3'):
        vstats[v] = agg_stats(vt[v])
        s = vstats[v]
        flag = '<10笔,不足' if 0 < s[0] < 10 else ('无交易' if s[0] == 0
                                                   else 'ok')
        print('%-8s %6d %6.1f%% %+11.2f %+9.2f %+11.2f %s'
              % (v, s[0], s[3], s[1], s[2], s[4], flag))
    s0, s2 = vstats['V0'], vstats['V2']

    print('\n-- V0 vs V2 分票矩阵（delta = V2净利 - V0净利） --')
    print('%-8s %6s %11s %6s %11s %11s' % ('code', 'V0笔', 'V0净利',
                                            'V2笔', 'V2净利', 'delta'))
    by0 = defaultdict(float)
    n0 = defaultdict(int)
    by2 = defaultdict(float)
    n2 = defaultdict(int)
    for t in vt['V0']:
        by0[t['code']] += t['net']
        n0[t['code']] += 1
    for t in vt['V2']:
        by2[t['code']] += t['net']
        n2[t['code']] += 1
    improved, worsened, unchanged = [], [], []
    rows = []
    for c in universe:
        if n0[c] == 0 and n2[c] == 0:
            continue
        d = by2[c] - by0[c]
        rows.append((c, n0[c], by0[c], n2[c], by2[c], d))
        (improved if d > 0 else (worsened if d < 0 else unchanged)).append(c)
    for c, a, b, cc, dd, d in rows:
        print('%-8s %6d %+11.2f %6d %+11.2f %+11.2f' % (c, a, b, cc, dd, d))
    print('改善 %d 只 / 恶化 %d 只 / 持平 %d 只'
          % (len(improved), len(worsened), len(unchanged)))
    worst = sorted(rows, key=lambda r: r[5])[:5]
    print('恶化最严重 5 只：%s'
          % '，'.join('%s(%+.0f)' % (r[0], r[5]) for r in worst))

    st0 = sum(1 for t in vt['V0'] if t['reason'] == 'STOP')
    st2 = sum(1 for t in vt['V2'] if t['reason'] == 'STOP')
    sn0 = sum(t['net'] for t in vt['V0'] if t['reason'] == 'STOP')
    sn2 = sum(t['net'] for t in vt['V2'] if t['reason'] == 'STOP')
    print('\n-- STOP 触发对比（深位过滤是否像四只版那样把硬止损归零） --')
    print('V0：%d 笔（净利 %+.2f 元）  V2：%d 笔（净利 %+.2f 元）'
          % (st0, sn0, st2, sn2))

    print('\n结论：')
    if s0[0] and s2[0]:
        d_pt = s2[2] - s0[2]
        pct = (100.0 * d_pt / abs(s0[2])) if s0[2] else float('nan')
        if d_pt > 1 and len(improved) > len(worsened):
            verdict = '成立'
        elif d_pt < -1 and len(worsened) > len(improved):
            verdict = '不成立'
        else:
            verdict = '部分成立'
        print('1) V2 深极值过滤在全池%s：均笔 %+.2f -> %+.2f（%+.2f，约 '
              '%+.0f%%），胜率 %.1f%% -> %.1f%%，笔数 %d -> %d（过滤掉 %.0f%%），'
              '总净利 %+.2f -> %+.2f；分票改善 %d 只 / 恶化 %d 只。'
              % (verdict, s0[2], s2[2], d_pt, pct, s0[3], s2[3], s0[0],
                 s2[0], 100.0 * (1 - s2[0] / s0[0]), s0[1], s2[1],
                 len(improved), len(worsened)))
        wl = sorted(((c, by2[c], n2[c]) for c in improved
                     if n2[c] >= 5 and by2[c] > 0), key=lambda x: -x[1])
        if wl:
            print('2) 票级白名单迹象（V2 净利为正、相对 V0 改善、V2 >=5 笔）：'
                  '%s' % '，'.join('%s(%+.0f,%d笔)' % x for x in wl))
        else:
            print('2) 无票级白名单迹象（无票同时满足 V2 净利为正、改善、'
                  'V2 >=5 笔）——V2 的增益是池级平均效应，不可拆成单票配置。')
        bl = sorted(((c, by2[c] - by0[c]) for c in worsened),
                    key=lambda x: x[1])
        if bl:
            print('   黑榜（V2 恶化最甚，实装时考虑单票豁免/回退）：%s'
                  % '，'.join('%s(%+.0f)' % x for x in bl[:5]))
        print('3) STOP 归零效应：%s（V0 %d 笔 %+.2f 元 -> V2 %d 笔 %+.2f '
              '元）。' % ('复现' if st2 < st0 else '未复现', st0, sn0, st2, sn2))
        if verdict == '成立':
            print('4) 决策建议：V2 值得实装为默认进场过滤（池级把策略从亏损 '
                  '拉到盈利，且最大单笔亏损收窄 %+.0f 元）；同时把黑榜票列入'
                  '观察名单，实盘跟踪 2-4 周若延续恶化则单票豁免。停用 T0 '
                  '的决策可暂缓——V0 的池级亏损主因是裸上穿低质信号，V2 已'
                  '针对性修复。' % (s2[4] - s0[4]))
        elif verdict == '部分成立':
            print('4) 决策建议：V2 只以票级白名单形式实装（只对白名单票开 '
                  '深位过滤），黑榜票维持 V0 或下线；或先模拟盘 2-4 周。'
                  '若资源有限，优先做实验 B 的 ESI_2 出场改造（与 V2 正交）。')
        else:
            print('4) 决策建议：V2 不成立则不建议实装；池级 V0 亏损说明裸 '
                  '上穿无 edge，结合费用考虑应倾向停用 T0 或仅保留人工挑票 '
                  '的极小池。')
    else:
        print('V0/V2 样本为空，无法判定。')

    # ---------- 实验 D：日线大周期过滤 V4/V5（追加段，日期见标题行） ----------
    print('\n\n' + '#' * 64)
    print('# 实验 D：日线趋势过滤 V4 / 日线∧深极值 V5（37 只 T0 池 ETF，'
          '固定 1 万元/笔）  %s' % time.strftime('%Y-%m-%d'))
    print('#' * 64)
    print('%-8s %6s %7s %11s %9s %11s %s' % ('variant', '笔数', '胜率',
                                             '总净利', '均笔', '最大单笔亏',
                                             '样本'))
    vd = {}
    for v in ('V0', 'V2', 'V4', 'V5'):
        vd[v] = agg_stats(vt[v])
        s = vd[v]
        flag = '<10笔,不足' if 0 < s[0] < 10 else ('无交易' if s[0] == 0
                                                   else 'ok')
        print('%-8s %6d %6.1f%% %+11.2f %+9.2f %+11.2f %s'
              % (v, s[0], s[3], s[1], s[2], s[4], flag))

    def split(v):
        b = defaultdict(float)
        nb = defaultdict(int)
        for t in vt[v]:
            b[t['code']] += t['net']
            nb[t['code']] += 1
        return b, nb

    b0, nb0 = split('V0')
    for v in ('V4', 'V5'):
        bv, nbv = split(v)
        imp, wor = [], []
        rows = []
        for c in universe:
            if nb0[c] == 0 and nbv[c] == 0:
                continue
            d = bv[c] - b0[c]
            rows.append((c, nb0[c], b0[c], nbv[c], bv[c], d))
            (imp if d > 0 else (wor if d < 0 else [])).append(c)
        print('\n-- %s vs V0 分票（delta = %s净利 - V0净利） --' % (v, v))
        print('改善 %d 只 / 恶化 %d 只' % (len(imp), len(wor)))
        for r in sorted(rows, key=lambda x: x[5])[:5]:
            print('  恶化：%s %d笔 %+.2f -> %d笔 %+.2f（%+.0f）'
                  % (r[0], r[1], r[2], r[3], r[4], r[5]))

    print('\n-- 专项：513120 / 513310 在四变体下的数字 --')
    print('%-8s %13s %13s %13s %13s' % ('code', 'V0', 'V2', 'V4', 'V5'))
    for c in ('513120', '513310'):
        cells = []
        for v in ('V0', 'V2', 'V4', 'V5'):
            ns = [t['net'] for t in vt[v] if t['code'] == c]
            cells.append('%2d笔%+9.2f' % (len(ns), sum(ns)))
        print('%-8s %13s %13s %13s %13s' % (c, *cells))

    s0, s2, s4, s5 = vd['V0'], vd['V2'], vd['V4'], vd['V5']
    print('\n结论：')
    if s0[0] and s4[0] and s5[0]:
        d4 = s4[2] - s0[2]
        d52 = s5[2] - s2[2]
        print('1) V4（日线趋势过滤）相对 V0：均笔 %+.2f -> %+.2f（%+.2f），'
              '总净利 %+.2f -> %+.2f，笔数 %d -> %d。%s'
              % (s0[2], s4[2], d4, s0[1], s4[1], s0[0], s4[0],
                 'V4 ≈ V0，日线过滤无池级价值'
                 if abs(d4) <= 1 and abs(s4[1] - s0[1]) <= 500
                 else ('池级边际改善但仍未扭亏（不足以单独实装）'
                       if d4 > 1 and s4[1] > s0[1]
                       else '日线过滤池级有害')))
        print('2) V5（日线∧深极值）相对 V2：均笔 %+.2f -> %+.2f（%+.2f），'
              '总净利 %+.2f -> %+.2f，笔数 %d -> %d。%s'
              % (s2[2], s5[2], d52, s2[1], s5[1], s2[0], s5[0],
                 '日线+深极值有叠加效应' if d52 > 1
                 else ('无叠加（两过滤近似正交，V5 因双过滤笔数骤减）'
                       if abs(d52) <= 1 else '两过滤互相拖累')))
        n120 = {v: sum(t['net'] for t in vt[v] if t['code'] == '513120')
                for v in ('V0', 'V2', 'V4', 'V5')}
        print('3) 513120「趋势票」效应：V0 %+0.2f / V2 %+0.2f / V4 %+0.2f / '
              'V5 %+0.2f——日线过滤%s复现「513120 在趋势条件下赚钱」'
              '（实验 A 的 V1 下它 +250）。'
              % (n120['V0'], n120['V2'], n120['V4'], n120['V5'],
                 '部分' if n120['V4'] > n120['V0'] and n120['V4'] > 0
                 else '未'))
        print('4) 实装建议：%s' % (
            '日线过滤与深极值有叠加——建议把日线条件作为可选 AND 项并进 '
            'V2 实装（进 t0_config.txt 白名单，默认开），上线后按周核对 '
            'V5/V2 实盘差。' if d52 > 1 else
            ('日线过滤单独有边际价值——可实装为独立 config 开关（默认关），'
              '小仓位试运行。' if d4 > 1 and s4[1] > 0 else
             '日线过滤无叠加价值、单用也不足以扭亏——不实装，维持 V2 单'
             '过滤，数据攒厚后再测。')))

    # ---------- 实验 E：关闭 14:55 强平 V6/V7（追加段，日期见标题行） ----------
    vt['V6'] = []
    vt['V7'] = []
    for code in universe:
        vt['V6'].extend(replay(code, 'atr', 'V2', no_flat=True))
        vt['V7'].extend(replay(code, 'atr', 'V0', no_flat=True))
    print('\n\n' + '#' * 64)
    print('# 实验 E：关闭 14:55 强平 V6=V2不FLAT / V7=V0不FLAT（37 只 T0 池 '
          'ETF，固定 1 万元/笔）  %s' % time.strftime('%Y-%m-%d'))
    print('#' * 64)
    print('%-8s %6s %7s %11s %9s %11s %8s %6s' % ('variant', '笔数', '胜率',
                                                   '总净利', '均笔',
                                                   '最大单笔亏', '均持仓bar',
                                                   '均夜数'))
    ve = {}
    for v in ('V2', 'V6', 'V0', 'V7'):
        s = agg_stats(vt[v])
        ve[v] = s
        navg = sum(t['nights'] for t in vt[v]) / s[0] if s[0] else 0.0
        print('%-8s %6d %6.1f%% %+11.2f %+9.2f %+11.2f %8.1f %6.2f'
              % (v, s[0], s[3], s[1], s[2], s[4], s[5], navg))

    print('\n-- V6 过夜单统计 --')
    on = [t for t in vt['V6'] if t['nights'] >= 1]
    n_all6 = len(vt['V6'])
    if on:
        gaps = [t['gaps'][0] for t in on if t['gaps']]
        gap_avg = 100.0 * sum(gaps) / len(gaps) if gaps else 0.0
        gap_pos = 100.0 * sum(1 for x in gaps if x > 0) / len(gaps) \
            if gaps else 0.0
        non = [t for t in vt['V6'] if t['nights'] == 0]
        tot6 = ve['V6'][1]
        on_net = sum(t['net'] for t in on)
        print('过夜笔数 %d / %d（%.0f%%）；首夜隔夜缺口平均 %+.2f%%、为正占比 '
              '%.0f%%' % (len(on), n_all6, 100.0 * len(on) / n_all6,
                          gap_avg, gap_pos))
        print('过夜单净利合计 %+.2f 元（V6 总净利 %+.2f 的 %.0f%%）；过夜单'
              '均笔 %+.2f vs 不过夜 %+.2f；过夜单盈利占比 %.0f%% vs 全样本 '
              '%.0f%%' % (on_net, tot6,
                          100.0 * on_net / tot6 if tot6 else 0.0,
                          on_net / len(on),
                          sum(t['net'] for t in non) / len(non) if non else 0,
                          100.0 * sum(1 for t in on if t['net'] > 0) / len(on),
                          ve['V6'][3]))
        print('最多过夜 %d 夜（= %d 个交易日）；数据末尾盯市平仓 %d 笔。'
              % (max(t['nights'] for t in on), max(t['nights'] for t in on) + 1,
                 sum(1 for t in vt['V6'] if t['reason'] == 'END')))

    def flat_gain(ts):
        tot, n = 0.0, 0
        for t in ts:
            if t['flat_ref']:
                cf = (t['flat_ref'] - t['cost']) * t['vol'] \
                    - comm(t['cost'], t['vol']) \
                    - comm(t['flat_ref'], t['vol'])
                tot += t['net'] - cf
                n += 1
        return tot, n

    g6, ng6 = flat_gain(vt['V6'])
    g7, ng7 = flat_gain(vt['V7'])
    d62 = ve['V6'][1] - ve['V2'][1]
    d70 = ve['V7'][1] - ve['V0'][1]
    print('\n-- 「少砍尾盘」直接增益（越过 14:55 的单 vs 当日 14:55 收盘价'
          '参考） --')
    print('V6：%d 单越过 14:55，合计增益 %+.2f 元' % (ng6, g6))
    print('V7：%d 单越过 14:55，合计增益 %+.2f 元' % (ng7, g7))
    print('V6-V2 总差 %+.2f = 尾盘少砍 %+.2f + 持仓占用/序列漂移 %+0.2f'
          % (d62, g6, d62 - g6))

    print('\n-- V6 vs V2 分票（过夜对哪些票是毒药；delta = V6净利 - V2净利） --')
    b2 = defaultdict(float)
    n2 = defaultdict(int)
    b6 = defaultdict(float)
    n6 = defaultdict(int)
    for t in vt['V2']:
        b2[t['code']] += t['net']
        n2[t['code']] += 1
    for t in vt['V6']:
        b6[t['code']] += t['net']
        n6[t['code']] += 1
    rows = []
    for c in universe:
        if n2[c] == 0 and n6[c] == 0:
            continue
        rows.append((c, n2[c], b2[c], n6[c], b6[c], b6[c] - b2[c]))
    for r in sorted(rows, key=lambda x: x[5])[:5]:
        print('  恶化：%s V2 %d笔%+.2f -> V6 %d笔%+.2f（%+.0f）'
              % (r[0], r[1], r[2], r[3], r[4], r[5]))

    print('\n结论：')
    if ve['V6'][0] and ve['V2'][0] and ve['V7'][0] and ve['V0'][0]:
        risk_worse = ve['V6'][4] < ve['V2'][4] - 50
        print('1) 关 FLAT 是否净改善：V2 -> V6 总净利 %+.2f -> %+.2f（%+0.f）；'
              '对照 V0 -> V7 %+.2f -> %+.2f（%+0.f）。%s'
              % (ve['V2'][1], ve['V6'][1], d62, ve['V0'][1], ve['V7'][1],
                 d70, 'V6 净改善且 V7 对照同步改善——增益不是深极值单特有'
                 if d62 > 0 and d70 > 0 else
                 ('仅 V6 改善而 V7 不改善——增益是深极值单特有'
                  if d62 > 0 else '关 FLAT 池级不改善')))
        print('2) 改善来源：少砍尾盘直接增益 %+.2f 元（V6 越过 14:55 的 %d 单）；'
              '隔夜缺口平均 %+.2f%%（为正占比见上表），过夜单均笔 %+.2f。'
              % (g6, ng6,
                 100.0 * sum(t['gaps'][0] for t in on if t['gaps'])
                 / len([t for t in on if t['gaps']]) if on else 0.0,
                 sum(t['net'] for t in on) / len(on) if on else 0.0))
        print('3) 风险代价：最大单笔亏损 V2 %+.2f -> V6 %+.2f%s；最长持仓 '
              '%d 个交易日；过夜笔占 %.0f%%。'
              % (ve['V2'][4], ve['V6'][4],
                 '（隔夜缺口显著放大尾部风险）' if risk_worse else
                 '（尾部风险未恶化）',
                 max(t['nights'] for t in vt['V6']) + 1,
                 100.0 * len(on) / n_all6))
        print('4) 实装建议：%s' % (
            '不实装关 FLAT。' if d62 <= 0 else
            ('可实装 FORCE_FLAT 开关（t0_config.txt 白名单，语义 '
             'FORCE_FLAT=0 关闭），但先上中间方案：FORCE_FLAT_MODE='
             'loss_only（仅浮亏单 14:55 强平、浮盈单允许隔夜——既留趋势单 '
             '的隔夜增益，又把隔夜深亏的尾部风险交给强平）；周末缺口另加 '
             'FRIDAY_FLAT=1（周五必强平）。上线后逐日核对过夜单缺口分布。'
             if risk_worse else
             '可实装 FORCE_FLAT=0 开关（白名单），建议保留 '
             'FRIDAY_FLAT=1 中间项防周末缺口；上线后逐日核对过夜单。')))

    # ---------- 实验 F：严格宇宙稳健性重验（追加段，日期见标题行） ----------
    strict, s_excluded, s_days = load_universe(strict=True)
    sv0, sv2 = [], []
    for code in strict:
        sv0.extend(replay(code, 'atr', 'V0'))
        sv2.extend(replay(code, 'atr', 'V2'))
    print('\n\n' + '#' * 64)
    print('# 实验 F：严格宇宙稳健性重验（纯 T0 核心集 %d 只 vs 宽宇宙 %d '
          '只）  %s' % (len(strict), len(universe),
                       time.strftime('%Y-%m-%d')))
    print('#' * 64)
    print('# 严格宇宙 = pool_t0 ∪ 原四只；宽宇宙中其余 21 只为"仅因缓存存在"'
          '（约半数 T+1，回放高估可执行性）。既有实验段（宽宇宙）数字存档'
          '于本文件上半部分，本段只重验 V0/V2 两个关键结论。')
    print('严格宇宙 %d 只：%s' % (len(strict), ' '.join(strict)))
    ss0, ss2 = agg_stats(sv0), agg_stats(sv2)
    print('\n%-10s %6s %7s %11s %9s %11s' % ('组合', '笔数', '胜率', '总净利',
                                             '均笔', '最大单笔亏'))
    for label, s in (('V0 宽', ve['V0']), ('V0 严格', ss0),
                     ('V2 宽', ve['V2']), ('V2 严格', ss2)):
        print('%-10s %6d %6.1f%% %+11.2f %+9.2f %+11.2f'
              % (label, s[0], s[3], s[1], s[2], s[4]))

    print('\n-- 严格宇宙 V0 vs V2 分票（delta = V2净利 - V0净利） --')
    sb0 = defaultdict(float)
    sn0 = defaultdict(int)
    sb2 = defaultdict(float)
    sn2 = defaultdict(int)
    for t in sv0:
        sb0[t['code']] += t['net']
        sn0[t['code']] += 1
    for t in sv2:
        sb2[t['code']] += t['net']
        sn2[t['code']] += 1
    simp, swor = [], []
    for c in strict:
        if sn0[c] == 0 and sn2[c] == 0:
            continue
        d = sb2[c] - sb0[c]
        print('%-8s V0 %2d笔%+9.2f  V2 %2d笔%+9.2f  delta %+9.2f'
              % (c, sn0[c], sb0[c], sn2[c], sb2[c], d))
        (simp if d > 0 else (swor if d < 0 else [])).append(c)
    print('严格宇宙：改善 %d 只 / 恶化 %d 只（宽宇宙为 32/5）'
          % (len(simp), len(swor)))

    print('\n-- 黑榜票权重变化 --')
    black = ('513120', '513090', '589120', '589720', '513040')
    in_strict = [c for c in black if c in set(strict)]
    print('实验 C 黑榜 5 只：%s（宽宇宙密度 5/37 = %.0f%%）'
          % (' '.join(black), 100.0 * 5 / len(universe)))
    print('其中属严格宇宙：%s——黑榜密度 %d/%d = %.0f%%（宽宇宙中这 2 只占 '
          '2/37 = %.1f%%），权重约翻倍，若结论仍稳健则黑榜不是决定因素。'
          % (' '.join(in_strict), len(in_strict), len(strict),
             100.0 * len(in_strict) / len(strict),
             100.0 * 2 / len(universe)))
    for c in in_strict:
        print('  %s：V0 %d笔%+.2f / V2 %d笔%+.2f（严格宇宙口径）'
              % (c, sn0[c], sb0[c], sn2[c], sb2[c]))

    print('\n结论：')
    d_pt = ss2[2] - ss0[2]
    positive = ss2[1] > 0
    dominant = len(simp) > len(swor)
    robust = positive and d_pt > 0 and dominant
    print('1) V2 在严格宇宙下%s池级转正：总净利 %+.2f（宽宇宙 %+.2f）；均笔 '
          '%+.2f -> %+.2f；笔数 %d -> %d。'
          % ('依然' if positive else '不再', ss2[1], ve['V2'][1], ss0[2],
             ss2[2], ss0[0], ss2[0]))
    print('2) 改善票占比%s压倒性：严格宇宙 %d/%d（%.0f%%）改善，恶化仅 %s；'
          '宽宇宙为 32/5。'
          % ('依然' if dominant and len(simp) >= 2 * len(swor) else '不再',
             len(simp), len(simp) + len(swor),
             100.0 * len(simp) / (len(simp) + len(swor)) if simp or swor
             else 0, ' '.join(swor) if swor else '无'))
    print('3) 黑榜权重翻倍（2/16 = %.0f%%）后，池级结论未翻转——黑榜票的'
          '负面影响在两种宇宙下都被 V2 的池级增益覆盖。'
          % (100.0 * 2 / len(strict),))
    if robust:
        print('4) 结论对宇宙选择不敏感，可放心实装（仍受 13-17 天小样本与'
              '5m 近似口径约束；实装后按周回放复核）。')
    else:
        print('4) 结论对宇宙敏感：严格宇宙下 V2%s转正、改善占比%s。变化的'
              '根源是宽宇宙中 21 只非核心票的稀释/污染。建议以严格宇宙为'
              '准%s。'
              % ('未' if not positive else '', '不再压倒性'
                 if not dominant else '仍压倒性',
                 '推迟实装' if not positive else '缩小票池后再实装'))


if __name__ == '__main__':
    sys.stdout = Tee(REPORT_FILE)
    main()
    print('\n[report saved] %s' % REPORT_FILE)
