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
盘中（--live）信号在 bar 中段运行（10:01 / 11:01 / 13:31 / 14:31，对应 9:30-10:30 / 10:30-11:30 / 13:00-14:00 / 14:00-15:00 四根 bar 的中点），持仓每 15 分钟。

用法：
    python fisher_scanner.py --once                      # 扫一次退出（配合 cron / 任务计划）
    python fisher_scanner.py --loop                      # 常驻，每日 4 个时点自动扫描
    python fisher_scanner.py --once --limit 50           # 只扫前 50 只（调试用）
    python fisher_scanner.py --once --pool-file pool.csv # 自定义股票池（CSV 需含 code 列）
    python fisher_scanner.py --once --pool-file holdings.csv --side down  # 持仓下穿监控
    python fisher_scanner.py --buy 600036 --price 38.9     # 登记买入到 holdings.csv
    python fisher_scanner.py --inspect 600498 [--push]     # 单票体检（支持中文名），--push 推送到企业微信
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
ESI_LEDGER = Path(__file__).resolve().parent / "cache" / "esi_ledger.csv"  # Fisher-ESI 进出场台账
ESI_WINDOW_BARS = 6           # 失效判定窗口：进场后 6 根完结 30m bar（3 交易小时）
ESI_ENTRY_FISHER_MAX = 2.5    # 进场登记：上穿时 60m fisher 上限
# 30m bar 收盘后判定窗口（每 15 分钟持仓任务网格上）：当前时刻落在区间内才跑失效判定
ESI_JUDGE_WINDOWS = [("10:01", "10:15"), ("10:31", "10:45"), ("11:01", "11:15"),
                     ("11:31", "11:45"), ("13:31", "13:45"), ("14:01", "14:15"),
                     ("14:31", "14:45"), ("15:01", "15:15")]
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
_TDX_BAR_ENDS_30 = ["10:00", "10:30", "11:00", "11:30",
                    "13:30", "14:00", "14:30", "15:00"]  # 30 分钟 bar 收盘时刻（当日第 1~8 根）


def _tdx_map_bar_times(df, bar_ends):
    """tdx 分钟线盘中时间戳有跨午休怪癖（如 60m 第二根标 13:00），不读原始时间戳，
    按「当日第几根」映射到固定收盘时刻。bar_ends 的元素数必须等于每日 bar 数。"""
    dates = df["时间"].dt.date
    for d in dates.unique():
        idx = df.index[dates == d]
        for k, i in enumerate(idx):
            if k < len(bar_ends):
                hh, mm = bar_ends[k].split(":")
                df.at[i, "时间"] = datetime(d.year, d.month, d.day, int(hh), int(mm))


def _fetch_min_tdx(code, category, count, bar_ends):
    """通达信（xmtdx）分钟 K 线 + 近似前复权（乘新浪日线因子，当日缓存）。

    复权：tdx 原始数据是不复权的，乘新浪日线前复权因子（当日缓存，与 sina 分支共用
    cache/daily_qfq/，按 bar 日期逐日映射——除权日缺口会被因子抹平，避免 Fisher 被
    跳空打到极端区产生假信号。因子获取失败（如新浪限流）时本股退回不复权，
    不置 FAIL、不触发通道降级。
    """
    from xmtdx import TdxClient, Market
    market = Market.SH if code.startswith(("5", "6")) else Market.SZ
    with TdxClient.from_best_host(ping_timeout=3.0) as c:
        bars = c.get_security_bars(market, code, category, 0, count)
    if not bars:
        return None
    df = pd.DataFrame([{"时间": datetime(b.year, b.month, b.day),
                        "最高": b.high, "最低": b.low, "收盘": b.close} for b in bars])
    _tdx_map_bar_times(df, bar_ends)
    try:
        fac = _daily_qfq_factor(code)                     # 新浪日线前复权因子（当日缓存）
        fmap = dict(zip(fac["date"], fac["factor"]))
        factor = df["时间"].dt.strftime("%Y-%m-%d").map(fmap).ffill().fillna(1.0)
        for col in ("最高", "最低", "收盘"):
            df[col] = df[col].astype(float) * factor.values
    except Exception as e:
        logging.warning("%s 复权因子获取失败，本股退回不复权数据: %s", code, e)
    return df


def _fetch_60m_tdx(code, count=200):
    """通达信 60 分钟 K 线（见 _fetch_min_tdx 复权/时间映射说明）。
    count 单次上限 800（≈200 交易日），HSSR 回测用 800 深历史。"""
    from xmtdx import KlineCategory
    return _fetch_min_tdx(code, KlineCategory.MIN_60, count, _TDX_BAR_ENDS)


def _fetch_30m_tdx(code):
    """通达信 30 分钟 K 线（见 _fetch_min_tdx 复权/时间映射说明）。
    count=600（单次上限 800）：30m 每天 8 根，约 75 个交易日，保证 Fisher 暖机。"""
    from xmtdx import KlineCategory
    return _fetch_min_tdx(code, KlineCategory.MIN_30, 600, _TDX_BAR_ENDS_30)

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


def _fetch_30m_gm(code):
    """掘金 30 分钟前复权 K 线（frequency='1800s'）。须 .venv-gm 环境。"""
    global _gm_ready
    from gm.api import history, ADJUST_PREV, set_token
    if not _gm_ready:
        token_file = Path(__file__).resolve().parent / "gm_token.key"
        set_token(token_file.read_text(encoding="utf-8").strip())
        _gm_ready = True
    end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    df = history(symbol=_gm_symbol(code), frequency="1800s",
                 start_time=start, end_time=end, adjust=ADJUST_PREV, df=True)
    if df is None or len(df) == 0:
        return None
    df = df.rename(columns={"eob": "时间", "high": "最高", "low": "最低", "close": "收盘"})
    df["时间"] = pd.to_datetime(df["时间"]).dt.tz_localize(None)
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


