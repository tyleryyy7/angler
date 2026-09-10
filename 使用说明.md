# Fisher Transform 60 分钟线「刚上穿」扫描器（沪深 A 股）

基于 akshare 的盘中实时预警脚本。扫描股票池，选出**刚发生 Fisher 上穿 Trigger** 的股票。
信号分两类：**完结确认**（60 分钟 bar 收盘后判定）和**盘中信号**（`--live`，bar 未走完就判定，
可能收盘前消失/翻转，推送会标注「未完结」）。同一根 bar 同一完结状态的信号当日只推一次；
盘中信号推过后，收盘完结确认仍会再推一次。

数据源由 `fisher_scanner.py` 配置区的 `DATA_SOURCE`（或 `--source` 参数）决定：

- `"tdx"`（默认，通达信 xmtdx 库）：TCP 协议直连券商行情服务器，快、无限流、免注册；
  原始数据不复权，脚本已自动乘新浪日线前复权因子（当日缓存）做近似前复权。
- `"sina"`（新浪）：前复权准确，但分钟线接口会限流（HTTP 456），冷却几十分钟自愈。
- `"gm"`（掘金）：须 .venv-gm 环境且终端运行；免费版 60 分钟历史有配额，仅作兜底。
- `"em"`（东方财富）：K线接口被本机网络 WAF 封锁，本机不可用。

盘中调度器 `run_scan.py` 按 **tdx → sina → gm** 顺序自动降级（失败率 >50% 即切换）。

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
├── scan_all.cmd         # 定时任务入口：全部池 + 持仓，完结确认（bar 收盘后）
├── scan_mid.cmd         # bar 中段扫描入口（--live，盘中信号）
├── scan_holdings.cmd    # 持仓每 15 分钟监控入口（--holdings --live）
├── scan_daily.cmd       # 深水池日共振收盘复核入口（--daily-confirm，15:10）
├── scan_hssr.cmd        # HSSR 周报入口（--hssr-report，每周日 20:00）
├── build_pool.cmd       # 建池任务入口（.build_lock 互斥锁 + gm 建五池 + .venv 深水池 HSSR 注解）
├── run_hidden.vbs       # 隐藏控制台启动器（计划任务经它调用 .cmd，不弹窗）
├── holdings.csv         # 持仓清单（--buy 登记，下穿监控对象）
├── AGENTS.md            # AI 助手交接文档
├── requirements.txt     # 依赖
├── 使用说明.md          # 本文件
├── results/             # 扫描结果 CSV（运行后自动生成）
└── scanner.log          # 运行日志（运行后自动生成）
```

## 每日工作流（推荐）

全自动：Windows 计划任务工作日 00:00 建池（gm 版）；盘中每根 60 分钟 bar 的中段
（10:01/11:01/13:31/14:31，四根 bar 的中点）扫盘中信号（未完结 bar）、收盘后（10:31/11:31/14:01/15:01）
扫完结确认，全部池 + 持仓并推送企业微信；持仓另加每 15 分钟（9:31 起）高频监控
（见下文「正式运行」）。

手动命令：

```bash
# 建池（掘金 gm 版，约 1 分钟；需掘金终端运行并登录）
.venv-gm\Scripts\python.exe build_pool_gm.py

# 深水池 HSSR 注解（主环境 .venv，默认 tdx 800 根深历史；build_pool.cmd 已含此步）
.venv\Scripts\python.exe fisher_scanner.py --annotate-hssr

# tdx 夜间不可用时，用 sina 兜底（串行 1.5s/股防限流，约 4 分钟）
.venv\Scripts\python.exe fisher_scanner.py --annotate-hssr --source sina

# tdx 优先、失败票自动切 sina 兜底
.venv\Scripts\python.exe fisher_scanner.py --annotate-hssr --source auto

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
| 深水 pool_deep.csv（实验） | 无 MACD 闸门，日线 Fisher < -2；附 HSSR 预计算列（见下） | 深度超卖反弹，池内按五维打分降序 |
| T0 pool_t0.csv | T+0 ETF + 日线 Fisher < -2（剔联接/货币，上市 120 天+，成交额 ≥ 1 亿、振幅 ≥ 1%） | 超卖反弹，当日可进出 |
| T1 pool_t1.csv | T+1 ETF + 日线 Fisher < -2（同上过滤） | 超卖反弹 |

