# -*- coding: utf-8 -*-
"""
右侧 / 左侧 / 深水 / T0 / T1 五池生成器（tdxq 通达信客户端数据源，盘后/夜间运行）

主环境 .venv 运行：
    .venv\\Scripts\\python.exe build_pool_tdxq.py             # 全量
    .venv\\Scripts\\python.exe build_pool_tdxq.py --limit 150 # 调试

前提：通达信客户端（TdxW.exe）登录在线；客户端内做过「盘后数据下载」
（日线历史深度决定可建池范围，建议至少下载近 2 年日线）。

数据流：tdxq_fetch.py 子进程（TQ 会话，D:\\tdx\\PYPlugins\\user\\）批量取 1d K线
落盘 cache/tdxq/{code}_1d.csv，本进程读 CSV 分类。全市场名单走 tdxq --universe；
股票名称/ST 过滤用新浪全市场快照（夜间每天一次）。注意：TQ 日线 amount 单位是
万元（gm 是元），已换算。上市满 1 年用日线 ≥244 根代理；ETF 的 T+0/T+1 用名称
关键词分类（gm 的 trade_n 字段 TQ 没有）。
"""

import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
TDXQ_FETCH = r"D:\tdx\PYPlugins\user\tdxq_fetch.py"
CACHE_DIR = BASE / "cache" / "tdxq"

# ----------------------- 配置区（与 build_pool_gm.py 一致） -----------------------
MIN_PRICE = 2.0
AVG_AMOUNT_MIN = 2e8          # 20 日均成交额下限（元；TQ amount 是万元，读取时已换算）
AVG_AMPLITUDE_MIN = 2.5
NEED_DAYS = 20
LIST_MIN_BARS = 244           # 上市满 1 年 ≈ 244 根日 K
DAILY_COUNT = 400             # 拉取日 K 根数（覆盖 1 年判定 + MACD/周线暖机）
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
FISHER_LEN = 9
FISHER_DEEP_MAX = -2.0
ETF_AVG_AMOUNT_MIN = 1e8
ETF_AVG_AMPLITUDE_MIN = 1.0
ETF_LIST_MIN_BARS = 120       # ETF 上市满 120 天 ≈ 120 根日 K
OUT_RIGHT, OUT_LEFT, OUT_DEEP = "pool_right.csv", "pool_left.csv", "pool_deep.csv"
OUT_T0, OUT_T1 = "pool_t0.csv", "pool_t1.csv"
PROGRESS_FILE = BASE / "tdxq_progress.txt"
SAVE_EVERY = 200

# T+0 ETF 名称关键词（跨境 QDII / 债券 / 商品；货币/联接已在前面剔除）
T0_KEYWORDS = ("纳指", "纳斯达克", "标普", "道琼斯", "恒生", "H股", "日经", "德国",
               "法国", "沙特", "东南亚", "美国", "全球", "亚太", "QDII", "港",
               "中概", "海外", "债", "黄金", "白银", "豆粕", "原油", "有色")
# ---------------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])


def fisher_transform(high, low, length=9):
    """Fisher Transform，与 fisher_scanner.py / Pine / 同花顺实现一致。"""
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
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return dif, dea


def tdxq_run(extra_args):
    """调 tdxq_fetch 子进程，返回成败。"""
    r = subprocess.run([sys.executable, TDXQ_FETCH] + extra_args,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=3600)
    out = (r.stdout or "").strip().splitlines()
    tail = out[-1] if out else "(no output)"
    tail = tail.encode("ascii", "replace").decode("ascii")   # TQ 输出含 GBK 中文，防 GBK 控制台崩
    if r.returncode != 0:
        # 单批全部无数据（停牌/次新）时助手返回 1，属常态，只记日志不当失败
        logging.warning("tdxq_fetch 返回 %d: %s %s", r.returncode, tail,
                        (r.stderr or "")[-200:].encode("ascii", "replace").decode("ascii"))
    else:
        logging.info("tdxq_fetch: %s", tail)
    return True


