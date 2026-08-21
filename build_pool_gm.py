# -*- coding: utf-8 -*-
"""
右侧 / 左侧 / 深水 三股票池生成器（掘金 gm 数据源，盘后/夜间运行）

在 .venv-gm 环境运行（gm SDK 依赖老版本 pandas，与 akshare 环境互斥）：
    .venv-gm\\Scripts\\python.exe build_pool_gm.py             # 全量
    .venv-gm\\Scripts\\python.exe build_pool_gm.py --limit 150 # 调试
    .venv-gm\\Scripts\\python.exe build_pool_gm.py --resume    # 断点续跑

前提：掘金终端（MyQuant）正在运行且已登录；token 写在 gm_token.key（一行）。
token 获取：掘金终端 -> 量化交易 -> token 管理。

过滤与分类逻辑与 build_pool_dual.py（新浪版）一致：
    公共：沪深主板（SHSE.60 / SZSE.00）、剔 ST/停牌（get_symbols 自带）、
          上市满 1 年（listed_date 精确判定）、最新收盘 >= MIN_PRICE、
          20 日均成交额 >= AVG_AMOUNT_MIN、20 日均振幅 >= AVG_AMPLITUDE_MIN
    右侧：MACD DIF 连升两日 且 DIF > DEA 且 DIF > 0（零上趋势已成）
    左侧：MACD DIF 连升两日 且 DIF < DEA 且 DIF < 0（零下拐点埋伏）
    深水：无 MACD 闸门，日线 Fisher(9) < FISHER_DEEP_MAX
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------- 配置区（按需修改） -----------------------
MIN_PRICE = 2.0               # 剔股价低于此值（元）
AVG_AMOUNT_MIN = 2e8          # 20 日均成交额下限（元）
AVG_AMPLITUDE_MIN = 2.5       # 20 日均振幅下限（%）
NEED_DAYS = 20                # 均值窗口（交易日）
HIST_DAYS = 240               # 拉取自然日数（约 160 根日 bar，够 MACD 暖机 + 统计窗口）
LIST_MIN_DAYS = 365           # 上市满 1 年
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
FISHER_LEN = 9
FISHER_DEEP_MAX = -2.0        # 日线 Fisher 低于此值进深水池
RETRY = 3
OUT_RIGHT = "pool_right.csv"
OUT_LEFT = "pool_left.csv"
OUT_DEEP = "pool_deep.csv"
PROGRESS_FILE = Path(__file__).resolve().parent / "gm_progress.txt"
SAVE_EVERY = 200              # 每处理 N 只增量落盘一次
TOKEN_FILE = Path(__file__).resolve().parent / "gm_token.key"
# -----------------------------------------------------------------

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])


def fisher_transform(high, low, length=9):
    """Fisher Transform，与 fisher_scanner.py / Pine / 同花顺实现一致（自包含副本，
    避免导入 fisher_scanner 引入 akshare 环境依赖）。"""
    hl2 = (np.asarray(high, dtype=float) + np.asarray(low, dtype=float)) / 2.0
    n = len(hl2)
    value = np.zeros(n)
    fish = np.zeros(n)
    for i in range(n):
        s = max(0, i - length + 1)
        hh, ll = hl2[s:i + 1].max(), hl2[s:i + 1].min()
        div = (hh - ll) if hh != ll else 1.0
        prev_v = value[i - 1] if i > 0 else 0.0
        v = 0.66 * ((hl2[i] - ll) / div - 0.5) + 0.67 * prev_v
        v = 0.999 if v > 0.99 else (-0.999 if v < -0.99 else v)
        value[i] = v
        prev_f = fish[i - 1] if i > 0 else 0.0
        fish[i] = 0.5 * np.log((1.0 + v) / (1.0 - v)) + 0.5 * prev_f
    return fish


def macd(close):
    """标准 MACD：DIF = EMA12 - EMA26，DEA = EMA9(DIF)。"""
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return dif, dea


def gm_init():
    """初始化 gm：读 token、set_token。终端未运行/token 缺失时给出清晰报错。"""
    if not TOKEN_FILE.exists():
        sys.exit("缺少 gm_token.key：请在掘金终端生成 token 后写入该文件（一行）")
    from gm.api import set_token
    set_token(TOKEN_FILE.read_text(encoding="utf-8").strip())


def fetch_universe():
    """全 A 主板、非 ST、非停牌、上市满 1 年。返回 [{code, name, symbol}]。"""
    from gm.api import get_symbols, get_instruments
    stocks = get_symbols(1010, skip_suspended=True, skip_st=True,
                         exchanges=["SHSE", "SZSE"], df=True)
    stocks = stocks[stocks["symbol"].str.contains(r"^(SHSE\.60|SZSE\.00)")]
    logging.info("主板非ST非停牌 %d 只", len(stocks))

    cutoff = datetime.now() - timedelta(days=LIST_MIN_DAYS)
    out = []
    symbols = stocks["symbol"].tolist()
    for k in range(0, len(symbols), 200):          # get_instruments 分批
        batch = symbols[k:k + 200]
        try:
            info = get_instruments(symbols=batch, df=True)
        except Exception as e:
            logging.warning("get_instruments 批次失败: %s", e)
            continue
        listed = pd.to_datetime(info["listed_date"])
        if getattr(listed.dt, "tz", None) is not None:
            listed = listed.dt.tz_localize(None)   # gm 返回带时区，去掉再比较
        info = info[listed <= cutoff]
        for _, r in info.iterrows():
            code = r["symbol"].split(".")[1]
            name = r.get("sec_name", "")
            out.append({"code": code, "name": name, "symbol": r["symbol"]})
        time.sleep(0.1)
    logging.info("上市满 1 年后剩余 %d 只", len(out))
    return out


def fetch_daily(symbol):
    """拉近 HIST_DAYS 天前复权日线，带重试。失败返回 None。"""
    from gm.api import history, ADJUST_PREV
    end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start = (datetime.now() - timedelta(days=HIST_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    for k in range(RETRY):
        try:
            df = history(symbol=symbol, frequency="1d", start_time=start, end_time=end,
                         fields="open,high,low,close,volume,amount",
                         adjust=ADJUST_PREV, df=True)
            if df is not None and len(df) > 0:
                return df
        except Exception as e:
            logging.warning("%s 日线第 %d 次拉取失败: %s", symbol, k + 1, e)
            time.sleep(1.0 * (k + 1))
    return None


def classify(df):
    """返回 (avg_amount, avg_amplitude, dif, dea, side, fisher_daily)；基础过滤不过返回 None。"""
    if df is None or len(df) < NEED_DAYS + 35:    # MACD 暖机 + 统计窗口
        return None
    close = df["close"].astype(float).reset_index(drop=True)
    if close.iloc[-1] < MIN_PRICE:
        return None

    d = df.tail(NEED_DAYS + 1)                     # 多 1 根作昨收
    h = d["high"].astype(float).values
    l = d["low"].astype(float).values
    c = d["close"].astype(float).values
    amt = pd.to_numeric(d["amount"], errors="coerce").values
    avg_amp = ((h[1:] - l[1:]) / c[:-1] * 100).mean()
    avg_amount = amt[1:].mean()
    if not (avg_amount >= AVG_AMOUNT_MIN and avg_amp >= AVG_AMPLITUDE_MIN):
        return None

    fish = fisher_transform(df["high"].astype(float).values,
                            df["low"].astype(float).values, FISHER_LEN)
    fisher_daily = float(fish[-1])

    dif, dea = macd(close)
    d1, d2, d3 = dif.iloc[-1], dif.iloc[-2], dif.iloc[-3]
    dea1 = dea.iloc[-1]
    side = None
    if d1 > d2 > d3:
        if d1 > dea1 and d1 > 0:        # 右侧：零上，DIF 在 DEA 上方走强
            side = "right"
        elif d1 < dea1 and d1 < 0:      # 左侧：零下，DIF 拐头向 DEA 靠近
            side = "left"
    return avg_amount, avg_amp, float(d1), float(dea1), side, fisher_daily


def save_results(right, left, deep):
    pd.DataFrame(right).to_csv(OUT_RIGHT, index=False, encoding="utf-8-sig")
    pd.DataFrame(left).to_csv(OUT_LEFT, index=False, encoding="utf-8-sig")
    pd.DataFrame(deep).to_csv(OUT_DEEP, index=False, encoding="utf-8-sig")


def main():
    parser = argparse.ArgumentParser(description="右侧/左侧/深水三池生成器（掘金 gm 版）")
    parser.add_argument("--limit", type=int, help="只处理前 N 只（调试）")
    parser.add_argument("--resume", action="store_true",
                        help="断点续跑：跳过 gm_progress.txt 中已处理的票，并合并已有 CSV 结果")
    args = parser.parse_args()

    gm_init()
    universe = fetch_universe()
    if args.limit:
        universe = universe[:args.limit]
        logging.info("调试模式：只处理前 %d 只", len(universe))

    right, left, deep, done = [], [], [], set()
    if args.resume:
        if PROGRESS_FILE.exists():
            done = set(PROGRESS_FILE.read_text(encoding="utf-8").split())
        for path, lst in ((OUT_RIGHT, right), (OUT_LEFT, left), (OUT_DEEP, deep)):
            p = Path(path)
            if p.exists() and p.stat().st_size > 0:
                old = pd.read_csv(p, dtype={"code": str})
                lst.extend(old.to_dict("records"))
        logging.info("断点续跑：跳过已处理 %d 只，已有右侧 %d / 左侧 %d / 深水 %d",
                     len(done), len(right), len(left), len(deep))

    fails = 0
    t0 = time.time()
    n = len(universe)
    processed = 0
    with PROGRESS_FILE.open("a", encoding="utf-8") as prog:
        for i, item in enumerate(universe):
            code, name, symbol = item["code"], item["name"], item["symbol"]
            if code in done:
                continue
            df = fetch_daily(symbol)
            if df is None:
                fails += 1
            else:
                r = classify(df)
                if r:
                    rec = {
                        "code": code, "name": name,
                        "close": float(df["close"].iloc[-1]),
                        "avg_amount": round(r[0] / 1e8, 2),   # 亿元
                        "avg_amplitude": round(r[1], 2),       # %
                        "dif": round(r[2], 4), "dea": round(r[3], 4),
                        "fisher_daily": round(r[5], 3),
                    }
                    if r[4] == "right":
                        right.append(rec)
                    elif r[4] == "left":
                        left.append(rec)
                    if r[5] < FISHER_DEEP_MAX:
                        deep.append(rec)
            prog.write(code + "\n")
            prog.flush()
            processed += 1
            if processed % SAVE_EVERY == 0:
                logging.info("进度 %d/%d，右侧 %d，左侧 %d，深水 %d，失败 %d",
                             i + 1, n, len(right), len(left), len(deep), fails)
                save_results(right, left, deep)

    save_results(right, left, deep)
    logging.info("完成：%d 只耗时 %.1f 分钟，右侧 %d 只 -> %s，左侧 %d 只 -> %s，深水 %d 只 -> %s，失败 %d 只",
                 n, (time.time() - t0) / 60,
                 len(right), OUT_RIGHT, len(left), OUT_LEFT, len(deep), OUT_DEEP, fails)


if __name__ == "__main__":
    main()