（零上回调 DIF>0 但 DIF<DEA、零下反弹 DIF<0 但 DIF>DEA 的中间态两边都不入。）

**深水日共振**（收盘复核，15:10）：深水池中当天任一根已完结 60m bar Fisher 上穿
且当日日 K（已完结）Fisher 也上穿的票，推送 pond=深水日共振，结果写
`results/fisher_cross_*_deepres.csv`。手动跑：`run_scan.py --daily-confirm`
或 `fisher_scanner.py --daily-resonance`。

**深水池打分**（pool_deep.csv 按 score 降序，列 sA~sE 为分项）：

| 维度 | 权重 | 规则 |
|---|---|---|
| A 下跌减速 | 30% | MACD 绿柱连缩 2 日 +1；DIF 拐头 +1；Fisher 底背离 +1，封顶 +2 |
| B 位置支撑 | 25% | 距 240 日前低 ≤3% → +2，≤8% → +1；破位 → -2 |
| C 资金 CMF(20) | 20% | 连续 3 日改善 +1（且 >0 则 +2）；连续 3 日恶化 -1；创 20 日新低 -2 |
| D 周线共振 | 15% | 周线 Fisher < -2 → +2；周线 Fisher >1 且拐头向下 → -2（周一最糙周四五最准） |
| E 极端度归一化 | 10% | 当前 Fisher 在自身历史分布 <5% 分位 → +2；<15% → +1 |

总分 = Σ权重×分项，范围 -2~+2，仅 gm 版建池支持。

**深水池 HSSR 预计算**（`pool_deep.csv` 的 hssr/hssr_n 两列）：建池后由主环境 .venv
注解步骤（`build_pool.cmd` 第二步，或手动 `fisher_scanner.py --annotate-hssr`）
对每只股票回测历史 60m 完结 bar 上穿信号——信号出现后 10 根 bar close 上涨记成功，
取最近 20 次可评估信号（最后 10 根 bar 内的信号不可评估，剔除），样本 < 5 留空。
深历史走 tdx 800 根（约 200 交易日）；tdx 夜间不可用时可用 `--source sina`（串行防限流）
或 `--source auto`（tdx 失败后自动切 sina）兜底；gm 60m 批量拉取有配额限制，不可用于此。
深水推送按档位附仓位建议：≥75% 正常仓位；50–75% 仓位减半；<50% 不建议买入；
样本不足标注「HSSR 样本不足 (n<5)」。

注意：日线 Fisher 以凌晨建池时的上一交易日收盘为准，盘中固定不变。

### 持仓监控（下穿预警 + 上穿回钩）与观察池

买入后登记到 `holdings.csv`（或直接告诉我帮你登记）：

```bash
.venv\Scripts\python.exe fisher_scanner.py --buy 600036 --price 38.86   # --price 可省，缺省取最新价
```

- **持仓双向监控**：每个时点扫下穿（卖出预警「持仓鱼塘：N 条鱼下穿」）+ 上穿（回钩/加仓提示
  「持仓鱼塘：N 条鱼回钩」）。部分减仓不用动文件。
- **清仓**：`--sell CODE` 一条命令把股票移出持仓并加入观察池 `watchlist.csv`，
  继续盯 60 分钟上穿，命中推送「观察鱼塘：N 条鱼回钩」——日线逻辑还在的票不会跟丢。
- **观察池**：也可直接 `--watch CODE` 手动加入任意股票、`--unwatch CODE` 移出；`--buy` 买回时自动移出观察池。
- 持仓/观察池无命中不推送（避免噪音）；卖出后不想盯了就 `--unwatch CODE` 或删掉 watchlist.csv 对应行。

手动触发持仓下穿扫描：

```bash
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file holdings.csv --side down
```

卖出后编辑 `holdings.csv` 删掉对应行即可。

### 单票体检（--inspect）

```bash
.venv\Scripts\python.exe fisher_scanner.py --inspect 600498          # 支持代码或中文名
.venv\Scripts\python.exe fisher_scanner.py --inspect 烽火通信 --push  # --push 把报告推送到企业微信
```

名称先从各池/持仓/观察池 CSV 反查代码，查不到再用新浪全市场快照反查，都查不到才报错。
报告含五段：身份（持仓成本/现价/浮盈、观察池、五池归属，深水池附 score 和 hssr/hssr_n）、
日线（fisher/trigger、深水条件 fisher<-2、日 K 上穿）、60m（完结/live 两种口径信号、
最近一次上穿/下穿时间）、30m+ESI（台账记录与 open 窗口进度 x/6）、
HSSR（现场计算，附仓位档位）。单项取数失败只标注「取数失败」，不影响其他段。

