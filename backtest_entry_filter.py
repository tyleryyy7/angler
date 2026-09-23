# -*- coding: utf-8 -*-
"""backtest_entry_filter.py — 验证 60m 上穿进场时 fisher60 高低对未来收益的影响。

问题：executor_v2 进场只看「上穿」不看位置，高位（fisher 钝化区）贴线上穿
是典型低质信号（2026-09-22 烽火通信 09:33 贴线 0.003 上穿案例）。

数据：cache/daily_qfq/tdxq/*_1h.csv（tdxq 前复权 60m，全部完结 bar）
信号：fisher 上穿拐点（fish[i] > fish[i-1] 且 fish[i-1] <= fish[i-2]，
      trigger = fish[i-1]），进场 = 信号 bar close
口径：fisher_series 与 executor_v2/executor_t0 逐字一致（hl2 输入、窗口 9、
      v 超 ±0.99 截到 ±0.999）
结局：持有 10 / 20 根 bar 后的 close；另计 10 根内最差 close（抗回撤参考）

用法：.venv\\Scripts\\python.exe backtest_entry_filter.py
"""
import csv
import glob
import os
import sys

LENGTH = 9
# argv: [period] [hold1_bars] [hold2_bars]   (defaults = 60m study)
PERIOD = sys.argv[1] if len(sys.argv) > 1 else '1h'
HOLD1 = int(sys.argv[2]) if len(sys.argv) > 2 else 10
HOLD2 = int(sys.argv[3]) if len(sys.argv) > 3 else 20
DATA_GLOB = 'cache/daily_qfq/tdxq/*_%s.csv' % PERIOD
MIN_BARS = 60          # 少于这个 bar 数的文件跳过


def fisher_series(high, low, length=LENGTH):
    n = len(high)
    hl2 = [(high[i] + low[i]) / 2.0 for i in range(n)]
    value = 0.0
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
        out.append(0.5 * math.log((1.0 + v) / (1.0 - v)) + 0.5 * (out[-1] if out else 0.0))
    return out


import math  # noqa: E402  (kept after def to mirror executor layout)


def macd_hist(close, fast=12, slow=26, signal=9):
    def ema(vals, n):
        out = []
        k = 2.0 / (n + 1)
        e = vals[0]
        for v in vals:
            e = v * k + e * (1 - k)
            out.append(e)
        return out
    ef = ema(close, fast)
    es = ema(close, slow)
    dif = [ef[i] - es[i] for i in range(len(close))]
    dea = ema(dif, signal)
    return [(dif[i] - dea[i]) * 2.0 for i in range(len(close))]


def bucket(f):
    if f <= -1.0:
        return '<=-1.0'
    if f <= -0.5:
        return '-1.0~-0.5'
    if f <= 0.0:
        return '-0.5~0.0'
    if f <= 0.5:
        return '0.0~0.5'
    if f <= 1.0:
        return '0.5~1.0'
    if f <= 1.5:
        return '1.0~1.5'
    return '>1.5'


ORDER = ['<=-1.0', '-1.0~-0.5', '-0.5~0.0', '0.0~0.5',
         '0.5~1.0', '1.0~1.5', '>1.5']


# 小周期闸门（R1）：30m/15m/5m 任一在下行段 -> 假性失效（不进）。回测用
# 信号时刻之前的最后一根完结小周期 bar 判定（fish < 前一根 = 下行）。
# 15m 研究（T0 语境）用 5m 单周期近似 t0 执行器的 5m/1m 闸门（1m 缓存基本不存在）。
GATE_TFS = ('30m', '15m', '5m') if PERIOD == '1h' else ('5m',)
_tf_cache = {}


def load_tf(code6, period):
    key = (code6, period)
    if key in _tf_cache:
        return _tf_cache[key]
    path = os.path.join(os.path.dirname(DATA_GLOB),
                        '%s_%s.csv' % (code6, period))
    rows = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for r in csv.DictReader(f):
                try:
                    rows.append((r['time'], float(r['high']), float(r['low'])))
                except (TypeError, ValueError):
                    continue
    out = None
    if len(rows) >= 3:
        fs = fisher_series([r[1] for r in rows], [r[2] for r in rows])
        out = ([r[0] for r in rows], fs)
    _tf_cache[key] = out
    return out


def gate_state(code6, upto_time):
    # None = 任一周期数据不足（unknown）；True = 全上行（放行）；False = 有下行
    for period in GATE_TFS:
        tf = load_tf(code6, period)
        if tf is None:
            return None
        times, fs = tf
        idx = None
        for i in range(len(times) - 1, -1, -1):
            if times[i] <= upto_time:
                idx = i
                break
        if idx is None or idx < 1:
            return None
        if fs[idx] < fs[idx - 1]:
            return False
    return True


