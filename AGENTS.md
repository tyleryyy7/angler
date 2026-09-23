# AGENTS.md — 给 AI 助手的项目交接文档

## 项目是什么

沪深 A 股/ETF 量化扫描系统：夜间建股票池，盘中扫描 60 分钟 Fisher Transform
上穿信号（买入），持仓股监控下穿信号（卖出预警），结果推送到企业微信机器人
（用户在微信接收）。信号分两类：完结确认（bar 收盘后）和盘中信号（bar 未完结，
`--live` 判定，可能收盘前消失/翻转，推送有标注）。全流程由 Windows 计划任务驱动。

## 文件结构

- `fisher_scanner.py` — 主扫描器（主环境 `.venv` 运行）。信号定义、上穿/下穿判定、
  并发扫描、企业微信推送、多数据源（tdxq/sina；em 本机被封）。配置区在文件头部。
- `run_scan.py` — 盘中扫描调度器（`.venv` 运行）：扫持仓/深水/观察池，通道降级切换。
- `build_pool_tdxq.py` — 建池（通达信客户端 TQ 版，**当前默认**，主环境 `.venv` 运行，
  须客户端登录在线）。产出 pool_right.csv / pool_left.csv / pool_deep.csv / pool_t0.csv / pool_t1.csv。
  全市场名单走 tdxq --universe；股票名称/ST 过滤用新浪快照；ETF 名称用 TQ get_stock_info，
  T+0/T+1 用名称关键词分类（TQ 无 trade_n 字段）。
- `build_pool_dual.py` — 建池（新浪版，**备用**，主环境 `.venv` 运行）。
  tdxq 不可用时切回，输出文件名相同。
- `build_pool_gm.py` — 已删除（掘金 gm 版，2026-09-14 随 gm 通道退役，git 历史可查）。
- `build_pool.py` — 已删除（旧单池方案，git 历史可查）。
- `scan_all.cmd` — 盘中扫描入口（收盘完结确认，无参数，CRLF 换行，勿改 LF）。
- `scan_mid.cmd` — bar 中段扫描入口：`run_scan.py --live`（未完结 bar 也判定，CRLF）。
- `scan_holdings.cmd` — 持仓每 5 分钟监控入口：`run_scan.py --holdings --live`（CRLF）。
- `scan_daily.cmd` — 深水池日共振收盘复核入口：`run_scan.py --daily-confirm`（CRLF）。
- `scan_hssr.cmd` — HSSR 周报入口：`run_scan.py --hssr-report`（CRLF）。
- `build_pool.cmd` — 建池任务入口（CRLF）：`.build_lock` 互斥锁 + tdxq 建五池 + `.venv` 深水池 HSSR 注解。
- `run_hidden.vbs` — 隐藏控制台启动器，所有计划任务经它调用 .cmd（防弹窗）。
- `holdings.csv` — 用户持仓（code,name,buy_date,buy_price），gitignore。
- `watchlist.csv` — 观察池（清仓/放过但继续盯上穿回钩的票），gitignore。
  --buy 登记持仓、--sell 清仓入观察池、--watch 手动加观察池、--unwatch 移出观察池；
  持仓双向监控（上穿回钩+下穿预警）。
- `webhook.key` — 密钥文件，gitignore，**绝不提交**（gm_token.key 已随 gm 退役删除）。
- `cache/esi_ledger.csv` — Fisher-ESI 进出场台账（code,entry_time,entry_fisher60,
  fail_time,fail_fisher30,failed,bars_held,status；status: open/closed），cache/ 整体已 gitignore。
- `results/`、`scanner.log`、`tdxq_progress.txt` — 运行产物，gitignore
  （2026-09-17 整理：results 仅保留 9 月起，scanner.log 截断保留最近 5000 行）。
