# -*- coding: utf-8 -*-
"""build_ths_block.py — 建池后生成同花顺板块导入文件（ths_blocks/*.txt）。

背景（2026-09-17 调查结论，Logger/CustomBlock 日志实证）：
同花顺自定义板块的权威名单在云端（按账号+版本号），离线写本地文件永不上云，
且任何设备改动板块触发版本 bump 后，PC 轮询会全量下载覆盖本地。因此放弃
离线写文件方案，改为生成导入文件，由用户在客户端手动导入（导入动作本身
触发上传，PC/手机双端一致且持久）。

输出：D:\\angler\\ths_blocks\\<板块名>.txt，GBK 编码，每行 "代码 名称"。
板块名须与客户端里的板块名完全一致（客户端建板块时至少放 1 只票，空板块不上云）。

由 build_pool.cmd 在建池+候选扫描后调用；也可手动跑：
.venv\\Scripts\\python.exe build_ths_block.py
"""

import csv
import glob
import os
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, 'ths_blocks')

# (来源类型, 来源键, 板块名/文件名)
# pool = 完整池文件；cand = 盘后候选（results/candidates_最新.csv 按池分流，
# 「日K上穿」合并名单按代码所属池归入）
BLOCKS = [
    ('pool', 'pool_right.csv', '右侧鱼塘_ALL'),
    ('pool', 'pool_left.csv',  '左侧鱼塘_ALL'),
    ('pool', 'pool_deep.csv',  '深水池_ALL'),
    ('pool', 'pool_t0.csv',    'T0_ALL'),
    ('pool', 'pool_t1.csv',    'T1_ALL'),
    ('cand', '右侧',           '右侧鱼塘'),
    ('cand', '左侧',           '左侧鱼塘'),
    ('cand', '深水',           '深水池'),
    ('cand', 'T0',             'T0'),
    ('cand', 'T1',             'T1'),
]


def log(msg):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg))


def read_pool_rows(fname):
    """pool CSV -> [(code6, name)]"""
    out = []
    with open(os.path.join(BASE, fname), 'r', encoding='utf-8-sig',
              newline='') as f:
        for r in csv.DictReader(f):
            c = (r.get('code') or '').strip().split('.')[0].zfill(6)
            n = (r.get('name') or '').strip()
            if c:
                out.append((c, n))
    return out


def read_candidates():
    """最新 results/candidates_*.csv -> {池名: [(code, name)]}。
    「日K上穿」合并名单按代码所属池文件归属分流。"""
    files = sorted(glob.glob(os.path.join(BASE, 'results', 'candidates_*.csv')))
    if not files:
        return None
    day_rows, dayk_rows = [], []
    with open(files[-1], 'r', encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            c = (r.get('code') or '').strip().split('.')[0].zfill(6)
            n = (r.get('name') or '').strip()
            if not c:
                continue
            pond = (r.get('pool') or '').strip()
            if pond == '日K上穿':
                dayk_rows.append((c, n))
            else:
                day_rows.append((pond, c, n))
    pond_of = {}
    for fname, pond in (('pool_right.csv', '右侧'), ('pool_left.csv', '左侧'),
                        ('pool_deep.csv', '深水'), ('pool_t0.csv', 'T0'),
                        ('pool_t1.csv', 'T1')):
        p = os.path.join(BASE, fname)
        if os.path.exists(p):
            for c, n in read_pool_rows(fname):
                pond_of.setdefault(c, (pond, n))
    out = {}
    for pond, c, n in day_rows:
        out.setdefault(pond, []).append((c, n))
    for c, n in dayk_rows:
        pn = pond_of.get(c)
        if pn:
            out.setdefault(pn[0], []).append((c, n or pn[1]))
    return out


def write_txt(bname, rows):
    path = os.path.join(OUT_DIR, bname + '.txt')
    with open(path, 'w', encoding='gbk', errors='replace') as f:
        for c, n in rows:
            f.write('%s %s\n' % (c, n))
    return path


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cand = None
    for kind, key, bname in BLOCKS:
        if kind == 'pool':
            p = os.path.join(BASE, key)
            if not os.path.exists(p):
                log('[WARN] %s 不存在，跳过 %s' % (key, bname))
                continue
            rows = read_pool_rows(key)
        else:
            if cand is None:
                cand = read_candidates()
                if cand is None:
                    log('[WARN] 无 candidates 文件，候选板块全部跳过')
                    break
            rows = cand.get(key, [])
        write_txt(bname, rows)
        log('%s <- %d 只' % (bname, len(rows)))
    log('完成，导入文件在 %s（同花顺：自定义板块设置 -> 选板块 -> 导入）' % OUT_DIR)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('[ERR] %s' % e)
