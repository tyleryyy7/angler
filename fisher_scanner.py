# -*- coding: utf-8 -*-
"""
Fisher Transform 60分钟线「刚上穿」扫描器（沪深A股，akshare；数据源见配置区 DATA_SOURCE，
可选新浪 sina / 东方财富 em，默认新浪——东财接口对部分网络环境有 WAF 封锁）

信号定义（与你的 Pine / 同花顺代码完全一致）：
    fish2 = fish1[1]，所以「上穿」= fish1 由跌转升的拐点：
    fish[t] > fish[t-1] 且 fish[t-1] <= fish[t-2]
    每次扫描只判断【最新已完结】的那根 60 分钟 bar。

A股 60 分钟 bar 一天 4 根，东财时间戳为 bar 结束时刻：10:30 / 11:30 / 14:00 / 15:00。
建议在每根 bar 收盘后 1 分钟运行：10:31 / 11:31 / 14:01 / 15:01。

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
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------- 配置区（按需修改） -----------------------
DATA_SOURCE = "sina"      # 数据源："sina"（新浪）或 "em"（东方财富，原默认；本机被其 WAF 封锁时不可用）
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
# -----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)


def _patch_requests_timeout(default=15):
    """给 requests（含 akshare 内部调用）补默认超时，防止连接挂起卡死整个扫描。"""
    import requests
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


def signal_bar_index(df, now=None):
    """返回用于判定信号的 bar 下标。

    东财 60 分钟 bar 时间戳 = bar 结束时刻（10:30/11:30/14:00/15:00）。
    盘中最后一根可能正在形成（now 早于其结束时刻），此时丢弃它、用 -2；
    否则最后一根已完结，用 -1。
    """
    if now is None:
        now = datetime.now()
    last_ts = pd.to_datetime(df["时间"].iloc[-1])
    if last_ts.date() == now.date() and now < last_ts:
        return -2
    return -1


def _sina_symbol(code):
    """新浪代码前缀：6/5(股票/沪ETF 51/58) -> sh，其余（00/30/68/15/16）-> sz。"""
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def fetch_60m(code):
    """拉取单只股票的 60 分钟前复权 K 线，带重试。失败返回 None。

    新浪源注意：qfq 由「分钟线 + 日线前复权因子」合成，每股需 2 次请求，
    全市场扫描耗时约为东财源的 2 倍；失败率上升时把 REQUEST_INTERVAL 调大到 0.4~0.5。
    """
    import akshare as ak
    for k in range(RETRY):
        try:
            if DATA_SOURCE == "sina":
                df = ak.stock_zh_a_minute(symbol=_sina_symbol(code), period="60", adjust="qfq")
                if df is not None and len(df) > 0:
                    # 统一为东财列名契约：时间/最高/最低/收盘（时间戳同为 bar 结束时刻）
                    return df.rename(columns={"day": "时间", "high": "最高",
                                              "low": "最低", "close": "收盘"})
            else:
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


def scan_one(row, side="up"):
    """扫描单只股票，命中返回 dict，未命中返回 None，拉取失败返回 'FAIL'。"""
    code, name = str(row["code"]), row["name"]
    df = fetch_60m(code)
    time.sleep(REQUEST_INTERVAL)   # 每个 worker 内部的节流
    if df is None or len(df) < MIN_BARS:
        return "FAIL"
    high = pd.to_numeric(df["最高"], errors="coerce").values
    low = pd.to_numeric(df["最低"], errors="coerce").values
    fish, trig = fisher_transform(high, low, FISHER_LEN)
    j = len(df) + signal_bar_index(df)
    crossed = just_crossed_down(fish, trig, j) if side == "down" else just_crossed_up(fish, trig, j)
    if crossed:
        return {
            "code": code, "name": name,
            "bar_time": str(df["时间"].iloc[j]),
            "close": float(pd.to_numeric(df["收盘"], errors="coerce").iloc[j]),
            "fisher": round(float(fish[j]), 3),
            "trigger": round(float(trig[j]), 3),
        }
    return None


def scan(pool, save=True, label="", side="up"):
    """并发扫描整个股票池，返回命中「最新完结bar刚上穿/下穿」的股票清单。

    用进程池而非线程池：akshare 新浪前复权链路用的 py_mini_racer 是 C 扩展，
    多线程下会崩解释器；多进程各自独立则无此问题。
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    hits, fails, done = [], 0, 0
    t0 = time.time()
    n = len(pool)
    rows = [row for _, row in pool.iterrows()]
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(scan_one, row, side) for row in rows]
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            if r == "FAIL":
                fails += 1
            elif r:
                hits.append(r)
            if done % 50 == 0:
                logging.info("进度 %d/%d，命中 %d，失败 %d", done, n, len(hits), fails)

    result = pd.DataFrame(hits)
    if len(result):
        result = result.sort_values("code").reset_index(drop=True)
    elapsed = time.time() - t0
    logging.info("扫描完成：%d 只耗时 %.1f 分钟，命中 %d 只，失败 %d 只",
                 n, elapsed / 60, len(result), fails)
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
    if "right" in f:
        return "右侧鱼塘"
    if "left" in f:
        return "左侧鱼塘"
    if "deep" in f:
        return "深水鱼塘"
    if "t0" in f:
        return "T0鱼塘"
    return "鱼塘"


