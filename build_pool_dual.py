# -*- coding: utf-8 -*-
"""
右侧 / 左侧 / 深水 三股票池生成器（沪深主板，新浪数据源，盘后运行）

公共过滤条件：
    沪深主板（60/00 开头；剔创业板 30、科创板 68、北交所）
    非 ST / *ST / 退市整理、非停牌（当日有成交）
    股价 >= MIN_PRICE 元
    20 日均成交额 >= AVG_AMOUNT_MIN（新浪前复权日线自带真实成交额）
    20 日均振幅 >= AVG_AMPLITUDE_MIN（振幅 = (最高-最低)/昨收 × 100%）
    上市满 1 年（日线 bar 数 >= LIST_MIN_BARS 近似判定）

MACD（12/26/9，前复权日线收盘，EMA 递推口径与通达信/同花顺一致）：
    要求 DIF 连续上升：DIF[-1] > DIF[-2] > DIF[-3]
    DIF > DEA 且 DIF > 0 → 右侧交易池 pool_right.csv（零上趋势已成）
    DIF < DEA 且 DIF < 0 → 左侧交易池 pool_left.csv（零下拐点埋伏）
    其余（零上回调、零下反弹、DIF==DEA）两边都不入

深水池（实验性，无 MACD 闸门）：
    日线 Fisher(FISHER_LEN) 最新值 < FISHER_DEEP_MAX（深度超卖）→ pool_deep.csv
    与右侧/左侧池独立，允许重叠。

用法：
    python build_pool_dual.py                 # 生成 pool_right.csv / pool_left.csv / pool_deep.csv
    python build_pool_dual.py --limit 100     # 调试：只处理粗筛后前 100 只

注意：请在收盘后运行（盘中运行时当日日 bar 未完结，会参与均值、MACD 和日线 Fisher 计算）。
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd

# ----------------------- 配置区（按需修改） -----------------------
MIN_PRICE = 2.0               # 剔股价低于此值（元）
AVG_AMOUNT_MIN = 2e8          # 20 日均成交额下限（元）
AVG_AMPLITUDE_MIN = 2.5       # 20 日均振幅下限（%）
NEED_DAYS = 20                # 均值窗口（交易日）
LIST_MIN_BARS = 245           # 上市满 1 年的近似 bar 数（一年约 242 个交易日）
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
FISHER_DEEP_MAX = -2.0        # 日线 Fisher 低于此值进深水池（深度超卖）
REQUEST_INTERVAL = 0.2        # 每股请求间隔（秒），防新浪限流
RETRY = 3
OUT_RIGHT = "pool_right.csv"
OUT_LEFT = "pool_left.csv"
OUT_DEEP = "pool_deep.csv"
PROGRESS_FILE = Path(__file__).resolve().parent / "dual_progress.txt"  # 断点：每行一个已处理 code
SAVE_EVERY = 200              # 每处理 N 只增量落盘一次（防中途卡死丢进度）
# -----------------------------------------------------------------

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])


def _patch_requests_timeout(default=15):
    """给 requests（含 akshare 内部调用）补默认超时，防止连接挂起卡死整个任务。"""
    import requests
    _orig = requests.sessions.Session.request
    def _req(self, method, url, **kw):
        kw.setdefault("timeout", default)
        return _orig(self, method, url, **kw)
    requests.sessions.Session.request = _req


_patch_requests_timeout()


def fetch_daily_qfq(code):
    """拉取单只股票全历史前复权日线，带重试。失败返回 None。"""
    import akshare as ak
    symbol = ("sh" if code.startswith("6") else "sz") + code
    for k in range(RETRY):
        try:
            df = ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")
            if df is not None and len(df) > 0:
                return df
        except Exception as e:
            logging.warning("%s 日线第 %d 次拉取失败: %s", code, k + 1, e)
            time.sleep(1.0 * (k + 1))
    return None


def macd(close):
    """标准 MACD：DIF = EMA12 - EMA26，DEA = EMA9(DIF)。"""
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return dif, dea


def classify(df):
    """返回 (avg_amount, avg_amplitude, dif, dea, side, fisher_daily)。

    side 为 'right'/'left'/None（MACD 条件不满足时为 None）；fisher_daily 为最新日线 Fisher 值，
    供深水池判定（不依赖 MACD）。基础过滤不通过返回 None。
    """
    if df is None or len(df) < LIST_MIN_BARS:   # 上市不足一年
        return None
    close = df["close"].astype(float).reset_index(drop=True)

    d = df.tail(NEED_DAYS + 1)                   # 多 1 根作昨收
    h = d["high"].astype(float).values
    l = d["low"].astype(float).values
    c = d["close"].astype(float).values
    amt = pd.to_numeric(d["amount"], errors="coerce").values
    avg_amp = ((h[1:] - l[1:]) / c[:-1] * 100).mean()
    avg_amount = amt[1:].mean()
    if not (avg_amount >= AVG_AMOUNT_MIN and avg_amp >= AVG_AMPLITUDE_MIN):
        return None

    # 日线 Fisher（与 60 分钟同一套实现，hl2 递归）
    from fisher_scanner import fisher_transform, FISHER_LEN
    fish, _ = fisher_transform(df["high"].astype(float).values,
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


def coarse_filter():
    """第 1 步：新浪全市场快照粗筛（沪深主板、非 ST/退、非停牌、股价达标）。"""
    import akshare as ak
    spot = ak.stock_zh_a_spot()
    spot = spot.rename(columns={"代码": "code", "名称": "name"})
    spot["code"] = spot["code"].str[2:]         # 去掉 sh/sz/bj 前缀
    spot = spot[spot["code"].str.startswith(("60", "00"))]   # 仅沪深主板
    spot = spot[~spot["name"].str.contains("ST|退", na=False)]
    price = pd.to_numeric(spot["最新价"], errors="coerce")
    turnover = pd.to_numeric(spot["成交额"], errors="coerce")
    spot = spot[(price >= MIN_PRICE) & (turnover > 0)]
    pool = spot[["code", "name"]].reset_index(drop=True)
    logging.info("粗筛后剩余 %d 只", len(pool))
    return pool


def save_results(right, left, deep):
    pd.DataFrame(right).to_csv(OUT_RIGHT, index=False, encoding="utf-8-sig")
    pd.DataFrame(left).to_csv(OUT_LEFT, index=False, encoding="utf-8-sig")
    pd.DataFrame(deep).to_csv(OUT_DEEP, index=False, encoding="utf-8-sig")


def main():
    parser = argparse.ArgumentParser(description="右侧/左侧双股票池生成器")
    parser.add_argument("--limit", type=int, help="只处理粗筛后前 N 只（调试）")
    parser.add_argument("--resume", action="store_true",
                        help="断点续跑：跳过 dual_progress.txt 中已处理的票，并合并已有 CSV 结果")
    args = parser.parse_args()

    pool = coarse_filter()
    if args.limit:
        pool = pool.head(args.limit)
        logging.info("调试模式：只处理前 %d 只", len(pool))

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
    n = len(pool)
    processed = 0
    with PROGRESS_FILE.open("a", encoding="utf-8") as prog:
        for i, row in pool.iterrows():
            code, name = str(row["code"]), row["name"]
            if code in done:
                continue
            df = fetch_daily_qfq(code)
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
            time.sleep(REQUEST_INTERVAL)

    save_results(right, left, deep)
    logging.info("完成：%d 只耗时 %.1f 分钟，右侧 %d 只 -> %s，左侧 %d 只 -> %s，深水 %d 只 -> %s，失败 %d 只",
                 n, (time.time() - t0) / 60,
                 len(right), OUT_RIGHT, len(left), OUT_LEFT, len(deep), OUT_DEEP, fails)


if __name__ == "__main__":
    main()