- `executor_v2.py` — 大QMT 内置执行器 git 主版本（项目根目录；部署/格式说明见
  使用说明.md「大QMT 内置执行器」一节；部署副本 D:\国金证券QMT交易端\python\钓鱼.py
  可直接写入，用 `sync_qmt.cmd` 一键同步，备份在 QMT python\backups\）。
  运行时台账：`pending.csv`（R2）、`sim_pos.csv`（幽灵持仓账）、`exdef.csv`
  （T+1 冻结期 latched 的卖出信号，解冻即执行，坑 #16）。
  `executor_t0.py`（高频T0 版）部署副本为同目录 高频T0.py，同一脚本同步。
  2026-09-21 防磨损改版：ESI 止损默认改 5m（TF_ESI=TF_MID，1m 反复抽止损是当日
  T0 池磨损主因）；ESI/STOP 止损后该票当日禁止再进场（FAILED_TODAY，写进
  daily_t0.csv 第 6 列，重启不丢）；BUY 去重键改用 15m bar 收盘时刻
  （ctx_bar_key），fish 在 trigger 附近抖动不再反复触发"新"上穿；
  USE_REENTRY_5M / USE_EXIT_CONFIRM 加入 t0_config.txt 白名单。
  2026-09-23 修复：三条进场路径（cross_up/REENTRY5M/REACTIVATED）的去重键统一为
  key_bar——旧版 REENTRY5M 用 'RE|' 前缀键占同一个 LAST_ACT 槽，与主路径互不失效，
  同一根 15m bar 内 3 秒双发、仓位翻倍（513120 当日 10000→20000 两次）。
  2026-09-23 再加：**深极值进场过滤（ENTRY_DEEP_MIN=-1.5）**——全池回放
  （backtest_t0_replay.py 实验 C，37 只 T0 ETF × 13~17 天，固定 1 万/笔）：
  裸 15m 上穿池级亏损 −3006 元，过滤后 +276（均笔 −5.41→+0.93，32/37 改善，
  硬止损 7 笔→2 笔）；浅位上穿无 edge 是池级结论。1h 趋势对齐（V1）在全池
  反恶化（−1884），勿走。配套：watchlist_t0 移除 513120（实验 A/B/C 三重
  垫底，池级黑榜：513120/513090/589120/589720/513040）。ENTRY_DEEP_MIN 进
  t0_config.txt 白名单（999=禁用）。大周期过滤全否决（实验 D，2026-09-23）：1h 趋势对齐（V1）池级 −1884 反恶化；日线趋势（V4）边际改善仍亏（−641）；日线∧深极值（V5）无叠加（−163 差于 V2）。维持 V2 单过滤。513120 是罕见的'趋势票'（V4 下 +109），单票不足以支撑开关，未来如需 per-code 规则路由再议。严格宇宙稳健性（实验 F，2026-09-23）：宽宇宙 37 只混入了 21 只缓存遗留票（约半数 T+1，回放高估可执行性）；收紧到纯 T0 16 只（pool_t0 13 + 实盘独有 3）后 V2 更强（+276→+646，均笔 +4.55，最大单笔亏 -68.5，14/16 改善，恶化仍只有 513120/513090）。结论对宇宙选择不敏感；周度/月度复核一律用严格宇宙（replay strict 开关）。关强平否决（实验 E，2026-09-23）：V2 去 FLAT 后 +276→-619，过夜 8/8 全亏（首夜缺口均值 -0.68%、为正占比 0%）——深水池=日线 Fisher<-2=下跌趋势池，隔夜漂移结构性为负，14:55 强平是生存机制不是洁癖；样本期若池子转升势可重测。ESI 二次确认制（晚一个 5m cross down 才卖）
  扩样后效应减半（285 笔，+0.05%/笔，15/37 票反例），未实装，数据攒厚再议；
  ESI 时间宽限（延迟 N bar）全样本恶化，否决。回放均含双边佣金 max(5,0.025%)。
  2026-09-22 再加：**止损自适应（STOP_MODE='atr'）**——硬止损宽度按票计算
  ATR14(15m)×1.5、夹取 [0.4%, 2.5%]（下限防最低佣金噪音、上限防高缺口票
  宽失控），每 15m bar 每票算一次缓存；'fixed' 保留原固定 0.5%。动机：
  四只 T0 ETF 平均隔夜缺口 0.6%~1.6%，固定 0.5% 止损在高缺口票上被随机
  振出。参数 STOP_MODE/STOP_ATR_MULT/STOP_PCT_MIN/MAX 全进 t0_config.txt
  白名单。**跳空实证**：缺口回补率 46~56%（无方向优势），"禁顺缺口交易"
  无数据支持未采纳；跳空的真实危害是波动尺度错配 + 早盘价格坐标污染。
  取数走 `ContextInfo.get_market_data_ex_ori`（新接口 get_market_data2 的无 pandas
  封装；官方推荐的 get_market_data_ex 依赖 pandas，内置环境不可用），
  旧 `get_history_data` 仅作异常回退（2026-09-18 起：旧接口盘中曾返回止于昨日的
  分钟历史，导致 fisher 盘中冻结）。
- `sync_qmt.cmd` / `sync_qmt.ps1` — 执行器一键同步脚本（CRLF；中文路径必须走 ps1
  PowerShell，cmd 直接写中文路径会被 GBK 解析乱码——2026-09-15 实测）。
  同步两个执行器：executor_v2.py → 钓鱼.py、executor_t0.py → 高频T0.py。
  **2026-09-18 实测失效**：客户端对 python\ 目录有文件虚拟化——PowerShell 写入的明文
  只有 PS 进程可见，客户端读的是它自己保存的 blob 视图（在客户端编辑器保存过一次
  后触发），外部同步不再生效。部署只能改在客户端编辑器里全选粘贴仓库文件内容；
  盘中调参走 D:\qmt\t0_config.txt（config_override，白名单键），不要在客户端改代码。