def fetch_universe():
    """全市场名单：tdxq --universe 拿代码，新浪快照补名称。返回 (stocks, etfs)。"""
    if not tdxq_run(["--universe", "--out-dir", str(CACHE_DIR)]):
        sys.exit("tdxq universe 获取失败：确认通达信客户端已登录")
    uni = pd.read_csv(CACHE_DIR / "universe.csv", dtype={"code": str})

    import akshare as ak
    spot = ak.stock_zh_a_spot()
    spot = spot.rename(columns={"代码": "code", "名称": "name"})
    spot["code"] = spot["code"].str[2:]
    names = spot.set_index("code")["name"].to_dict()

    stocks, etfs = [], []
    for _, row in uni.iterrows():
        tqcode, typ = row["code"], row["type"]
        code = tqcode.split(".")[0]
        name = names.get(code, "")
        if typ == "stock":
            if not code.startswith(("60", "00")):
                continue
            if pd.isna(name) or not name or "ST" in name or "退" in name:
                continue
            stocks.append({"code": code, "name": name})
        else:
            if code.startswith(("51", "58", "15", "16")):
                etfs.append({"code": code, "name": name})   # 名称后面用 TQ 补

    # ETF 名称：新浪快照不含 ETF，用 TQ get_stock_info 逐个补（夜间约 1-2 分钟）
    etf_codes_file = CACHE_DIR / "_etf_codes.txt"
    etf_codes_file.write_text("\n".join(x["code"] for x in etfs), encoding="utf-8")
    if tdxq_run(["--codes-file", str(etf_codes_file), "--names", "--out-dir", str(CACHE_DIR)]):
        etf_names = pd.read_csv(CACHE_DIR / "names.csv", dtype={"code": str})
        name_map = etf_names.set_index("code")["name"].to_dict()
        for x in etfs:
            x["name"] = name_map.get(x["code"], "")
    etfs = [x for x in etfs if x["name"] and "联接" not in x["name"]
            and "货币" not in x["name"]]
    for x in etfs:
        x["trade_n"] = 0 if any(k in x["name"] for k in T0_KEYWORDS) else 1
    logging.info("股票初筛 %d 只；ETF 初筛 %d 只（T+0 %d / T+1 %d）",
                 len(stocks), len(etfs),
                 sum(1 for x in etfs if x["trade_n"] == 0),
                 sum(1 for x in etfs if x["trade_n"] == 1))
    return stocks, etfs


def prefetch_daily(codes):
    """批量预取日 K 到 cache/tdxq/（分 500 只一组，避免单批过大）。"""
    ok = True
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        codes_file = CACHE_DIR / "_build_codes.txt"
        codes_file.write_text("\n".join(chunk), encoding="utf-8")
        logging.info("预取日 K %d-%d/%d", i, i + len(chunk), len(codes))
        if not tdxq_run(["--codes-file", str(codes_file), "--period", "1d",
                         "--count", str(DAILY_COUNT), "--out-dir", str(CACHE_DIR)]):
            ok = False
    return ok


def load_daily(code):
    """读 tdxq 日 K 缓存：时间/开高低收/量/额（额换算为元），盘中剔除当日未完结 K。"""
    p = CACHE_DIR / ("%s_1d.csv" % code)
    if not p.exists():
        return None
    df = pd.read_csv(p)
    if len(df) == 0:
        return None
    df = df.rename(columns={"time": "eob"})
    df["eob"] = pd.to_datetime(df["eob"])
    df["amount"] = df["amount"].astype(float) * 1e4   # TQ 万元 -> 元
    now = datetime.now()
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        df = df[df["eob"].dt.date < now.date()]       # 剔除当日未完结日 K
    return df if len(df) else None


