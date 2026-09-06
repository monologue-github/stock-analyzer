# 重构修复报告 v3.2

> 日期：2026-08-27　改动物：`stock_gui.py`（唯一算法源），`stock_predict.py` 由 `build_cli.py` 重新生成
> 验证：两文件 `py_compile` 通过；sz002241 / sz000725 全流程 `analyze()` 实测正常（样本池 L1/L2/L3、信号、回测、日志均输出正确）

---

## 一、高优先级（运行时安全 / 数据正确性）

### #1 全局可变状态线程安全 ✅
- 新增模块级 `_STATE_LOCK = threading.RLock()`（stock_gui.py:82）。
- `tier_sample` 的 `(tier, 日期)` 缓存改为「锁内查库 + 回写」原子操作，两个线程不会再同时重建同一天的同市值层池。
- 板块排行缓存（`_TOP_SECTORS_*`）、板块上下文缓存（`_SECTOR_CACHE`/`_BK_LIST_*`）、指数快照缓存（`_IDX_QUOTE_CACHE`）的读-改-写全部持锁。
- 「清除缓存」按钮同步清空内存中的层池缓存。

### #2 数据库连接泄露 ✅
- 新增上下文管理器 `db_conn(commit=False)`（stock_gui.py:117）：自动提交/回滚/关闭，异常时先 rollback 再上抛。
- 替换了全部裸 `conn = _cx()` 调用点：`init_db`、`db_hist_count`、`sanitize_daily_db`、`get_daily`、`prefetch`、`stocks_age`、`refresh_all_codes`、`get_stock_info`、`industry_peers`、`tier_sample`、`_load_pools`、`load_pools_progressive`、「清除缓存」。SQLite 并发连接耗尽风险消除。

### #3 异常静默 → 全部记录日志 ✅ / #10 引入 logging ✅
- 日志初始化 `setup_logging()`（stock_gui.py:47，位于内嵌缓存层内，CLI 同样生效）：
  - 文件：`stock_gui.log`，INFO+，RotatingFileHandler 5MB×2，UTF-8；
  - 控制台：仅 WARNING+。
- 原 `except: pass` 的位置全部改为 `log.warning/exception`：`get_daily` 增量拉取失败、样本池回填/匹配失败、`_start_progressive` 增量加载失败、综合评估(action)、回测统计(bt_stats)、AI 后台任务等。GUI 出问题时可在日志中追溯。

### #4 Tkinter 线程安全退出 ✅
- 新增 `App._safe_after(ms, fn)`（stock_gui.py:2960）：调用前检查 `root.winfo_exists()`，并捕获 `RuntimeError/tk.TclError`。
- 所有后台线程到主线程的调度（`_progress`、`_run_bg` 回调、增量加载更新、定时器重入）统一走 `_safe_after`。窗口关闭瞬间不再抛错。

### #5 历史信号前视偏差 ✅（策略正确性底线）
本轮重构前信号打分本身已因果化（统计预测维度不参与历史打分），但仍残留两处"用今日数据标注过去"的前视：
1. **弱势行情过滤**：原逻辑用「今日大盘跌>1.5% / 今日板块跌>2%」全局开关，去收紧**全部历史** BUY 阈值——历史某天的信号被未来的市场信息影响。
   → 改为 `_weak_day(day)`（stock_gui.py:2356）：只查 `idx_chg_by_date[day]` / `sec_chg_by_date[day]` **当日值**；当日数据缺失时不放大阈值（保守回退=原行为）。
2. **趋势跟踪信号**（MA20/60 金叉路径）：原 `idx_ok = ic > idx_chg_today - 1.2` 拿当日大盘涨跌过滤历史金叉。
   → 改为只看当日大盘：`idx_ok = ic is None or ic > -1.2`。
- 现状确认：`res['signals']` 的生成完全只用 T 日及以前的数据；`t_pred/up_prob/multi_pred` 只进入**当天**的综合评估（action），不回填历史 —— `backtest_signals` 的模拟结果因此有效。
- 附注：`analyze` 与 `backtest_signals` 职责已实际分离（信号在 analyze 内滚动生成、无未来样本权重泄漏），未再做更大规模拆分。