def _fetch_min_sina(code, scale):
    """新浪分钟线前复权：分钟线 jsonp（1 次请求）× 本地缓存的日线因子。

    与 akshare stock_zh_a_minute(adjust='qfq') 的计算口径一致（同一交易日逐位相同），
    但每股只需 1 次 HTTP 请求（akshare 原版要 5 次）。
    """
    import json
    import requests
    symbol = _sina_symbol(code)
    r = requests.get(SINA_KLINE_URL,
                     params={"symbol": symbol, "scale": scale, "ma": "no", "datalen": 1970},
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


def _fetch_60m_sina(code):
    return _fetch_min_sina(code, 60)


def _fetch_30m_sina(code):
    return _fetch_min_sina(code, 30)


def fetch_60m(code, source=None, count=None):
    """拉取单只股票的 60 分钟前复权 K 线，带重试。失败返回 None。

    source: "tdx"（默认，近似前复权：乘新浪日线因子）/ "sina" / "gm"（须在 .venv-gm 运行）/ "em"（东财）。
    count: 仅 tdx 源生效（单次上限 800），None 时用各源默认深度。
    """
    source = source or DATA_SOURCE
    for k in range(RETRY):
        try:
            if source == "gm":
                df = _fetch_60m_gm(code)
                if df is not None and len(df) > 0:
                    return df
            elif source == "tdx":
                df = _fetch_60m_tdx(code, count) if count else _fetch_60m_tdx(code)
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


def fetch_30m(code, source=None):
    """拉取单只股票的 30 分钟前复权 K 线，带重试。失败返回 None。
    结构同 fetch_60m；tdx 用 MIN_30/count=600，sina 用 scale=30。"""
    source = source or DATA_SOURCE
    for k in range(RETRY):
        try:
            if source == "gm":
                df = _fetch_30m_gm(code)
                if df is not None and len(df) > 0:
                    return df
            elif source == "tdx":
                df = _fetch_30m_tdx(code)
                if df is not None and len(df) > 0:
                    return df
            elif source == "sina":
                df = _fetch_30m_sina(code)
                if df is not None and len(df) > 0:
                    return df
            else:
                import akshare as ak
                df = ak.stock_zh_a_hist_min_em(symbol=code, period="30", adjust="qfq")
                if df is not None and len(df) > 0:
                    return df
        except Exception as e:
            logging.warning("%s 30m 第 %d 次拉取失败: %s", code, k + 1, e)
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
            merge_cols = ["code", "score"] + [c for c in ("hssr", "hssr_n")
                                              if c in pool.columns]
            result = result.merge(pool[merge_cols], on="code", how="left")
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


def fetch_daily_sina(code):
    """新浪日线前复权 OHLC（akshare stock_zh_a_daily, adjust="qfq"），升序返回。
    失败或数据不足（< MIN_BARS 的一半）返回 None。"""
    import akshare as ak
    try:
        df = ak.stock_zh_a_daily(symbol=_sina_symbol(code), adjust="qfq")
    except Exception as e:
        logging.warning("%s 日线拉取失败: %s", code, e)
        return None
    if df is None or len(df) < MIN_BARS // 2:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close"]]


def daily_crossed_up(code):
    """当日已完结日 K 是否刚完成 Fisher 上穿（fisher_transform + just_crossed_up 判最后一根）。
    取数失败返回 False 并 log warning。"""
    df = fetch_daily_sina(code)
    time.sleep(REQUEST_INTERVAL)   # 新浪限速
    if df is None:
        logging.warning("%s 日线数据不足，按未上穿处理", code)
        return False
    high = pd.to_numeric(df["high"], errors="coerce").values
    low = pd.to_numeric(df["low"], errors="coerce").values
    fish, trig = fisher_transform(high, low, FISHER_LEN)
    return just_crossed_up(fish, trig, len(df) - 1)


def scan_one_deep_resonance(row, source=None):
    """深水日共振单票复核：当天任一根已完结 60m bar 上穿，且当日日 K 上穿。
    命中返回 dict；不满足返回 'SKIP'；60m 取数失败返回 'FAIL'。"""
    code, name = str(row["code"]), row["name"]
    df = fetch_60m(code, source)
    time.sleep(REQUEST_INTERVAL)
    if df is None:
        return "FAIL"
    if len(df) < MIN_BARS:
        return "SKIP"
    high = pd.to_numeric(df["最高"], errors="coerce").values
    low = pd.to_numeric(df["最低"], errors="coerce").values
    fish, trig = fisher_transform(high, low, FISHER_LEN)
    times = pd.to_datetime(df["时间"])
    today = datetime.now().date()
    today_idx = [i for i in range(len(df)) if times.iloc[i].date() == today]
    first_hit = next((i for i in today_idx if just_crossed_up(fish, trig, i)), None)
    if first_hit is None:
        return "SKIP"
    if not daily_crossed_up(code):
        return "SKIP"
    daily = fetch_daily_sina(code)   # 已上过穿：再取一次日线拿 fisher/trigger 末值
    if daily is None:
        return "SKIP"
    d_high = pd.to_numeric(daily["high"], errors="coerce").values
    d_low = pd.to_numeric(daily["low"], errors="coerce").values
    d_fish, d_trig = fisher_transform(d_high, d_low, FISHER_LEN)
    return {
        "code": code, "name": name,
        "bar_time": str(df["时间"].iloc[first_hit]),
        "close": float(pd.to_numeric(df["收盘"], errors="coerce").iloc[first_hit]),
        "fisher": round(float(d_fish[-1]), 3),
        "trigger": round(float(d_trig[-1]), 3),
        "bar_state": "日共振",
    }


def scan_deep_resonance(pool, source=None):
    """深水池日共振收盘复核：并发扫描，写 results/fisher_cross_%Y%m%d_%H%M_deepres.csv。

    与 scan() 同用进程池（py_mini_racer 多线程会崩解释器，勿改线程池）。
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    hits, fails, skips, done = [], 0, 0, 0
    t0 = time.time()
    n = len(pool)
    rows = [row for _, row in pool.iterrows()]
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(scan_one_deep_resonance, row, source) for row in rows]
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
                logging.warning("前 %d 只全部失败，数据源疑似不可用，提前终止本池扫描", done)
                for f in futures:
                    f.cancel()
                break

    result = pd.DataFrame(hits)
    if len(result):
        if "score" in pool.columns:   # 深水池带打分：附分数并按分数降序
            result = result.merge(pool[["code", "score"]], on="code", how="left")
            result = result.sort_values("score", ascending=False).reset_index(drop=True)
        else:
            result = result.sort_values("code").reset_index(drop=True)
    result.attrs["total"] = done
    result.attrs["fails"] = fails
    elapsed = time.time() - t0
    logging.info("深水日共振复核完成：%d 只耗时 %.1f 分钟，命中 %d 只，失败 %d 只，跳过 %d 只",
                 n, elapsed / 60, len(result), fails, skips)
    RESULT_DIR.mkdir(exist_ok=True)
    out = RESULT_DIR / ("fisher_cross_%s_deepres.csv" % datetime.now().strftime("%Y%m%d_%H%M"))
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
    同一根 bar 的同一信号按完结状态去重：盘中（未完结）推过后，收盘完结确认仍会再推一次；
    同一状态同一 bar 当日只推一次。去重键末段为 bar_state；兼容当日旧的 5 段键（不带状态）。
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
        # 去重：同一根 bar 的同一信号按完结状态当日只推一次（盘中推过，收盘确认仍再推）
        pushed = _load_pushed()
        today = datetime.now().strftime("%Y-%m-%d")
        keys = ["%s|%s|%s|%s|%s|%s" % (today, pond, side, r["code"], r["bar_time"],
                                       r.get("bar_state", ""))
                for _, r in result.iterrows()]
        legacy = [k.rsplit("|", 1)[0] for k in keys]   # 兼容当日此前写入的 5 段旧键
        keep = [i for i, k in enumerate(keys)
                if k not in pushed and legacy[i] not in pushed]
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
                text = "%s %s（%s）%s" % (r["code"], r["name"], r["score"], tag)
                if "hssr" in result.columns:   # 深水池 HSSR 档位（建池时预计算）
                    h, n_sig = r.get("hssr"), r.get("hssr_n")
                    if h is None or pd.isna(h) or n_sig is None or pd.isna(n_sig):
                        text += " HSSR 样本不足 (n<5)"
                    else:
                        h, n_sig = float(h), int(n_sig)
                        wins = int(round(h * n_sig / 100))
                        tier = "正常仓位" if h >= 75 else ("仓位减半" if h >= 50 else "不建议买入")
                        text += " HSSR %.0f%% (%d/%d) · %s" % (h, wins, n_sig, tier)
                return text
            return "%s %s%s" % (r["code"], r["name"], tag)

        lines = [title] + [_row_text(r) for _, r in result.head(PUSH_MAX_ROWS).iterrows()]
        if len(result) > PUSH_MAX_ROWS:
            lines.append("……共 %d 只，完整清单见 results CSV" % len(result))
        content = "\n".join(lines)
        should_push = True
    pending_keys = keys if (should_push and len(result)) else None
    if WECOM_WEBHOOK and should_push:
        try:
            import requests
            r = requests.post(WECOM_WEBHOOK, timeout=10,
                              json={"msgtype": "markdown",
                                    "markdown": {"content": content}})
            if r.json().get("errcode") != 0:
                logging.warning("企业微信推送返回异常: %s", r.text[:200])
            elif pending_keys:
                # 推送成功才记录去重键：推送失败不记账，下次扫描会重试推送
                _save_pushed(pushed | set(pending_keys))
        except Exception as e:
            logging.warning("企业微信推送失败: %s", e)
    elif pending_keys:
        _save_pushed(pushed | set(pending_keys))


def push_text(text):
    """直接推送一条 markdown 文本到企业微信（HSSR 周报等非 scan 结果用）。"""
    print(text)
    if WECOM_WEBHOOK:
        try:
            import requests
            r = requests.post(WECOM_WEBHOOK, timeout=10,
                              json={"msgtype": "markdown", "markdown": {"content": text}})
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


# ----------------------- Fisher-ESI 早期失效规则（持仓版） -----------------------
# 进场：持仓票完结 60m bar Fisher 上穿且 0 < fisher < 2.5 → 台账登记 open。
# 失效：进场起 6 根完结 30m bar 窗口内，完结 30m bar 下穿 Trigger 且最新 60m bar
# （允许未完结）fisher > 0 且环比下降 → Fisher-Fail（推送「Fisher失效」平仓预警）。
# 窗口内存活满 6 根未失效 → 记成功 closed。HSSR = 近 20 条 closed 中未失效占比，周日推送。
ESI_COLUMNS = ["code", "entry_time", "entry_fisher60", "fail_time",
               "fail_fisher30", "failed", "bars_held", "status"]


def load_esi_ledger():
    """读 ESI 台账；不存在则返回带表头的空表。
    字符串列统一成 object 并把 NaN 归一为 ""：台账多为空单元格，pandas 3.x 下
    全空列会读成 float64，之后 at/loc 写入中文/时间串会抛 LossySetitemError。"""
    if ESI_LEDGER.exists():
        df = pd.read_csv(ESI_LEDGER, dtype={"code": str})
        for col in ("entry_time", "fail_time", "failed", "status"):
            if col in df.columns:
                df[col] = df[col].astype(object).where(df[col].notna(), "")
        return df
    return pd.DataFrame(columns=ESI_COLUMNS)


def save_esi_ledger(df):
    ESI_LEDGER.parent.mkdir(exist_ok=True)
    df.to_csv(ESI_LEDGER, index=False, encoding="utf-8-sig")


def esi_register_entries(hits):
    """持仓 60m 上穿命中的进场登记。返回新登记条数。

    只登记完结 bar（盘中未完结信号不算进场），且 0 < fisher < ESI_ENTRY_FISHER_MAX；
    台账已有该 code 的 open 记录、或当日已有 failed 记录（当日禁止重新开仓）则跳过。
    """
    if hits is None or len(hits) == 0:
        return 0
    led = load_esi_ledger()
    today = datetime.now().strftime("%Y-%m-%d")
    open_codes, failed_today = set(), set()
    if len(led):
        open_codes = set(led.loc[led["status"] == "open", "code"].astype(str))
        f = led[led["failed"].fillna("") == "是"]
        if len(f):
            fdate = pd.to_datetime(f["fail_time"], errors="coerce").dt.strftime("%Y-%m-%d")
            failed_today = set(f.loc[fdate == today, "code"].astype(str))
    n = 0
    for _, r in hits.iterrows():
        if r.get("bar_state") != "完结":
            continue
        fisher = float(r.get("fisher", 0))
        if not (0 < fisher < ESI_ENTRY_FISHER_MAX):
            continue
        code = str(r["code"]).zfill(6)
        if code in open_codes or code in failed_today:
            continue
        led = pd.concat([led, pd.DataFrame([{
            "code": code, "entry_time": str(r["bar_time"]),
            "entry_fisher60": round(fisher, 3),
            "fail_time": "", "fail_fisher30": "",
            "failed": "", "bars_held": 0, "status": "open",
        }])], ignore_index=True)
        open_codes.add(code)
        n += 1
    if n:
        save_esi_ledger(led)
        logging.info("ESI 台账新登记 %d 条 open 记录", n)
    return n


def esi_check_invalidation(now=None):
    """ESI 失效判定（30m 收盘后由 run_scan --holdings 门控调用）。

    台账无 open 记录直接返回空表（零取数）。每条 open 记录：
    fetch_30m 后按 bar_time > entry_time 计数已完结 30m bar；
    超过 6 根 → 记成功 closed（failed=否, bars_held=6）；
    窗口内判最后一根完结 30m bar just_crossed_down，命中再 fetch_60m 比较最新 bar
    （允许未完结）fisher > 0 且 < 前一根 → 记 failed=是 closed，收集进结果推送。
    返回失效命中 DataFrame（带 attrs，风格同 scan）。
    """
    now = now or datetime.now()
    led = load_esi_ledger()
    opens = led[led["status"] == "open"]
    if len(opens) == 0:
        result = pd.DataFrame()
        result.attrs["total"] = 0
        result.attrs["fails"] = 0
        return result
    names = _load_list("holdings.csv").set_index("code")["name"].to_dict()
    hits = []
    changed = False
    for i, rec in opens.iterrows():
        code = str(rec["code"]).zfill(6)
        entry_time = pd.to_datetime(rec["entry_time"])
        df = fetch_30m(code)
        time.sleep(REQUEST_INTERVAL)
        if df is None or len(df) < MIN_BARS:
            logging.warning("ESI: %s 30m 取数失败/不足，本次跳过判定", code)
            continue
        times = pd.to_datetime(df["时间"])
        done = [k for k in range(len(df))
                if entry_time < times.iloc[k] <= now]   # 进场后已完结的 30m bar
        bars_held = len(done)
        if bars_held > ESI_WINDOW_BARS:                  # 窗口存活：记成功
            led.at[i, "bars_held"] = ESI_WINDOW_BARS
            led.at[i, "failed"] = "否"
            led.at[i, "status"] = "closed"
            changed = True
            logging.info("ESI: %s 窗口存活满 %d 根 30m bar，记成功", code, ESI_WINDOW_BARS)
            continue
        led.at[i, "bars_held"] = bars_held
        changed = True
        if bars_held == 0:
            continue
        high = pd.to_numeric(df["最高"], errors="coerce").values
        low = pd.to_numeric(df["最低"], errors="coerce").values
        fish, trig = fisher_transform(high, low, FISHER_LEN)
        j = done[-1]                                     # 最后一根完结 30m bar
        if not just_crossed_down(fish, trig, j):
            if bars_held == ESI_WINDOW_BARS:             # 第 6 根也未失效：记成功
                led.at[i, "failed"] = "否"
                led.at[i, "status"] = "closed"
                logging.info("ESI: %s 窗口存活满 %d 根 30m bar，记成功", code, ESI_WINDOW_BARS)
            continue
        df60 = fetch_60m(code)                           # 大周期确认：未死但环比降低
        time.sleep(REQUEST_INTERVAL)
        if df60 is None or len(df60) < 3:
            logging.warning("ESI: %s 60m 取数失败，本次跳过判定", code)
            continue
        h60 = pd.to_numeric(df60["最高"], errors="coerce").values
        l60 = pd.to_numeric(df60["最低"], errors="coerce").values
        fish60, _ = fisher_transform(h60, l60, FISHER_LEN)
        cur, prev = fish60[-1], fish60[-2]               # 最新 bar 允许未完结，取当前值
        if not (cur > 0 and cur < prev):
            if bars_held == ESI_WINDOW_BARS:
                led.at[i, "failed"] = "否"
                led.at[i, "status"] = "closed"
                logging.info("ESI: %s 窗口存活满 %d 根 30m bar，记成功", code, ESI_WINDOW_BARS)
            continue
        led.at[i, "fail_time"] = str(df["时间"].iloc[j])
        led.at[i, "fail_fisher30"] = round(float(fish[j]), 3)
        led.at[i, "failed"] = "是"
        led.at[i, "status"] = "closed"
        changed = True
        hits.append({
            "code": code, "name": names.get(code, ""),
            "bar_time": str(df["时间"].iloc[j]),
            "close": float(pd.to_numeric(df["收盘"], errors="coerce").iloc[j]),
            "fisher": round(float(fish[j]), 3),
            "trigger": round(float(trig[j]), 3),
            "bar_state": "失效",
        })
        logging.info("ESI: %s Fisher-Fail（30m 下穿 @%s，60m fisher %.3f→%.3f）",
                     code, df["时间"].iloc[j], prev, cur)
    if changed:
        save_esi_ledger(led)
    result = pd.DataFrame(hits)
    result.attrs["total"] = len(opens)
    result.attrs["fails"] = 0
    return result


def hssr_report(now=None):
    """HSSR 周报文本：按 code 取最近 20 条 closed 台账记录，HSSR = 未失效数/总数，
    附平均失效分钟数。无数据时返回提示文案（也推送）。"""
    now = now or datetime.now()
    led = load_esi_ledger()
    lines = ["**Fisher-ESI 周报（%s）**" % now.strftime("%Y-%m-%d")]
    closed = led[led["status"] == "closed"] if len(led) else led
    if len(closed) == 0:
        lines.append("暂无已完结台账记录，HSSR 无法计算。")
        return "\n".join(lines)
    names = _load_list("holdings.csv").set_index("code")["name"].to_dict()
    for code, g in closed.groupby("code"):
        g = g.tail(20)                                   # 滚动 20 条
        total = len(g)
        ok = int((g["failed"].fillna("") == "否").sum())
        line = "%s %s：HSSR %.0f%%（%d/%d）" % (
            code, names.get(str(code).zfill(6), ""), ok / total * 100, ok, total)
        f = g[g["failed"].fillna("") == "是"]
        if len(f):
            mins = (pd.to_datetime(f["fail_time"], errors="coerce")
                    - pd.to_datetime(f["entry_time"], errors="coerce")).dt.total_seconds() / 60
            mins = mins.dropna()
            if len(mins):
                line += "，平均失效 %.0f 分钟" % mins.mean()
        lines.append(line)
    return "\n".join(lines)


# ----------------------- 深水池 60m 历史信号成功率（HSSR 预计算） -----------------------
# 价格版口径：历史完结 60m bar 上穿信号（just_crossed_up，无 ESI 过滤）出现后
# N_HOLD 根 bar close 上涨记成功；取最近 N_SIGNALS 次可评估信号（最后 N_HOLD 根
# 内的信号尚无足够后续 bar，剔除）；样本 < 5 记样本不足。
# 深历史走 tdx 800 根（≈200 交易日）；gm 60m 批量拉取有配额坑（status 1014），不可用于此。
HSSR_N_HOLD = 10        # 成功判定持有窗口（60m bar 数）
HSSR_N_SIGNALS = 20     # 统计窗口：最近 N 次可评估信号
HSSR_MIN_SAMPLE = 5     # 可评估样本少于此数视为样本不足（hssr 留空）


def compute_hssr(code, n_hold=HSSR_N_HOLD, n_signals=HSSR_N_SIGNALS, source="tdx"):
    """单只股票 60m 历史上穿信号成功率。返回 (hssr 百分数或 None, n_eval)。

    样本不足（n_eval < HSSR_MIN_SAMPLE）返回 (None, n_eval)；取数失败返回 (None, 0)。
    source: tdx（默认，800 根深历史）/ sina（夜间 fallback，串行防限流）。
    """
    df = fetch_60m(code, source=source, count=800)
    # sina 限流严格，拉长间隔；tdx 保持原速
    time.sleep(1.5 if source == "sina" else REQUEST_INTERVAL)
    if df is None:
        return None, 0
    high = pd.to_numeric(df["最高"], errors="coerce").values
    low = pd.to_numeric(df["最低"], errors="coerce").values
    close = pd.to_numeric(df["收盘"], errors="coerce").values
    fish, trig = fisher_transform(high, low, FISHER_LEN)
    sigs = [j for j in range(2, len(df) - n_hold)
            if just_crossed_up(fish, trig, j)]   # 最后 n_hold 根内的信号不可评估
    sigs = sigs[-n_signals:]
    n_eval = len(sigs)
    if n_eval < HSSR_MIN_SAMPLE:
        return None, n_eval
    wins = sum(1 for j in sigs if close[j + n_hold] > close[j])
    return round(wins / n_eval * 100, 1), n_eval


def annotate_pool_hssr(pool_file="pool_deep.csv", source="tdx"):
    """建池后注解步骤：为池内每只股票预计算 HSSR，新增 hssr/hssr_n 两列写回原文件。

    主环境 .venv 运行；整体 try/except，任何异常保留原文件不破坏。
    hssr 为百分数数值（样本不足留空），hssr_n 为可评估样本数。
    source: tdx（默认，进程池并发）/ sina（串行防限流）/ auto（tdx 失败后 sina 兜底）。
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    path = Path(__file__).resolve().parent / pool_file
    try:
        if not path.exists():
            logging.warning("HSSR 注解：%s 不存在，跳过", pool_file)
            return
        pool = pd.read_csv(path, dtype={"code": str})
        n = len(pool)
        t0 = time.time()
        results = {}

        def _run(src, codes):
            if not codes:
                return
            workers = 1 if src == "sina" else WORKERS
            done = 0
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(compute_hssr, str(c), source=src): str(c) for c in codes}
                for fut in as_completed(futures):
                    results[futures[fut]] = fut.result()
                    done += 1
                    if done % 20 == 0:
                        logging.info("HSSR 注解进度 %d/%d (%s)", done, len(codes), src)

        if source == "auto":
            _run("tdx", pool["code"].tolist())
            failed = [c for c in pool["code"] if results.get(str(c), (None, 0))[0] is None]
            if failed:
                logging.info("tdx 失败/样本不足 %d 只，切 sina 兜底", len(failed))
                _run("sina", failed)
        else:
            _run(source, pool["code"].tolist())

        pool["hssr"] = pool["code"].map(lambda c: results.get(str(c), (None, 0))[0])
        pool["hssr_n"] = pool["code"].map(lambda c: results.get(str(c), (None, 0))[1])
        pool.to_csv(path, index=False, encoding="utf-8-sig")
        ok = pool["hssr"].notna().sum()
        logging.info("HSSR 注解完成：%d 只耗时 %.1f 分钟，有效 %d 只，样本不足/失败 %d 只，已写回 %s",
                     n, (time.time() - t0) / 60, ok, n - ok, pool_file)
    except Exception as e:
        logging.error("HSSR 注解失败，保留原 %s 不动: %s", pool_file, e)


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