- `qmt-live/` — 目录联接 → D:\qmt（QMT 运行时文件：watchlist.txt / pending.csv /
  sim_pos.csv 模拟仓位账本），VSCode 可视化查看编辑，gitignore。
  watchlist.txt 由 `build_qmt_watchlist.py` 生成（合并五池+watchlist.csv，去重，
  补 .SH/.SZ 后缀，每股 100，pool_t0 的标第三列标 T0 允许日内回转，写前备份 .bak；
  2026-09-15 起全池约 400 只跑模拟盘）。sim_pos.csv 列为 code,vol,cost,buy_date，
  当日买入的股票（非 T0）禁止当日卖出（T+1），实盘卖出量取 m_nCanUseVolume。
- `build_qmt_watchlist.py` — 生成 QMT 执行器 watchlist（`.venv` 运行）。
- `build_ths_block.py` — 建池后生成同花顺板块导入文件 `ths_blocks/<板块名>.txt`
  （GBK，每行"代码 名称"；`.venv` 运行，已挂 build_pool.cmd 末尾）。10 个板块：
  _ALL 后缀=完整池（右侧鱼塘_ALL←pool_right、左侧鱼塘_ALL←pool_left、
  深水池_ALL←pool_deep、T0_ALL←pool_t0、T1_ALL←pool_t1）；无后缀=盘后候选
  （右侧鱼塘、左侧鱼塘、深水池、T0、T1 ← results/candidates_最新.csv 按池分流，
  「日K上穿」合并名单按代码所属池归入）。**用户每天收盘后在客户端手动导入**
  （自定义板块设置 → 选板块 → 导入；导入动作触发上传，PC/手机双端一致持久）。
  **为何放弃离线写文件**（2026-09-17 调查，Logger\CustomBlock 日志实证）：
  板块权威名单在云端（账号+版本号 v465），离线写本地文件永不上云；任何设备改动板块
  → 版本 bump → PC 每 5 分钟轮询到后全量下载覆盖本地（离线内容+未上云板块全抹掉）。
  上传只发生在 UI 操作路径（新建/导入/编辑）。新建/改名板块只能在客户端做，
  且板块里至少要有一只票（空板块不上云）；同名板块 add 报 -703。
  本地镜像文件（调查备查）：stockblock.ini（GBK，NAME_MAP/CONTEXT）、
  custom_block\<十进制ID>（JSON，ln=混淆板块名）、custom_block\0（sortstr 可见性清单）。
- `tick_bar_builder.py` — 快照驱动实时 bar 聚合器（第二阶段）。git 主版本在项目根，
  部署副本 D:\tdx\PYPlugins\user\tick_bar_builder.py（TQ UserPY 策略「随终端运行」常驻），
  机制详见「数据通道」段 tdxq 条目。
- `weekend test/` — 大QMT 内置策略测试资产（2026-09-12 周末完成，已纳入 git）：
  成果总结 md + fisher_test_daily_v2.py（纯信号日线版，prev off-by-one 已修复）+
  fisher_trade_test_v1.py（含下单版，零轴离场为占位规则）。架构方向：外部系统出信号
  → 信号文件（FileIO 已验证可用）→ 大QMT 内置执行器下单（半自动）。

## Python 环境

- `.venv`（唯一环境）：akshare + pandas 3.x，跑 fisher_scanner.py / build_pool_tdxq.py /
  build_pool_dual.py / run_scan.py。gm 双环境时代（.venv-gm）已于 2026-09-14 结束。

## 策略定义（当前版本）

公共过滤（股票）：沪深主板（60/00）、非 ST/退/停牌、股价 ≥ 2 元、
20 日均成交额 ≥ 2 亿、20 日均振幅 ≥ 2.5%、上市满 1 年。

- 右侧池 pool_right.csv：MACD DIF 连升两日 且 DIF > DEA 且 **DIF > 0**（零轴闸门是后加的，勿去掉）
- 左侧池 pool_left.csv：DIF 连升两日 且 DIF < DEA 且 **DIF < 0**
- 深水池 pool_deep.csv：无 MACD，日线 Fisher(9) < -2（实验性）；池内按五维打分降序
  （score_deep：下跌减速30%/位置支撑25%/资金CMF20%/周线共振15%/极端度10%）。
  附 HSSR 预计算列 hssr/hssr_n（价格版：历史 60m 完结 bar 上穿后 10 根 close 上涨记成功，
  取最近 20 次可评估信号，样本 <5 留空），由建池后 .venv 注解步骤
  （`fisher_scanner.py --annotate-hssr --source tdxq`，2026-09-16 起切 tdxq：
  并行约 2 分钟、无限流风险、满样本 n=20；sina 源保留作无客户端时的备用）写回 pool_deep.csv；
  深水推送按档位附仓位建议：≥75% 正常仓位、50-75% 仓位减半、<50% 不建议买入、
  样本不足标「HSSR 样本不足 (n<5)」
- T0/T1 ETF 池：ETF 统一走深水方案（日线 Fisher < -2），按 trade_n 拆分；不走 MACD
- 深水日共振：收盘后复核深水池，日内任一根完结 60m bar 上穿 且 当日日 K 上穿，
  推送 pond=深水日共振（bar_state=日共振），结果写 results/fisher_cross_*_deepres.csv
