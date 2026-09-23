# AGENTS.md — 给 AI 助手的项目交接文档

## 项目是什么

沪深 A 股/ETF 量化扫描系统：夜间建股票池，盘中扫描 60 分钟 Fisher Transform
上穿信号（买入），持仓股监控下穿信号（卖出预警），结果推送到企业微信机器人
（用户在微信接收）。信号分两类：完结确认（bar 收盘后）和盘中信号（bar 未完结，
`--live` 判定，可能收盘前消失/翻转，推送有标注）。全流程由 Windows 计划任务驱动。

## 文件结构

- `fisher_scanner.py` — 主扫描器（`.venv` 运行）。信号定义、上穿/下穿判定、进程池
  并发、企业微信推送、多数据源（tdxq/sina）。配置区在文件头部。
- `run_scan.py` — 盘中扫描调度器：依次扫持仓（下穿+上穿）→ 深水池 → 右侧/左侧/T0/T1
  → 观察池。`--holdings` 只扫持仓；`--live` 未完结 bar 参与判定；无参数 = 全量完结确认。
- `build_pool_tdxq.py` — 建池（通达信 TQ 版，**当前默认**，须客户端登录在线）。
  产出 pool_right/left/deep/t0/t1 五个 CSV。全市场名单走 tdxq --universe；名称/ST
  过滤用新浪快照；ETF 名称用 TQ get_stock_info，T+0/T+1 按名称关键词分类。
  盘中（15:30 前）运行自动剔除当日未完结日 K。
- `build_pool_dual.py` — 建池（新浪版，**备用**），输出文件名相同。
  （build_pool_gm.py / build_pool.py 已删，git 历史可查。）
- `.cmd` 入口（全部必须 CRLF）：`build_pool.cmd` 建池一条龙（.build_lock 互斥 +
  盘后下载 + 建五池 + HSSR 注解 + --post-close 候选）；`scan_all.cmd` 完结确认；
  `scan_mid.cmd` bar 中段 --live；`scan_holdings.cmd` 持仓 5 分钟 --live；
  `scan_daily.cmd` 深水日共振；`scan_hssr.cmd` HSSR 周报。
  `run_hidden.vbs` 隐藏控制台，所有计划任务经它调用。
- `holdings.csv` — 用户持仓（gitignore）。`watchlist.csv` — 观察池（gitignore）：
  --buy 登记持仓、--sell 清仓入观察池、--watch/--unwatch 手动加/移；
  持仓双向监控（上穿回钩+下穿预警）。
- `webhook.key` — 密钥文件，gitignore，**绝不提交**。
- `cache/esi_ledger.csv` — Fisher-ESI 进出场台账（status: open/closed）；
  `cache/esi_pending.csv` — 待激活台账。cache/ 整体 gitignore。
- `results/`、`scanner.log`、`tdxq_progress.txt` — 运行产物，gitignore。
- `executor_v2.py` — 大QMT 内置执行器 git 主版本（部署见 使用说明.md；部署副本
  D:\国金证券QMT交易端\python\钓鱼.py）。运行时台账：`pending.csv`（R2）、
  `sim_pos.csv`（幽灵持仓账）、`exdef.csv`（T+1 冻结期 latched 卖出信号，坑 #16）。
- `executor_t0.py` — 高频T0 版，部署副本同目录 高频T0.py。当前机制要点：
  ESI 止损周期 5m；ESI/STOP 止损后当日禁再进场（FAILED_TODAY，daily_t0.csv 第 6 列）；
  三条进场路径去重键统一为 15m bar 收盘时刻（key_bar，防止同 bar 双发仓位翻倍）；
  `ENTRY_DEEP_MIN=-1.5` 深极值进场过滤（厚样本实验 G 后半推翻：不再转正但仍砍亏
  约 45%，保留待周度复核，999=禁用）；`STOP_MODE='atr'`（ATR14(15m)×1.5 夹取
  [0.4%, 2.5%]，'fixed'=固定 0.5%）；取数 `get_market_data_ex_ori`（盘中勿用旧
  get_history_data，曾返回止于昨日的分钟历史）。上述参数全在 t0_config.txt 白名单。
  T0 黑榜：513120/513090/589120/589720/513040（513120 是趋势票特例，见未来方向）。
  已否决方向（勿再走）：1h/日线大周期过滤、ESI 二次确认、ESI 时间宽限、关强平
  （深水池=下跌趋势池，隔夜漂移结构性为负，14:55 强平是生存机制）。
  **教训：小样本回测必须警惕行情段巧合**（17 天窗口结论被 77 天厚样本推翻）。
