# -*- coding: utf-8 -*-
"""
预筛股票池生成器（沪深 A 股，新浪数据源）

每日收盘后运行一次，按以下条件过滤全市场，输出 pool.csv：
    全部沪深 A 股（60/68/00/30 开头，剔北交所）
    剔 ST / *ST / 退市整理
    剔停牌（当日无成交：最新价为 0 或当日成交额为 0）
    剔股价 < MIN_PRICE 元
    20 日均成交额 >= AVG_AMOUNT_MIN（新浪日线无成交额字段，
        用 volume × (high+low+close)/3 典型价近似，误差一般 <5%）
    20 日均振幅 >= AVG_AMPLITUDE_MIN（振幅 = (最高-最低)/昨收 × 100%）

用法：
    python build_pool.py            # 生成 pool.csv（默认文件名）
    python build_pool.py -o my.csv  # 指定输出文件

生成后用：python fisher_scanner.py --once --pool-file pool.csv
注意：请在收盘后运行（盘中运行时当日日 bar 未完结，会参与均值计算）。
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd
import requests

# ----------------------- 配置区（按需修改） -----------------------
MIN_PRICE = 2.0               # 剔股价低于此值（元）
AVG_AMOUNT_MIN = 2e8          # 20 日均成交额下限（元）
AVG_AMPLITUDE_MIN = 2.5       # 20 日均振幅下限（%）
NEED_DAYS = 20                # 均值窗口（交易日）
FETCH_BARS = 25               # 多取几根，保证窗口内每根都有昨收
REQUEST_INTERVAL = 0.2        # 每股请求间隔（秒），防新浪限流
RETRY = 3
TIMEOUT = 10
SINA_DAILY_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/x/"
                  "CN_MarketDataService.getKLineData")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# -----------------------------------------------------------------

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])


def fetch_daily(code, bars=FETCH_BARS):
    """拉取单只股票最近 N 根日 K（不复权），返回 DataFrame 或 None。"""
    symbol = ("sh" if code.startswith("6") else "sz") + code
    params = {"symbol": symbol, "scale": 240, "ma": "no", "datalen": bars}
    for k in range(RETRY):
        try:
            r = requests.get(SINA_DAILY_URL, params=params,
                             headers=HEADERS, timeout=TIMEOUT)
            text = r.text
            data = json.loads(text[text.index("["):text.rindex("]") + 1])
            if data:
                return pd.DataFrame(data)
            return None
        except Exception as e:
            logging.warning("%s 日线第 %d 次拉取失败: %s", code, k + 1, e)
            time.sleep(1.0 * (k + 1))
    return None


def pass_filter(df):
    """按 20 日均成交额 / 20 日均振幅过滤，通过则返回 (avg_amount, avg_amplitude)。"""
    if df is None or len(df) < NEED_DAYS + 1:   # 次新股跳过（多 1 根作昨收）
        return None
    d = df.tail(NEED_DAYS + 1).reset_index(drop=True)
    high = d["high"].astype(float)
    low = d["low"].astype(float)
    close = d["close"].astype(float)
    volume = d["volume"].astype(float)
    prev_close = close.shift(1)

    window = slice(1, NEED_DAYS + 1)            # 最近 20 根
    amp = ((high - low) / prev_close * 100).iloc[window]
    typ_price = (high + low + close) / 3
    amount = (volume * typ_price).iloc[window]

    avg_amp = amp.mean()
    avg_amount = amount.mean()
    if avg_amount >= AVG_AMOUNT_MIN and avg_amp >= AVG_AMPLITUDE_MIN:
        return avg_amount, avg_amp
    return None


def coarse_filter():
    """第 1 步：新浪全市场快照粗筛（沪深、非 ST/退、非停牌、股价达标）。"""
    import akshare as ak
    spot = ak.stock_zh_a_spot()
    spot = spot.rename(columns={"代码": "code", "名称": "name"})
    spot["code"] = spot["code"].str[2:]         # 去掉 sh/sz/bj 前缀
    spot = spot[spot["code"].str.startswith(("60", "68", "00", "30"))]
    spot = spot[~spot["name"].str.contains("ST|退", na=False)]
    price = pd.to_numeric(spot["最新价"], errors="coerce")
    turnover = pd.to_numeric(spot["成交额"], errors="coerce")
    spot = spot[(price >= MIN_PRICE) & (turnover > 0)]
    pool = spot[["code", "name"]].reset_index(drop=True)
    logging.info("粗筛后剩余 %d 只", len(pool))
    return pool


def main():
    parser = argparse.ArgumentParser(description="预筛股票池生成器")
    parser.add_argument("-o", "--out", default="pool.csv", help="输出 CSV 路径")
    parser.add_argument("--limit", type=int, help="只处理粗筛后前 N 只（调试）")
    args = parser.parse_args()

    pool = coarse_filter()
    if args.limit:
        pool = pool.head(args.limit)
        logging.info("调试模式：只处理前 %d 只", len(pool))

    hits, fails = [], 0
    t0 = time.time()
    n = len(pool)
    for i, row in pool.iterrows():
        code, name = str(row["code"]), row["name"]
        df = fetch_daily(code)
        if df is None:
            fails += 1
        else:
            r = pass_filter(df)
            if r:
                hits.append({
                    "code": code, "name": name,
                    "close": float(df["close"].iloc[-1]),
                    "avg_amount": round(r[0] / 1e8, 2),      # 亿元
                    "avg_amplitude": round(r[1], 2),          # %
                })
        if (i + 1) % 200 == 0:
            logging.info("进度 %d/%d，入选 %d，失败 %d", i + 1, n, len(hits), fails)
        time.sleep(REQUEST_INTERVAL)

    result = pd.DataFrame(hits)
    out = Path(args.out)
    result.to_csv(out, index=False, encoding="utf-8-sig")
    logging.info("完成：%d 只耗时 %.1f 分钟，入选 %d 只，失败 %d 只，已保存 %s",
                 n, (time.time() - t0) / 60, len(result), fails, out)


if __name__ == "__main__":
    main()
