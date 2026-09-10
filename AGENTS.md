# AGENTS.md — 给 AI 助手的项目交接文档

## 项目是什么

沪深 A 股/ETF 量化扫描系统：夜间建股票池，盘中扫描 60 分钟 Fisher Transform
上穿信号（买入），持仓股监控下穿信号（卖出预警），结果推送到企业微信机器人
（用户在微信接收）。信号分两类：完结确认（bar 收盘后）和盘中信号（bar 未完结，
`--live` 判定，可能收盘前消失/翻转，推送有标注）。全流程由 Windows 计划任务驱动。

## 文件结构

- `fisher_scanner.py` — 主扫描器（主环境 `.venv` 运行）。信号定义、上穿/下穿判定、
  并发扫描、企业微信推送、多数据源（tdx/sina/gm/em）。配置区在文件头部。
- `run_scan.py` — 盘中扫描调度器（`.venv` 运行）：依次扫五池+持仓下穿，通道降级切换。
- `build_pool_gm.py` — 建池（掘金 gm 版，**当前默认**，`.venv-gm` 运行）。
  产出 pool_right.csv / pool_left.csv / pool_deep.csv / pool_t0.csv / pool_t1.csv。
- `build_pool_dual.py` — 建池（新浪版，**备用**，主环境 `.venv` 运行）。
  gm 不可用时切回，输出文件名相同。
- `build_pool.py` — 已删除（旧单池方案，git 历史可查）。
- `scan_all.cmd` — 盘中扫描入口（收盘完结确认，无参数，CRLF 换行，勿改 LF）。
- `scan_mid.cmd` — bar 中段扫描入口：`run_scan.py --live`（未完结 bar 也判定，CRLF）。
- `scan_holdings.cmd` — 持仓每 15 分钟监控入口：`run_scan.py --holdings --live`（CRLF）。
- `scan_daily.cmd` — 深水池日共振收盘复核入口：`run_scan.py --daily-confirm`（CRLF）。
- `scan_hssr.cmd` — HSSR 周报入口：`run_scan.py --hssr-report`（CRLF）。
- `build_pool.cmd` — 建池任务入口（CRLF）：`.build_lock` 互斥锁 + gm 建五池 + `.venv` 深水池 HSSR 注解。
- `run_hidden.vbs` — 隐藏控制台启动器，所有计划任务经它调用 .cmd（防弹窗）。
- `holdings.csv` — 用户持仓（code,name,buy_date,buy_price），gitignore。
- `watchlist.csv` — 观察池（清仓/放过但继续盯上穿回钩的票），gitignore。
  --buy 登记持仓、--sell 清仓入观察池、--watch 手动加观察池、--unwatch 移出观察池；
  持仓双向监控（上穿回钩+下穿预警）。
- `webhook.key` / `gm_token.key` — 密钥文件，gitignore，**绝不提交**。
- `cache/esi_ledger.csv` — Fisher-ESI 进出场台账（code,entry_time,entry_fisher60,
  fail_time,fail_fisher30,failed,bars_held,status；status: open/closed），cache/ 整体已 gitignore。
- `results/`、`scanner.log`、`dual_progress.txt`、`gm_progress.txt` — 运行产物，gitignore。

## 两个 Python 环境（重要，不要混用）

- `.venv`（主环境）：akshare + pandas 3.x，跑 fisher_scanner.py / build_pool_dual.py。
- `.venv-gm`：gm SDK 强制 pandas 1.5 / numpy 1.26，与 akshare 冲突，
  所以 gm 相关脚本只能跑在 `.venv-gm\Scripts\python.exe`。
  **绝不要在主环境 pip install gm**（会降级 pandas 搞坏 akshare）。

## 策略定义（当前版本）

公共过滤（股票）：沪深主板（60/00）、非 ST/退/停牌、股价 ≥ 2 元、
20 日均成交额 ≥ 2 亿、20 日均振幅 ≥ 2.5%、上市满 1 年。

