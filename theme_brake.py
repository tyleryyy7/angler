# -*- coding: utf-8 -*-
"""theme_brake.py — 动态刹车（每月运行）：watchlist 逐票 trailing 3 个月回放，
净额为负且笔数足够 → 写入 D:\qmt\v2_blocklist.txt，executor_v2 每 bar 热读，
名单内票只管理卖出不进新仓。结果推送企业微信。

口径：回放整段缓存但只统计进场日在最近 63 个交易日内的交易（LIVE 规则：
E_NOESI + 进场门槛0 + cap2.5 + 卖门槛0.15——与线上现行一致；线上改规则后
这里要同步改 PARAMS）。票级数据只覆盖窗口内新开仓，窗口前开仓跨期持有的
交易不计（刹车看的是"最近新信号质量"，不是存量持仓损益）。

用法：.venv\\Scripts\\python.exe theme_brake.py [--no-push] [--days 63]
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

TDXQ_FETCH = r"D:\tdx\PYPlugins\user\tdxq_fetch.py"
DATA_DIR = BASE / 'cache' / 'daily_qfq' / 'tdxq'
WATCHLIST = r'D:\qmt\watchlist.txt'
BLOCKLIST = r'D:\qmt\v2_blocklist.txt'

PARAMS = dict(variant='E_NOESI', min_cross=0.0, max_f60=1.0,
              min_cross_sell=0.50)          # REV 2026-10-05b 线上规则
                                            # （max_f60=1.0 近似 SZ4 的 f60>=1 跳过；
                                            # 分档倍率/加仓在刹车评估里从简）
MIN_TRADES = 4                              # 窗口内笔数下限（少于则样本不足不判）
DEFAULT_DAYS = 126                          # trailing 窗口（交易日）。新规则单笔
                                            # 数减半，63 日窗口会让 11/12 票样本不足
                                            # （2026-10-05 实测），改 126 日
FETCH_COUNTS = {'5m': 7000, '15m': 2400, '30m': 1300, '1h': 900, '1d': 200}


def load_watchlist():
    codes = []
    with open(WATCHLIST, encoding='utf-8') as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith('#'):
                continue
            codes.append(ln.split(',')[0].strip().split('.')[0])
    return codes


def refresh(codes):
    """补取各周期缓存（merge 写，不冲深度）。"""
    cf = DATA_DIR / '_brake_codes.txt'
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text('\n'.join(codes), encoding='utf-8')
    for period, cnt in FETCH_COUNTS.items():
        r = subprocess.run(
            [sys.executable, TDXQ_FETCH, '--codes-file', str(cf),
             '--period', period, '--count', str(cnt), '--batch', '25',
             '--out-dir', str(DATA_DIR)],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=1800)
        tail = (r.stdout or '').strip().splitlines()
        print('[fetch %s] %s' % (period, tail[-1] if tail else '(no output)'))


def judge(codes, days):
    import backtest_t0_replay as t0
    import backtest_v2_replay as br
    rows = []
    for code in codes:
        if not all((DATA_DIR / ('%s_%s.csv' % (code, p))).exists()
                   for p in ('5m', '15m', '30m', '1h')):
            rows.append((code, 0, 0.0, 0.0, '数据缺失'))
            continue
        b5 = t0.load_bars(code, '5m')
        cal = sorted({b['time'][:10] for b in b5})
        cutoff = cal[-days] if len(cal) > days else cal[0]
        ts = [t for t in br.replay(code, **PARAMS) if t['eday'] >= cutoff]
        n = len(ts)
        net = sum(t['net'] for t in ts)
        win = sum(1 for t in ts if t['net'] > 0) * 100.0 / n if n else 0.0
        verdict = ('刹车' if n >= MIN_TRADES and net < 0 else
                   '样本不足' if n < MIN_TRADES else '正常')
        rows.append((code, n, net, win, verdict))
    return rows, cutoff


def drift_scan(days):
    """参数漂移月报：池并集（右/左/深水）trailing 窗口重扫卖门槛档位，
    报告当前线上档位是否仍然最优。纯分析，不改任何行为。"""
    import backtest_t0_replay as t0
    import backtest_v2_replay as br
    codes = []
    for fn in ('pool_right.csv', 'pool_left.csv', 'pool_deep.csv'):
        p = BASE / fn
        if not p.exists():
            continue
        import csv as _csv
        with open(p, encoding='utf-8-sig') as f:
            for row in _csv.DictReader(f):
                c = (row.get('code') or '').strip()
                if c and c not in codes:
                    codes.append(c)
    codes = [c for c in codes
             if all((DATA_DIR / ('%s_%s.csv' % (c, p))).exists()
                    for p in ('5m', '15m', '30m', '1h'))]
    if not codes:
        return None
    refresh(codes)
    sweep = [0.25, 0.35, 0.50, 0.75]
    totals = {x: [0, 0.0] for x in sweep}
    per_code = {}                   # code -> (n, net) under 线上 PARAMS
    for code in codes:
        b5 = t0.load_bars(code, '5m')
        cal = sorted({b['time'][:10] for b in b5})
        cutoff = cal[-days] if len(cal) > days else cal[0]
        ts0 = [t for t in br.replay(code, **PARAMS) if t['eday'] >= cutoff]
        per_code[code] = (len(ts0), sum(t['net'] for t in ts0))
        for smc in sweep:
            kw = dict(PARAMS)
            kw['min_cross_sell'] = smc
            ts = [t for t in br.replay(code, **kw) if t['eday'] >= cutoff]
            totals[smc][0] += len(ts)
            totals[smc][1] += sum(t['net'] for t in ts)
    cur = PARAMS['min_cross_sell']
    best = max(sweep, key=lambda x: totals[x][1])
    return sweep, totals, cur, best, len(codes), cutoff, per_code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-push', action='store_true')
    ap.add_argument('--days', type=int, default=DEFAULT_DAYS)
    args = ap.parse_args()

    codes = load_watchlist()
    codes_wl = list(codes)
    print('watchlist %d 只，trailing %d 个交易日' % (len(codes), args.days))
    refresh(codes)
    rows, cutoff = judge(codes, args.days)

    blocked = [r for r in rows if r[4] == '刹车']
    lines = ['# dynamic brake list (theme_brake.py %s, window from %s, LIVE rules)'
             % (datetime.now().strftime('%Y-%m-%d'), cutoff)]
    for code, n, net, win, verdict in rows:
        if verdict == '刹车':
            lines.append('%s  # n=%d net=%+.0f win=%.0f%%' % (code, n, net, win))
    with open(BLOCKLIST, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    msg = ['**动态刹车月报**（窗口自 %s，%d 个交易日，现行规则回放）'
           % (cutoff, args.days), '']
    for code, n, net, win, verdict in rows:
        mark = {'刹车': '🔴', '正常': '🟢', '样本不足': '⚪',
                '数据缺失': '⚫'}[verdict]
        msg.append('%s %s：%d 笔 %+.0f（胜率 %.0f%%）'
                   % (mark, code, n, net, win))
    msg.append('')
    msg.append('刹车 %d 只：%s' % (len(blocked),
                                  ' '.join(r[0] for r in blocked) or '无'))
    # 组合层：watchlist 整体 trailing 净额（报告级，人工决定是否全局降档）
    wl_net = sum(r[2] for r in rows)
    wl_n = sum(r[1] for r in rows)
    msg.append('')
    msg.append('**组合层**：watchlist 合计 %d 笔 %+.0f%s'
               % (wl_n, wl_net,
                  ' ⚠️ 组合 trailing 转负，可考虑 v2_config.txt 加 GLOBAL_SCALE=0.5 全局降档'
                  if wl_net < 0 and wl_n >= 10 else ''))
    # 参数漂移：池并集 trailing 重扫卖门槛（纯报告）
    try:
        d = drift_scan(args.days)
        if d:
            sweep, totals, cur, best, ncodes, dcut, per_code = d
            msg.append('')
            msg.append('**参数漂移**（池并集 %d 只，卖门槛 trailing 净额）' % ncodes)
            for x in sweep:
                n, net = totals[x]
                mark = ' ←线上' if abs(x - cur) < 1e-9 else (
                    ' ←当期最优' if x == best and x != cur else '')
                msg.append('%.2f：%d 笔 %+.0f%s' % (x, n, net, mark))
            if abs(best - cur) > 1e-9:
                msg.append('⚠️ 线上档 %.2f 非当期最优（最优 %.2f），关注但勿自动改'
                           % (cur, best))
            # 名单建议：池中 trailing 最强且不在 watchlist 的 TOP5 +
            # watchlist 中最弱的（人工确认，不自动换）
            wl = set(codes_wl)
            cands = sorted(((n, net, c) for c, (n, net) in per_code.items()
                            if c not in wl and n >= MIN_TRADES and net > 0),
                           key=lambda x: -x[1])[:5]
            if cands:
                msg.append('')
                msg.append('**名单建议**（人工确认）')
                msg.append('候选换入：' + ' '.join('%s(%d笔%+0.0f)' % (c, n, net)
                                                 for n, net, c in cands))
    except Exception as e:
        msg.append('')
        msg.append('（参数漂移扫描失败：%s）' % e)
    text = '\n'.join(msg)
    print(text)
    if not args.no_push:
        from fisher_scanner import push_text
        push_text(text)


if __name__ == '__main__':
    main()