def pool_tag(args):
    """结果 CSV 文件名后缀：right / left / holdings / 空。"""
    f = (args.pool_file or "").lower()
    if "holding" in f:
        return "holdings"
    if "right" in f:
        return "right"
    if "left" in f:
        return "left"
    if "deep" in f:
        return "deep"
    if "t0" in f:
        return "t0"
    return ""


def notify(result, pond="鱼塘", side="up"):
    """结果通知：打印到控制台，并推送到企业微信机器人（WECOM_WEBHOOK 留空则跳过）。

    下穿（持仓监控）无命中时不推送，避免每个时点刷「0 条」噪音。
    """
    suffix = "下穿" if side == "down" else ""
    if len(result) == 0:
        print("本次扫描：无刚%s标的" % ("下穿" if side == "down" else "上穿"))
        content = "**%s：0 条鱼%s**" % (pond, suffix)
    else:
        print("本次扫描命中 %d 只：\n%s" % (len(result), result.to_string(index=False)))
        lines = ["**%s：%d 条鱼%s**" % (pond, len(result), suffix)]
        lines += ["%s %s" % (r["code"], r["name"])
                  for _, r in result.head(PUSH_MAX_ROWS).iterrows()]
        if len(result) > PUSH_MAX_ROWS:
            lines.append("……共 %d 只，完整清单见 results CSV" % len(result))
        content = "\n".join(lines)
    should_push = len(result) > 0 or (PUSH_EMPTY and side == "up")
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


def buy(code, price=None):
    """把股票登记进 holdings.csv（持仓监控清单）；已在清单中则跳过。"""
    path = Path(__file__).resolve().parent / "holdings.csv"
    if path.exists():
        h = pd.read_csv(path, dtype={"code": str})
    else:
        h = pd.DataFrame(columns=["code", "name", "buy_date", "buy_price"])
    code = str(code).zfill(6)
    if code in h["code"].astype(str).values:
        logging.info("%s 已在持仓清单中", code)
        return
    import akshare as ak
    spot = ak.stock_zh_a_spot()
    row = spot[spot["代码"].str[2:] == code]
    name = row["名称"].iloc[0] if len(row) else ""
    if price is None and len(row):
        price = float(row["最新价"].iloc[0])
    h = pd.concat([h, pd.DataFrame([{
        "code": code, "name": name,
        "buy_date": datetime.now().strftime("%Y-%m-%d"),
        "buy_price": price if price else "",
    }])], ignore_index=True)
    h.to_csv(path, index=False, encoding="utf-8-sig")
    print("已登记持仓: %s %s 买入价 %s" % (code, name, price))


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
            notify(scan(pool, label=pool_tag(args), side=args.side),
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
    parser.add_argument("--buy", metavar="CODE", help="登记买入到 holdings.csv 后退出")
    parser.add_argument("--price", type=float, help="买入价（配合 --buy，缺省取最新价）")
    args = parser.parse_args()

    if args.buy:
        buy(args.buy, args.price)
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
        notify(scan(pool, label=pool_tag(args), side=args.side),
               pool_label(args), side=args.side)


if __name__ == "__main__":
    main()