def classify(df):
    """返回 (avg_amount, avg_amplitude, dif, dea, side, fisher_daily)；基础过滤不过返回 None。"""
    if df is None or len(df) < max(NEED_DAYS + 35, LIST_MIN_BARS):   # 兼作上市满 1 年代理
        return None
    close = df["close"].astype(float).reset_index(drop=True)
    if close.iloc[-1] < MIN_PRICE:
        return None

    d = df.tail(NEED_DAYS + 1)
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
        if d1 > dea1 and d1 > 0:
            side = "right"
        elif d1 < dea1 and d1 < 0:
            side = "left"
    return avg_amount, avg_amp, float(d1), float(dea1), side, fisher_daily


def score_deep(df):
    """深水池五维打分（与 build_pool_gm.py 一致）。"""
    close = df["close"].astype(float).reset_index(drop=True)
    high = df["high"].astype(float).reset_index(drop=True)
    low = df["low"].astype(float).reset_index(drop=True)
    vol = pd.to_numeric(df["volume"], errors="coerce").reset_index(drop=True)
    fish = fisher_transform(high.values, low.values, FISHER_LEN)
    dif, dea = macd(close)
    hist = dif - dea

    a = 0
    if hist.iloc[-1] > hist.iloc[-2] > hist.iloc[-3] and hist.iloc[-1] < 0:
        a += 1
    if dif.iloc[-1] > dif.iloc[-2]:
        a += 1
    if close.iloc[-1] <= close.iloc[-20:].min() and fish[-1] > fish[-20:].min():
        a += 1
    a = min(a, 2)

    b = 0
    lo = low.iloc[max(0, len(low) - 260):-20]
    if len(lo) > 0:
        prior_low = lo.min()
        c = close.iloc[-1]
        if c < prior_low:
            b = -2
        else:
            dist = (c - prior_low) / prior_low
            if dist <= 0.03:
                b = 2
            elif dist <= 0.08:
                b = 1

    clv = (((close - low) - (high - close)) / (high - low).replace(0, np.nan)).fillna(0)
    cmf = (clv * vol).rolling(NEED_DAYS).sum() / vol.rolling(NEED_DAYS).sum()
    cmf = cmf.dropna()
    cc = 0
    if len(cmf) >= 21:
        if cmf.iloc[-1] > cmf.iloc[-2] > cmf.iloc[-3]:
            cc = 2 if cmf.iloc[-1] > 0 else 1
        elif cmf.iloc[-1] < cmf.iloc[-2] < cmf.iloc[-3]:
            cc = -1
        if cmf.iloc[-1] < cmf.iloc[-21:-1].min():
            cc = -2

    d = 0
    wk = df.copy()
    wk["dt"] = pd.to_datetime(wk["eob"])
    wk = wk.set_index("dt").resample("W-FRI").agg({"high": "max", "low": "min"}).dropna()
    if len(wk) >= 25:
        wfish = fisher_transform(wk["high"].values, wk["low"].values, FISHER_LEN)
        if wfish[-1] < FISHER_DEEP_MAX:
            d = 2
        elif wfish[-1] > 1 and wfish[-1] < wfish[-2]:
            d = -2

    e = 0
    pct = float((fish < fish[-1]).mean())
    if pct < 0.05:
        e = 2
    elif pct < 0.15:
        e = 1

    total = 0.30 * a + 0.25 * b + 0.20 * cc + 0.15 * d + 0.10 * e
    return {"score": round(total, 2), "sA": a, "sB": b, "sC": cc, "sD": d, "sE": e}