- 右侧池 pool_right.csv：MACD DIF 连升两日 且 DIF > DEA 且 **DIF > 0**（零轴闸门是后加的，勿去掉）
- 左侧池 pool_left.csv：DIF 连升两日 且 DIF < DEA 且 **DIF < 0**
- 深水池 pool_deep.csv：无 MACD，日线 Fisher(9) < -2（实验性）；池内按五维打分降序
  （score_deep：下跌减速30%/位置支撑25%/资金CMF20%/周线共振15%/极端度10%）。
  附 HSSR 预计算列 hssr/hssr_n（价格版：历史 60m 完结 bar 上穿后 10 根 close 上涨记成功，
  取最近 20 次可评估信号，样本 <5 留空），由建池后 .venv 注解步骤
  （`fisher_scanner.py --annotate-hssr`，默认走 tdx 800 根深历史；
  `--source sina` 强制新浪（串行 1.5s/股防限流）；`--source auto` 则 tdx 失败后 sina 兜底）写回 pool_deep.csv；
  深水推送按档位附仓位建议：≥75% 正常仓位、50-75% 仓位减半、<50% 不建议买入、
  样本不足标「HSSR 样本不足 (n<5)」
- T0/T1 ETF 池：ETF 统一走深水方案（日线 Fisher < -2），按 trade_n 拆分；不走 MACD
- 深水日共振：收盘后复核深水池，日内任一根完结 60m bar 上穿 且 当日日 K 上穿，
  推送 pond=深水日共振（bar_state=日共振），结果写 results/fisher_cross_*_deepres.csv
- 持仓：60 分钟 Fisher 下穿预警，无命中不推送

**Fisher-ESI 早期失效规则（持仓版）**：台账 cache/esi_ledger.csv。
- 进场登记：持仓票**完结** 60m bar Fisher 上穿 且 0 < fisher < 2.5 → 记 open；
  已有 open 记录或当日已有 failed 记录（当日禁止重新开仓）则跳过（`esi_register_entries`，
  由 run_scan --holdings 上穿扫描后自动调用）。
- 失效判定：自进场 bar 起 6 根完结 30m bar 窗口（3 交易小时）内，同时满足
  ① 完结 30m bar Fisher 下穿 Trigger；② 最新 60m bar（允许未完结，取当前值）
  fisher > 0 且 < 前一根（大周期未死但环比降低）→ Fisher-Fail，记 failed=是 closed，
  推送 pond=Fisher失效（side=down）平仓预警（`esi_check_invalidation`，
  仅在 30m 收盘后窗口 10:01-10:15/10:31-10:45/…/15:01-15:15 跑，门控在 run_scan --holdings 分支）。
- 窗口存活满 6 根未失效 → 记成功 closed（failed=否，bars_held=6）。
- HSSR 周报：按 code 取最近 20 条 closed 记录，HSSR = 未失效数/总数，附平均失效分钟数，
  每周日 20:00 推送（`hssr_report`，run_scan --hssr-report，在周末保护之前豁免）。

信号：fish2 = fish1[1]，上穿 = Fisher 由跌转升拐点。默认只判最新已完结 60 分钟 bar；
`--live` 时盘中未完结 bar 也参与判定（命中带 `bar_state=未完结`，推送标注「盘中信号」）。
`notify()` 按 `{日期}|{鱼塘}|{side}|{code}|{bar_time}|{bar_state}` 去重（cache/pushed_signals.json），
同一根 bar 同一完结状态的信号当日只推一次；盘中（未完结）推过后，收盘完结确认仍会再推一次
（去重判定兼容当日旧的 5 段键，防止升级当天重复推送）。

## 计划任务（Windows schtasks）

- `fisher_建池` 00:00 周一~周五 → build_pool.cmd（gm 建五池 + .venv HSSR 注解两步，
  **需掘金终端运行并登录**）
