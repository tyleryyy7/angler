# AGENTS.md — 给 AI 助手的项目交接文档

## 项目是什么

沪深 A 股/ETF 量化扫描系统：夜间建股票池，盘中每个 60 分钟 bar 收盘后扫描
Fisher Transform 上穿信号（买入），持仓股监控下穿信号（卖出预警），
结果推送到企业微信机器人（用户在微信接收）。全流程由 Windows 计划任务驱动。

## 文件结构

- `fisher_scanner.py` — 主扫描器（主环境 `.venv` 运行）。信号定义、上穿/下穿判定、
  并发扫描、企业微信推送。配置区在文件头部。
- `build_pool_gm.py` — 建池（掘金 gm 版，**当前默认**，`.venv-gm` 运行）。
  产出 pool_right.csv / pool_left.csv / pool_deep.csv / pool_t0.csv / pool_t1.csv。
- `build_pool_dual.py` — 建池（新浪版，**备用**，主环境 `.venv` 运行）。
  gm 不可用时切回，输出文件名相同。
- `build_pool.py` — 已删除（旧单池方案，git 历史可查）。
- `scan_all.cmd` — 盘中扫描入口：依次扫 右侧/左侧/深水/T0/T1/持仓下穿（CRLF 换行，勿改 LF）。
- `build_pool.cmd` — 建池任务入口（CRLF）。
- `run_hidden.vbs` — 隐藏控制台启动器，所有计划任务经它调用 .cmd（防弹窗）。
- `holdings.csv` — 用户持仓（code,name,buy_date,buy_price），gitignore。
- `webhook.key` / `gm_token.key` — 密钥文件，gitignore，**绝不提交**。
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
- 深水池 pool_deep.csv：无 MACD，日线 Fisher(9) < -2（实验性）
- T0/T1 ETF 池：ETF 统一走深水方案（日线 Fisher < -2），按 trade_n 拆分；不走 MACD
- 持仓：60 分钟 Fisher 下穿预警，无命中不推送

信号：fish2 = fish1[1]，上穿 = Fisher 由跌转升拐点，只判断最新已完结 60 分钟 bar。

## 计划任务（Windows schtasks，周一到周五）

- `fisher_建池` 00:00 → build_pool.cmd（gm 建五池，**需掘金终端运行并登录**）
- `fisher_扫描1031/1131/1401/1501` → scan_all.cmd
- 任务经 run_hidden.vbs 隐藏运行。
- 重建命令见 使用说明.md。查询：`schtasks /query | findstr fisher`

## 踩过的坑（改代码前必读）

1. **东财接口（push2.eastmoney.com）被本机网络 WAF 封锁**（按 TLS 指纹/IP），
   Python 任何 TLS 栈都不通，故默认数据源是新浪（`DATA_SOURCE = "sina"`）。勿轻易切回 em。
2. **py_mini_racer 多线程会崩解释器**（akshare 新浪前复权链路依赖它）：
   fisher_scanner.py 的并发必须用**进程池**（ProcessPoolExecutor），勿改线程池。
3. **akshare 内部请求无超时**，曾导致任务挂死 2 小时：
   两个脚本都有 `_patch_requests_timeout()`，勿删。
4. **计划任务里 .cmd 中跑 pythonw.exe 会被杀**（0xC000013A，GUI 程序退出带走隐藏控制台）：
   .cmd 里一律用 `python.exe`，靠 run_hidden.vbs 隐藏窗口。
5. **.cmd 文件必须 CRLF 换行**（LF 会导致批处理解析异常）。
6. 新浪限流：每股请求间隔 ≥ 0.2s；全市场快照（stock_zh_a_spot）每天只调一次。
7. Git Bash 里调 cmd/schtasks 等 Windows 命令要先 `export MSYS2_ARG_CONV_EXCL='*'`，
   否则 /c 等参数会被路径转换吃掉。

## 常用操作

- 登记持仓：`.venv\Scripts\python.exe fisher_scanner.py --buy 600036 --price 38.86`
- 手动扫描：`.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_right.csv`
- 手动建池：`.venv-gm\Scripts\python.exe build_pool_gm.py`
- 推送渠道：企业微信机器人 webhook（webhook.key），notify() 已实现，按池文件名
  自动区分推送文案（右侧/左侧/深水/T0/T1/持仓鱼塘）。

## Git

远程：git@github.com:tyleryyy7/angler.git（main 分支，SSH key 已配好）。
提交前确认 git status 里没有 webhook.key / gm_token.key / holdings.csv / pool*.csv。
README.md 是 使用说明.md 的副本，改文档时两边同步。
