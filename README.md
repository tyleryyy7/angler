# Fisher Transform 60 分钟线「刚上穿」扫描器（沪深 A 股）

基于 akshare 的盘中实时预警脚本。每个 60 分钟 bar 收盘后扫描股票池，
选出**最新完结 bar 刚发生 Fisher 上穿 Trigger** 的股票。

数据源由 `fisher_scanner.py` 配置区的 `DATA_SOURCE` 决定：

- `"sina"`（默认，新浪）：60 分钟前复权 K 线 + 全市场行情均走新浪接口。
  适用于东财接口被网络环境（WAF/出口策略）封锁的场景。
  注意：新浪前复权是「分钟线 × 日线复权因子」合成，每股 2 次请求，全市场扫描耗时约为东财的 2 倍；
  全市场行情为分页拉取（约 1 分钟），频繁重复调用会被新浪暂时封 IP，`--loop` 模式每日只刷一次，无此问题。
- `"em"`（东方财富，原实现）：若你的网络能正常访问东财接口，可切回，速度更快。

## 信号定义

与你的 TradingView Pine 代码 / 同花顺公式完全一致：

- `fish2 = fish1[1]`，即 Trigger 就是 Fisher 的前一根值
- 因此「上穿」= **Fisher 由跌转升的拐点**：`fish[t] > fish[t-1]` 且 `fish[t-1] <= fish[t-2]`
- 指标为递归计算，脚本取最近全部 bar（暖机 80 根以上即精确）

A 股 60 分钟 bar 一天 4 根，东财时间戳为 bar **结束**时刻：10:30 / 11:30 / 14:00 / 15:00。

## 文件结构

```
fisher_60min_scanner/
├── fisher_scanner.py    # 主程序（配置区在文件头部，参数可调）
├── build_pool_gm.py     # 五池生成器（掘金 gm 版，当前默认；跑在 .venv-gm）
├── build_pool_dual.py   # 三池生成器（新浪版，备用）
├── scan_all.cmd         # 定时任务入口：五个池 + 持仓下穿 依次扫描
├── build_pool.cmd       # 建池任务入口（gm 版）
├── run_hidden.vbs       # 隐藏控制台启动器（计划任务经它调用 .cmd，不弹窗）
├── holdings.csv         # 持仓清单（--buy 登记，下穿监控对象）
├── AGENTS.md            # AI 助手交接文档
├── requirements.txt     # 依赖
├── 使用说明.md          # 本文件
├── results/             # 扫描结果 CSV（运行后自动生成）
└── scanner.log          # 运行日志（运行后自动生成）
```

## 每日工作流（推荐）

全自动：Windows 计划任务工作日 00:00 建池（gm 版），盘中 10:31/11:31/14:01/15:01
扫描全部五个池 + 持仓下穿并推送企业微信（见下文「正式运行」）。

手动命令：

```bash
# 建池（掘金 gm 版，约 1 分钟；需掘金终端运行并登录）
.venv-gm\Scripts\python.exe build_pool_gm.py

# 盘中扫描（按池选用）
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_right.csv
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_deep.csv
```

### 方案一：右侧 / 左侧 / 深水 / T0 / T1 五池构建（两个数据源任选）

五个池一次构建（T0/T1 ETF 池仅 gm 版支持），输出文件名固定
（`pool_right.csv` / `pool_left.csv` / `pool_deep.csv` / `pool_t0.csv` / `pool_t1.csv`），扫描器无需任何改动。

**方案 A：掘金 gm（推荐，需安装掘金终端并登录）**

```bash
python -m venv .venv-gm                                    # 首次：独立环境（gm 依赖老版 pandas）
.venv-gm\Scripts\python.exe -m pip install gm
# 在掘金终端生成 token，写入 gm_token.key（一行，已 gitignore）
.venv-gm\Scripts\python.exe build_pool_gm.py               # 全量约 2 分钟，走本地终端，无新浪限流
.venv-gm\Scripts\python.exe build_pool_gm.py --resume      # 断点续跑
```

优点：自带剔 ST/停牌、按上市日期精确过滤满 1 年、真实成交额、前复权日线、极快。

**方案 B：新浪（无需注册任何账号，开箱即用）**

