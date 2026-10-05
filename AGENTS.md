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
  `--post-close --date YYYY-MM-DD` 补算指定交易日盘后候选（要求 tdxq 数据恰停在当日收盘）。
- `build_pool_tdxq.py` — 建池（通达信 TQ 版，**当前默认**，须客户端登录在线）。
  产出 pool_right/left/deep/t0/t1 五个 CSV。全市场名单走 tdxq --universe；名称/ST
  过滤用新浪快照；ETF 名称用 TQ get_stock_info，T+0/T+1 按名称关键词分类。
  盘中（15:30 前）运行自动剔除当日未完结日 K。
- `build_pool_dual.py` — 建池（新浪版，**备用**），输出文件名相同。
  （build_pool_gm.py / build_pool.py 已删，git 历史可查。）
- `.cmd` 入口（全部必须 CRLF）：`build_pool.cmd` 建池一条龙（.build_lock 互斥 +
  盘后下载 + 建五池 + HSSR 注解 + --post-close 候选）；`scan_all.cmd` 完结确认；
  `scan_mid.cmd` bar 中段 --live；`scan_holdings.cmd` 持仓 5 分钟 --live；
  `scan_daily.cmd` 深水日共振；`scan_hssr.cmd` HSSR 周报；`theme_brake.cmd` 月度
  动态刹车。
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
  卖出侧与买入侧对称（REV 2026-09-29b）：1h 下穿需幅度 ≥MIN_CROSS(0.15) 且
  15m/30m/5m 全下行才卖，否则 PSELL 挂起观察、fish 翻回线上作废。
  **REV 2026-09-30a 打包**：R3 ESI 改按票拆分（股票关/ETF 开，esi_enabled()）；
  进场幅度门槛移除（MIN_CROSS_ENTRY=0，扫描 +127k）；进场加 MAX_F60=2.5
  上限（+33k）；T0 六只 ETF 并入 watchlist.txt 走 T+1 规则（带 T0 标记），
  高频T0 策略同日停用。**REV 2026-09-30b**：EXDEF 台账加 REV 版本戳 +
  执行前状态复核（坑 #16）。**REV 2026-09-30c**：ETF 进场热开关
  `D:\qmt\v2_config.txt` 的 ETF_ENTRIES（每 bar 热读，0=ETF 只管理卖出
  不再新买，股票不受影响）。OPEN_GUARD_UNTIL='09:35' 开盘护栏（坑 #19）。
  **REV 2026-10-05a**：BLOCKLIST 动态刹车——`D:\qmt\v2_blocklist.txt` 每 bar 热读，
  名单内票只管理卖出不进新仓（config_override 内联读取，启动横幅 blocklist=N）。
  名单由 `theme_brake.py` 每月 1 日 18:00 生成（watchlist 逐票 trailing 63 交易日
  LIVE 规则回放，n≥5 且净额<0 → 刹车；MIN_TRADES/PARAMS 在文件头，线上规则变了
  要同步）。注意：回放口径 LIVE 与线上规则绑定，卖门槛/分档实装后要改 PARAMS。
  **REV 2026-10-05b 四件套**（全市场 904 只验证，详见 fm_report/fm_combo*.txt）：
  ① 卖门槛分品种 SELL_MC_STOCK=0.50 / SELL_MC_ETF=0.15（替代 MIN_CROSS，
  config 白名单可热调）；② MAX_F60 进场上限移除（被 SZ4 的 f60≥1 跳过取代）；
  ③ SZ4 分档（股票）：watchlist VOLUME=1 单位，f60<0→2 单位、0≤f60<1→1 单位、
  f60≥1→跳过、13:00 后×0.5（取整百股，<100 股放弃）；R2 激活同样分档。
  ④ 顺势加仓（股票）：浮盈≥1% & 日线 MA20 TREND（regime_trend，与回放
  build_regime 同口径，日缓存）& 新鲜 1h 上穿 → 加 1 单位，每仓 ≤3 次，
  台账 D:\qmt\adds.csv（带 REV 戳，平仓归零）；加仓 VERIFY 失败只回滚加仓
  部分（simpos_add_rollback）；冻结期（sellable=0）也允许加仓（买入不受 T+1
  限制）。ETF 不加分档不加仓。EXDEF 台账因 REV 戳自动丢弃旧锁存。
  **待办提醒：theme_brake.py PARAMS 仍是旧 LIVE 口径（卖门槛0.15/无分档），
  实装部署后要把 PARAMS 改为 min_cross_sell=0.50（分档/加仓在刹车评估里
  从简，只改卖门槛）。**
  厚样本回放（`backtest_v2_replay.py`，231 只池票全信号口径×10.5 个月，
  2026-09-29）：V0 +368k（均毛利 +0.323%/笔，T+1 有真边际，费用占比可承受）。
  组件归因：卖出对称化 +99k（REV 09-29b 验证通过）；**ESI 快割 −258k 负贡献**
  （与 T0 完全相反，胜率 43.4% vs 32.9%，关 USE_ESI_EXIT 待用户定夺）；
  进场幅度门槛 −61k（与 HSSR 贴线桶结论冲突，**2026-09-30 裁决：门槛扫描
  0~0.30 单调递减，MIN_CROSS 应移除**——分桶显示贴线桶胜率 42.1% 确实低于
  深穿桶 46.0%（HSSR 方向复现），但贴线桶期望值 +0.268%/笔为正且量最大，
  HSSR 旧结论只量了胜率没量期望）；fish60≥2.5 高位进场桶毛利为负且
  **2026 年独占 −19k（2024/2025 不亏），cap=2.5/2.0/1.5 均 +33k~36k 且
  valid 窗口成立**（cap 上限闸门待实装，幅度门槛移除待实装）。R1 闸门/卖出闸门近中性；
  fish60≥2.5 高位进场桶毛利为负（支持加上限闸门）。注意：回放是全信号口径
  （非历史建池点）且无滑点，数字偏乐观。
  ESI 尸检+G 路由实验（2026-09-29）：快割是"多数小对、少数大错"（扛住胜率
  仅 43% 但均值 +0.489%，砍右尾），2024 震荡市无害、2025 趋势市 −0.767%/笔。
  G_REGIME_ESI（TREND 日关 ESI，MA20/MA10/DIF 三指标）全部落在 V0 与
  E_NOESI 之间（最优 MA10 +477k < E_NOESI +626k）——**ESI 对落刀票池是
  票性级错误，行情态路由无解**。深度门槛扫描（x=0.5~3.0%）单调收敛无内部
  最优（x=3% +589k < 全关 +626k）——**结论：USE_ESI_EXIT=0，已实装
  （REV 2026-09-29c，待客户端粘贴部署）**；
  备选 x=3% 灾难险（valid +209k vs 全关 +230k，心理保险非数学最优，未采纳）。
  **全市场厚样本回放（2026-10-05，cache/_fm_universe.txt 公共过滤后 904 只 ×
  2024-11~2026-09-29，106 万笔，脚本 cache/_fm_replay.py/_fm_analyze.py，报告
  results/fm_report.txt）**：LIVE（ESI关/门槛0/cap2.5）+4.16M、均毛利 +0.682%/笔、
  胜率 43.6%，三年皆正（2024 +65k / 2025 +3072k / 2026 +1018k）。ESI 负贡献复现
  放大（+0.8~0.9M，关 ESI 正确）；进场幅度门槛总净额单调递减（维持移除正确）；
  **MAX_F60=2.5 cap 在全市场口径不成立**（去掉 +19k，2~2.5 桶 +0.237%/笔为正，
  231 宇宙高位桶亏损是小样本偏差，建议放宽/移除）；R1 闸门近中性（−43k）、卖出
  闸门微正（+54k）。**卖出侧 MIN_CROSS=0.15 太浅是最大泄漏点**：1-2 天持仓桶
  −4.4M；卖出门槛扫描 valid 窗口（>2025-08-31）峰值 0.50（+2223k vs 0.15 的
  +1985k，+12%），2026 段 0.25~0.50 最优，0.75+ 回落（S100≈买入持有吃 beta，
  总净额不可比）——建议提到 0.35~0.50（待实装）。时段：上午桶 +0.85~0.90%/笔，
  下午 +0.35~0.43%/笔（仍正，可降仓勿禁）。f60 位置单调：<-2 桶 +0.728% 最佳。
  **池归属@进场日（无前视重放）：right +0.386% 最差，left +0.757% / deep +0.570%
  / none +0.585%，当时不满足公共过滤票 +0.893% 最好——右侧池是反向筛选，
  fisher 上穿本质是反转信号**。费用占毛利 23%（1 万名义地板佣所致），名义 ≥5 万
  可降均笔费用约 15%。
  **加减仓验证（2026-10-05，同一宇宙）**：A 初始仓位分档（资金中性口径，脚本
  cache/_fm_sizing.py）——下午减半 +15.6% valid、深位加倍+高位减半+下午减半
  +24% valid、网格最优（f60<1 才做+深位 2x+下午 0.5x）+37% valid；唯一负格=
  下午×f60∈1~2.5（−0.067%/笔）。B 顺势加仓（浮盈≥1% 再出 1h 上穿加 1 单位，
  最多 2 次，cache/_fm_replay_add.py）——加仓笔 21190 笔 +97.1 万、均毛利
  +0.669%/笔 ≈ 主仓质量（+0.682%），valid(+63万)>build(+34万)，但 2024 震荡市
  加仓笔为负（−3.9万）；资金中性看是"更多资金吃略低质量"，非免费午餐。
  结论倾向：先实装 A 分档（执行器改动小），B 待 2024 类市况过滤再谈。
  B 二轮验证（_fm_replay_add2.py，加仓限日线 regime=TREND）：**MA20 保护成立**
  ——+1%&MA20 加仓笔 +0.748%/笔（反超主仓 +0.682%）、2024 震荡市亏损 −3.9万
  →−0.2万基本免疫；DIF 版太慢不可用；代价是总量减半（趋势市少赚）。B 实装
  形态定为：浮盈≥+2% & MA20 TREND & 盘中再出 1h 上穿 → 加 1 单位，每票≤2 次。
  **组合矩阵（2026-10-05，_fm_replay_combo.py + _fm_combo_analyze.py，
  报告 results/fm_combo_report.txt）：加仓×卖门槛强交互——慢出场下加仓笔
  变长乘趋势单（S050 基座加仓笔 +2.96%/笔、胜率 55.3%、2024 +1.9万），
  远比 0.15 基座下（+0.75%/笔）好。全链路最优组合（资金中性 valid）：
  S050_NC + ADD(+1%,MA20) + SZ4分档(深位2x/0~1档1x/≥1不做/下午半)
  = +464万 valid（现行 LIVE 全1x 基准 +199万，2.3 倍）。
  MAX_F60 cap 在抬门槛后仍是负贡献（去 cap +2.7~3.3万）→ 建议移除。
  sizing 用现有 CSV 加权重算即可验证（费用按缩放名义重算），0 倍率=跳票
  会改变后续信号序列，重算口径偏保守。**
  **T0 ETF 对照（_fm_replay_t0etf.py，59 只 T+0 宇宙 2026-10-05）：T+0 能力
  无价值**——同规则 T+0 vs T+1 几乎相同（+17.9万 vs +18.1万），当日离场桶
  净亏 −4.4万（噪声止损），T+1 冻结是保护不是成本；ETF 加仓笔弱（+0.50%/笔
  vs 股票 +2.96%）；**T0 ETF 宇宙 2026 年全变体为负**（QDII/商品主题无趋势），
  watchlist T0 标记 ETF 的新买值得用 ETF_ENTRIES 热开关审视。
  **进场 regime 闸门否决（_fm_replay_reg.py，2026-10-05）：TREND 才进场在
  股票/ETF 两宇宙全坏（股票 S050 −28%、ETF −47%）——反转策略的最好进场
  （深位上穿）天然在 FALL 态，趋势闸门砍掉的恰是利润主体。regime 路由第三次
  独立失败（V8/G_ESI/进场闸门），定论：日线趋势指标不进进场端，只用于保护
  加仓（趋势跟踪行为）。主题强弱半衰期仅数月，静态名单不可信；可行生产规则
  = 每月 trailing 3 个月回放当刹车（负就停该主题新买），不当油门。**
  **T0 主题去重（用户规则：每板块一只，2026-10-05）**：59 只按名称聚 15 主题，
  留流动性最强者，均笔质量 +1.43%→+1.71%（+20%）。弱势主题：互联网中概
  （6 只全亏合计 −1.0万）、恒生科技（−0.2万）——不建议配置。强主题代表：
  创新药 513120(+8275)/中韩半导体 513310(+10657，+5.3%/笔最强)/纳指科技
  159509(+9557)/黄金股 517520(+8825)/有色 512400(+7180)/日经 159866(+5165)。
  watchlist 现状问题：513750+513090 同主题重合、513040 在负主题互联网。
  **日内做T 否决（2026-10-05，cache/_fm_tprobe.py，904 只 5m 拐点 178 万腿）**：
  正T 均毛 +0.004%/腿、反T −0.012%/腿，费用 0.07~0.15%/腿 → 名义任何档位都亏
  （1 万名义持仓日内合计 −2223 万）。与高频T0 同一死因：5m bar 收盘口径无
  日内价差边际，勿再试。
  补网格验证（_fm_tprobe2.py，周期×幅度门槛×方向）：15m 优于 5m（反T 转正
  +0.036%/腿，5m 有微弱动量延续、15m 微弱反转），拐点幅度门槛 0.2 为边际最高
  点——但最好格子的毛边际仍只有费用线（双边万一+印花税+免5 ≈0.07%/腿）的
  一半，任何名义/门槛组合都不打平。
