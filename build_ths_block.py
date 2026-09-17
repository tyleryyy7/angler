# -*- coding: utf-8 -*-
"""build_ths_block.py — 建池后把五池同步到同花顺自定义板块（覆盖更新）。

同花顺板块两处存储（用户目录 mo_<uid>，自动探测）：
1. stockblock.ini（GBK）：[BLOCK_NAME_MAP_TABLE] HEXID=名称；
   [BLOCK_STOCK_CONTEXT] HEXID=mkt:code,mkt:code,,
2. custom_block/<十进制ID>（UTF-8）：
   {"context":"code|code|...,mkt|mkt|...|","ln":"...","xn":""}

市场代码：17=沪股票(60/68) 33=深股票(00/30) 20=沪ETF(51/58) 36=深ETF(15)。

注意：hexin.exe 运行时写入无效（客户端退出会用内存旧数据覆盖文件），
检测到客户端运行时直接跳过。每天首次写入前备份原文件。

手动：.venv\\Scripts\\python.exe build_ths_block.py
"""

import csv
import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
THS_ROOT = r'C:\同花顺软件\同花顺'

# 池文件 -> (板块ID 十六进制, 板块名)
BLOCKS = [
    ('pool_right.csv', '14C', '右侧鱼塘'),
    ('pool_left.csv',  '14B', '左侧鱼塘'),
    ('pool_deep.csv',  '146', '深水池'),
    ('pool_t0.csv',    '28',  'T0'),
    ('pool_t1.csv',    '14D', 'T1鱼塘'),   # 新建板块
]


def log(msg):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg))


def ths_running():
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq hexin.exe'],
                             capture_output=True, text=True, timeout=15).stdout
        return 'hexin.exe' in out
    except Exception:
        return False


def find_user_dir():
    dirs = [d for d in glob.glob(os.path.join(THS_ROOT, 'mo_*'))
            if os.path.isdir(d)]
    if not dirs:
        raise RuntimeError('未找到同花顺用户目录 mo_*：%s' % THS_ROOT)
    # 多个用户目录时取最新修改的
    return max(dirs, key=os.path.getmtime)


def market_of(code):
    if code.startswith(('60', '68')):
        return 17
    if code.startswith(('00', '30')):
        return 33
    if code.startswith(('51', '58')):
        return 20
    if code.startswith('15'):
        return 36
    # 兜底按交易所
    return 17 if code.startswith('6') else 33


def read_pool_codes(path):
    codes = []
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            c = (row.get('code') or '').strip().split('.')[0].zfill(6)
            if c:
                codes.append(c)
    return codes


def backup(path, day):
    if os.path.exists(path):
        dst = path + '.bak_' + day
        if not os.path.exists(dst):
            shutil.copy2(path, dst)
            log('备份 %s' % os.path.basename(dst))


def write_block_file(user_dir, hexid, codes, day):
    dec = str(int(hexid, 16))
    path = os.path.join(user_dir, 'custom_block', dec)
    backup(path, day)
    mkts = [str(market_of(c)) for c in codes]
    ctx = '|'.join(codes) + '|,' + '|'.join(mkts) + '|'
    payload = {'context': ctx, 'ln': '', 'xn': ''}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
    log('custom_block/%s <- %d 只' % (dec, len(codes)))


def update_ini(user_dir, entries, day):
    """entries: {hexid: (name, codes)}。替换/新增 CONTEXT 行；新板块补 NAME_MAP。"""
    path = os.path.join(user_dir, 'stockblock.ini')
    backup(path, day)
    with open(path, 'rb') as f:
        text = f.read().decode('gbk', errors='replace')
    lines = text.splitlines()

    # 板块名表：新板块补登记
    try:
        ni = lines.index('[BLOCK_NAME_MAP_TABLE]')
    except ValueError:
        raise RuntimeError('stockblock.ini 缺 [BLOCK_NAME_MAP_TABLE]')
    existing_names = {l.split('=', 1)[0].strip().upper() for l in lines[ni + 1:] if '=' in l}
    add_names = [(hid, name) for hid, (name, _) in entries.items()
                 if hid.upper() not in existing_names]

    try:
        ci = lines.index('[BLOCK_STOCK_CONTEXT]')
    except ValueError:
        raise RuntimeError('stockblock.ini 缺 [BLOCK_STOCK_CONTEXT]')

    # 上下文表：替换已有行，缺失的插到节首
    remaining = dict(entries)
    for i in range(ci + 1, len(lines)):
        l = lines[i]
        if l.startswith('['):
            break
        if '=' in l:
            key = l.split('=', 1)[0].strip().upper()
            if key in remaining:
                _, codes = remaining.pop(key)
                lines[i] = '%s=%s' % (key, ','.join(
                    '%d:%s' % (market_of(c), c) for c in codes) + ',,')
    insert_at = ci + 1
    for hid, (name, codes) in remaining.items():
        lines.insert(insert_at, '%s=%s' % (hid.upper(), ','.join(
            '%d:%s' % (market_of(c), c) for c in codes) + ',,'))
        insert_at += 1
    for hid, name in add_names:
        lines.insert(ni + 1, '%s=%s' % (hid.upper(), name))
        log('新建板块 %s=%s' % (hid.upper(), name))

    with open(path, 'wb') as f:
        f.write('\r\n'.join(lines).encode('gbk', errors='replace'))
    log('stockblock.ini 更新 %d 个板块' % len(entries))


def main():
    if ths_running():
        log('[SKIP] 同花顺客户端(hexin.exe)正在运行，写入会被客户端退出时覆盖，'
            '请先关闭客户端再跑')
        return
    user_dir = find_user_dir()
    day = datetime.now().strftime('%Y%m%d')
    entries = {}
    for pool_file, hexid, name in BLOCKS:
        p = os.path.join(BASE, pool_file)
        if not os.path.exists(p):
            log('[WARN] %s 不存在，跳过板块 %s' % (pool_file, name))
            continue
        codes = read_pool_codes(p)
        entries[hexid] = (name, codes)
        write_block_file(user_dir, hexid, codes, day)
    if entries:
        update_ini(user_dir, entries, day)
    log('完成：%s' % ', '.join('%s %d只' % (entries[h][0], len(entries[h][1]))
                               for h in entries))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('[ERR] %s' % e)
    sys.exit(0)   # 不阻断 build_pool.cmd 主流程