- `fisher_持仓` 每天 9:31–15:20 每 15 分钟 → scan_holdings.cmd
  （daily 任务，周末由 run_scan.py 内的 weekday 保护直接退出；节假日空跑但不会重复推送，去重兜底）
- `fisher_扫描1001/1101/1331/1431` 周一~周五 → scan_mid.cmd（bar 中段，--live 盘中信号）
- `fisher_扫描1031/1131/1401/1501` 周一~周五 → scan_all.cmd（bar 收盘后，完结确认）
- `fisher_日共振` 周一~周五 15:10 → scan_daily.cmd（深水池日共振收盘复核）
- `fisher_HSSR周报` 每周日 20:00 → scan_hssr.cmd（Fisher-ESI 台账 HSSR 推送；
  run_scan 的 --hssr-report 分支在周末保护之前，周日可跑）
- 任务经 run_hidden.vbs 隐藏运行。
- 所有 fisher 任务已开启「错过计划启动后尽快补跑」（StartWhenAvailable）：
  电脑睡眠/关机错过触发点时，唤醒后会自动补跑一次（2026-08-26 起）。
- `build_pool_gm.py` 盘中（15:30 前）运行时自动剔除当日未完结日 K，避免半成品 bar 污染指标。
- 重建命令见 使用说明.md。查询：`schtasks /query | findstr fisher`

## 数据通道（降级链：tdx → sina → gm）

- **tdx（默认）**：xmtdx 库（通达信 TCP 协议，纯标准库，主环境 .venv），快、无限流、
  不需要 akshare；原始数据不复权，现已在 fetch 时乘新浪日线前复权因子（当日缓存，
  与 sina 分支共用 cache/daily_qfq/，按 bar 日期逐日映射）做近似前复权；
  因子获取失败时本股退回不复权（log warning），不触发通道降级。
  注意：tdx 盘中第二根 60m bar 会标 13:00（跨午休怪癖），fetch 时按「当日第几根」
  映射到 10:30/11:30/14:00/15:00 收盘时刻，勿改回原始时间戳。30m 同理：每天 8 根，
  `_TDX_BAR_ENDS_30`（10:00/10:30/11:00/11:30/13:30/14:00/14:30/15:00），
  `fetch_30m` 用 `KlineCategory.MIN_30`、count=600（单次上限 800，保证 Fisher 暖机），
  时间映射循环已抽成 `_tdx_map_bar_times(df, bar_ends)` 供 60m/30m 共用。
- **sina**：akshare，前复权准，但会限流（HTTP 456，冷却几十分钟自愈）。
  HSSR 注解可用 `--source sina` 或 `--source auto`（tdx 失败后自动切 sina，串行 1.5s/股防限流）。
- **gm**：掘金，须 .venv-gm 且终端运行；免费版 60m 历史有配额（报 status 1014），只作兜底。
  HSSR 预计算需要的 800 根 60m 深历史走 tdx（`fetch_60m(code, count=800)`，
  gm 配额坑不可用于此，注解步骤绝不放 gm 脚本）。
- **em（东财）**：push2his K 线接口被本机网络 WAF 按 TLS 指纹封锁，不可用。

`run_scan.py` 是盘中扫描调度器：优先扫持仓（下穿+上穿）和深水池，再扫右/左/T0/T1/观察池，通道失败率 >50% 自动降级。
参数：`--holdings` 只扫持仓两项（每 15 分钟任务用）；`--live` 盘中未完结 bar 参与判定（中段任务用）；无参数 = 全量完结确认。
`fisher_scanner.py --source tdx|sina|gm|em` 可手动指定通道，`--live` 可手动跑盘中信号。

## 踩过的坑（改代码前必读）

1. **东财接口（push2his.eastmoney.com K线）被本机网络 WAF 封锁**（按 TLS 指纹/IP），
   Python 任何 TLS 栈都不通。勿轻易切回 em。