- `sync_qmt.cmd` / `sync_qmt.ps1` — **2026-09-18 实测失效**：客户端对 python\ 目录有
  文件虚拟化，外部写入客户端不可见。部署只能改在客户端编辑器里全选粘贴仓库文件内容；
  盘中调参走 D:\qmt\t0_config.txt（config_override，白名单键），不要在客户端改代码。
- `qmt-live/` — 目录联接 → D:\qmt（QMT 运行时文件），gitignore。
  `watchlist.txt`（executor_v2 交易名单，手工维护小名单，init 时读取，改了要重启策略）；
  `watchlist_t0.txt`（executor_t0 交易名单，手工维护，格式 CODE,VOLUME,T0）。
  sim_pos.csv 列 code,vol,cost,buy_date；非 T0 当日买入禁卖（T+1），
  实盘卖出量取 m_nCanUseVolume。（watchlist.txt 原由 build_qmt_watchlist.py 全池生成，
  现为手工小名单，生成脚本保留。）
- `build_ths_block.py` — 建池后生成同花顺板块导入文件 `ths_blocks/<板块名>.txt`
  （GBK，已挂 build_pool.cmd 末尾）。10 个板块：_ALL=完整池，无后缀=盘后候选分流。
  **用户每天收盘后在客户端手动导入**（导入触发上传云端）。板块权威名单在云端，
  离线写本地文件永不上云（2026-09-17 调查结论，勿再尝试离线写入）。
- `tick_bar_builder.py` — 快照驱动实时 bar 聚合器。部署副本
  D:\tdx\PYPlugins\user\tick_bar_builder.py（UserPY 策略「随终端运行」常驻）。
- `weekend test/` — 大QMT 内置策略测试资产（已纳入 git）。

## Python 环境

- `.venv`（唯一环境）：akshare + pandas 3.x。

## 策略定义（当前版本）

公共过滤（股票）：沪深主板（60/00）、非 ST/退/停牌、股价 ≥ 2 元、
20 日均成交额 ≥ 2 亿、20 日均振幅 ≥ 2.5%、上市满 1 年。

- 右侧池 pool_right.csv：MACD DIF 连升两日 且 DIF > DEA 且 **DIF > 0**（零轴闸门勿去掉）
- 左侧池 pool_left.csv：DIF 连升两日 且 DIF < DEA 且 **DIF < 0**
- 深水池 pool_deep.csv：无 MACD，日线 Fisher(9) < -2；池内按五维打分降序
  （score_deep：下跌减速30%/位置支撑25%/资金CMF20%/周线共振15%/极端度10%）。
  附 HSSR 预计算列 hssr/hssr_n（历史 60m 完结 bar 上穿后 10 根 close 上涨记成功，
  最近 20 次，样本 <5 留空），建池后由 `fisher_scanner.py --annotate-hssr --source tdxq`
  注解写回；深水推送按档位附仓位建议：≥75% 正常、50-75% 减半、<50% 不建议、
  n<5 标「HSSR 样本不足」。
- T0/T1 ETF 池：ETF 统一走深水方案（日线 Fisher < -2），按名称关键词拆分；不走 MACD
- 深水日共振：收盘后复核深水池，日内任一完结 60m bar 上穿 且 当日日 K 上穿 →
  pond=深水日共振，写 results/fisher_cross_*_deepres.csv