- `executor_t0.py` — 高频T0 版，**2026-09-30 起停用**（同批 ETF 走 v2 T+1 规则
  回放 +91.8k vs 日内 −156k，周期选错非调参可救；名单并入 watchlist.txt 带
  T0 标记）。部署副本同目录 高频T0.py。历史机制要点存档：
  ESI 止损周期 5m；ESI/STOP 止损后当日禁再进场（FAILED_TODAY，daily_t0.csv 第 6 列）；
  三条进场路径去重键统一为 15m bar 收盘时刻（key_bar，防止同 bar 双发仓位翻倍）；
  `ENTRY_DEEP_MIN=-1.5` 深极值进场过滤（10 个月厚样本复核 2026-09-29：池级仍未转正，
  但减亏 53%、宽宇宙 36 改善/1 恶化、严格宇宙 12/12 全改善；999=禁用）；
  门槛扫描（实验 I）：−2.0/−2.5 总净额更优（严格宇宙 −8.9k vs −1.5 的 −17.7k），
  均笔各档相当（−11~−12），改不改值待用户定夺。
  **实验 H（名义本金扫描）推翻"上规模降费用占比"直觉**：策略毛边际仅约
  +0.012%/笔，低于比例佣金 0.05%/来回（万2.5 双边），任何规模都净亏，
  2 万名义（地板佣解绑点）已是损失最小点；FEE_MIN_NOTIONAL=5万 不能救策略。
  实验 K（消融）：ESI 正贡献 +21.4k（最大），5m 闸门 +0.7k、ATR 硬止损 +0.1k
  （近中性）。实验 J：V8 REGIME 未通过实装标准（见未来方向）。`STOP_MODE='atr'`（ATR14(15m)×1.5 夹取
  [0.4%, 2.5%]，'fixed'=固定 0.5%）；取数 `get_market_data_ex_ori`（盘中勿用旧
  get_history_data，曾返回止于昨日的分钟历史）。上述参数全在 t0_config.txt 白名单。
  ~~T0 黑榜~~ 已于 2026-09-29 戒除：513120/513090/513040 重回 watchlist_t0.txt
  （各约 1.5 万名义）；589120/589720 与 513120 同为创新药主题，T0 名单不允许
  主题重合，未回加（非黑名单）。黑榜从未在代码里实装，纯文档+手工排除。
  已否决方向（勿再走）：1h/日线大周期过滤、ESI 二次确认、ESI 时间宽限、关强平
  （深水池=下跌趋势池，隔夜漂移结构性为负，14:55 强平是生存机制）。
  **教训：小样本回测必须警惕行情段巧合**（17 天窗口结论被 77 天厚样本推翻）。