def unwatch(code):
    """把股票移出 watchlist.csv（停止监控）。"""
    code = str(code).zfill(6)
    w = _load_list("watchlist.csv")
    row = w[w["code"] == code]
    if len(row) == 0:
        print("%s 不在观察池中" % code)
        return
    _save_list("watchlist.csv", w[w["code"] != code].reset_index(drop=True))
    print("已移出观察池: %s %s" % (code, row["name"].iloc[0]))


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


# ----------------------- 单票体检（--inspect） -----------------------
INSPECT_POOL_FILES = ["holdings.csv", "watchlist.csv", "pool_right.csv",
                      "pool_left.csv", "pool_deep.csv", "pool_t0.csv", "pool_t1.csv"]


def _inspect_resolve(arg):
    """--inspect 参数解析：6 位数字直接用；否则按名称先从各池/持仓/观察池 CSV 反查，
    查不到再用新浪全市场快照反查代码。返回 (code, name)；都查不到返回 (None, None)。"""
    arg = str(arg).strip()
    if arg.isdigit() and len(arg) == 6:
        return arg, ""
    for f in INSPECT_POOL_FILES:
        df = _load_list(f) if f in ("holdings.csv", "watchlist.csv") else (
            pd.read_csv(Path(__file__).resolve().parent / f, dtype={"code": str})
            if (Path(__file__).resolve().parent / f).exists() else None)
        if df is None or "name" not in df.columns:
            continue
        hit = df[df["name"] == arg]
        if len(hit):
            return str(hit["code"].iloc[0]).zfill(6), arg
    # CSV 都查不到：用新浪全市场快照按名称反查（约 20 秒，手动低频操作可接受）
    try:
        import akshare as ak
        spot = ak.stock_zh_a_spot()
        hit = spot[spot["名称"] == arg]
        if len(hit):
            return str(hit["代码"].iloc[0])[2:], arg
    except Exception as e:
        logging.warning("名称 %s 快照反查失败: %s", arg, e)
    return None, None