def classify_etf(df):
    if df is None or len(df) < max(NEED_DAYS + 35, ETF_LIST_MIN_BARS):
        return None
    d = df.tail(NEED_DAYS + 1)
    h = d["high"].astype(float).values
    l = d["low"].astype(float).values
    c = d["close"].astype(float).values
    amt = pd.to_numeric(d["amount"], errors="coerce").values
    avg_amp = ((h[1:] - l[1:]) / c[:-1] * 100).mean()
    avg_amount = amt[1:].mean()
    if not (avg_amount >= ETF_AVG_AMOUNT_MIN and avg_amp >= ETF_AVG_AMPLITUDE_MIN):
        return None
    fish = fisher_transform(df["high"].astype(float).values,
                            df["low"].astype(float).values, FISHER_LEN)
    fisher_daily = float(fish[-1])
    if fisher_daily >= FISHER_DEEP_MAX:
        return None
    return avg_amount, avg_amp, fisher_daily


def save_results(right, left, deep):
    pd.DataFrame(right).to_csv(OUT_RIGHT, index=False, encoding="utf-8-sig")
    pd.DataFrame(left).to_csv(OUT_LEFT, index=False, encoding="utf-8-sig")
    ddf = pd.DataFrame(deep)
    if len(ddf) and "score" in ddf.columns:
        ddf = ddf.sort_values("score", ascending=False)
    ddf.to_csv(OUT_DEEP, index=False, encoding="utf-8-sig")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="五池生成器（tdxq 通达信客户端版）")
    parser.add_argument("--limit", type=int, help="只处理前 N 只（调试）")
    args = parser.parse_args()

    stocks, etfs = fetch_universe()
    if args.limit:
        stocks = stocks[:args.limit]
        logging.info("调试模式：只处理前 %d 只", args.limit)

    prefetch_daily([x["code"] for x in stocks] + [x["code"] for x in etfs])

    right, left, deep, fails = [], [], [], 0
    t0 = time.time()
    n = len(stocks)
    processed = 0
    with PROGRESS_FILE.open("w", encoding="utf-8") as prog:
        for i, item in enumerate(stocks):
            code, name = item["code"], item["name"]
            df = load_daily(code)
            if df is None:
                fails += 1
            else:
                r = classify(df)
                if r:
                    rec = {"code": code, "name": name,
                           "close": float(df["close"].iloc[-1]),
                           "avg_amount": round(r[0] / 1e8, 2),
                           "avg_amplitude": round(r[1], 2),
                           "dif": round(r[2], 4), "dea": round(r[3], 4),
                           "fisher_daily": round(r[5], 3)}
                    if r[4] == "right":
                        right.append(rec)
                    elif r[4] == "left":
                        left.append(rec)
                    if r[5] < FISHER_DEEP_MAX:
                        rec.update(score_deep(df))
                        deep.append(rec)
            prog.write(code + "\n")
            prog.flush()
            processed += 1
            if processed % SAVE_EVERY == 0:
                logging.info("进度 %d/%d，右侧 %d，左侧 %d，深水 %d，失败 %d",
                             i + 1, n, len(right), len(left), len(deep), fails)
                save_results(right, left, deep)

    save_results(right, left, deep)
    logging.info("股票池完成：%d 只耗时 %.1f 分钟，右侧 %d / 左侧 %d / 深水 %d，失败 %d",
                 n, (time.time() - t0) / 60, len(right), len(left), len(deep), fails)

    t0e, t1e, efails = [], [], 0
    for item in etfs:
        df = load_daily(item["code"])
        if df is None:
            efails += 1
            continue
        r = classify_etf(df)
        if r:
            rec = {"code": item["code"], "name": item["name"],
                   "close": float(df["close"].iloc[-1]),
                   "avg_amount": round(r[0] / 1e8, 2),
                   "avg_amplitude": round(r[1], 2),
                   "fisher_daily": round(r[2], 3)}
            (t0e if item["trade_n"] == 0 else t1e).append(rec)
    pd.DataFrame(t0e).to_csv(OUT_T0, index=False, encoding="utf-8-sig")
    pd.DataFrame(t1e).to_csv(OUT_T1, index=False, encoding="utf-8-sig")
    logging.info("ETF 池完成：T+0 %d 只，T+1 %d 只，失败 %d", len(t0e), len(t1e), efails)


if __name__ == "__main__":
    main()
