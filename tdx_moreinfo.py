# -*- coding: utf-8 -*-
"""tdx_moreinfo.py - batch get_more_info helper (one TQ session).

Usage: python tdx_moreinfo.py 600036.SH,000001.SZ,...
Prints JSON to stdout: {code: {field: value, ...}}.
Must live in D:/tdx/PYPlugins/user/ (TQ path lock). Master in git repo D:/angler.
"""
import json
import sys

from tqcenter import tq

FIELDS = ['Zsz', 'Ltsz', 'DynaPE', 'PB_MRQ', 'HisHigh', 'HisLow',
          'ZAF', 'ZAFPre5', 'ZAFPre20', 'IsT0Fund', 'IsZCZGP']


def main():
    codes = [c.strip() for c in sys.argv[1].split(',') if c.strip()]
    tq.initialize(__file__)
    out = {}
    try:
        for c in codes:
            try:
                d = tq.get_more_info(stock_code=c, field_list=FIELDS)
                if d:
                    out[c] = d
            except Exception:
                pass
    finally:
        tq.close()
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == '__main__':
    main()