- 持仓：60 分钟 Fisher 下穿预警，无命中不推送

**Fisher-ESI 规则 v2（2026-09-14 重写，旧 6 根窗口规则废弃）**：台账 cache/esi_ledger.csv
（+entry_price 列），待激活台账 cache/esi_pending.csv。
- R1 进场过滤（所有买入信号，各池通用）：60m 上穿命中时检查 30m/15m/5m，任一周期
  处于下行段（fish < trigger）→ bar_state=假性失效，推送标注「待激活」并写入
  esi_pending（`esi_register_pending`，--once 与 run_scan 都挂钩）。
- **穿越幅度门槛（2026-09-22 起，仅 executor_v2）**：新鲜 60m 上穿必须
  fish60 ≥ trigger + 0.15（MIN_CROSS），贴线穿越直接跳过。实证依据
  backtest_entry_filter.py（854 票 2.3 万信号：贴线≤0.05 桶 HSSR 27~35%
  vs 深穿 0.2~0.5 桶 52~53%，单调有效）。高位 fish60 / 60m MACD 红柱缩短
  两项假设均不被数据支持（高位桶收益不差；红柱缩短剥掉贴线后无差异），未采纳。
  R2 再激活路径不受 MIN_CROSS 约束。
- **尾盘禁入（2026-09-22 起，executor_v2 ENTRY_TO='14:40'）**：14:40 后不再
  开新仓（15:00 bar 信号是最差时段桶，ret10 −0.4~−0.7%、HSSR 38~44%），
  与 executor_t0 的 ENTRY_TO 对齐。已有持仓的离场监控不受影响。
- **台账新增 entry_delta 列（2026-09-22 起）**：进场时穿越幅度（fisher−trigger），
  旧行读入自动补空；攒数后用于校准 MIN_CROSS。
- R2 信号激活：pending 票由持仓任务每 5 分钟复查（`check_pending_activation`）：
  三周期在信号后都重新上穿过且当前 fish > trigger，且 60m fish > trigger（趋势未破坏）
  → 推送「信号激活」并台账记 open（entry_time=激活时刻）；60m fish < trigger → void 作废。
- 进场登记（`esi_register_entries`）：完结且非假性失效的持仓上穿信号，0 < fisher < 2.5
  → 记 open；已有 open 或当日 failed 跳过（当日禁止重新开仓）。
- R3 持仓失效卖出（`esi_check_invalidation`，30m 收盘后窗口门控不变）：open 记录
  在完结 30m bar 下穿时——浮亏（现价 < entry_price）→ 推送 pond=Fisher失效 卖出预警，
  failed=是 closed；浮盈 → 继续持有。60m 下穿正常离场由持仓 down 扫描挂钩
  `esi_close_on_60m_down` 记 failed=否 closed。
- HSSR 周报口径不变：按 code 取最近 20 条 closed，HSSR = 未失效数/总数（failed=否占比），
  每周日 20:00 推送（`hssr_report`，run_scan --hssr-report，在周末保护之前豁免）。

信号：fish2 = fish1[1]，上穿 = Fisher 由跌转升拐点。默认只判最新已完结 60 分钟 bar；
`--live` 时盘中未完结 bar 也参与判定（命中带 `bar_state=未完结`，推送标注「盘中信号」）。
`notify()` 按 `{日期}|{鱼塘}|{side}|{code}|{bar_time}|{bar_state}` 去重（cache/pushed_signals.json），
同一根 bar 同一完结状态的信号当日只推一次；盘中（未完结）推过后，收盘完结确认仍会再推一次
（去重判定兼容当日旧的 5 段键，防止升级当天重复推送）。

## 计划任务（Windows schtasks）

- `fisher_建池` **17:00**（2026-09-16 起，原 16:00 太早，TQ 服务端当日分钟数据尚未入库导致 verify 失败） 周一~周五 → build_pool.cmd 一条龙（2026-09-15 起合并）：
  盘后自动下载（tdx_postclose_download.py，refresh_kline 5m/1d 全池+校验）
  → tdxq 建五池 → 深水池 HSSR 注解 → --post-close 全五池完结扫描出候选清单并推送
  （写 results/candidates_<交易日>.csv，供次日人工挑票入观察池。候选推送为每票一条
  卡片：收盘价/涨跌幅/成交额/振幅 | fisher/trigger/状态/小周期 | 深水分+HSSR 档位 |
  市值/历史位置/PE，基本面经 tdx_moreinfo.py 子进程批量取 get_more_info）。
  候选分两个独立名单：60m 上穿（分池列出；2026-09-15 起加日K过滤：日线费雪
  回升才保留，executor_t0 同款，run_scan.py `DAY_FILTER_MODE` 可切 above/off，
  剔除数写在推送标题行）+ 日K上穿（六池并集，读 cache/tdxq
  日线缓存判定，按深水分降序；与 60m 名单重叠的标「※60m已上穿」）。
  **需通达信客户端（TdxW.exe）运行并登录**