### Fisher-ESI 早期失效规则（持仓版）

对持仓票的 60m 上穿进场做「早期失效」跟踪，台账存 `cache/esi_ledger.csv`：

- **进场登记**：持仓票出现**完结** 60m bar Fisher 上穿，且 0 < fisher < 2.5 → 台账记 open
  （`run_scan.py --holdings` 上穿扫描后自动调用；已有 open 或当日已 failed 的票不重复登记，
  当日禁止重新开仓）。
- **失效判定（Fisher-Fail）**：自进场 bar 起 6 根完结 30m bar 窗口（3 交易小时）内，
  同时满足 ①完结 30m bar Fisher 下穿 Trigger；②最新 60m bar（允许未完结）fisher > 0
  且环比下降（大周期未死但开始走弱）→ 记 failed=是，推送「Fisher失效」平仓预警。
  判定在 30m bar 收盘后窗口（10:01-10:15 / 10:31-10:45 / … / 15:01-15:15，
  正好落在每 15 分钟持仓任务的网格上）自动执行。
- **窗口存活**：满 6 根 30m bar 未失效 → 记成功（failed=否，bars_held=6）。
- **HSSR 周报**：按 code 取最近 20 条 closed 台账记录，HSSR = 未失效数/总数，
  附平均失效分钟数，每周日 20:00 推送企业微信。手动跑：
  `.venv\Scripts\python.exe run_scan.py --hssr-report`。

## 安装与快速测试