- ~~sync_qmt.cmd / sync_qmt.ps1~~ — **已删除（2026-10-05）**：2026-09-18 实测失效，
  客户端对 python\ 目录有文件虚拟化，外部写入客户端不可见。部署只能改在客户端编辑器里
  全选粘贴仓库文件内容；盘中调参走 D:\qmt\t0_config.txt（config_override，白名单键），
  不要在客户端改代码。
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
  （六池并集按深水分降序，重叠标「※60m已上穿」）。2026-10-05 起候选卡片/CSV
  附 SZ4 仓位档（档2x/档1x/档0·不进场，基于 60m f60，仅股票，日K行不标注）
  与 🔴刹车 标记（v2_blocklist.txt 动态刹车名单；CSV 列 sz4/brake）。**需通达信客户端登录在线，
  且 16:00 前做完客户端盘后数据下载**。
- `fisher_日共振` 周一~周五 15:10 → scan_daily.cmd
- `fisher_HSSR周报` 每周日 20:00 → scan_hssr.cmd
- `fisher_动态刹车` 每月 1 日 18:00 → theme_brake.cmd（StartWhenAvailable 已开）

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
  **取数无 800 根上限**（2026-09-29 实测 count=20000 照常返回；客户端本地 5m 库
  自 2024-11-15 起）。tdxq_fetch.py 已改 merge 写（防浅取冲掉深缓存；重叠区
  close 不一致=除权换锚，丢弃旧历史保口径），仓库主副本 D:\angler\tdxq_fetch.py。
  2026-09-29 已对 T0 回放宇宙 15 只全深度回填（5m 21793 根 ≈ 10 个月起 2024-11-15）。
  **客户端本地库本身就是全市场**（2026-10-05 实测：日线/5m/1m 各 ~5200 只 60/00 主板，
  日线全历史；5m 深度 ≈10.5 个月起 2024-11-15；1m ≈70 交易日）——全市场回测只需
  用 cache/_fm_backfill.py 回填 CSV（tdxq_fetch --batch 25，见坑 #20）。注意 9-30 的
  分钟线缺口：refresh_kline 事后也拉不到，只能客户端「盘后数据下载」补。
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
    EXDEF 两道加固（REV 2026-09-30b）：台账带 REV 版本戳，换规则重启即丢弃旧锁存
    （新规则会当 bar 重新评估重锁，丢弃无成本——2026-09-30 集合竞价 600580 旧规则
    贴线锁存被新规则执行的事故）；执行前按当前 bar 复核理由（EXIT_A 需 fish60 仍
    破线、DEFER_ESI 需仍浮亏），隔夜跳空恢复则作废不重卖。
