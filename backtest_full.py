# -*- coding: utf-8 -*-
"""backtest_full.py — 完整策略单票回测（事件驱动，无前视偏差）

策略分层复刻（以 fisher_scanner.py 线上定义为准）：
  Setup（建池，日线）：深水池 = 日线 Fisher(9) < -2。当日是否可买用**前一交易日收盘**
    的日线 fisher 判定（池子每晚 00:00 用已完结日 K 重建，当天盘中用的是昨收盘口径）。
  Trigger（60m）：完结 60m bar 费雪上穿（fish 上穿 trigger = 由跌转升拐点）。
  进场闸门（可选）：0 < fisher60 < 2.5（ESI 进场条件）。
  离场（先到先出；A 股 T+1：进场当日触发的离场递延到次日首根完结 60m bar 成交，
    reason 带 (T+1) 标记）：
    a) ESI 失效：进场后 6 根完结 30m bar 窗口内，完结 30m bar 费雪下穿
       且当时 60m fisher > 0 且 < 前一根 → 平仓；
    b) 6 根窗口存活 → 之后持有直到完结 60m bar 下穿。

数据：日线 sina(akshare qfq)，60m/30m sina jsonp 直连（前复权因子当日缓存）。

用法：.venv\\Scripts\\python.exe backtest_full.py 600580 [--gate]
"""
import sys
import time

import numpy as np
import pandas as pd

import fisher_scanner as fs

CODE = sys.argv[1] if len(sys.argv) > 1 else "600580"
USE_GATE = "--gate" in sys.argv
ESI_WINDOW = 6          # 6 根完结 30m bar（3 交易小时）
FISHER_LEN = 9

BAR60_ENDS = ["10:30", "11:30", "14:00", "15:00"]


def fisher(high, low):
    return fs.fisher_transform(np.asarray(high, float), np.asarray(low, float), FISHER_LEN)


