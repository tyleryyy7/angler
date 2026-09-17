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
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
THS_ROOT = r'C:\同花顺软件\同花顺'
HEXIN_EXE = os.path.join(THS_ROOT, 'hexin.exe')
AUTO_CLOSE = True    # 同花顺开着时自动关闭（客户端运行时会覆盖板块文件）
RELAUNCH = True      # 写完后自动重新打开客户端

# 板块映射（2026-09-17 用户在客户端重建后）：
# _ALL 后缀 = 完整池（pool_*.csv 覆盖）；无后缀 = 盘后候选
# （results/candidates_<最新>.csv 按池分流；「日K上穿」合并名单按代码所属池归入）。
# 板块十六进制 ID 运行时从 stockblock.ini 按名字反查（客户端重建板块会变 ID），
# 名字必须与客户端里的板块名完全一致；找不到名字的板块会跳过并告警。
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


def read_name_map(user_dir):
    """stockblock.ini [BLOCK_NAME_MAP_TABLE] -> {板块名: HEXID}。"""
    path = os.path.join(user_dir, 'stockblock.ini')
    with open(path, 'rb') as f:
        lines = f.read().decode('gbk', errors='replace').splitlines()
    try:
        ni = lines.index('[BLOCK_NAME_MAP_TABLE]')
    except ValueError:
        raise RuntimeError('stockblock.ini 缺 [BLOCK_NAME_MAP_TABLE]')
    out = {}
    for l in lines[ni + 1:]:
        if l.startswith('['):
            break
        if '=' in l:
            k, v = l.split('=', 1)
            out[v.strip()] = k.strip().upper()
    return out


def update_ini(user_dir, entries, day):
    """entries: {hexid: (name, codes)}。只替换/补写 CONTEXT 行；
    板块名单以客户端为准，脚本不动 NAME_MAP。"""
    path = os.path.join(user_dir, 'stockblock.ini')
    backup(path, day)
    with open(path, 'rb') as f:
        text = f.read().decode('gbk', errors='replace')
    lines = text.splitlines()

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

    with open(path, 'wb') as f:
        f.write('\r\n'.join(lines).encode('gbk', errors='replace'))
    log('stockblock.ini 更新 %d 个板块' % len(entries))


def read_candidates():
    """最新 results/candidates_*.csv：{池名: [codes]}。
    「日K上穿」合并名单按代码所属池文件归属分流。"""
    files = sorted(glob.glob(os.path.join(BASE, 'results', 'candidates_*.csv')))
    if not files:
        return None
    day_rows, dayk_rows = [], []
    with open(files[-1], 'r', encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            c = (row.get('code') or '').strip().split('.')[0].zfill(6)
            if not c:
                continue
            pond = (row.get('pool') or '').strip()
            if pond == '日K上穿':
                dayk_rows.append(c)
            else:
                day_rows.append((pond, c))
    # 代码 -> 所属池名（用于日K上穿名单分流；一码多池时取第一个）
    pond_of = {}
    for pool_file, pond in (('pool_right.csv', '右侧'), ('pool_left.csv', '左侧'),
                            ('pool_deep.csv', '深水'), ('pool_t0.csv', 'T0'),
                            ('pool_t1.csv', 'T1')):
        p = os.path.join(BASE, pool_file)
        if os.path.exists(p):
            for c in read_pool_codes(p):
                pond_of.setdefault(c, pond)
    out = {}
    for pond, c in day_rows:
        out.setdefault(pond, []).append(c)
    for c in dayk_rows:
        pond = pond_of.get(c)
        if pond:
            out.setdefault(pond, []).append(c)
    return out


def close_ths():
    """先普通结束，15 秒不退再强杀。返回是否已关闭。"""
    subprocess.run(['taskkill', '/IM', 'hexin.exe'], capture_output=True,
                   timeout=20)
    for _ in range(15):
        if not ths_running():
            break
        time.sleep(1)
    if ths_running():
        subprocess.run(['taskkill', '/F', '/IM', 'hexin.exe'],
                       capture_output=True, timeout=20)
        time.sleep(2)
    ok = not ths_running()
    log('关闭同花顺客户端：%s' % ('成功' if ok else '失败'))
    return ok


def main():
    closed_by_us = False
    if ths_running():
        if not AUTO_CLOSE:
            log('[SKIP] 同花顺客户端(hexin.exe)正在运行，写入会被客户端退出时覆盖，'
                '请先关闭客户端再跑')
            return
        if not close_ths():
            log('[SKIP] 无法关闭同花顺客户端，放弃写入')
            return
        closed_by_us = True
    user_dir = find_user_dir()
    day = datetime.now().strftime('%Y%m%d')
    name_map = read_name_map(user_dir)
    cand = None
    entries = {}
    for kind, key, name in BLOCKS:
        hexid = name_map.get(name)
        if not hexid:
            log('[WARN] 客户端板块里找不到「%s」，跳过（板块须在客户端先建好）' % name)
            continue
        if kind == 'pool':
            p = os.path.join(BASE, key)
            if not os.path.exists(p):
                log('[WARN] %s 不存在，跳过板块 %s' % (key, name))
                continue
            codes = read_pool_codes(p)
        else:
            if cand is None:
                cand = read_candidates()
                if cand is None:
                    log('[WARN] 无 candidates 文件，候选板块全部跳过')
                    break
            codes = cand.get(key, [])
            if not codes:
                log('[INFO] 候选板块 %s 今日无标的，清空' % name)
        entries[hexid] = (name, codes)
        write_block_file(user_dir, hexid, codes, day)
    if entries:
        update_ini(user_dir, entries, day)
    log('完成：%s' % ', '.join('%s %d只' % (entries[h][0], len(entries[h][1]))
                               for h in entries))
    if closed_by_us and RELAUNCH and os.path.exists(HEXIN_EXE):
        subprocess.Popen([HEXIN_EXE], cwd=THS_ROOT)
        log('已重新启动同花顺客户端')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('[ERR] %s' % e)
    sys.exit(0)   # 不阻断 build_pool.cmd 主流程