```bash
pip install -r requirements.txt
.venv\Scripts\python.exe build_pool_dual.py            # 全量约 90 分钟，建议夜间运行
.venv\Scripts\python.exe build_pool_dual.py --resume   # 断点续跑
```

两个方案的过滤/分类逻辑完全一致，输出可互相替换：

```bash
# 盘中按策略选用（scan_all.cmd 已含全部五个池 + 持仓监控）：
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_right.csv   # 右侧
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_left.csv    # 左侧
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_deep.csv    # 深水
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_t0.csv      # T+0 ETF
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_t1.csv      # T+1 ETF
```

公共条件：仅沪深主板（剔创业板/科创板/北交所）、非 ST/退、非停牌、股价 ≥ 2 元、
20 日均成交额 ≥ 2 亿、20 日均振幅 ≥ 2.5%、上市满 1 年。输出含 dif/dea/fisher_daily 核对列。

五个池的分化条件：

| 池 | 条件 | 思路 |
|---|---|---|
| 右侧 pool_right.csv | MACD DIF 连升两日 且 DIF > DEA 且 **DIF > 0** | 零上趋势已成，追随 |
| 左侧 pool_left.csv | MACD DIF 连升两日 且 DIF < DEA 且 **DIF < 0** | 零下拐点将至，埋伏 |
| 深水 pool_deep.csv（实验） | 无 MACD 闸门，日线 Fisher < -2 | 深度超卖反弹 |
| T0 pool_t0.csv | T+0 ETF + 日线 Fisher < -2（剔联接/货币，上市 120 天+，成交额 ≥ 1 亿、振幅 ≥ 1%） | 超卖反弹，当日可进出 |
| T1 pool_t1.csv | T+1 ETF + 日线 Fisher < -2（同上过滤） | 超卖反弹 |

（零上回调 DIF>0 但 DIF<DEA、零下反弹 DIF<0 但 DIF>DEA 的中间态两边都不入。）

注意：日线 Fisher 以凌晨建池时的上一交易日收盘为准，盘中固定不变。

### 持仓监控（下穿预警）

买入后登记到 `holdings.csv`（或直接告诉我帮你登记）：

```bash
.venvScriptspython.exe fisher_scanner.py --buy 600036 --price 38.86   # --price 可省，缺省取最新价
```

盘中持仓随 4 个时点任务自动监控 60 分钟**下穿**（由升转跌拐点），命中推送
「持仓鱼塘：N 条鱼下穿」；无命中不推送（避免噪音）。手动触发：

```bash
.venvScriptspython.exe fisher_scanner.py --once --pool-file holdings.csv --side down
```

卖出后编辑 `holdings.csv` 删掉对应行即可。

## 安装与快速测试

```bash
pip install -r requirements.txt

# 先小规模测试（只扫前 50 只，验证环境）
.venvScriptspython.exe fisher_scanner.py --once --limit 50
```

## 正式运行（两种方式选一）

### 方式 A：cron / 任务计划程序（推荐，最稳）

每根 bar 收盘后 1 分钟各跑一次：

```cron
# Linux crontab（周一到周五）；第一行为凌晨建池（用前一交易日收盘数据）
0  0  * * 1-5  cd /路径 && /usr/bin/python3 build_pool_dual.py
31 10 * * 1-5  cd /路径 && for p in right left deep t0 t1; do /usr/bin/python3 fisher_scanner.py --once --pool-file pool_$p.csv; done
31 11 * * 1-5  cd /路径 && for p in right left deep t0 t1; do /usr/bin/python3 fisher_scanner.py --once --pool-file pool_$p.csv; done
1  14 * * 1-5  cd /路径 && for p in right left deep t0 t1; do /usr/bin/python3 fisher_scanner.py --once --pool-file pool_$p.csv; done
1  15 * * 1-5  cd /路径 && for p in right left deep t0 t1; do /usr/bin/python3 fisher_scanner.py --once --pool-file pool_$p.csv; done
```

Windows 已在「任务计划程序」注册 5 个任务（用 `schtasks /query | findstr fisher` 查看）：