def inspect_stock(arg, push=False, source=None):
    """单票体检：输出该票在本系统里的完整画像（身份/日线/60m/30m+ESI/HSSR）。
    每项独立 try 降级；push=True 时用 push_text 推送 markdown 到企业微信。"""
    code, name = _inspect_resolve(arg)
    if not code:
        print("未在 holdings/watchlist/各池 CSV 中找到「%s」，无法体检" % arg)
        return
    now = datetime.now()
    lines = ["**单票体检：%s %s（%s）**" % (code, name or "?", now.strftime("%Y-%m-%d %H:%M"))]
    df60 = None      # 身份段若已拉过 60m（算浮盈），60m 段复用不重复拉取

    # 1. 身份：持仓 / 观察池 / 五个池
    try:
        sec = ["【身份】"]
        h = _load_list("holdings.csv")
        row = h[h["code"].astype(str).str.zfill(6) == code]
        holding_price = None
        if len(row):
            r = row.iloc[0]
            name = name or str(r.get("name", ""))
            sec.append("持仓：是（买入 %s @ %s）" % (r.get("buy_date", ""), r.get("buy_price", "")))
            holding_price = pd.to_numeric(r.get("buy_price"), errors="coerce")
        else:
            sec.append("持仓：否")
        w = _load_list("watchlist.csv")
        wr = w[w["code"].astype(str).str.zfill(6) == code]
        sec.append("观察池：%s" % ("是" if len(wr) else "否"))
        if not name and len(wr):
            name = str(wr.iloc[0].get("name", ""))
        pool_names = {"pool_right.csv": "右侧池", "pool_left.csv": "左侧池",
                      "pool_deep.csv": "深水池", "pool_t0.csv": "T0 ETF池",
                      "pool_t1.csv": "T1 ETF池"}
        base = Path(__file__).resolve().parent
        for f, label in pool_names.items():
            p = base / f
            if not p.exists():
                sec.append("%s：文件不存在" % label)
                continue
            df = pd.read_csv(p, dtype={"code": str})
            hit = df[df["code"].astype(str).str.zfill(6) == code]
            if len(hit) == 0:
                sec.append("%s：不在池中" % label)
                continue
            r = hit.iloc[0]
            extra = ""
            if f == "pool_deep.csv":
                parts = []
                if "score" in df.columns and pd.notna(r.get("score")):
                    parts.append("score=%s" % r["score"])
                if "hssr" in df.columns:
                    hv, hn = r.get("hssr"), r.get("hssr_n")
                    if pd.notna(hv) and pd.notna(hn):
                        parts.append("hssr=%.1f%% (n=%d)" % (float(hv), int(hn)))
                    else:
                        parts.append("HSSR 样本不足 (n<5)")
                extra = "（%s）" % "，".join(parts) if parts else ""
            sec.append("%s：在池中%s" % (label, extra))
            if not name and "name" in df.columns:
                name = str(r.get("name", ""))
        lines.append("\n".join(sec))
        if holding_price is not None and not pd.isna(holding_price):
            # 现价用 60m 最新收盘近似（60m 拉取失败则在 60m 段再说明）
            df60 = fetch_60m(code, source)
            if df60 is not None and len(df60):
                cur = float(pd.to_numeric(df60["收盘"], errors="coerce").iloc[-1])
                pnl = (cur / float(holding_price) - 1) * 100
                lines.append("现价 %.2f，成本 %.2f，浮盈 %+.1f%%"
                             % (cur, float(holding_price), pnl))
    except Exception as e:
        lines.append("【身份】取数失败: %s" % e)
    if not name:
        try:
            name = _lookup_name_price(code)[0] or ""   # 纯代码输入时补查名称（快照约 20 秒）
        except Exception:
            pass

    # 2. 日线
    try:
        ddf = fetch_daily_sina(code)
        if ddf is None:
            lines.append("【日线】取数失败或数据不足")
        else:
            high = pd.to_numeric(ddf["high"], errors="coerce").values
            low = pd.to_numeric(ddf["low"], errors="coerce").values
            fish, trig = fisher_transform(high, low, FISHER_LEN)
            j = len(ddf) - 1
            sec = ["【日线】（共 %d 根，最新 %s）" % (len(ddf), ddf["date"].iloc[-1]),
                   "fisher %.3f / trigger %.3f，收盘 %.2f"
                   % (fish[j], trig[j], float(pd.to_numeric(ddf["close"], errors="coerce").iloc[j])),
                   "深水条件 fisher<-2：%s" % ("满足" if fish[j] < -2 else "不满足"),
                   "最后一根日 K 上穿：%s" % ("是" if just_crossed_up(fish, trig, j) else "否")]
            lines.append("\n".join(sec))
    except Exception as e:
        lines.append("【日线】取数失败: %s" % e)

    # 3. 60m
    try:
        if df60 is None:
            df60 = fetch_60m(code, source)
        if df60 is None or len(df60) < MIN_BARS:
            lines.append("【60m】取数失败或 bar 不足（%s 根）"
                         % (len(df60) if df60 is not None else "0"))
        else:
            high = pd.to_numeric(df60["最高"], errors="coerce").values
            low = pd.to_numeric(df60["最低"], errors="coerce").values
            fish, trig = fisher_transform(high, low, FISHER_LEN)
            times = pd.to_datetime(df60["时间"])
            jc = len(df60) + signal_bar_index(df60, now=now, live=False)
            jl = len(df60) + signal_bar_index(df60, now=now, live=True)
            up_at = next((str(times.iloc[k]) for k in range(len(df60) - 1, 1, -1)
                          if just_crossed_up(fish, trig, k)), None)
            down_at = next((str(times.iloc[k]) for k in range(len(df60) - 1, 1, -1)
                            if just_crossed_down(fish, trig, k)), None)
            sec = ["【60m】（共 %d 根）" % len(df60),
                   "最近完结 bar %s：fisher %.3f / trigger %.3f"
                   % (times.iloc[jc], fish[jc], trig[jc]),
                   "最近一次上穿：%s" % (up_at or "样本内无"),
                   "最近一次下穿：%s" % (down_at or "样本内无"),
                   "当前信号（完结口径）：%s上穿 / %s下穿"
                   % ("" if just_crossed_up(fish, trig, jc) else "未",
                      "" if just_crossed_down(fish, trig, jc) else "未"),
                   "当前信号（live 盘中口径，bar %s）：%s上穿 / %s下穿"
                   % (times.iloc[jl],
                      "" if just_crossed_up(fish, trig, jl) else "未",
                      "" if just_crossed_down(fish, trig, jl) else "未")]
            lines.append("\n".join(sec))
    except Exception as e:
        lines.append("【60m】取数失败: %s" % e)

    # 4. 30m + ESI 台账
    try:
        led = load_esi_ledger()
        mine = led[led["code"].astype(str).str.zfill(6) == code] if len(led) else led
        if len(mine) == 0:
            lines.append("【30m+ESI】台账无该票记录")
        else:
            sec = ["【30m+ESI】台账 %d 条：" % len(mine)]
            for _, r in mine.iterrows():
                desc = "  进场 %s fisher60=%s status=%s" % (
                    r.get("entry_time", ""), r.get("entry_fisher60", ""),
                    r.get("status", ""))
                if r.get("failed") == "是":
                    desc += "（失效 @%s fisher30=%s）" % (r.get("fail_time", ""),
                                                        r.get("fail_fisher30", ""))
                elif r.get("status") == "closed":
                    desc += "（bars_held=%s）" % r.get("bars_held", "")
                sec.append(desc)
            opens = mine[mine["status"] == "open"]
            if len(opens):
                entry_time = pd.to_datetime(opens.iloc[-1]["entry_time"])
                df30 = fetch_30m(code, source)
                if df30 is None:
                    sec.append("open 记录窗口进度：30m 取数失败")
                else:
                    t30 = pd.to_datetime(df30["时间"])
                    done30 = int(((t30 > entry_time) & (t30 <= now)).sum())
                    sec.append("open 记录（进场 %s）窗口进度：已完结 30m bar %d/%d"
                               % (entry_time, done30, ESI_WINDOW_BARS))
            lines.append("\n".join(sec))
    except Exception as e:
        lines.append("【30m+ESI】取数失败: %s" % e)

    # 5. HSSR 现场计算
    try:
        h, n_sig = compute_hssr(code)
        if h is None:
            txt = ("取数失败" if n_sig == 0 else
                   "HSSR 样本不足 (n=%d<%d)" % (n_sig, HSSR_MIN_SAMPLE))
        else:
            tier = "正常仓位" if h >= 75 else ("仓位减半" if h >= 50 else "不建议买入")
            txt = "HSSR %.1f%%（%d/%d）· %s" % (h, int(round(h * n_sig / 100)), n_sig, tier)
        lines.append("【HSSR】（60m 上穿后 10 根 close 上涨口径，最近 20 次）%s" % txt)
    except Exception as e:
        lines.append("【HSSR】计算失败: %s" % e)

    lines[0] = "**单票体检：%s %s（%s）**" % (code, name or "?",
                                           now.strftime("%Y-%m-%d %H:%M"))
    report = "\n\n".join(lines)
    print(report)
    if push:
        push_text(report)