2. **py_mini_racer 多线程会崩解释器**（akshare 新浪前复权链路依赖它）：
   fisher_scanner.py 的并发必须用**进程池**（ProcessPoolExecutor），勿改线程池。
3. **akshare 内部请求无超时**，曾导致任务挂死 2 小时：
   两个脚本都有 `_patch_requests_timeout()`，勿删。
4. **计划任务里 .cmd 中跑 pythonw.exe 会被杀**（0xC000013A，GUI 程序退出带走隐藏控制台）：
   .cmd 里一律用 `python.exe`，靠 run_hidden.vbs 隐藏窗口。
5. **.cmd 文件必须 CRLF 换行**（LF 会导致批处理解析异常）。
6. 新浪限流：每股请求间隔 ≥ 0.2s；全市场快照（stock_zh_a_spot）每天只调一次。
   分钟线接口超限会返回 HTTP 456（akshare 表现为 list index out of range），
   冷却几十分钟自愈。扫描失败率 >50% 时 notify 会推「数据源异常」而非「0 条鱼」。
   优化：sina 分支不走 akshare 的 qfq 合成（每股 5 次请求），改为分钟线 jsonp 直连
   + 日线复权因子当日缓存（cache/daily_qfq/，同日因子不变，准确性无损），每股 1 次请求。
7. 东财状态（2026-08-24）：push2（行情快照）已对 Python 解封，
   push2his（K线，扫描器依赖）仍按 TLS 指纹封锁，暂不能切回 em。
8. Git Bash 里调 cmd/schtasks 等 Windows 命令要先 `export MSYS2_ARG_CONV_EXCL='*'`，
   否则 /c 等参数会被路径转换吃掉。
9. gm 免费版 quota：日线历史随便拉，但 60 分钟线大批量拉取会报
   `{"status": 1014, "message": "历史行情服务调用错误"}`，所以 gm 不作扫描主通道。
10. gm `get_symbols(skip_st=True)` 的 is_st 标志不完整（实测 ST龙津/ST洲际 漏剔），
    建池必须叠加名称过滤 `~sec_name.str.contains("ST|退")`。
11. **建池并发写坏池文件**（2026-09-09 实发）：fisher_建池 的 StartWhenAvailable 补跑
    与手动 build_pool.cmd 同时运行，两轮 gm 建池交错，增量 save_results 用未完成的
    深水池（38 只）覆盖完整池（127 只），随后 HSSR 注解按 38 只写回。build_pool.cmd
    已加 `.build_lock` 目录互斥锁（第二个实例直接退出），**手动补跑前确认没有别的
    建池在跑**；异常中断残留 .build_lock 时手动 `rmdir .build_lock`。

## 常用操作

- 登记持仓：`.venv\Scripts\python.exe fisher_scanner.py --buy 600036 --price 38.86`
- 移出观察池：`.venv\Scripts\python.exe fisher_scanner.py --unwatch 600498`
- 手动扫描：`.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_right.csv`
  （加 `--live` 判盘中未完结 bar）
- 手动持仓监控：`.venv\Scripts\python.exe run_scan.py --holdings --live`
- 手动建池：`.venv-gm\Scripts\python.exe build_pool_gm.py`
- 单票体检：`.venv\Scripts\python.exe fisher_scanner.py --inspect 600498`（支持中文名，
  先查各池/持仓/观察池 CSV 反查、查不到用新浪快照反查；加 `--push` 推送报告到企业微信）
- 推送渠道：企业微信机器人 webhook（webhook.key），notify() 已实现，按池文件名
  自动区分推送文案（右侧/左侧/深水/T0/T1/持仓鱼塘）。

## Git

远程：git@github.com:tyleryyy7/angler.git（main 分支，SSH key 已配好）。
提交前确认 git status 里没有 webhook.key / gm_token.key / holdings.csv / pool*.csv。
README.md 是 使用说明.md 的副本，改文档时两边同步。
