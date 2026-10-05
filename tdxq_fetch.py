# -*- coding: utf-8 -*-
# tdxq_fetch.py - batch kline fetch helper for the angler scanner (TDX TQ channel).
#
# Lives in <TDX>/PYPlugins/user/ because tqcenter must run from there (DLL path lock).
# Invoked by fisher_scanner.py via subprocess with the main .venv python:
#   python tdxq_fetch.py --codes-file CODES.txt --period 1h --count 800 --out-dir DIR
# Writes one CSV per code: {code6}_{period}.csv, columns time,open,high,low,close,volume,amount.
# period: 1h / 30m / 5m / 1d (this TQ build uses 1h, not 60m).

import argparse
import csv
import os
import sys

from tqcenter import tq

BATCH = 100  # max codes per get_market_data call


def merge_cache(path, new_rows):
    """Merge freshly fetched rows into the existing cache so history deepens
    instead of being truncated by shallow fetches (probe count=5 etc.).
    Front-adjusted series are re-anchored after a corporate action: if the
    first overlapping timestamp's close disagrees, the old history is on a
    stale anchor -> drop it and keep only the new rows (seam-free beats deep).
    """
    if not os.path.exists(path) or not new_rows:
        return new_rows
    try:
        with open(path, "r", encoding="utf-8") as f:
            r = csv.reader(f)
            next(r, None)               # header
            old_rows = [row for row in r if row]
    except Exception:
        return new_rows
    if not old_rows:
        return new_rows
    new_by_t = {row[0]: row for row in new_rows}
    for row in old_rows:
        n = new_by_t.get(row[0])
        if n is None:
            continue
        try:
            stale = abs(float(row[4]) - float(n[4])) > max(1e-6, abs(float(n[4])) * 1e-4)
        except (ValueError, IndexError):
            stale = True
        if stale:
            print("[INFO] %s: qfq anchor changed (corp action), rewrite new only"
                  % os.path.basename(path))
            return new_rows
        break   # one consistent overlap point suffices (anchor is global)
    merged = [row for row in old_rows if row[0] not in new_by_t] + new_rows
    merged.sort(key=lambda r: r[0])
    return merged


def to_tq_code(code):
    code = code.strip()
    if "." in code:
        return code
    return code + (".SH" if code.startswith(("5", "6")) else ".SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes-file")
    ap.add_argument("--period", default="1h")
    ap.add_argument("--count", type=int, default=800)
    ap.add_argument("--batch", type=int, default=BATCH,
                    help="max codes per get_market_data call")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--universe", action="store_true",
                    help="write universe lists (A-share '5' + ETF '31') to out-dir/universe.csv")
    ap.add_argument("--names", action="store_true",
                    help="write per-code names (get_stock_info) for codes-file to out-dir/names.csv")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    written = failed = 0
    tq.initialize(__file__)
    try:
        if args.universe:
            rows = ([(c, "stock") for c in tq.get_stock_list("5")]
                    + [(c, "etf") for c in tq.get_stock_list("31")])
            out = os.path.join(args.out_dir, "universe.csv")
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["code", "type"])
                w.writerows(rows)
            print("DONE universe=%d" % len(rows))
            return
        codes = []
        if args.codes_file:
            with open(args.codes_file, "r", encoding="utf-8") as f:
                codes = [to_tq_code(x) for x in f if x.strip() and not x.startswith("#")]
        if args.names:
            out = os.path.join(args.out_dir, "names.csv")
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["code", "name"])
                for c in codes:
                    try:
                        info = tq.get_stock_info(stock_code=c)
                        w.writerow([c.split(".")[0], info.get("Name", "")])
                    except Exception as e:
                        print("[ERR] name %s: %s" % (c, e))
            print("DONE names=%d" % len(codes))
            return
        for i in range(0, len(codes), args.batch):
            chunk = codes[i:i + args.batch]
            try:
                df = tq.get_market_data(stock_list=chunk, period=args.period,
                                        count=args.count, dividend_type="front")
            except Exception as e:
                print("[ERR] batch %d failed: %s" % (i // BATCH, e))
                failed += len(chunk)
                continue
            if not isinstance(df, dict) or not df:
                print("[ERR] batch %d empty" % (i // BATCH))
                failed += len(chunk)
                continue
            close = df.get("Close"); high = df.get("High"); low = df.get("Low")
            open_ = df.get("Open"); vol = df.get("Volume"); amt = df.get("Amount")
            for code in chunk:
                try:
                    sub = close[[code]].join(high[[code]], rsuffix="_h").join(
                        low[[code]], rsuffix="_l").join(open_[[code]], rsuffix="_o").join(
                        vol[[code]], rsuffix="_v").join(amt[[code]], rsuffix="_a").dropna()
                    if len(sub) == 0:
                        failed += 1
                        continue
                    out = os.path.join(args.out_dir,
                                       "%s_%s.csv" % (code.split(".")[0], args.period))
                    rows = [[str(ts), row[code + "_o"], row[code + "_h"],
                             row[code + "_l"], row[code],
                             row[code + "_v"], row[code + "_a"]]
                            for ts, row in sub.iterrows()]
                    rows = merge_cache(out, rows)
                    with open(out, "w", newline="", encoding="utf-8") as f:
                        w = csv.writer(f)
                        w.writerow(["time", "open", "high", "low", "close", "volume", "amount"])
                        w.writerows(rows)
                    written += 1
                except Exception as e:
                    print("[ERR] %s: %s" % (code, e))
                    failed += 1
    finally:
        tq.close()
    print("DONE written=%d failed=%d" % (written, failed))
    sys.exit(0 if written else 1)


if __name__ == "__main__":
    main()
