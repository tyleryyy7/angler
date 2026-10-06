# angler（钓鱼）

沪深 A 股 / ETF 量化交易系统：夜间建股票池，盘中扫描 60 分钟 Fisher Transform
上穿信号（买入）、监控下穿信号（卖出），结果推送企业微信机器人；QMT 内置执行器
按同一套规则自动下单。全流程 Windows 计划任务驱动，单数据源（通达信客户端 TQ 接口）。

## 策略一句话

**买看 60m Fisher 上穿 + 小周期不拖累，卖看 60m 下穿（分品种幅度门槛）让利润奔跑**；
深位加倍、高位不做、下午减半、日共振加档；浮盈且日线趋势态时顺势加仓；
月度回放为负的票自动进刹车名单只卖不买。

所有规则都经过全市场 904 只公共过滤票 × 10.5 个月（2024-11 起）× 百万级成交笔数
的回放验证，验证口径与结论档案见 `AGENTS.md` 与 `results/`。

## 系统架构

```
通达信客户端(TQ)                 企业微信机器人
      │ 取数(日线/分钟线)              ▲ 推送
      ▼                              │
建池(build_pool.cmd, 交易日16:00)     │
  → 五池 CSV + 盘后候选(带SZ4仓位档/刹车标)
      │                              │
扫描(run_scan.py, 盘中暂停中)  ───────┤
      │                              │
执行(executor_v2.py, QMT客户端内置) ──┘ 自动下单+台账+成交核对
      ▲                              │
月检(theme_brake.py, 每月1日) ───────┘
  → trailing回放 → 刹车名单/参数漂移/名单建议/组合层提示
```

## 目录导览

| 文件 | 作用 |
|---|---|
| `executor_v2.py` | **大QMT 内置执行器 git 主版本**（部署：客户端编辑器全选粘贴，REV 2026-10-05c） |
| `fisher_scanner.py` | 主扫描器：信号定义、上穿/下穿判定、多源取数、企业微信推送 |
| `run_scan.py` | 盘中扫描调度器 + 盘后候选清单生成 |
| `build_pool_tdxq.py` | 建池（通达信版，当前默认）；`build_pool_dual.py` 新浪版备用 |
| `theme_brake.py` | 月度动态刹车：trailing 回放 → 刹车名单 + 参数漂移 + 名单建议 |
| `backtest_v2_replay.py` / `backtest_t0_replay.py` | 回放框架（v2 股票规则 / T0 ETF 规则） |
| `tdxq_fetch.py` | TQ 批量取数助手（部署副本在通达信 PYPlugins） |
| `build_ths_block.py` | 同花顺板块导入文件生成 |
| `build_qmt_watchlist.py` | QMT 名单生成（现为手工小名单，脚本保留） |
| `tdx_postclose_download.py` | 盘后数据自动下载（16:00 建池任务调用） |
| `tick_bar_builder.py` | 快照驱动实时 bar 聚合器（通达信 UserPY 常驻） |
| `weekend test/` | QMT 内置策略测试资产 |

运行产物（gitignore）：`pool_*.csv`、`results/`、`cache/`、`scanner.log`、
`ths_blocks/`、`holdings.csv`、`watchlist.csv`、`webhook.key`。

## 文档

- **`使用说明.md`** — 运维手册：环境搭建、计划任务注册、部署步骤、常见问题
- **`AGENTS.md`** — 项目交接文档：策略规则精确口径、数据通道、踩坑记录（改代码前必读）、
  全部回测结论档案（含被否决方向，防止重复踩坑）

## 环境

- Python 3.11（`.venv`）：akshare + pandas；扫描端进程池并发
- 通达信客户端（TdxW.exe）登录常开 + 每日盘后数据下载
- 大QMT 客户端（国金证券）内置 Python 3.6 运行执行器

> 仅个人量化研究用途，不构成投资建议。
