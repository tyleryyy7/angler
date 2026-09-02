# -*- coding: utf-8 -*-
"""
Fisher Transform 60分钟线「刚上穿」扫描器（沪深A股，akshare；数据源见配置区 DATA_SOURCE，
可选新浪 sina / 东方财富 em，默认新浪——东财接口对部分网络环境有 WAF 封锁）

信号定义（与你的 Pine / 同花顺代码完全一致）：
    fish2 = fish1[1]，所以「上穿」= fish1 由跌转升的拐点：
    fish[t] > fish[t-1] 且 fish[t-1] <= fish[t-2]
    默认只判断【最新已完结】的那根 60 分钟 bar；加 --live 则盘中未完结 bar 也参与判定。

A股 60 分钟 bar 一天 4 根，东财时间戳为 bar 结束时刻：10:30 / 11:30 / 14:00 / 15:00。
完结信号建议在每根 bar 收盘后 1 分钟运行：10:31 / 11:31 / 14:01 / 15:01；
盘中（--live）信号在 bar 中段运行（10:16 / 11:16 / 13:46 / 14:46），持仓每 15 分钟。

用法：
    python fisher_scanner.py --once                      # 扫一次退出（配合 cron / 任务计划）
    python fisher_scanner.py --loop                      # 常驻，每日 4 个时点自动扫描
    python fisher_scanner.py --once --limit 50           # 只扫前 50 只（调试用）
    python fisher_scanner.py --once --pool-file pool.csv # 自定义股票池（CSV 需含 code 列）
    python fisher_scanner.py --once --pool-file holdings.csv --side down  # 持仓下穿监控
    python fisher_scanner.py --buy 600036 --price 38.9     # 登记买入到 holdings.csv
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------- 配置区（按需修改） -----------------------
DATA_SOURCE = "tdx"       # 数据源："tdx"（通达信 xmtdx，默认，快且无限流）/ "sina" / "gm"（须 .venv-gm）/ "em"（本机被封）
FISHER_LEN = 9            # Fisher 窗口长度，与 Pine/同花顺参数一致
MIN_BARS = 80             # 60分钟bar少于此数视为暖机不足，跳过（次新股、长期停牌）
REQUEST_INTERVAL = 0.25   # 每个 worker 每只股票之间的请求间隔（秒），防限流
WORKERS = 4               # 并发进程数；新浪源建议 <=4（约 4~5 次请求/秒，实测安全），东财源可到 8
RETRY = 3                 # 单只票拉取失败重试次数
EXCLUDE_ST = True         # 排除 ST / *ST / 退市整理股
ONLY_SH_SZ = True         # 只保留沪深（60/68/00/30 开头），排除北交所
SCAN_TIMES = ["10:31", "11:31", "14:01", "15:01"]   # --loop 模式的每日扫描时刻
# 企业微信机器人 webhook：优先读环境变量 FISHER_WECOM_WEBHOOK，其次读本地 webhook.key
# 文件（该文件已加入 .gitignore，切勿提交到 git，防止 webhook 泄露后被群发垃圾消息）
_webhook_file = Path(__file__).resolve().parent / "webhook.key"
WECOM_WEBHOOK = os.environ.get("FISHER_WECOM_WEBHOOK", "").strip() or (
    _webhook_file.read_text(encoding="utf-8").strip() if _webhook_file.exists() else "")
PUSH_EMPTY = True           # 无命中时是否也推送一条「无信号」
PUSH_MAX_ROWS = 50          # 单条推送最多列出的只数（超出提示看 CSV）
RESULT_DIR = Path(__file__).resolve().parent / "results"
LOG_FILE = Path(__file__).resolve().parent / "scanner.log"
CACHE_DIR = Path(__file__).resolve().parent / "cache" / "daily_qfq"  # 日线复权因子缓存（当日有效）
PUSHED_FILE = Path(__file__).resolve().parent / "cache" / "pushed_signals.json"  # 当日已推送信号去重记录
SINA_KLINE_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/x/"
                  "CN_MarketDataService.getKLineData")
SINA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# -----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)


def _patch_requests_timeout(default=15):
    """给 requests（含 akshare 内部调用）补默认超时，防止连接挂起卡死整个扫描。
    未安装 requests 的环境（如精简版 .venv-gm）直接跳过。"""
    try:
        import requests
    except ImportError:
        return
    _orig = requests.sessions.Session.request
    def _req(self, method, url, **kw):
        kw.setdefault("timeout", default)
        return _orig(self, method, url, **kw)
    requests.sessions.Session.request = _req


_patch_requests_timeout()


def fisher_transform(high, low, length=9):
    """Fisher Transform，与 Pine / 同花顺实现逐行对应。

    value = clip(0.66*((hl2-ll)/(hh-ll)-0.5) + 0.67*value[1], -0.999, 0.999)
    fish1 = 0.5*ln((1+value)/(1-value)) + 0.5*fish1[1]
    首根之前的值按 nz() 取 0。递归指标，必须逐根计算；
    衰减系数 0.67/0.5 使初始值误差迅速消失，取最近 150~200 根 bar 即足够精确。
    """
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
    trigger = np.concatenate([[np.nan], fish[:-1]])  # fish2 = fish1[1]
    return fish, trigger


def just_crossed_up(fish, trigger, j):
    """第 j 根 bar 是否刚完成上穿。等价于 fisher 在该根由跌转升。"""
    if j < 2:
        return False
    return fish[j] > trigger[j] and fish[j - 1] <= trigger[j - 1]


def just_crossed_down(fish, trigger, j):
    """第 j 根 bar 是否刚完成下穿。等价于 fisher 在该根由升转跌。"""
    if j < 2:
        return False
    return fish[j] < trigger[j] and fish[j - 1] >= trigger[j - 1]


def signal_bar_index(df, now=None, live=False):
    """返回用于判定信号的 bar 下标。

    东财 60 分钟 bar 时间戳 = bar 结束时刻（10:30/11:30/14:00/15:00）。
    盘中最后一根可能正在形成（now 早于其结束时刻）：
    live=False（默认）丢弃它、用 -2；live=True（盘中信号）直接用它、用 -1。
    注意 live 信号基于未完结 bar，收盘前可能消失或翻转。
    """
    if now is None:
        now = datetime.now()
    last_ts = pd.to_datetime(df["时间"].iloc[-1])
    if last_ts.date() == now.date() and now < last_ts:
        return -1 if live else -2
    return -1


def _sina_symbol(code):
    """新浪代码前缀：6/5(股票/沪ETF 51/58) -> sh，其余（00/30/68/15/16）-> sz。"""
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def _gm_symbol(code):
    """gm 代码格式：沪 6/5 开头 -> SHSE.xxxxxx，其余 -> SZSE.xxxxxx。"""
    return ("SHSE." if code.startswith(("5", "6")) else "SZSE.") + code


_gm_ready = False

_TDX_BAR_ENDS = ["10:30", "11:30", "14:00", "15:00"]   # 60 分钟 bar 收盘时刻（当日第 1~4 根）

def _fetch_60m_tdx(code):
    """通达信（xmtdx）60 分钟 K 线（不复权）。

    注意：tdx 盘中第二根 bar（10:30-11:30）会标记为 13:00（跨午休怪癖），
    因此不采用原始时间戳，按「当日第几根」映射到固定收盘时刻 10:30/11:30/14:00/15:00。
    """
    from xmtdx import TdxClient, Market, KlineCategory
    market = Market.SH if code.startswith(("5", "6")) else Market.SZ
    with TdxClient.from_best_host(ping_timeout=3.0) as c:
        bars = c.get_security_bars(market, code, KlineCategory.MIN_60, 0, 200)
    if not bars:
        return None
    df = pd.DataFrame([{"时间": datetime(b.year, b.month, b.day),
                        "最高": b.high, "最低": b.low, "收盘": b.close} for b in bars])
    dates = df["时间"].dt.date
    for d in dates.unique():                       # 按当日序号映射收盘时刻
        idx = df.index[dates == d]
        for k, i in enumerate(idx):
            if k < len(_TDX_BAR_ENDS):
                hh, mm = _TDX_BAR_ENDS[k].split(":")
                df.at[i, "时间"] = datetime(d.year, d.month, d.day, int(hh), int(mm))
    return df

def _fetch_60m_gm(code):
    """掘金 60 分钟前复权 K 线。注意 gm 的 frequency='3600s' 才是 60 分钟（'60s' 是 1 分钟）。"""
    global _gm_ready
    from gm.api import history, ADJUST_PREV, set_token
    if not _gm_ready:
        token_file = Path(__file__).resolve().parent / "gm_token.key"
        set_token(token_file.read_text(encoding="utf-8").strip())
        _gm_ready = True
    end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    df = history(symbol=_gm_symbol(code), frequency="3600s",
                 start_time=start, end_time=end, adjust=ADJUST_PREV, df=True)
    if df is None or len(df) == 0:
        return None
    df = df.rename(columns={"eob": "时间", "high": "最高", "low": "最低", "close": "收盘"})
    df["时间"] = pd.to_datetime(df["时间"]).dt.tz_localize(None)  # 去时区 11，便于和本地时间比较
    return df


def _daily_qfq_factor(code):
    """新浪日线前复权因子（qfq_close/raw_close，按日期）。

    当日缓存到 cache/daily_qfq/：同一天内因子不变（除权除息开盘前已确定），
    每天首次扫描拉一次，之后读本地，把新浪请求量降到每股 1 次。
    """
    import akshare as ak
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    cache = CACHE_DIR / ("%s_%s.csv" % (code, today))
    if cache.exists():
        return pd.read_csv(cache, dtype={"date": str})
    symbol = _sina_symbol(code)
    raw = ak.stock_zh_a_daily(symbol=symbol, adjust="")
    qfq = ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")
    m = qfq[["date", "close"]].merge(raw[["date", "close"]], on="date",
                                     suffixes=("_q", "_r"))
    m["factor"] = m["close_q"].astype(float) / m["close_r"].astype(float)
    out = m[["date", "factor"]]
    for old in CACHE_DIR.glob("%s_*.csv" % code):   # 清掉该票的旧日期缓存
        old.unlink()
    out.to_csv(cache, index=False)
    return out


def _fetch_60m_sina(code):
    """新浪 60 分钟前复权：分钟线 jsonp（1 次请求）× 本地缓存的日线因子。

    与 akshare stock_zh_a_minute(adjust='qfq') 的计算口径一致（同一交易日逐位相同），
    但每股只需 1 次 HTTP 请求（akshare 原版要 5 次）。
    """
    import json
    import requests
    symbol = _sina_symbol(code)
    r = requests.get(SINA_KLINE_URL,
                     params={"symbol": symbol, "scale": 60, "ma": "no", "datalen": 1970},
                     headers=SINA_HEADERS, timeout=15)
    data = json.loads(r.text[r.text.index("["):r.text.rindex("]") + 1])
    if not data:
        return None
    df = pd.DataFrame(data)
    fac = _daily_qfq_factor(code)
    dates = df["day"].str.split(" ", expand=True)[0]
    fmap = dict(zip(fac["date"], fac["factor"]))
    factor = dates.map(fmap).ffill().fillna(1.0)   # 当日因子缺失时用最近交易日因子
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float) * factor.values
    return df.rename(columns={"day": "时间", "high": "最高",
                              "low": "最低", "close": "收盘"})


def fetch_60m(code, source=None):
    """拉取单只股票的 60 分钟前复权 K 线，带重试。失败返回 None。

    source: "tdx"（默认，不复权）/ "sina" / "gm"（须在 .venv-gm 运行）/ "em"（东财）。
    """
    source = source or DATA_SOURCE
    for k in range(RETRY):
        try:
            if source == "gm":
                df = _fetch_60m_gm(code)
                if df is not None and len(df) > 0:
                    return df
            elif source == "tdx":
                df = _fetch_60m_tdx(code)
                if df is not None and len(df) > 0:
                    return df
            elif source == "sina":
                df = _fetch_60m_sina(code)
                if df is not None and len(df) > 0:
                    return df
            else:
                import akshare as ak
                df = ak.stock_zh_a_hist_min_em(symbol=code, period="60", adjust="qfq")
                if df is not None and len(df) > 0:
                    return df
        except Exception as e:
            logging.warning("%s 第 %d 次拉取失败: %s", code, k + 1, e)
        time.sleep(1.0 * (k + 1))
    return None


def get_pool():
    """沪深 A 股股票池：排除北交所、ST/退市、停牌（最新价为 0/空）。

    sina 分支：新浪全市场行情（分页拉取约 1 分钟；频繁调用会被新浪暂时封 IP，
    --loop 模式每日只刷一次，够用）。em 分支：东财实时行情快照。
    """
    import akshare as ak
    if DATA_SOURCE == "sina":
        spot = ak.stock_zh_a_spot()
        spot = spot.rename(columns={"代码": "code", "名称": "name"})
        spot["code"] = spot["code"].str[2:]  # 去掉 sh/sz/bj 前缀
    else:
        spot = ak.stock_zh_a_spot_em()
        spot = spot.rename(columns={"代码": "code", "名称": "name"})
    if ONLY_SH_SZ:
        spot = spot[spot["code"].str.startswith(("60", "68", "00", "30"))]
    if EXCLUDE_ST:
        spot = spot[~spot["name"].str.contains("ST|退", na=False)]
    spot = spot[pd.to_numeric(spot["最新价"], errors="coerce") > 0]
    pool = spot[["code", "name"]].reset_index(drop=True)
    logging.info("股票池共 %d 只", len(pool))
    return pool


def load_pool(args):
    if args.pool_file:
        pool = pd.read_csv(args.pool_file, dtype={"code": str})
        logging.info("从 %s 加载股票池 %d 只", args.pool_file, len(pool))
    else:
        pool = get_pool()
    if args.limit:
        pool = pool.head(args.limit)
        logging.info("调试模式：只扫描前 %d 只", len(pool))
    return pool


def scan_one(row, side="up", source=None, live=False):
    """扫描单只股票：命中返回 dict；未命中 None；拉取失败 'FAIL'；bar 不足 'SKIP'。"""
    code, name = str(row["code"]), row["name"]
    df = fetch_60m(code, source)
    time.sleep(REQUEST_INTERVAL)   # 每个 worker 内部的节流
    if df is None:
        return "FAIL"
    if len(df) < MIN_BARS:
        return "SKIP"
    high = pd.to_numeric(df["最高"], errors="coerce").values
    low = pd.to_numeric(df["最低"], errors="coerce").values
    fish, trig = fisher_transform(high, low, FISHER_LEN)
    now = datetime.now()
    j = len(df) + signal_bar_index(df, now=now, live=live)
    crossed = just_crossed_down(fish, trig, j) if side == "down" else just_crossed_up(fish, trig, j)
    if crossed:
        bar_ts = pd.to_datetime(df["时间"].iloc[j])
        return {
            "code": code, "name": name,
            "bar_time": str(df["时间"].iloc[j]),
            "close": float(pd.to_numeric(df["收盘"], errors="coerce").iloc[j]),
            "fisher": round(float(fish[j]), 3),
            "trigger": round(float(trig[j]), 3),
            # 当日且尚未到 bar 结束时刻 → 未完结（盘中信号）
            "bar_state": "未完结" if bar_ts.date() == now.date() and now < bar_ts else "完结",
        }
    return None


def scan(pool, save=True, label="", side="up", source=None, live=False):
    """并发扫描整个股票池，返回命中「刚上穿/下穿」的股票清单。

    live=False 只判最新完结 bar；live=True 盘中未完结 bar 也参与判定。
    用进程池而非线程池：akshare 新浪前复权链路用的 py_mini_racer 是 C 扩展，
    多线程下会崩解释器；多进程各自独立则无此问题。
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    hits, fails, skips, done = [], 0, 0, 0
    t0 = time.time()
    n = len(pool)
    rows = [row for _, row in pool.iterrows()]
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(scan_one, row, side, source, live) for row in rows]
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            if r == "FAIL":
                fails += 1
            elif r == "SKIP":
                skips += 1
            elif r:
                hits.append(r)
            if done % 50 == 0:
                logging.info("进度 %d/%d，命中 %d，失败 %d，跳过 %d", done, n, len(hits), fails, skips)
            if done >= 20 and fails == done:
                # 前 20 只全失败：数据源整体不可用（如限流），提前终止，剩余取消
                logging.warning("前 %d 只全部失败，数据源疑似不可用，提前终止本池扫描", done)
                for f in futures:
                    f.cancel()
                break

    result = pd.DataFrame(hits)
    if len(result):
        if "score" in pool.columns:   # 带打分的池（如深水池）：附分数并按分数降序
            result = result.merge(pool[["code", "score"]], on="code", how="left")
            result = result.sort_values("score", ascending=False).reset_index(drop=True)
        else:
            result = result.sort_values("code").reset_index(drop=True)
    result.attrs["total"] = done       # 用实际处理数，配合提前终止时的失败率判定
    result.attrs["fails"] = fails
    elapsed = time.time() - t0
    logging.info("扫描完成：%d 只耗时 %.1f 分钟，命中 %d 只，失败 %d 只，跳过 %d 只",
                 n, elapsed / 60, len(result), fails, skips)
    if save:
        RESULT_DIR.mkdir(exist_ok=True)
        tag = "_%s" % label if label else ""
        out = RESULT_DIR / ("fisher_cross_%s%s.csv" % (datetime.now().strftime("%Y%m%d_%H%M"), tag))
        result.to_csv(out, index=False, encoding="utf-8-sig")
        logging.info("结果已保存: %s", out)
    return result