- 持仓：60 分钟 Fisher 下穿预警，无命中不推送

**Fisher-ESI 规则 v2**：台账 cache/esi_ledger.csv（含 entry_price、entry_delta 列），
待激活台账 cache/esi_pending.csv。
- R1 进场过滤（所有买入信号）：60m 上穿命中时 30m/15m/5m 任一下行（fish < trigger）
  → bar_state=假性失效，推送标「待激活」并写入 esi_pending。
- 穿越幅度门槛（仅 executor_v2，MIN_CROSS=0.15）：新鲜 60m 上穿必须
  fish60 ≥ trigger + 0.15。实证：贴线桶 HSSR 27~35% vs 深穿桶 52~53%。R2 路径不受限。
- 尾盘禁入（executor_v2/t0 ENTRY_TO='14:40'）：15:00 bar 信号是最差时段桶。
- R2 信号激活：pending 票由持仓任务每 5 分钟复查：三周期信号后都重新上穿且当前
  fish > trigger 且 60m fish > trigger → 推送「信号激活」台账记 open；
  60m fish < trigger → void 作废。
- 进场登记（`esi_register_entries`）：完结且非假性失效的持仓上穿，0 < fisher < 2.5
  → 记 open；已有 open 或当日 failed 跳过。
- R3 持仓失效卖出（`esi_check_invalidation`，30m 收盘后窗口门控）：open 记录完结
  30m bar 下穿时——浮亏 → 推送 Fisher失效 卖出预警，failed=是 closed；浮盈 → 持有。
  60m 下穿正常离场由 `esi_close_on_60m_down` 记 failed=否 closed。
- HSSR 周报：按 code 取最近 20 条 closed，HSSR = failed=否占比，每周日 20:00 推送。

信号：fish2 = fish1[1]，上穿 = Fisher 由跌转升拐点。默认只判最新已完结 60m bar；
`--live` 盘中未完结 bar 也判定（推送标注「盘中信号」）。`notify()` 按
`{日期}|{鱼塘}|{side}|{code}|{bar_time}|{bar_state}` 去重（cache/pushed_signals.json）；
盘中推过后收盘完结确认仍会再推一次。

## 计划任务（Windows schtasks）

**当前状态（2026-09-22 起）：盘中扫描全部暂停**（数据源难以获取）——fisher_持仓 +
fisher_扫描1001/1101/1331/1431/1031/1131/1401/1501 共 9 个任务已 schtasks 禁用
（未删除，`Enable-ScheduledTask -TaskName <名>` 恢复）。QMT 两个执行器在客户端内
独立运行，不在暂停范围。

保留运行：
- `fisher_建池` 周一~周五 **16:00** → build_pool.cmd 一条龙：盘后自动下载
  （tdx_postclose_download.py，refresh_kline 5m/1d 全池+校验）→ tdxq 建五池 →
  深水池 HSSR 注解 → --post-close 全五池完结扫描出候选清单并推送
  （results/candidates_<交易日>.csv，供次日人工挑票入观察池）。候选分两个名单：
  60m 上穿（分池，加日K过滤，run_scan.py DAY_FILTER_MODE 可切）+ 日K上穿
  （六池并集按深水分降序，重叠标「※60m已上穿」）。**需通达信客户端登录在线，
  且 16:00 前做完客户端盘后数据下载**。
- `fisher_日共振` 周一~周五 15:10 → scan_daily.cmd
- `fisher_HSSR周报` 每周日 20:00 → scan_hssr.cmd

机制要点：
- refresh_kline API 拉不到当日分钟数据（2026-09-17 实测），当日分钟线只能靠客户端
  「盘后数据下载」。verify 失败先检查客户端是否做过盘后下载，别反复重试 refresh_kline。
- 盘中扫描时段保护（run_scan.py main）：持仓/中段/完结类扫描仅 09:25–15:30 执行；
  日共振允许补跑到 18:00；--hssr-report / --post-close 不受限。防睡眠唤醒后
  StartWhenAvailable 把错过的盘中任务一次性补跑。