- **2026-09-17 实测确认：refresh_kline API 拉不到当日分钟数据**——当日分钟线进本地
  静态库只能靠客户端的「盘后数据下载」（手动点击，或客户端设置里开自动盘后下载）。
  当天验证：19:05/19:15 两轮 refresh_kline verify 双失败（数据停昨日），
  用户手动下载后 20:15 同脚本 VERIFY 立刻 OK。**verify 失败时应先检查客户端
  是否做过盘后下载**，而不是反复重试 refresh_kline。
- **盘中扫描已暂停（2026-09-22 用户决定，数据源难以获取）**：fisher_持仓 +
  fisher_扫描1001/1101/1331/1431/1031/1131/1401/1501 共 9 个任务已 schtasks 禁用
  （未删除，PowerShell `Enable-ScheduledTask -TaskName <名>` 恢复）。
  保留运行：fisher_建池（17:00 盘后一条龙出候选）、fisher_日共振（15:10）、
  fisher_HSSR周报、stockdb_盘后同步——即只做盘后候选分析。
  QMT 两个执行器（钓鱼/高频T0）在客户端内独立运行，不在本次暂停范围。
- `fisher_持仓` 每天 9:31–15:20 每 5 分钟 → scan_holdings.cmd **【已禁用】**
  （daily 任务，周末由 run_scan.py 内的 weekday 保护直接退出；节假日空跑但不会重复推送，去重兜底）
- `fisher_扫描1001/1101/1331/1431` 周一~周五 → scan_mid.cmd（bar 中段，--live 盘中信号）**【已禁用】**
- `fisher_扫描1031/1131/1401/1501` 周一~周五 → scan_all.cmd（bar 收盘后，完结确认）**【已禁用】**
- `fisher_日共振` 周一~周五 15:10 → scan_daily.cmd（深水池日共振收盘复核）
- **盘中扫描时段保护（2026-09-17 起，run_scan.py main）**：持仓/中段/完结确认类扫描
  仅在 09:25–15:30 执行，其余时刻记日志退出；日共振允许补跑到 18:00；
  --hssr-report / --post-close 不受限。防止睡眠唤醒后 StartWhenAvailable 把错过的
  盘中任务一次性全部补跑（2026-09-17 实发：10:51 睡到 19:04，唤醒瞬间 36 个 python
  并发挤 TQ 会话，补跑扫描大面积失败）。
- download_data.cmd / scan_candidates.cmd 保留作手动入口（已并入 build_pool.cmd，无独立任务）
- `fisher_HSSR周报` 每周日 20:00 → scan_hssr.cmd（Fisher-ESI 台账 HSSR 推送；
  run_scan 的 --hssr-report 分支在周末保护之前，周日可跑）
- `stockdb_盘后同步` 周一~周五 **15:50** → stockdb_sync.cmd → ps1 启动
  D:\stockdb\数据更新.exe（free-stockdb 本地库盘后同步，2026-09-16 新增，见数据通道段）
- 推送排版（2026-09-16）：notify 与盘后候选卡片均改为每票最多三行短句
  （`**代码 名称** 价 涨幅` / `额 振 | F值 状态(小周期)` / `分 HSSR 市值 位置 PE`），
  企业微信折行后不再粘成一片；行尾重复的「假性失效，待激活」尾巴已并入 F 值行。
- 任务经 run_hidden.vbs 隐藏运行。
- 所有 fisher 任务已开启「错过计划启动后尽快补跑」（StartWhenAvailable）：
  电脑睡眠/关机错过触发点时，唤醒后会自动补跑一次（2026-08-26 起）。
- `build_pool_tdxq.py` 盘中（15:30 前）运行时自动剔除当日未完结日 K，避免半成品 bar 污染指标。
- 重建命令见 使用说明.md。查询：`schtasks /query | findstr fisher`

## 数据通道（单通道 tdxq；2026-09-17 起 sina 应用户要求停用）

- **tdxq（默认主力）**：官方通达信客户端 TQ 接口（D:\tdx\PYPlugins\user\tqcenter.py），
  走已登录客户端（TdxW.exe）会话，绕开公共服务器封锁，原生前复权、无限流、
  bar 时间即收盘时刻无需映射。扫描时整池批量预取（`D:\tdx\PYPlugins\user\tdxq_fetch.py`
  子进程，一次 TQ 会话）落盘 cache/tdxq/，scan_one 本地读 CSV；缓存过期（交易日 10:35 后
  仍无当日 bar）自动单票补取，补取失败触发降级；**补取后会复核新鲜度，盘中仍停在
  昨日的数据视为取数失败返回 None**（2026-09-17 起，防止用昨日静态数据做盘中判定）。
  **前提：客户端登录常开 + 分钟数据
  已积累**（盘后下载已由 `fisher_盘后下载` 任务自动化（refresh_kline 5m/1d 全池，
  2026-09-15 起），分钟历史深度随每日自动下载增长，HSSR 800 根会逐步补齐）。