def main():
    import csv
    sigs = []
    n_files = 0
    for path in glob.glob(DATA_GLOB):
        code6 = os.path.basename(path).split('_')[0]
        rows = []
        with open(path, encoding='utf-8') as f:
            rd = csv.DictReader(f)
            for r in rd:
                try:
                    rows.append((float(r['high']), float(r['low']), float(r['close']),
                                 r['time'] or '', (r['time'] or '')[11:16]))
                except (TypeError, ValueError):
                    continue
        if len(rows) < MIN_BARS:
            continue
        n_files += 1
        high = [r[0] for r in rows]
        low = [r[1] for r in rows]
        close = [r[2] for r in rows]
        times = [r[3] for r in rows]
        hhmm = [r[4] for r in rows]
        fs = fisher_series(high, low)
        mh = macd_hist(close)
        for i in range(2, len(fs)):
            if fs[i] > fs[i - 1] and fs[i - 1] <= fs[i - 2]:
                e = close[i]
                if i + HOLD1 < len(close):
                    r1 = close[i + HOLD1] / e - 1.0
                    worst = min(close[i + 1: i + HOLD1 + 1]) / e - 1.0
                else:
                    r1 = None
                    worst = None
                r2 = close[i + HOLD2] / e - 1.0 if i + HOLD2 < len(close) else None
                sigs.append((code6, fs[i], fs[i] - fs[i - 1], hhmm[i],
                             times[i], r1, worst, r2,
                             mh[i], mh[i - 1], mh[i - 2]))
    print('files: %d, signals: %d' % (n_files, len(sigs)))

    # attach R1 gate state (codes without all small-TF caches -> unknown)
    for k, s in enumerate(sigs):
        sigs[k] = s + (gate_state(s[0], s[4]),)

    def stats(cols):
        if not cols:
            return None
        r1 = [x for x in cols[0] if x is not None]
        if not r1:
            return None
        n = len(r1)
        w = [x for x in cols[1] if x is not None] if len(cols) > 1 else []
        r2 = [x for x in cols[2] if x is not None] if len(cols) > 2 else []
        hssr = 100.0 * sum(1 for x in r1 if x > 0) / n
        return (n, 100.0 * sum(r1) / n,
                hssr, 100.0 * sum(w) / len(w) if w else 0,
                100.0 * sum(r2) / len(r2) if r2 else 0)

    print('\n== R1 gate (30m/15m/5m) ==')
    print('%-9s %5s %9s %7s %9s' % ('gate', 'n', 'ret10%', 'HSSR10', 'ret20%'))
    for label, pred in (('pass', lambda g: g is True),
                        ('fail', lambda g: g is False),
                        ('unknown', lambda g: g is None)):
        sel = [s for s in sigs if pred(s[11])]
        cols = list(zip(*[(s[5], s[6], s[7]) for s in sel])) if sel else []
        st = stats(cols)
        if st:
            print('%-9s %5d %+9.2f %6.0f%% %+9.2f' % (label, st[0], st[1], st[2], st[4]))

    base = [s for s in sigs if s[11] is True]

    bk = {}
    for c6, f, delta, hhmm, fulltime, r1, worst, r2, h0, h1, h2, gate in base:
        bk.setdefault(bucket(f), []).append((r1, worst, r2))
    hdr = ('%-11s %5s %9s %7s %9s %9s' % ('fish60', 'n', 'ret10%', 'HSSR10', 'worst10%', 'ret20%'))
    print(hdr)
    for b in ORDER:
        if b not in bk:
            continue
        cols = list(zip(*bk[b]))
        st = stats(cols)
        print('%-11s %5d %+9.2f %6.0f%% %+9.2f %+9.2f' % (b, *st))

    print('\nthreshold cut: fish60 <= T')
    print('%-6s %5s %9s %7s %9s' % ('T', 'n', 'ret10%', 'HSSR10', 'ret20%'))
    for T in (0.5, 1.0, 1.5, 99.0):
        sel = [s for s in base if s[1] <= T]
        cols = list(zip(*[(s[5], s[6], s[7]) for s in sel])) if sel else []
        st = stats(cols)
        if st:
            print('%-6s %5d %+9.2f %6.0f%% %+9.2f' % (T, st[0], st[1], st[2], st[4]))

    def dbucket(d):
        if d <= 0.05:
            return '贴线<=0.05'
        if d <= 0.2:
            return '0.05~0.2'
        if d <= 0.5:
            return '0.2~0.5'
        return '>0.5'
    print('\ncross magnitude bucket (fish60 - trigger)')
    print('%-11s %5s %9s %7s %9s' % ('delta', 'n', 'ret10%', 'HSSR10', 'ret20%'))
    dk = {}
    for c6, f, delta, hhmm, fulltime, r1, worst, r2, h0, h1, h2, gate in base:
        dk.setdefault(dbucket(delta), []).append((r1, worst, r2))
    for b in ('贴线<=0.05', '0.05~0.2', '0.2~0.5', '>0.5'):
        if b not in dk:
            continue
        cols = list(zip(*dk[b]))
        st = stats(cols)
        print('%-11s %5d %+9.2f %6.0f%% %+9.2f' % (b, st[0], st[1], st[2], st[4]))

    def tbucket(t):
        return {'10:30': '10:30(首根)', '11:30': '11:30',
                '14:00': '14:00', '15:00': '15:00'}.get(t, 'other')
    print('\nsignal bar time bucket')
    print('%-13s %5s %9s %7s %9s' % ('bar', 'n', 'ret10%', 'HSSR10', 'ret20%'))
    tk = {}
    for c6, f, delta, hhmm, fulltime, r1, worst, r2, h0, h1, h2, gate in base:
        tk.setdefault(tbucket(hhmm), []).append((r1, worst, r2))
    for b in ('10:30(首根)', '11:30', '14:00', '15:00'):
        if b not in tk:
            continue
        cols = list(zip(*tk[b]))
        st = stats(cols)
        print('%-13s %5d %+9.2f %6.0f%% %+9.2f' % (b, st[0], st[1], st[2], st[4]))

    def hbucket(h0, h1):
        if h0 > 0 and h1 > 0:
            return 'red_shrink(红柱缩短)' if h0 < h1 else 'red_grow(红柱伸长)'
        if h0 <= 0 and h1 <= 0:
            return 'green_grow(绿柱伸长)' if h0 < h1 else 'green_shrink(绿柱缩短)'
        if h0 > 0:
            return 'red_from_neg(绿翻红)'
        return 'green_from_pos(红翻绿)'
    print('\n60m MACD histogram state at signal (gate-pass only)')
    print('%-24s %5s %9s %7s %9s' % ('hist', 'n', 'ret10%', 'HSSR10', 'ret20%'))
    hk = {}
    for c6, f, delta, hhmm, fulltime, r1, worst, r2, h0, h1, h2, gate in base:
        hk.setdefault(hbucket(h0, h1), []).append((r1, worst, r2))
    for b in sorted(hk):
        cols = list(zip(*hk[b]))
        st = stats(cols)
        if st:
            print('%-24s %5d %+9.2f %6.0f%% %+9.2f' % (b, st[0], st[1], st[2], st[4]))
    print('\nred-bar shrinking depth (h0>0 and falling)')
    print('%-22s %5s %9s %7s %9s' % ('falls', 'n', 'ret10%', 'HSSR10', 'ret20%'))
    rk = {}
    for c6, f, delta, hhmm, fulltime, r1, worst, r2, h0, h1, h2, gate in base:
        if h0 > 0 and h0 < h1:
            tag = 'consec>=2(连续缩短)' if h1 < h2 else 'first_fall(首根缩短)'
            rk.setdefault(tag, []).append((r1, worst, r2))
    for b in ('first_fall(首根缩短)', 'consec>=2(连续缩短)'):
        if b not in rk:
            continue
        cols = list(zip(*rk[b]))
        st = stats(cols)
        if st:
            print('%-22s %5d %+9.2f %6.0f%% %+9.2f' % (b, st[0], st[1], st[2], st[4]))

    print('\n60m MACD histogram state at signal (ALL signals, no gate)')
    print('%-24s %5s %9s %7s %9s' % ('hist', 'n', 'ret10%', 'HSSR10', 'ret20%'))
    ak = {}
    for s in sigs:
        c6, f, delta, hhmm, fulltime, r1, worst, r2, h0, h1, h2, gate = s
        ak.setdefault(hbucket(h0, h1), []).append((r1, worst, r2))
    for b in sorted(ak):
        cols = list(zip(*ak[b]))
        st = stats(cols)
        if st:
            print('%-24s %5d %+9.2f %6.0f%% %+9.2f' % (b, st[0], st[1], st[2], st[4]))

    print('\ninteraction: MIN_CROSS=0.15 x red_shrink (gate-pass only)')
    print('%-30s %5s %9s %7s %9s' % ('group', 'n', 'ret10%', 'HSSR10', 'ret20%'))
    xk = {}
    for c6, f, delta, hhmm, fulltime, r1, worst, r2, h0, h1, h2, gate in base:
        if delta < 0.15:
            continue
        tag = 'cross_ok + red_shrink' if (h0 > 0 and h0 < h1) else 'cross_ok + not_red_shrink'
        xk.setdefault(tag, []).append((r1, worst, r2))
    for b in sorted(xk):
        cols = list(zip(*xk[b]))
        st = stats(cols)
        if st:
            print('%-30s %5d %+9.2f %6.0f%% %+9.2f' % (b, st[0], st[1], st[2], st[4]))


if __name__ == '__main__':
    main()