def pool_label(args):
    """从股票池文件名推断鱼塘：pool_right/left/deep.csv -> 右侧/左侧/深水鱼塘，holdings.csv -> 持仓鱼塘。"""
    f = (args.pool_file or "").lower()
    if "holding" in f:
        return "持仓鱼塘"
    if "watch" in f:
        return "观察鱼塘"
    if "right" in f:
        return "右侧鱼塘"
    if "left" in f:
        return "左侧鱼塘"
    if "deep" in f:
        return "深水鱼塘"
    if "t0" in f:
        return "T0鱼塘"
    if "t1" in f:
        return "T1鱼塘"
    return "鱼塘"


def pool_tag(args):
    """结果 CSV 文件名后缀：right / left / holdings / 空。"""
    f = (args.pool_file or "").lower()
    if "holding" in f:
        return "holdings"
    if "watch" in f:
        return "watch"
    if "right" in f:
        return "right"
    if "left" in f:
        return "left"
    if "deep" in f:
        return "deep"
    if "t0" in f:
        return "t0"
    if "t1" in f:
        return "t1"
    return ""


def _load_pushed():
    """读当日已推送信号键集合（去重用）；文件缺失/损坏返回空集合。"""
    import json
    try:
        keys = json.loads(PUSHED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    today = datetime.now().strftime("%Y-%m-%d")
    return set(k for k in keys if k.startswith(today + "|"))


def _save_pushed(keys):
    """写回已推送信号键（只保留当日条目）。"""
    import json
    PUSHED_FILE.parent.mkdir(exist_ok=True)
    PUSHED_FILE.write_text(json.dumps(sorted(keys), ensure_ascii=False), encoding="utf-8")


def notify(result, pond="鱼塘", side="up"):
    """结果通知：打印到控制台，并推送到企业微信机器人（WECOM_WEBHOOK 留空则跳过）。

    静默规则：下穿（持仓监控）与观察池/持仓上穿（回钩）无命中时不推送，避免噪音；
    池类（右侧/左侧/深水/T0/T1）受 PUSH_EMPTY 控制。
    失败率超过一半时（如数据源限流），推送「数据源异常」而不是可能漏报的「N 条鱼」。
    同一根 bar 的同一信号当日只推一次（盘中未完结信号推过后，收盘完结确认不重复推送）。
    """
    total = result.attrs.get("total", 0)
    fails = result.attrs.get("fails", 0)
    if side == "down":
        suffix = "下穿"
    elif pond in ("持仓鱼塘", "观察鱼塘"):
        suffix = "回钩"
    else:
        suffix = ""
    if total and fails > total / 2:
        print("本次扫描异常：失败 %d/%d，结果不可信" % (fails, total))
        content = "**%s：数据源异常，本次结果不可信（失败 %d/%d）**" % (pond, fails, total)
        should_push = True
    elif len(result) == 0:
        print("本次扫描：无刚%s标的" % ("下穿" if side == "down" else "上穿"))
        content = "**%s：0 条鱼%s**" % (pond, suffix)
        should_push = (PUSH_EMPTY and side == "up"
                       and pond not in ("持仓鱼塘", "观察鱼塘"))
    else:
        # 去重：同一根 bar 的同一信号当日只推一次
        pushed = _load_pushed()
        today = datetime.now().strftime("%Y-%m-%d")
        keys = ["%s|%s|%s|%s|%s" % (today, pond, side, r["code"], r["bar_time"])
                for _, r in result.iterrows()]
        keep = [i for i, k in enumerate(keys) if k not in pushed]
        if len(keep) < len(keys):
            logging.info("%s：%d 只命中已推送过，去重后剩 %d 只",
                         pond, len(result) - len(keep), len(keep))
        print("本次扫描命中 %d 只（去重后 %d 只）：\n%s"
              % (len(result), len(keep), result.to_string(index=False)))
        if not keep:
            return
        result = result.iloc[keep].reset_index(drop=True)
        keys = [keys[i] for i in keep]
        has_live = "bar_state" in result.columns and (result["bar_state"] == "未完结").any()
        title = "**%s：%d 条鱼%s%s**" % (pond, len(result), suffix,
                                        "（含盘中信号，bar 未完结）" if has_live else "")

        def _row_text(r):
            tag = "（未完结）" if r.get("bar_state") == "未完结" else ""
            if "score" in result.columns:   # 带打分的池附分数
                return "%s %s（%s）%s" % (r["code"], r["name"], r["score"], tag)
            return "%s %s%s" % (r["code"], r["name"], tag)

        lines = [title] + [_row_text(r) for _, r in result.head(PUSH_MAX_ROWS).iterrows()]
        if len(result) > PUSH_MAX_ROWS:
            lines.append("……共 %d 只，完整清单见 results CSV" % len(result))
        content = "\n".join(lines)
        should_push = True
        _save_pushed(pushed | set(keys))
    if WECOM_WEBHOOK and should_push:
        try:
            import requests
            r = requests.post(WECOM_WEBHOOK, timeout=10,
                              json={"msgtype": "markdown",
                                    "markdown": {"content": content}})
            if r.json().get("errcode") != 0:
                logging.warning("企业微信推送返回异常: %s", r.text[:200])
        except Exception as e:
            logging.warning("企业微信推送失败: %s", e)


def _load_list(filename):
    """读持仓/观察清单 CSV；不存在则返回带表头的空表。"""
    path = Path(__file__).resolve().parent / filename
    if path.exists():
        return pd.read_csv(path, dtype={"code": str})
    return pd.DataFrame(columns=["code", "name", "buy_date", "buy_price"])


def _save_list(filename, df):
    df.to_csv(Path(__file__).resolve().parent / filename, index=False, encoding="utf-8-sig")


def _lookup_name_price(code):
    """用新浪全市场快照查名称和最新价（约 20 秒，低频操作可接受）。"""
    import akshare as ak
    spot = ak.stock_zh_a_spot()
    row = spot[spot["代码"].str[2:] == code]
    name = row["名称"].iloc[0] if len(row) else ""
    price = float(row["最新价"].iloc[0]) if len(row) else None
    return name, price


def buy(code, price=None):
    """把股票登记进 holdings.csv（持仓监控清单）；已在清单中则跳过。
    若该票在观察池中则自动移出（买回了就不用盯回钩了）。"""
    h = _load_list("holdings.csv")
    code = str(code).zfill(6)
    if code in h["code"].astype(str).values:
        logging.info("%s 已在持仓清单中", code)
        return
    name, spot_price = _lookup_name_price(code)
    if price is None:
        price = spot_price
    h = pd.concat([h, pd.DataFrame([{
        "code": code, "name": name,
        "buy_date": datetime.now().strftime("%Y-%m-%d"),
        "buy_price": price if price else "",
    }])], ignore_index=True)
    _save_list("holdings.csv", h)
    w = _load_list("watchlist.csv")
    if code in w["code"].astype(str).values:
        _save_list("watchlist.csv", w[w["code"] != code].reset_index(drop=True))
        print("已从观察池移出")
    print("已登记持仓: %s %s 买入价 %s" % (code, name, price))


def watch(code):
    """把股票加入 watchlist.csv（观察池，监控 60 分钟上穿回钩）。"""
    w = _load_list("watchlist.csv")
    code = str(code).zfill(6)
    if code in w["code"].astype(str).values:
        logging.info("%s 已在观察池中", code)
        return
    name, price = _lookup_name_price(code)
    w = pd.concat([w, pd.DataFrame([{
        "code": code, "name": name,
        "buy_date": datetime.now().strftime("%Y-%m-%d"),
        "buy_price": price if price else "",
    }])], ignore_index=True)
    _save_list("watchlist.csv", w)
    print("已加入观察池: %s %s" % (code, name))


def sell(code):
    """清仓：从 holdings.csv 移除并加入 watchlist.csv（继续盯 60 分钟上穿回钩）。"""
    code = str(code).zfill(6)
    h = _load_list("holdings.csv")
    row = h[h["code"] == code]
    if len(row) == 0:
        print("%s 不在持仓清单中" % code)
    else:
        _save_list("holdings.csv", h[h["code"] != code].reset_index(drop=True))
        print("已从持仓移除: %s %s" % (code, row["name"].iloc[0]))
        watch(code)


def run_loop(args):
    """常驻模式：每个交易日 SCAN_TIMES 时刻自动扫描一次。"""
    logging.info("进入常驻模式，每日扫描时刻: %s", ",".join(SCAN_TIMES))
    pool = None
    last_pool_date = None
    while True:
        now = datetime.now()
        if now.weekday() < 5 and now.strftime("%H:%M") in SCAN_TIMES:
            if last_pool_date != now.date():      # 每天刷新一次股票池
                pool = load_pool(args)
                last_pool_date = now.date()
            notify(scan(pool, label=pool_tag(args), side=args.side,
                        source=args.source or DATA_SOURCE),
                   pool_label(args), side=args.side)
            time.sleep(61)                        # 跳过当前这一分钟
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Fisher 60分钟线上穿/下穿扫描器")
    parser.add_argument("--once", action="store_true", help="扫描一次后退出")
    parser.add_argument("--loop", action="store_true", help="常驻定时扫描")
    parser.add_argument("--pool-file", help="自定义股票池 CSV（需含 code 列）")
    parser.add_argument("--limit", type=int, help="只扫描前 N 只（调试）")
    parser.add_argument("--side", choices=["up", "down"], default="up",
                        help="up=上穿（默认，选股），down=下穿（持仓监控）")
    parser.add_argument("--source", choices=["sina", "gm", "em", "tdx"], default=None,
                        help="数据源（缺省用配置区 DATA_SOURCE）；gm 须在 .venv-gm 环境运行")
    parser.add_argument("--buy", metavar="CODE", help="登记买入到 holdings.csv 后退出")
    parser.add_argument("--price", type=float, help="买入价（配合 --buy，缺省取最新价）")
    parser.add_argument("--sell", metavar="CODE", help="清仓：移出持仓并加入观察池后退出")
    parser.add_argument("--watch", metavar="CODE", help="加入观察池（盯 60 分钟上穿回钩）后退出")
    parser.add_argument("--live", action="store_true",
                        help="盘中信号：未完结的 60 分钟 bar 也参与判定（可能收盘前消失/翻转）")
    args = parser.parse_args()

    source = args.source or DATA_SOURCE
    if args.buy:
        buy(args.buy, args.price)
        return
    if args.sell:
        sell(args.sell)
        return
    if args.watch:
        watch(args.watch)
        return
    if args.loop:
        run_loop(args)
    else:  # 默认 --once
        if args.pool_file and not Path(args.pool_file).exists():
            logging.info("股票池文件 %s 不存在，跳过扫描", args.pool_file)
            return
        pool = load_pool(args)
        if len(pool) == 0:
            logging.info("股票池为空，跳过扫描")
            return
        notify(scan(pool, label=pool_tag(args), side=args.side, source=source, live=args.live),
               pool_label(args), side=args.side)


if __name__ == "__main__":
    main()