- 所有 fisher 任务已开 StartWhenAvailable；经 run_hidden.vbs 隐藏运行。
- 查询：`schtasks /query | findstr fisher`

## 数据通道（单通道 tdxq）

- **tdxq（默认主力）**：官方通达信客户端 TQ 接口（D:\tdx\PYPlugins\user\tqcenter.py），
  走已登录客户端会话，原生前复权、无限流、bar 时间即收盘时刻。扫描时整池批量预取
  （tdxq_fetch.py 子进程一次会话）落盘 cache/tdxq/，scan_one 本地读 CSV；缓存过期自动
  单票补取；**补取后复核新鲜度，盘中仍停昨日视为取数失败返回 None**（坑 #14）。
  前提：客户端登录常开 + 分钟数据已积累。
  **盘中分钟线是静态库（get_market_data 盘中仅日K）**，解法是 `tick_bar_builder.py`
  （UserPY 策略常驻）：每 20s 批量 get_pricevol 轮询 top50 深水+持仓+观察池+ESI
  pending 票（上限 100），快照合成当日 5m bar 并聚合 15m/30m/1h，原子写
  cache/daily_qfq/tdxq/ 缓存（非除权日复权 ratio=1）。5m high/low 为轮询价 max/min。
  日志 D:\qmt\tick_builder.log。
- **sina（2026-09-17 起停用，勿在自动化链路使用）**：akshare，会限流（HTTP 456，
  冷却几十分钟自愈）。代码里 sina 分支保留（手动 --source sina / HSSR 备用 /
  建池的名称+ST 过滤仍走新浪快照），恢复自动化使用需用户确认。
- 已退役/不可用（均勿切回，细节 git 历史可查）：tdx 公开服务器（服务端拒数）、
  qmt miniQMT（权限被收回）、gm 掘金（整体退役）、em 东财（TLS 指纹封锁，坑 #1/7）、
  腾讯分钟线（接口下线/连接级中止，仅日线 qfq 可作备选）、tushare（付费放弃）、
  akshare 升级（无新通道，只是公开源封装）、stockdb（2026-09-23 用户决定删除，
  D:\stockdb 已删；曾测出口径与 tdxq 不一致，重启用需重新下载+对拍）。

## 踩过的坑（改代码前必读）

1. 东财 push2his K线接口被本机网络 WAF 按 TLS 指纹封锁，Python 任何 TLS 栈都不通。
2. **py_mini_racer 多线程会崩解释器**：fisher_scanner.py 并发必须用进程池，勿改线程池。
3. **akshare 内部请求无超时**曾挂死任务 2 小时：`_patch_requests_timeout()` 勿删。
4. 计划任务 .cmd 里跑 pythonw.exe 会被杀（0xC000013A）：一律用 python.exe。
5. **.cmd 文件必须 CRLF 换行**。
6. 新浪限流：每股请求间隔 ≥ 0.2s；全市场快照每天只调一次；分钟线超限 HTTP 456
   （akshare 表现为 list index out of range）；扫描失败率 >50% 时 notify 推
   「数据源异常」而非「0 条鱼」。
7. 东财 push2（快照）已解封，push2his（K线）仍封。
8. Git Bash 调 Windows 命令先 `export MSYS2_ARG_CONV_EXCL='*'`，否则 /c 被路径转换吃掉。
9. （gm 已退役，坑记录随代码删除。）
10. **建池并发写坏池文件**：build_pool.cmd 有 `.build_lock` 目录互斥锁，手动补跑前
    确认没有别的建池在跑；异常中断残留锁时手动 `rmdir .build_lock`。
11. **大QMT 内置 Python**：pandas 不可用（缺 unicodedata）；取数用
    `ContextInfo.get_history_data` / `get_market_data_ex_ori`；订阅用 `set_universe`；
    源文件必须 UTF-8 且**路径/字符串里的中文会炸**（按 GBK 读源文件，一切路径用纯英文）；
    回测必须显式设起止日期且先在客户端「补充数据」；FileIO 可用。