| 任务名 | 触发 | 动作 |
|---|---|---|
| `fisher_建池` | 工作日 00:00 | `build_pool.cmd`（gm 重建五池，需掘金终端运行） |
| `fisher_扫描1031` / `1131` / `1401` / `1501` | 工作日对应时刻 | `scan_all.cmd`：依次扫右侧/左侧/深水/T0/T1/持仓下穿，推送企业微信 |

所有任务经 `run_hidden.vbs` 隐藏启动，不弹控制台窗口。

注册命令（任务不存在或需重建时执行）：

```cmd
schtasks /create /f /tn "fisher_建池" /tr "wscript.exe \"D:\钓鱼\run_hidden.vbs\" build_pool.cmd" /sc weekly /d MON,TUE,WED,THU,FRI /st 00:00
schtasks /create /f /tn "fisher_扫描1031" /tr "wscript.exe \"D:\钓鱼\run_hidden.vbs\" scan_all.cmd" /sc weekly /d MON,TUE,WED,THU,FRI /st 10:31
:: 1131 / 1401 / 1501 三条同上，仅改 /tn 与 /st
```

结果 CSV 文件名带 `_right` / `_left` / `_deep` / `_t0` / `_t1` / `_holdings` 后缀区分。

### 方式 B：常驻模式

```bash
.venvScriptspython.exe fisher_scanner.py --loop
```

进程常驻，工作日 10:31 / 11:31 / 14:01 / 15:01 自动扫描（注意：此模式只按星期判断，
遇法定节假日会照常运行并扫描上一交易日数据，可在结果 CSV 的 bar_time 列看出来，不影响正确性）。

## 输出说明

结果存为 `results/fisher_cross_YYYYMMDD_HHMM.csv`：

| 列 | 含义 |
|---|---|
| code / name | 股票代码 / 名称 |
| bar_time | 发生上穿的 bar 时刻（应为最近一次 bar 收盘时刻） |
| close | 该 bar 收盘价（前复权） |
| fisher / trigger | 该 bar 的 Fisher / Trigger 值 |

推送已内置：扫描结果通过**企业微信机器人**推送到微信。配置方式：把 webhook 地址写入项目目录的
`webhook.key` 文件（一行，已在 .gitignore 中），或设置环境变量 `FISHER_WECOM_WEBHOOK`；
两者都没有则不推送。`PUSH_EMPTY=False` 可让上穿池无命中时不打扰（持仓下穿本就无命中不推送）。

## 重要注意事项

1. **盘中判定**：脚本会自动丢弃正在形成中的最后一根 bar（价格未走完会信号闪烁），
   只对已完结 bar 做判断。所以运行时刻必须晚于 bar 收盘时刻，建议照上文 +1 分钟。
2. **限流**：全市场约 5000 只，默认每只间隔 0.25 秒，东财源一轮约 30~45 分钟（新浪源每股 2 次请求，约 1~1.5 小时）。
   接口对频繁请求可能限流，脚本已带重试；若失败数偏多，把 `REQUEST_INTERVAL` 调大到 0.4~0.5。
   **强烈建议先用日线等条件预筛股票池**（如非 ST、成交额、趋势），存成含 `code` 列的 CSV，
   用 `--pool-file pool.csv` 运行，一轮几分钟。
3. **数据长度**：东财 60 分钟线的历史长度有限，少于 `MIN_BARS`（默认 80 根）的票自动跳过。
   递归指标衰减快，80 根以上信号精度即无虞。
4. **复权**：默认前复权（与同花顺默认一致）；除权缺口会造成假拐点，勿用不复权。
   新浪源的复权因子来自其日线接口，若个别票日线拉取失败会静默退回不复权数据，属极少数情况。
5. **与同花顺对数校验**：任选一只票，对比脚本输出 CSV 中的 fisher 值与同花顺副图读数，
   注意两边复权方式、周期（60 分钟）须一致。
6. **升级路径**：未来若换 QMT/xtdata，只需重写 `fetch_60m()` 一个函数
   （返回含 `时间/最高/最低/收盘` 列的 DataFrame），其余逻辑零改动。

## 免责声明

本工具仅为量化研究辅助，输出信号不构成投资建议。Fisher 上穿在震荡市中假信号较多，
建议结合位置（如零轴下方上穿）、成交量等条件过滤，并自行回测后再使用。