17. **tdx_postclose_download.py 勿挂 UserPY 策略**：TPyth 嵌入式 Python 没有
    numpy/pandas，tqcenter import 即崩，且客户端每次启动弹报错控制台。盘后下载只走
    16:00 计划任务（.venv python）。已从 `PYPlugins\py_strategy.cfg` 摘掉 AutoRun
    并加只读属性（客户端要再改策略注册需先 `attrib -r`）。
18. **handlebar 实盘是 tick 驱动（L1 快照约 3s 一跳），不是每根 bar 一次**：
    verify_fills 原设计「下一根 bar 核对成交」实际在报单后 ~3s 就查持仓，而成交
    回报反映到持仓查询有秒级~数十秒滞后（2026-09-28 实发：002149 重报双办成
    600 股、002185 三倍成 1500 股、T0 的 513750 双办成 13200 股；60ms 内回报
    到账的 600580/601606 则正常）。executor_v2 另有第二缺陷：去重键 key1h 用
    形成中 1h bar 的实时 fisher 值，逐 tick 漂移，LAST_ACT 永不匹配，VERIFY 耗尽
    删除后同一信号会再发单（会话掉线场景会每 3s 无限刷单）。修复（两执行器
    REV 2026-09-28a）：VERIFY 记录带提交时间戳、VERIFY_WAIT_SEC=90 后才首查、
    重报只补未成交差额；executor_v2 的 key1h 改 slot1h()（bar 时钟的 1h 槽位
    A/B/C/D，tick 间稳定）。**改完必须在客户端全选粘贴重新部署，启动横幅核对
    REV**（部署副本外部不可读写，文件虚拟化）。