```bash
pip install -r requirements.txt

# 先小规模测试（只扫前 50 只，验证环境）
.venv\Scripts\python.exe fisher_scanner.py --once --limit 50
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

Windows 已在「任务计划程序」注册以下任务（用 `schtasks /query | findstr fisher` 查看）：

| 任务名 | 触发 | 动作 |
|---|---|---|
| `fisher_建池` | 工作日 00:00 | `build_pool.cmd`（gm 重建五池 + .venv 深水池 HSSR 注解，需掘金终端运行） |
| `fisher_持仓` | 每天 9:31–15:20 每 15 分钟 | `scan_holdings.cmd`：持仓上穿/下穿盘中监控（周末脚本内自动退出） |
| `fisher_扫描1001` / `1101` / `1331` / `1431` | 工作日对应时刻 | `scan_mid.cmd`：全部池 + 持仓，**盘中信号**（未完结 bar） |
| `fisher_扫描1031` / `1131` / `1401` / `1501` | 工作日对应时刻 | `scan_all.cmd`：全部池 + 持仓，**完结确认**（bar 收盘后） |
| `fisher_日共振` | 工作日 15:10 | `scan_daily.cmd`：深水池日共振收盘复核（60m 日内上穿 + 当日日 K 上穿） |
| `fisher_HSSR周报` | 每周日 20:00 | `scan_hssr.cmd`：Fisher-ESI 台账 HSSR 周报推送 |

所有任务经 `run_hidden.vbs` 隐藏启动，不弹控制台窗口；均已开启
「错过计划启动后尽快补跑」（StartWhenAvailable）。

注册命令（任务不存在或需重建时执行）：

```cmd
schtasks /create /f /tn "fisher_建池" /tr "wscript.exe \"D:\钓鱼\run_hidden.vbs\" build_pool.cmd" /sc weekly /d MON,TUE,WED,THU,FRI /st 00:00
schtasks /create /f /tn "fisher_持仓" /tr "wscript.exe \"D:\钓鱼\run_hidden.vbs\" scan_holdings.cmd" /sc minute /mo 15 /st 09:31 /et 15:20
schtasks /create /f /tn "fisher_扫描1001" /tr "wscript.exe \"D:\钓鱼\run_hidden.vbs\" scan_mid.cmd" /sc weekly /d MON,TUE,WED,THU,FRI /st 10:01
:: 1101 / 1331 / 1431 三条同上（scan_mid.cmd），仅改 /tn 与 /st
schtasks /create /f /tn "fisher_扫描1031" /tr "wscript.exe \"D:\钓鱼\run_hidden.vbs\" scan_all.cmd" /sc weekly /d MON,TUE,WED,THU,FRI /st 10:31
:: 1131 / 1401 / 1501 三条同上（scan_all.cmd），仅改 /tn 与 /st
schtasks /create /f /tn "fisher_日共振" /tr "wscript.exe \"D:\钓鱼\run_hidden.vbs\" scan_daily.cmd" /sc weekly /d MON,TUE,WED,THU,FRI /st 15:10
schtasks /create /f /tn "fisher_HSSR周报" /tr "wscript.exe \"D:\钓鱼\run_hidden.vbs\" scan_hssr.cmd" /sc weekly /d SUN /st 20:00
```

补跑开关（新注册任务需执行一次）：

```powershell
Get-ScheduledTask -TaskName "任务名" | ForEach-Object { $_.Settings.StartWhenAvailable = $true; $_ } | Set-ScheduledTask
```

结果 CSV 文件名带 `_right` / `_left` / `_deep` / `_t0` / `_t1` / `_holdings` 后缀区分。

### 方式 B：常驻模式

```bash
.venv\Scripts\python.exe fisher_scanner.py --loop
```

进程常驻，工作日 10:31 / 11:31 / 14:01 / 15:01 自动扫描（注意：此模式只按星期判断，
遇法定节假日会照常运行并扫描上一交易日数据，可在结果 CSV 的 bar_time 列看出来，不影响正确性）。

## 输出说明

结果存为 `results/fisher_cross_YYYYMMDD_HHMM.csv`：

| 列 | 含义 |
|---|---|
| code / name | 股票代码 / 名称 |
| bar_time | 发生上穿的 bar 时刻（bar 结束时刻） |
| close | 该 bar 收盘价（盘中信号为当时最新价） |
| fisher / trigger | 该 bar 的 Fisher / Trigger 值 |
| bar_state | 完结 / 未完结（盘中信号，收盘前可能消失或翻转） |

推送已内置：扫描结果通过**企业微信机器人**推送到微信。配置方式：把 webhook 地址写入项目目录的
`webhook.key` 文件（一行，已在 .gitignore 中），或设置环境变量 `FISHER_WECOM_WEBHOOK`；
两者都没有则不推送。`PUSH_EMPTY=False` 可让上穿池无命中时不打扰（持仓下穿本就无命中不推送）。

## 重要注意事项

1. **盘中判定**：默认脚本自动丢弃正在形成中的最后一根 bar（价格未走完会信号闪烁），
   只对已完结 bar 做判断；加 `--live` 则未完结 bar 也参与判定（中段扫描任务使用，
   推送会标注「未完结」）。同一根 bar 同一完结状态的信号当日只推一次（`cache/pushed_signals.json`
   去重，键含 bar 完结状态），盘中信号推过后，收盘完结确认仍会再推一次。
2. **限流**：全市场约 5000 只，默认每只间隔 0.25 秒，东财源一轮约 30~45 分钟（新浪源每股 2 次请求，约 1~1.5 小时）。
   接口对频繁请求可能限流，脚本已带重试；若失败数偏多，把 `REQUEST_INTERVAL` 调大到 0.4~0.5。
   **强烈建议先用日线等条件预筛股票池**（如非 ST、成交额、趋势），存成含 `code` 列的 CSV，
   用 `--pool-file pool.csv` 运行，一轮几分钟。
3. **数据长度**：东财 60 分钟线的历史长度有限，少于 `MIN_BARS`（默认 80 根）的票自动跳过。
   递归指标衰减快，80 根以上信号精度即无虞。
4. **复权**：默认前复权（与同花顺默认一致）；除权缺口会造成假拐点，勿用不复权。
   tdx 源原始数据不复权，脚本已在 fetch 时乘新浪日线因子近似前复权（与 sina 分支同口径，
   实测与 sina 复权序列偏差 <0.2%）；个别票因子拉取失败会静默退回不复权数据，属极少数情况。
5. **与同花顺对数校验**：任选一只票，对比脚本输出 CSV 中的 fisher 值与同花顺副图读数，
   注意两边复权方式、周期（60 分钟）须一致。
6. **升级路径**：未来若换 QMT/xtdata，只需重写 `fetch_60m()` 一个函数
   （返回含 `时间/最高/最低/收盘` 列的 DataFrame），其余逻辑零改动。

## 免责声明

本工具仅为量化研究辅助，输出信号不构成投资建议。Fisher 上穿在震荡市中假信号较多，
建议结合位置（如零轴下方上穿）、成交量等条件过滤，并自行回测后再使用。
