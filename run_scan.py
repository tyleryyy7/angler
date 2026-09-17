# -*- coding: utf-8 -*-
"""
盘中扫描调度器（双通道互备，跑在主环境 .venv）

每个时点由 .cmd 调用一次，默认依次处理扫描项（2026-09-15 起三池）：
    持仓（下穿/上穿）→ pool_deep → watchlist
    （右侧/左侧/T0/T1 盘中不扫：tdxq 盘中分钟数据静态，改由 --post-close 盘后候选覆盖）

通道逻辑（2026-09-17 起仅 tdxq 单通道，sina 应用户要求停用）：
    1. tdxq（通达信客户端 TQ 接口，进程内，整池预取+本地缓存）——须客户端登录运行；
    2. 失败率 > 50% 判定通道失效（如客户端未开），推送「tdxq 通道不可用」告警，
       本次扫描缺失（不再降级 sina；陈旧数据由 _fetch_tdxq 新鲜度复核拦成失败）。

用法：
    .venv\\Scripts\\python.exe run_scan.py              # 三池扫描，只判完结 bar（收盘后任务）
    .venv\\Scripts\\python.exe run_scan.py --live             # 三池扫描，盘中未完结 bar 也参与判定（bar 中段任务）
    .venv\\Scripts\\python.exe run_scan.py --holdings --live     # 只扫持仓（每 5 分钟任务）
    .venv\\Scripts\\python.exe run_scan.py --hssr-report          # Fisher-ESI 台账 HSSR 周报（每周日任务）
    .venv\\Scripts\\python.exe run_scan.py --post-close           # 盘后候选：全五池完结扫描 + 候选清单（21:30 任务）
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import fisher_scanner as fs

BASE = Path(__file__).resolve().parent
FAIL_RATE_LIMIT = 0.5         # 失败率超过此值判定通道失效
CHANNELS = ("tdxq",)       # 进程内通道链。2026-09-17 起 sina 停用（用户指定），
                           # tdxq 失败/陈旧时不再降级 sina，走失败率告警兜底
POOLS = [("holdings.csv", "down"),        # 持仓下穿：卖出预警（最优先）
         ("holdings.csv", "up"),           # 持仓上穿：回钩/加仓提示
         ("pool_deep.csv", "up"),          # 深水池优先（用户指定）
         ("watchlist.csv", "up")]          # 观察池：盯 60 分钟上穿回钩
# 右侧/左侧/T0/T1 盘中扫描 2026-09-15 再次暂停：tdxq 盘中数据是静态的
#（官方确认 get_market_data 盘中仅日K，分钟线靠盘后下载积累），盘中推送
# 价值低；这四池改由 --post-close 盘后候选覆盖。第二阶段 tick 聚合器
#（subscribe_hq 订阅 top50 深水+持仓+观察）上线后再评估恢复。

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(fs.LOG_FILE, encoding="utf-8"),
                              logging.StreamHandler(sys.stdout)])


def push_alert(text):
    """直接推送一条文本告警（两通道都失效时用）。"""
    logging.warning(text)
    if fs.WECOM_WEBHOOK:
        try:
            import requests
            requests.post(fs.WECOM_WEBHOOK, timeout=10,
                          json={"msgtype": "markdown", "markdown": {"content": text}})
        except Exception as e:
            logging.warning("告警推送失败: %s", e)


def _enrich_hits(result):
    """命中行附候选卡片同款基本面：涨跌幅/市值/历史位置/PE（一次 TQ 子进程）。
    失败/缺数据则留 NaN，notify 自动省略对应段。"""
    codes = [str(r["code"]).zfill(6) for _, r in result.iterrows()]
    info = fetch_moreinfo(codes)
    chg, mcap, pos_pct, pe = [], [], [], []
    for c, (_, r) in zip(codes, result.iterrows()):
        mi = info.get(_tq_suffix(c))
        chg.append(_pct_change(c, mi))
        try:
            mcap.append(float(mi["Zsz"]))
        except (TypeError, KeyError, ValueError):
            mcap.append(None)
        try:
            hh, ll = float(mi["HisHigh"]), float(mi["HisLow"])
            close = float(r["close"])
            pos_pct.append((close - ll) / (hh - ll) * 100 if hh > ll else None)
        except (TypeError, KeyError, ValueError):
            pos_pct.append(None)
        try:
            pe.append(float(mi["DynaPE"]))
        except (TypeError, KeyError, ValueError):
            pe.append(None)
    result = result.copy()
    result["chg"], result["mcap"] = chg, mcap
    result["pos_pct"], result["pe"] = pos_pct, pe
    return result


def scan_with_failover(pool, pool_file, pond, tag, side, live=False, channels=None):
    """通道链（2026-09-17 起仅 tdxq，sina 停用）；不可用则推送告警。"""
    for source in (channels or CHANNELS):
        result = fs.scan(pool, label=tag, side=side, source=source, live=live)
        total = result.attrs.get("total", 0)
        fails = result.attrs.get("fails", 0)
        if not total or fails / total <= FAIL_RATE_LIMIT:
            if len(result):
                result = _enrich_hits(result)
            fs.notify(result, pond, side=side)
            return result
        logging.warning("%s 通道失败率 %.0f%%（%d/%d）: %s",
                        source, fails / total * 100, fails, total, pool_file)
    push_alert("**%s：tdxq 通道不可用（客户端未登录？），本次扫描缺失**" % pond)
    return None


TDX_MOREINFO = r"D:\tdx\PYPlugins\user\tdx_moreinfo.py"   # 批量基本面助手（TQ 路径锁死）


def _tq_suffix(code6):
    return code6 + ('.SH' if str(code6).startswith(('60', '68', '51', '58')) else '.SZ')


def fetch_moreinfo(codes6):
    """批量取基本面（总市值/流通/PE/PB/历史高低/涨幅），一次 TQ 会话子进程。
    失败返回 {}，调用方省略该段即可。"""
    import json
    import subprocess
    if not codes6:
        return {}
    try:
        r = subprocess.run([sys.executable, TDX_MOREINFO,
                            ",".join(_tq_suffix(c) for c in codes6)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        txt = r.stdout or ""
        return json.loads(txt[txt.index("{"):]) if "{" in txt else {}
    except Exception as e:
        logging.warning("moreinfo 获取失败: %s", e)
        return {}


def _pct_change(code6, mi=None):
    """当日涨跌幅：优先 moreinfo 的 ZAF；否则 cache/tdxq/{code}_1d.csv 末两行。"""
    if mi:
        try:
            return float(mi["ZAF"])
        except (KeyError, ValueError, TypeError):
            pass
    try:
        path = fs.TDXQ_CACHE_DIR.parent.parent / "tdxq" / ("%s_1d.csv" % code6)
        df = pd.read_csv(path)
        col = "close" if "close" in df.columns else "收盘"
        closes = df[col].astype(float).values
        if len(closes) >= 2 and closes[-2] > 0:
            return (closes[-1] / closes[-2] - 1) * 100
    except Exception:
        pass
    return None


def _fmt_candidate(r, pool_row, info):
    """一条候选卡片：三行短句（代码名加粗作锚点）——行情 | 信号细节 | 打分/HSSR+基本面。"""
    code = str(r["code"]).zfill(6)
    line1 = "**%s %s**" % (code, r["name"])
    close = r.get("close")
    if close:
        line1 += " %.2f" % float(close)
    mi = info.get(_tq_suffix(code))
    chg = _pct_change(code, mi)
    if chg is not None:
        line1 += " %+.1f%%" % chg
    seg2 = []
    if pool_row is not None:
        amt = pool_row.get("avg_amount")
        amp = pool_row.get("avg_amplitude")
        if pd.notna(amt):
            seg2.append("额%.1f亿" % float(amt))
        if pd.notna(amp):
            seg2.append("振%.1f%%" % float(amp))
    sig = "F%.2f/%.2f" % (float(r["fisher"]), float(r["trigger"]))
    state = r.get("bar_state", "")
    if state == "假性失效":
        sig += " 假性失效·待激活"
    elif state:
        sig += " %s" % state
    if r.get("small_tf"):
        sig += "(%s)" % r["small_tf"]
    seg2.append(sig)
    seg3 = []
    if pool_row is not None:
        if pd.notna(pool_row.get("score")) and str(pool_row.get("score")) != "":
            seg3.append("分%.1f" % float(pool_row["score"]))
        h, n_sig = pool_row.get("hssr"), pool_row.get("hssr_n")
        if pd.notna(h) and pd.notna(n_sig):
            h, n_sig = float(h), int(n_sig)
            tier = "正常仓位" if h >= 75 else ("仓位减半" if h >= 50 else "不建议买入")
            seg3.append("HSSR %.0f%%(%d/%d) %s"
                        % (h, int(round(h * n_sig / 100)), n_sig, tier))
    if mi:
        try:
            seg3.append("市值%.0f亿" % float(mi["Zsz"]))
        except (KeyError, ValueError, TypeError):
            pass
        try:
            hh, ll = float(mi["HisHigh"]), float(mi["HisLow"])
            if close and hh > ll:
                seg3.append("位置%.0f%%" % ((float(close) - ll) / (hh - ll) * 100))
        except (KeyError, ValueError, TypeError):
            pass
        try:
            seg3.append("PE%.0f" % float(mi["DynaPE"]))
        except (KeyError, ValueError, TypeError):
            pass
    lines = [line1, " | ".join(seg2)]
    if seg3:
        lines.append(" | ".join(seg3))
    return "\n".join(lines)


def daily_cross_check(code6):
    """当日完结日 K 是否 Fisher 上穿：优先读 tdxq 新鲜预取缓存
    （cache/daily_qfq/tdxq/{code}_1d.csv），退回建池缓存 cache/tdxq/；
    返回 (crossed, fish, trig)；数据缺失/不足返回 (False, None, None)。"""
    for base in (fs.TDXQ_CACHE_DIR, fs.TDXQ_CACHE_DIR.parent.parent / "tdxq"):
        try:
            df = pd.read_csv(base / ("%s_1d.csv" % code6))
            if len(df) < fs.FISHER_LEN + 2:
                continue
            high = pd.to_numeric(df["high"], errors="coerce").values
            low = pd.to_numeric(df["low"], errors="coerce").values
            fish, trig = fs.fisher_transform(high, low, fs.FISHER_LEN)
            j = len(df) - 1
            return fs.just_crossed_up(fish, trig, j), round(float(fish[j]), 2), \
                round(float(trig[j]), 2)
        except Exception:
            continue
    return False, None, None


def daily_fisher_state(code6):
    """最近完结日 K 的 (fish, trig, prev_fish)；数据缺失/不足返回 (None, None, None)。
    盘后调用（日 K 已完结），直接取末根；缓存来源同 daily_cross_check。"""
    for base in (fs.TDXQ_CACHE_DIR, fs.TDXQ_CACHE_DIR.parent.parent / "tdxq"):
        try:
            df = pd.read_csv(base / ("%s_1d.csv" % code6))
            if len(df) < fs.FISHER_LEN + 2:
                continue
            high = pd.to_numeric(df["high"], errors="coerce").values
            low = pd.to_numeric(df["low"], errors="coerce").values
            fish, trig = fs.fisher_transform(high, low, fs.FISHER_LEN)
            return float(fish[-1]), float(trig[-1]), float(fish[-2])
        except Exception:
            continue
    return None, None, None


DAY_FILTER_MODE = "up"   # 盘后 60m 候选日K过滤：up=日线费雪回升（executor_t0 同款）；
                         # above=不在下行段（fish >= trigger）；None/'off' 关闭


def _day_filter_pass(code6):
    """日K过滤判定；数据缺失放行（宁多勿漏，候选是人工挑票）。"""
    if DAY_FILTER_MODE in (None, "off"):
        return True
    d_f, d_t, d_prev = daily_fisher_state(code6)
    if d_f is None:
        return True
    return d_f > d_prev if DAY_FILTER_MODE == "up" else d_f >= d_t


def post_close_candidates():
    """盘后候选：全五池完结扫描，合并写 candidates CSV + 推送汇总（人工挑票入 watchlist）。
    凌晨跑（建池后）：目标交易日 = 上一交易日。"""
    now = datetime.now()
    day = now
    if now.strftime("%H:%M") < "09:35":
        day = now - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    day_str = day.strftime("%Y-%m-%d")
    pools5 = [("pool_right.csv", "右侧"), ("pool_left.csv", "左侧"),
              ("pool_deep.csv", "深水"), ("pool_t0.csv", "T0"),
              ("pool_t1.csv", "T1"), ("watchlist.csv", "观察")]
    # 盘后应已自动下载；最近交易日 15:00 完结 bar 不在缓存则放弃本次候选扫描。
    # 判定前先用探针票刷新缓存：缓存可能是注解/旧扫描在静态库无数据时写入的
    # 昨日数据，直接判会误判 tdxq 不可用（2026-09-16 实发）。
    # 2026-09-17 起 sina 停用：缺数据时直接中止，避免拿昨日 bar 当今日信号推送。
    fs._prefetch_tdxq(["600036", "000001", "510300"], period="1h", count=5)
    if not fs.tdxq_has_today_close():
        logging.warning("tdxq 缺当日收盘数据（盘后未下载？），候选扫描中止")
        push_alert("**盘后候选扫描中止：tdxq 无当日收盘数据（盘后下载未完成？）**")
        return
    frames = []
    pool_rows = {}   # code -> 池行（打分/HSSR/成交额等）
    hits = []        # (pond, result)
    for pool_file, pond in pools5:
        p = BASE / pool_file
        if not p.exists():
            continue
        pool = pd.read_csv(p, dtype={"code": str})
        if len(pool) == 0:
            continue
        pool_rows.update({str(rw["code"]).zfill(6): rw
                          for _, rw in pool.iterrows()})
        fake_args = SimpleNamespace(pool_file=pool_file)
        result = scan_with_failover(pool, pool_file, fs.pool_label(fake_args),
                                    fs.pool_tag(fake_args), "up", live=False,
                                    channels=CHANNELS)
        if result is not None and len(result):
            result = result.copy()
            result.insert(0, "pool", pond)
            hits.append((pond, result))
    # 日K 上穿名单（独立于 60m 名单）：六池并集。
    # 注意：cache/tdxq 的 1d 缓存是建池时写的（停在昨日），直接判会错位一天
    #（2026-09-15 风语筑误报实测）。先批量预取最新日线再判。
    fs._prefetch_tdxq(list(pool_rows.keys()), period="1d", count=30)
    # 60m 候选日K过滤（executor_t0 同款：日线费雪回升才保留；日K上穿名单
    # 按定义必然满足，无需过滤）。须在 1d 预取之后判，否则用建池旧缓存。
    dropped = []
    if DAY_FILTER_MODE not in (None, "off"):
        kept = []
        for pond, res in hits:
            mask = [_day_filter_pass(str(r["code"]).zfill(6))
                    for _, r in res.iterrows()]
            dropped.extend(str(r["code"]).zfill(6)
                           for m, (_, r) in zip(mask, res.iterrows()) if not m)
            res2 = res[mask]
            if len(res2):
                kept.append((pond, res2))
        if dropped:
            logging.info("盘后候选日K过滤(%s)剔除 %d 只: %s",
                         DAY_FILTER_MODE, len(dropped), ",".join(dropped))
        hits = kept
    frames = [res for _, res in hits]
    daily_hits = []
    hit60_codes = set()
    for _, res in hits:
        hit60_codes.update(str(r["code"]).zfill(6) for _, r in res.iterrows())
    for code, rw in pool_rows.items():
        crossed, d_fish, d_trig = daily_cross_check(code)
        if crossed:
            daily_hits.append({"code": code, "name": rw.get("name", ""),
                               "close": rw.get("close"), "fisher": d_fish,
                               "trigger": d_trig, "bar_state": "日K上穿",
                               "small_tf": "",
                               "also_60m": code in hit60_codes})
    info = fetch_moreinfo([str(r["code"]).zfill(6)
                           for _, res in hits for _, r in res.iterrows()]
                          + [d["code"] for d in daily_hits])
    # 排版：标题/分组行紧贴下文，空行只出现在一只票的信息结束之后
    cards = set()   # 需要"票后空行"的行号
    lines = ["**盘后候选 %s**" % day_str]
    hdr = "—— 60m 上穿 ——"
    if DAY_FILTER_MODE not in (None, "off"):
        hdr += "（日K过滤%s，剔除 %d 只）" % (DAY_FILTER_MODE, len(dropped))
    lines.append(hdr)
    for pool_file, pond in pools5:
        res = next((res for pd_, res in hits if pd_ == pond), None)
        if res is None:
            continue
        lines.append("【%s】%d 只" % (pond, len(res)))
        for _, r in res.iterrows():
            lines.append(_fmt_candidate(r, pool_rows.get(str(r["code"]).zfill(6)),
                                        info))
            cards.add(len(lines) - 1)
    lines.append("—— 日K上穿 ——")
    if daily_hits:
        # 按池内打分降序（截断时保住高分票；无打分的排最后）
        def _score(d):
            try:
                return float(pool_rows[d["code"]].get("score") or 0)
            except (KeyError, ValueError, TypeError):
                return 0.0
        daily_hits.sort(key=_score, reverse=True)
        for d in daily_hits:
            r = pd.Series(d)
            line = _fmt_candidate(r, pool_rows.get(d["code"]), info)
            if d["also_60m"]:
                line += " ※60m已上穿"
            lines.append(line)
            cards.add(len(lines) - 1)
    else:
        lines.append("（无）")
    if daily_hits:
        frames.append(pd.DataFrame([{k: v for k, v in d.items()
                                     if k != "also_60m"} | {"pool": "日K上穿",
                                                            "bar_time": day_str}
                                    for d in daily_hits]))
    if frames:
        out = pd.concat(frames, ignore_index=True)
        path = BASE / "results" / ("candidates_%s.csv" % day.strftime("%Y%m%d"))
        out.to_csv(path, index=False, encoding="utf-8-sig")
        logging.info("候选清单已保存: %s", path)
    text = "\n".join(ln + "\n" if i in cards else ln
                     for i, ln in enumerate(lines))   # 空行只隔票
    full_len = len(text.encode("utf-8"))
    while len(text.encode("utf-8")) > 3800:   # 企业微信 markdown 上限 4096 字节
        text = text.rsplit("\n", 1)[0]        # 逐行截尾，防中文截断乱码
    if len(text.encode("utf-8")) < full_len:
        text += "\n……（过长截断，完整见 results CSV）"
    fs.push_text(text)


def main():
    parser = argparse.ArgumentParser(description="盘中扫描调度器")
    parser.add_argument("--holdings", action="store_true",
                        help="只扫持仓（下穿+上穿），用于每 5 分钟持仓监控")
    parser.add_argument("--live", action="store_true",
                        help="盘中信号：未完结的 60 分钟 bar 也参与判定")
    parser.add_argument("--daily-confirm", action="store_true",
                        help="深水池日共振收盘复核（60m 日内上穿 + 当日日 K 上穿）")
    parser.add_argument("--hssr-report", action="store_true",
                        help="推送 Fisher-ESI 台账 HSSR 周报（每周日任务，须在周末保护之前）")
    parser.add_argument("--post-close", action="store_true",
                        help="盘后候选：全五池完结扫描 + 候选清单推送（21:30 任务）")
    args = parser.parse_args()

    if args.hssr_report:
        fs.push_text(fs.hssr_report())
        return

    if datetime.now().weekday() >= 5:
        logging.info("周末不交易，退出")
        return

    if args.post_close:
        post_close_candidates()
        return

    hm_now = datetime.now().strftime("%H:%M")
    if args.daily_confirm:
        if hm_now > "18:00":
            logging.info("日共振补跑过晚（%s），退出", hm_now)
            return
        p = BASE / "pool_deep.csv"
        if not p.exists():
            logging.info("pool_deep.csv 不存在，跳过")
            return
        pool = pd.read_csv(p, dtype={"code": str})
        if len(pool) == 0:
            logging.info("pool_deep.csv 为空，跳过")
            return
        result = fs.scan_deep_resonance(pool)
        if len(result):
            fs.notify(result, "深水日共振", "up")
        return

    # 盘中类扫描（持仓/中段 --live/完结确认）仅限交易时段；睡眠唤醒后的
    # StartWhenAvailable 补跑在盘后没有意义，还会与建池等任务并发挤 TQ 会话。
    if not "09:25" <= hm_now <= "15:30":
        logging.info("非交易时段（%s），盘中扫描退出", hm_now)
        return

    pools = [p for p in POOLS if p[0] == "holdings.csv"] if args.holdings else POOLS
    # 2026-09-17 起 sina 停用：tdxq 陈旧也不降级，陈旧数据经 _fetch_tdxq
    # 新鲜度复核返回 None 计入失败，触发失败率告警而不是盲扫
    channels = CHANNELS
    for pool_file, side in pools:
        p = BASE / pool_file
        if not p.exists():
            logging.info("%s 不存在，跳过", pool_file)
            continue
        pool = pd.read_csv(p, dtype={"code": str})
        if len(pool) == 0:
            logging.info("%s 为空，跳过", pool_file)
            continue

        fake_args = SimpleNamespace(pool_file=pool_file)
        result = scan_with_failover(pool, pool_file, fs.pool_label(fake_args),
                                    fs.pool_tag(fake_args), side, live=args.live,
                                    channels=channels)
        # Fisher-ESI v2：上穿信号分流——假性失效入待激活台账，其余完结信号进场登记
        if side == "up" and result is not None and len(result):
            fs.esi_register_pending(result)
            if args.holdings:
                fs.esi_register_entries(result)
        # 持仓 60m 下穿命中 → 对应 open 台账记正常离场（failed=否）
        if args.holdings and side == "down" and result is not None and len(result):
            fs.esi_close_on_60m_down(result)

    # Fisher-ESI v2：持仓任务每次复查待激活（R2 信号激活）
    if args.holdings:
        fs.check_pending_activation()
        # R3 失效卖出判定：只在 30m bar 收盘后窗口跑（每 5 分钟持仓任务的网格上）
        now = datetime.now()
        hm = now.strftime("%H:%M")
        if any(a <= hm <= b for a, b in fs.ESI_JUDGE_WINDOWS):
            esi_result = fs.esi_check_invalidation(now)
            if esi_result is not None and len(esi_result):
                fs.notify(esi_result, "Fisher失效", "down")


if __name__ == "__main__":
    main()