- **tdx（已删除 2026-09-14，git 历史可查）**：通达信公开服务器对本机整体拒数
  （握手正常但 K 线/报价全返回空，xmtdx/pytdx 双实现交叉验证为服务端行为），
  恢复无望，代码与 xmtdx/pytdx 库已删。
- **sina（2026-09-17 起停用，勿在自动化链路使用）**：akshare，前复权准，但会限流
  （HTTP 456，冷却几十分钟自愈）。分钟线 jsonp 直连 + 日线因子当日缓存，每股 1 次请求。
  用户要求暂不使用：run_scan 已改 tdxq 单通道（失败/陈旧不再降级，走告警兜底），
  盘后候选缺数据直接中止。代码里 sina 分支保留（手动 --source sina / HSSR 备用源 /
  build_pool_tdxq 的股票名称+ST 过滤仍走新浪快照），恢复自动化使用需用户确认。
- **qmt（已删除 2026-09-14，git 历史可查）**：国金 QMT miniQMT 通道。miniQMT 权限被收回
  （9/11 两协会新规行业性收紧，国金 7/6 起新开默认不含），恢复无望，代码与 xtquant 包已删。
- **gm（已删除 2026-09-14，git 历史可查）**：掘金通道整体退役（建池已切 tdxq、
  扫描兜底已删），.venv-gm / build_pool_gm.py / gm 取数代码均已删。
- **em（东财）**：push2his K 线接口被本机网络 WAF 按 TLS 指纹封锁，不可用。
- **腾讯（评估后弃用 2026-09-14）**：分钟 K 线 mkline 已 301 重定向到 web3.ifzq.gtimg.cn，
  而 web3 域名被本机网络连接级中止（WinError 10053）；fqkline 分钟级（m30/m60）已下线
  （一律 bad params），仅日线 qfq 可用（可作未来日线备选源）。
  评估对象 a-stock-data（SKILL.md 项目）分钟线全部依赖 mootdx/腾讯，对本机均无帮助。
- **akshare（已盘点 2026-09-14，无新通道）**：它只是公开源的封装。60m 前复权仅两条路——
  sina（已在用且项目直连优化更优）和 em（被封）；腾讯 tx 仅日线。升级 akshare 无意义。
- **tushare（已放弃 2026-09-14）**：积分制付费，60m 分钟线需约 5000 积分，用户不走付费路线。
- **tdxq（官方通达信客户端 TQ 接口）**：见上方主力条目。探针：`_probe_kimi.py`。
  **2026-09-15 官方文档确认：get_market_data 盘中仅日K，分钟线是静态库**
  （refresh_cache / refresh_kline 盘中都补不了当日分钟线，实测验证）。
  解法是 `tick_bar_builder.py`（git 主版本在项目根，部署副本
  D:\tdx\PYPlugins\user\tick_bar_builder.py，须以 UserPY 策略「随终端运行」常驻）：
  每 20s 批量 get_pricevol 轮询 top50 深水+持仓+观察池+ESI pending 票（上限 100），
  快照合成当日 5m bar 并聚合 15m/30m/1h，原子写 cache/daily_qfq/tdxq/ 四周期缓存
  （历史行保留，今日行重建覆盖 TQ 填充行；快照未复权价按 ratio=静态昨收前复权/快照
  LastClose 对齐，非除权日=1）。近似性：轮询间隔内盘中极值丢失，5m high/low 为
  轮询价 max/min。日志 D:\qmt\tick_builder.log。进程挂了自动退回 tdxq 静态 +
  新鲜度校验降级 sina。另：本客户端构建无 get_report_data（分笔），
  subscribe_hq 订阅推送因缺分笔接口暂未用。