19. **开盘首 tick 的 1h 形成 bar 是退化的（high==low，单 tick），下穿/上穿判定
    全是噪声**；且策略启动初期交易会话持仓缓存未同步完，同一次查询里有的票
    看得到持仓、有的看不到（2026-09-29 09:30:00.5：三只昨日买入票低开触发
    假下穿被市价全卖 ≈−596 元；603699 持仓查询返回 0 被当空仓又买 200 股）。
    executor_v2 已加 OPEN_GUARD_UNTIL='09:35'（REV 2026-09-29a），09:35 前
    不下任何单（含 EXDEF 延迟单）；executor_t0 早有 ENTRY_FROM 09:40 /
    STALE_EXIT_AT 09:35 同类护栏。
20. **tdxq_fetch 大批次深度请求会卡死**：100 只 × count=25000 的
    get_market_data 调用 12 分钟无响应（2026-10-05）；25 只/批 58 秒正常。
    深度回填一律 --batch 25（tdxq_fetch.py 已加该参数，部署副本已同步）。

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
- **验证结果（2026-09-29 实验 J，10 个月厚样本）：未通过，保持不实装**。
  V8 宽宇宙 7942 笔 −67.7k（好于 V0 −156.0k、好于 V2 −82.2k 但改善仅 17.6%
  未达 30% 且未转正）；分支归因 TREND 3474 笔均笔 −5.7 / FALL 4468 笔均笔
  −10.7（TREND 分支确为全变体最优均笔，方向对但力度不够）。后续若重启，
  先试更快 regime 指标（DIF 斜率/10 日线）。