def main():
    parser = argparse.ArgumentParser(description="Fisher 60分钟线上穿/下穿扫描器")
    parser.add_argument("--once", action="store_true", help="扫描一次后退出")
    parser.add_argument("--loop", action="store_true", help="常驻定时扫描")
    parser.add_argument("--pool-file", help="自定义股票池 CSV（需含 code 列）")
    parser.add_argument("--limit", type=int, help="只扫描前 N 只（调试）")
    parser.add_argument("--side", choices=["up", "down"], default="up",
                        help="up=上穿（默认，选股），down=下穿（持仓监控）")
    parser.add_argument("--source", choices=["sina", "gm", "em", "tdx", "auto"], default=None,
                        help="数据源（缺省用配置区 DATA_SOURCE）；gm 须在 .venv-gm 环境运行；"
                             "auto 仅用于 --annotate-hssr（tdx 失败后 sina 兜底）")
    parser.add_argument("--buy", metavar="CODE", help="登记买入到 holdings.csv 后退出")
    parser.add_argument("--price", type=float, help="买入价（配合 --buy，缺省取最新价）")
    parser.add_argument("--sell", metavar="CODE", help="清仓：移出持仓并加入观察池后退出")
    parser.add_argument("--watch", metavar="CODE", help="加入观察池（盯 60 分钟上穿回钩）后退出")
    parser.add_argument("--unwatch", metavar="CODE", help="移出观察池（停止监控）后退出")
    parser.add_argument("--live", action="store_true",
                        help="盘中信号：未完结的 60 分钟 bar 也参与判定（可能收盘前消失/翻转）")
    parser.add_argument("--daily-resonance", action="store_true",
                        help="深水池日共振收盘复核：日内任一根完结 60m bar 上穿且当日日 K 上穿")
    parser.add_argument("--hssr-report", action="store_true",
                        help="推送 Fisher-ESI 台账 HSSR 周报后退出")
    parser.add_argument("--annotate-hssr", action="store_true",
                        help="深水池 HSSR 预计算：为 --pool-file（默认 pool_deep.csv）"
                             "追加 hssr/hssr_n 两列后退出（主环境 .venv，默认走 tdx 800 根深历史；"
                             "--source sina 强制新浪；--source auto 则 tdx 失败后 sina 兜底）")
    parser.add_argument("--inspect", metavar="CODE或名称",
                        help="单票体检：输出该票完整画像（身份/日线/60m/30m+ESI/HSSR）后退出；"
                             "名称从各池/持仓/观察池 CSV 反查代码")
    parser.add_argument("--push", action="store_true",
                        help="配合 --inspect：把体检报告推送到企业微信")
    args = parser.parse_args()

    source = args.source or DATA_SOURCE
    if args.inspect:
        inspect_stock(args.inspect, push=args.push, source=source)
        return
    if args.annotate_hssr:
        src = args.source or "tdx"
        annotate_pool_hssr(args.pool_file or "pool_deep.csv", source=src)
        return
    if args.hssr_report:
        push_text(hssr_report())
        return
    if args.daily_resonance:
        pool_file = args.pool_file or "pool_deep.csv"
        if not Path(pool_file).exists():
            logging.info("股票池文件 %s 不存在，跳过", pool_file)
            return
        pool = pd.read_csv(pool_file, dtype={"code": str})
        if args.limit:
            pool = pool.head(args.limit)
        result = scan_deep_resonance(pool, source=source)
        if len(result):
            notify(result, "深水日共振", "up")
        return
    if args.buy:
        buy(args.buy, args.price)
        return
    if args.sell:
        sell(args.sell)
        return
    if args.watch:
        watch(args.watch)
        return
    if args.unwatch:
        unwatch(args.unwatch)
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