12. 费雪口径以扫描器 `fisher_transform` 为准（hl2 中价、窗口 9、±0.999 截断），
    QMT 侧统一版为 `weekend test/fisher_test_daily_v3.py`。
13. **客户端升级/重置会清空 UserPY 策略注册**：tick_bar_builder 会静默停跑（日志停在
    上一交易日，无异常栈），部署文件不受影响，重新挂策略即可。PYPlugins\user 目录
    只有 tick_bar_builder.py 需要常驻。
14. **未被 tick_bar_builder 覆盖的票盘中没有真实分钟线**（单票补取也只到昨日收盘，
    曾致 ESI 假激活）。盘中任何涉及非名单票的分钟级判定都要先确认数据含当日 bar。
    节假日休市时新鲜度校验会让分钟取数全部返回 None，属预期行为。
15. **交易会话未登录时 passorder 静默丢单**：ret=0 和客户端「接受」都不是成交证据。
    判据：下单后下一根 bar 持仓仍带 `(sim)` 标记；实锤看 XtClient 日志 passorder 行。
    对策（已实装）：报单后下一根 bar 用 strict_position 核对成交，未成交重报一次，
    仍失败撤 sim 幽灵账 + 企业微信告警；VERIFY 挂起期间该票不发新单。
16. **T+1 冻结期的卖出信号曾被静默丢弃**（600498 浮亏 −3.5% 仓位悬空）：现已实装
    冻结期仍评估卖出条件，命中写持久化台账 `D:\qmt\exdef.csv`，可卖首根 bar 市价执行
    （走 VERIFY 校验）；pos==0 清理 stale。PSELL（浮盈延迟确认）仍是内存态重启即丢。

## 未来方向（设计已定，等数据）

**Regime-Conditional 过滤路由（2026-09-24 设计，待 6-12 个月深数据验证）**：
厚样本（实验 G）显示池内两类票：FALL 态多数票适用 V2 深值反转（减亏 45%），
TREND 态少数票（黄金/创新药，+4.1k）适用浅位趋势跟随，V2 对它们合计伤害 −3825。
- 判定：日线 收盘>MA20 且 MA20 连续 2 日上行 → TREND，否则 FALL；切换需连续 2 根
  日线（迟滞防抖动）；TREND 分支进场另需 60m fish>trigger 确认。
- 路由：TREND → ENTRY_DEEP_MIN 视为 999（V0 行为）；FALL → −1.5（现有 V2）。
  出场逻辑状态无关。
- 实装：executor_t0 加 REGIME_MODE=on（白名单，默认 off），init 时按票算 regime 缓存。
- 验证：回放加 V8 变体；通过标准=池级转正+≥300 笔+比 max(V0,V2) 好 30%+分支归因合理。
  最大死因预判：MA20 判定滞后吃不到趋势前段，备选更快指标=DIF 斜率/10日线。

## 常用操作

- 登记持仓：`.venv\Scripts\python.exe fisher_scanner.py --buy 600036 --price 38.86`
- 加/移观察池：`--watch 002185` / `--unwatch 600498`
- 手动扫描：`.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_right.csv`
  （加 `--live` 判盘中未完结 bar）
- 手动持仓监控：`.venv\Scripts\python.exe run_scan.py --holdings --live`
- 手动建池：`.venv\Scripts\python.exe build_pool_tdxq.py`（须通达信客户端登录在线）
- 单票体检：`.venv\Scripts\python.exe fisher_scanner.py --inspect 600498`（支持中文名；
  加 `--push` 推送到企业微信）
- 推送渠道：企业微信机器人 webhook（webhook.key），notify() 按池文件名区分文案。

## Git

远程：git@github.com:tyleryyy7/angler.git（main 分支，SSH key 已配好）。
提交前确认 git status 里没有 webhook.key / holdings.csv / pool*.csv。
README.md 是 使用说明.md 的副本，改文档时两边同步。