## 二、中优先级

### #6 网络请求统一走熔断器 ✅
| 接口 | 原来 | 现在 |
|---|---|---|
| 腾讯行情文本接口 `http_get` | 自带 urllib + 死循环重试 | 薄封装委托 `_http_get(src_name="腾讯行情")` |
| 行业板块排行 `fetch_top_sectors` | 裸 urlopen，绕过熔断 | `_http_get(src_name="东财板块")`，每 host 记录日志 |
| 板块指数/行业名 `fetch_sector_context` | 私有裸 get | `_http_get(src_name="东财板块")` |

现在全部出网请求都向熔断器上报成败，503 限流风暴下不存在绕行通道。

### #7 魔数集中 ✅
新增 `class CFG`（stock_gui.py:89）：W_WINDOW/TOPK/LV_W 权重、买卖信号分数阈值与冷却期、弱势阈值(-1.5/-2.0)、波段适合度门槛(60)、多日预测天数(10)。原有模块级别名（`W_WINDOW/TOPK/LV_W`）保留，报告文本等调用点零改动。

### #8 GUI 图表自适应 ✅
三个图表面板由固定 `pack(fill=x, height=X)` 改为 grid + 行权重 6/2/3（stock_gui.py:2892），窗口高度变化时主图/量图/指标图按比例伸缩，指数条 sticky=ew 固定底部。

### #14 _run_bg 弃用轮询 ✅
原来每 250ms 轮询一次 holder 字典（最多 480 次）。现改为共享后台线程池 `submit + future.add_done_callback`，结果经 `_safe_after` 回主线程。无空转 CPU，无 2 分钟假超时。

## 三、低优先级

- **#9 类型注解**：对外关键函数已加（`get_daily`、`db_conn`、`db_hist_count`、`stocks_age->float`、`set_proxy`、`set_ai_model` 等），存量函数逐步迁移。
- **#11 索引**：stocks 表新增 `idx_stocks_industry`、`idx_stocks_tier`（init_db 内，旧库启动即建）（stock_gui.py:147）。
- **#12 共享线程池**：`_SHARED_EX`(data, 8 workers) 承担 prefetch/analyze 并发取数；prefetch 不再每次 `with ThreadPoolExecutor(...)` 新建销毁。
- 顺手修复两处会直接崩的旧 bug：
  - AI 分析 prompt 中引用了早已不存在的样本字段 `s["cl_o"]`（点 AI 分析必 KeyError）→ 改用 `n1_cl`；
  - 「相似样本明细」窗口引用旧字段 `hi_o/lo_o/t2_*` 全是空白键 → 改为 n1/n2 字段并容错缺列。

## 四、AI 分析模型切换 ✅

- 默认模型改为 **deepseek-v4-pro**（stock_gui.py:218）。
- 配置来源：`stock_gui.ini [deepseek] model`，设置窗口新增「AI 分析模型」下拉（deepseek-v4-pro / deepseek-chat / deepseek-reasoner），保存即持久化并即时生效。
- 若服务端不支持该模型名，请求会返回 HTTP 错误：界面提示 + 详细错误写入 `stock_gui.log`；此时切回 `deepseek-chat` 即可。

## 五、跳过项

- **#13 多文件拆分**：按要求不做。`build_cli.py` 的抽取锚点（`# ================= 内嵌缓存层` / `QT_URL = ` / `def slice_view`）未被破坏，CLI 单文件版照常生成（108.1KB）。

## 六、验证记录

```
python -m py_compile stock_gui.py        -> OK
python build_cli.py                      -> 已生成 stock_predict.py (108.1 KB)
python -m py_compile stock_predict.py    -> OK
python -c "analyze('sz002241', quick)"   -> samples=10 signals=9 bt=True
python -c "analyze('sz000725')"          -> L1 7样本 / L2 10 / L3 10，
                                            回测胜率 0.5，pool_note 正常
stock_gui.log                            -> 板块拉取失败等 WARNING 有完整堆栈
```

风险提示：本工具所有输出仅为历史数据技术统计，不构成投资建议。