def main():
    print("标的 %s | 闸门 %s" % (CODE, "开(0,2.5)" if USE_GATE else "关"))

    # ---- 日线：建池条件 ----
    import akshare as ak
    daily = ak.stock_zh_a_daily(symbol=fs._sina_symbol(CODE), adjust="qfq")
    daily["date"] = pd.to_datetime(daily["date"].astype(str))
    d_fish, _ = fisher(daily["high"], daily["low"])
    daily["fish"] = d_fish
    # 当日池条件 = 前一交易日收盘 fisher < -2（深水池口径，无前视）
    daily["in_pool"] = daily["fish"].shift(1) < -2
    pool_by_date = dict(zip(daily["date"].dt.date, daily["in_pool"]))

    # ---- 60m：触发 ----
    df60 = fs.fetch_60m(CODE, "sina")
    df60["时间"] = pd.to_datetime(df60["时间"])
    f60, t60 = fisher(df60["最高"], df60["最低"])

    # ---- 30m：ESI 失效 ----
    df30 = fs.fetch_30m(CODE, "sina")
    df30["时间"] = pd.to_datetime(df30["时间"])
    f30, t30 = fisher(df30["最高"], df30["最低"])
    t30_times = df30["时间"].values
    h30 = df30["最高"].astype(float).values
    l30 = df30["最低"].astype(float).values

    trades = []
    pos = 0
    entry_i = entry_time = entry_fish = entry_date = None
    deferred = None          # T+1: 当日触发的离场原因，次日首根 60m bar 执行

    for j in range(2, len(df60)):
        bar_time = df60["时间"].iloc[j]
        in_pool = pool_by_date.get(bar_time.date(), False)

        if pos == 0:
            if in_pool and fs.just_crossed_up(f60, t60, j):
                if USE_GATE and not (0 < f60[j] < 2.5):
                    continue
                pos = 1
                entry_i, entry_time, entry_fish = j, bar_time, f60[j]
                entry_date = bar_time.date()
                trades.append({"side": "BUY", "time": bar_time,
                               "price": float(df60["收盘"].iloc[j]),
                               "fish": round(float(f60[j]), 3), "reason": "上穿"})
        else:
            # T+1 递延执行：次日第一根完结 60m bar 收盘价成交
            #（跳空未单独建模，用首根 bar 收盘近似）
            if deferred is not None and bar_time.date() != entry_date:
                trades.append({"side": "SELL", "time": bar_time,
                               "price": float(df60["收盘"].iloc[j]),
                               "fish": round(float(f60[j]), 3),
                               "reason": deferred + "(T+1)"})
                pos = 0
                deferred = None
                continue
            same_day = bar_time.date() == entry_date
            # b) 60m 下穿离场
            if fs.just_crossed_down(f60, t60, j):
                if same_day:
                    if deferred is None:
                        deferred = "60m下穿"   # 当日卖不掉，递延到次日
                    continue
                trades.append({"side": "SELL", "time": bar_time,
                               "price": float(df60["收盘"].iloc[j]),
                               "fish": round(float(f60[j]), 3), "reason": "60m下穿"})
                pos = 0
                continue
            # a) ESI 失效：进场后 6 根完结 30m 窗口内逐根判定
            idx30 = np.where((t30_times > np.datetime64(entry_time))
                             & (t30_times <= np.datetime64(bar_time)))[0]
            idx30 = idx30[:ESI_WINDOW]
            for k in idx30:
                if not fs.just_crossed_down(f30, t30, k):
                    continue
                # 当时 60m fisher：取截至该 30m 时刻的已完结 60m bar 重算
                #（近似：线上规则允许未完结 60m bar 取当前值，回测里用完结值替代，
                # 会使 ESI 触发略滞后/偏保守）
                mask60 = df60["时间"].values <= t30_times[k]
                hh = list(df60["最高"].astype(float).values[mask60])
                ll = list(df60["最低"].astype(float).values[mask60])
                f_now, _ = fisher(hh, ll)
                if len(f_now) >= 2 and 0 < f_now[-1] < f_now[-2]:
                    if pd.Timestamp(t30_times[k]).date() == entry_date:
                        if deferred is None:
                            deferred = "ESI失效"   # 当日卖不掉，递延到次日
                        break
                    trades.append({"side": "SELL", "time": pd.Timestamp(t30_times[k]),
                                   "price": float(df30["收盘"].iloc[k]),
                                   "fish": round(float(f60[j]), 3), "reason": "ESI失效"})
                    pos = 0
                    break
            if pos == 0:
                continue

    # ---- 报告 ----
    bt = pd.DataFrame(trades)
    print("\n=== 交易明细 ===")
    print(bt.to_string(index=False) if len(bt) else "（无交易）")
    pnls = []
    for k in range(0, len(trades) - 1, 2):
        b, s = trades[k], trades[k + 1]
        if b["side"] == "BUY" and s["side"] == "SELL":
            pnls.append((s["price"] - b["price"]) / b["price"] * 100)
    first, last = df60["收盘"].iloc[0], df60["收盘"].iloc[-1]
    print("\n=== 汇总 ===")
    print("区间: %s ~ %s" % (df60["时间"].iloc[0], df60["时间"].iloc[-1]))
    print("闭合交易 %d 笔，胜率 %.0f%%，累计 %.2f%%（未含佣金印花）"
          % (len(pnls), 100 * sum(1 for x in pnls if x > 0) / max(len(pnls), 1), sum(pnls)))
    print("逐笔: %s" % ["%.2f" % x for x in pnls])
    print("同期买入持有: %.2f%%（%.2f -> %.2f）" % ((last - first) / first * 100, first, last))
    print("期末持仓: %s" % ("有" if pos else "无"))
    esi = [t for t in trades if t.get("reason") == "ESI失效"]
    print("ESI 失效离场次数: %d" % len(esi))
    t1 = [t for t in trades if "(T+1)" in str(t.get("reason"))]
    print("T+1 递延离场笔数: %d" % len(t1))


if __name__ == "__main__":
    main()