- **后续实验 M/N（2026-09-29）**：V9（纯 TREND，砍 FALL）均毛利 +0.043%/笔；
  分样本验证（build ~2025-08-31 选股 → valid 纯外推）：TOP6 票
  （513090/513380/513750/517520/513010/513020，按 build 窗口毛利排名）
  valid 窗口均毛利 **+0.087%/笔、净额 −286/216 笔 ≈ 打平**，是唯一超过
  佣金线（0.05%/来回）的组合；13/14 点时段过滤外推失败（+0.021%）勿用。
  V10 降频（30m 阶梯）均毛利 +0.031%（优于 V0 的 +0.012%，仍低于佣金线）。
  存活路径 = V9/V10 × 头部票 × 更低佣金率（万一以下才有像样净额）。
- **实验 O（2026-09-29，1m 约 70 交易日薄样本）**：粒度细化不救策略。
  **TF_ESI=1m 实测更差**（均毛利 −0.014% vs 5m 的 +0.005%，胜率 17% vs 28%，
  churn 加剧）——白名单保留 5m 勿切；V12 1m 原生快阶梯最差（−0.019%）；
  V11（1m 驱动双闸门真身）与同窗口 V0（5m 近似）差异 −0.026%/笔 ≈ 噪声，
  即 5m 回放近似口径未系统性高估，既有厚样本结论继续有效。

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