- **stockdb（free-stockdb，2026-09-16 重新启用评估中）**：本地数据引擎 D:\stockdb
  （stockdb.exe 本地库 127.0.0.1:7899 纯 HTTP；数据更新.exe 从官方同步源拉数据进 ./data，
  sync_url.txt 配源，**https 改成 http 可明显提速**（官方建议 09-16））。
  曾 2026-09-14 评估删除（当时结论：分钟数据盘后才更新、盘中不可用），
  09-16 重新调查后的实测结论：
  - 盘后自动下载：`数据更新.exe -run 15:50:00` 内建定时，已由计划任务
    `stockdb_盘后同步`（周一~周五 15:50，StartWhenAvailable）经 stockdb_sync.cmd/ps1 启动，
    目标是替代 tdx_postclose_download（不再依赖通达信客户端登录）。
    首跑实测（2026-09-17）：15:50 因关机错过，19:04 补跑成功，分钟库（data1/*.ldb）
    19:05 就绪且含当日数据，日线也含当日；data/.sync_manifest.json 不是可靠的就绪指标
    （分钟库更新时它不动）。正常交易日 15:50 准点跑的就绪时刻仍待观察。
  - 本地库就绪后 `rd.get_data(codes, frequency='60m', fq='qfq')` 原生批量前复权 60m
    （pybao/stock_sdk.py，pyd 支持 py3.8+；本地 HTTP `/?cmd=get&t=日k:代码:日期`），
    可作扫描新通道（待与 tdxq 缓存对拍验证，未接入）。
  - **09-17 对拍实测结论（cache/_stockdb_compare.py，8 只 × 1h/30m/1d）**：
    时间标注与 tdxq 一致（bar 收盘时刻）；但 30m 聚合口径不同（bar 边界划分不一致，
    全字段级差异），1h 历史 bar 也有 0.1%~0.3% 级 OHLC 差异（疑似复权口径不同）——
    **fisher 口径不可与 tdxq 缓存混用，接入前需先统一口径或整链切换**。
    SDK 用法坑：start/end 必须传 'YYYYMMDDHHMMSS' 紧凑字符串（'YYYY-MM-DD' 返回空）；
    limit+desc 对 1d 无效（返回最早若干根）；.venv 需 `pip install msgpack`（已装）。
    批量速度：50 只 × 60m × 10 根 = 37.9s（本地库，够盘后/低频用，盘中整池偏慢）。
    stockdb.exe（7899 本地服务）需常驻，无开机自启；09-16 曾报 leveldb 损坏
    （672 missing files），手动重同步恢复——稳定性待观察。
  - 实时行情：在线 API（`set_init('8.138.149.215:12328')`）get_last_tick 单代码实测
    1.9s/只且 34% 报 500，**撑不起盘中轮询**；官网宣称全市场 7000+ 实时 ticks 批量端点
    （ticks.html 插件在用）但 Python SDK 未暴露，待探测。公共服务器有风控：
    **公共无鉴权服务器 01042512727 官方强调仅供测试**，连续批量拉取触发风控后
    会静默返回随机 mock 数据（日志标记 `[cache_decoy][来源IP]`），远程暴力拉取会永久封设备。
  - 其他可用资产：全量资金流（`rd.get("资金流",code,date)`，可替换深水打分的近似 CMF）、
    zb.get 批量指标（40+ 含 cross 信号，无 Fisher）、基本面在线 API（get_valuation 等，
    候选卡片市值/PE 备份源）、bk.get 板块映射、MCP 服务（01042512727:7898/mcp）。
  - 集成注意：.cmd 里写中文路径会被 GBK 解析乱码，stockdb 相关启动统一走 ps1
    （PowerShell Start-Process，UTF-8 BOM）。

`run_scan.py` 是盘中扫描调度器：依次扫持仓（下穿+上穿）→ 深水池 → 右侧/左侧/T0/T1 → 观察池，通道失败率 >50% 自动降级。
（2026-09-14 曾因 sina 限流暂停四池盘中扫描，2026-09-15 已随 tdxq 主力化恢复全池。）
参数：`--holdings` 只扫持仓两项（每 5 分钟任务用）；`--live` 盘中未完结 bar 参与判定（中段任务用）；无参数 = 全量完结确认。
`fisher_scanner.py --source tdxq|sina|em` 可手动指定通道，`--live` 可手动跑盘中信号。

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
9. ~~gm quota/is_st 坑~~（gm 已退役 2026-09-14，坑记录随代码删除，git 历史可查）。
10. **建池并发写坏池文件**（2026-09-09 实发，gm 时代）：fisher_建池 的 StartWhenAvailable 补跑
    与手动 build_pool.cmd 同时运行，两轮 gm 建池交错，增量 save_results 用未完成的
    深水池（38 只）覆盖完整池（127 只），随后 HSSR 注解按 38 只写回。build_pool.cmd
    已加 `.build_lock` 目录互斥锁（第二个实例直接退出），**手动补跑前确认没有别的
    建池在跑**；异常中断残留 .build_lock 时手动 `rmdir .build_lock`。
11. **大QMT 内置 Python 环境**（weekend test/ 实测 2026-09-12）：pandas 不可用
    （缺 unicodedata），取数用 `ContextInfo.get_history_data(n, period, field, code)`
    返回原生 dict；订阅用 `set_universe`（无 subscribe_code）；源文件必须 UTF-8
    （不支持 GBK；且**路径/字符串里的中文也会炸**——内置环境按 GBK 读 UTF-8 源文件，
    中文路径直接 SyntaxError，一切文件路径用纯英文）；回测必须显式设起止日期（默认只跑 1 根 bar）；回测吃本地数据，
    需先在客户端「补充数据」；FileIO 可用（信号文件通道可行）。miniQMT 登录报
    "client disconnected" = 权限不含 miniQMT（国金 7/6 起新开默认不含）。
12. **两套费雪实现曾口径不一**（2026-09-14 对数统一）：扫描器 `fisher_transform`
    是 Pine/同花顺口径（hl2 中价输入、窗口 9、v 超 ±0.99 截到 ±0.999）；
    weekend test v1/v2 是 high 输入、窗口 10、±0.999 对称截断。以扫描器为准，
    QMT 侧统一版为 `weekend test/fisher_test_daily_v3.py`。
13. **客户端升级/重置会清空 UserPY 策略注册**（2026-09-16 实发）：TQ 管理器变空，
    tick_bar_builder 静默停跑（日志停在上一交易日 final flush，无异常栈），
    盘中 5m 缓存断更。部署文件（D:\tdx\PYPlugins\user\）不受影响，重新挂策略即可。
    该目录只有 tick_bar_builder.py 需要常驻（勾「随终端运行」），其余都是库/
    子进程/探针，挂了反而有害。
14. **未被 tick_bar_builder 覆盖的票盘中没有真实分钟线**（2026-09-17 ESI 假激活事故）：
    TQ 分钟线盘中是静态库，单票补取也只到昨日收盘。曾导致 ESI R2 用昨日数据把
    趋势已破的 pending 票（601890）误判「信号激活」、激活价=昨日收盘价。修复：
    `_fetch_tdxq` 补取后复核新鲜度（仍停昨日→返回 None，上层跳过）；
    tick_bar_builder 轮询名单加入 esi_pending 的 pending 票。**盘中任何涉及
    非名单票的分钟级判定都要先确认数据含当日 bar，勿信补取成功。**
    注：节假日（工作日但休市）该新鲜度校验会让分钟取数全部返回 None，
    扫描走失败率兜底而非拿旧数据出信号，属预期行为。
15. **交易会话未登录时 passorder 静默丢单**（2026-09-22 实发）：行情登录与交易登录
    是两个会话，行情/持仓查询（get_position）正常不代表能下单。交易会话掉线时
    passorder 照常返回 ret=0，无成交、**无委托记录**，执行器按"已报单"记 sim 账本
    形成幽灵持仓（次日 STALE 会发必失败的卖单，需手动清 sim_pos.csv）。
    判据：下单后下一根 bar 持仓仍带 `(sim)` 标记 = 大概率未成交；
    实锤看 `userdata/log/XtClient_<日期>.log` 里 passorder 行的 msg——
    正常是「股票买入/卖出」，丢单是「存在未登录的账号, 不能下单!」。
    修复：客户端交易面板重新登录交易账号（8891080156），两个执行器共用该会话。
    **同日更深一层**：会话半死时客户端 msg 显示「接受」但报单同样到不了柜台，
    连「未登录」提示都没有——ret=0 和「接受」都不是成交证据。
    对策（2026-09-22 已实装，executor_v2/executor_t0 同款）：报单后下一根 bar
    用 strict_position（查询失败返回 None 则跳过本轮，绝不在不可信数据上重报）
    核对成交，未成交自动重报一次，仍失败则撤 sim 幽灵账 + 企业微信告警
    （wecom_alert，stdlib urllib 读 webhook.key）；VERIFY 挂起期间该票不发新单。
16. **T+1 冻结期的卖出信号曾被静默丢弃（2026-09-23 实发，600498 浮亏 −3.5% 仓位悬空）**：
    executor_v2 原逻辑先查 `sellable<=0` 直接 `continue`，cross_down/ESI 判断在后面
    永远走不到——日志写 "exits deferred" 实为 discarded；解冻后序列已单边下行，
    无新拐点可触发，卖出机制失效。修法（已实装）：冻结期仍评估卖出条件，命中写入
    持久化台账 `D:\qmt\exdef.csv`（EXDEF：code,reason），可卖首根 bar 立即市价执行
    （reason=DEFER_EXIT_A/DEFER_ESI，走 VERIFY 成交校验）；pos==0 时清理 stale 项。
    注意 PSELL（浮盈延迟确认）仍是内存态重启即丢，靠新 cross 重建。

## 常用操作

- 登记持仓：`.venv\Scripts\python.exe fisher_scanner.py --buy 600036 --price 38.86`
- 移出观察池：`.venv\Scripts\python.exe fisher_scanner.py --unwatch 600498`
- 手动扫描：`.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_right.csv`
  （加 `--live` 判盘中未完结 bar）
- 手动持仓监控：`.venv\Scripts\python.exe run_scan.py --holdings --live`
- 手动建池：`.venv\Scripts\python.exe build_pool_tdxq.py`（须通达信客户端登录在线）
- 单票体检：`.venv\Scripts\python.exe fisher_scanner.py --inspect 600498`（支持中文名，
  先查各池/持仓/观察池 CSV 反查、查不到用新浪快照反查；加 `--push` 推送报告到企业微信）
- 推送渠道：企业微信机器人 webhook（webhook.key），notify() 已实现，按池文件名
  自动区分推送文案（右侧/左侧/深水/T0/T1/持仓鱼塘）。

## Git

远程：git@github.com:tyleryyy7/angler.git（main 分支，SSH key 已配好）。
提交前确认 git status 里没有 webhook.key / holdings.csv / pool*.csv。
README.md 是 使用说明.md 的副本，改文档时两边同步。
