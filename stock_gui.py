#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票形态相似度预测工具 GUI 增强版（腾讯行情源，纯标准库）

  - K线主图 + MA5/10/20/30/60（可勾选）+ 买卖点标记
  - 成交量副图
  - 可选指标副图：MACD / KDJ / RSI
  - 十字光标：对齐右侧价格轴与下方日期轴，显示当日OHLC
  - 右侧栏：预测结果与相似历史参考日期
  - 周期切换 / 复制报告 / 导出报告 / 样本明细

仅统计参考，不构成投资建议。
"""
import atexit
import configparser
import heapq
import json
import logging
import math
import os
import random
import re
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

try:
    import numpy as np            # 数值加速（缺失时自动退回纯Python）
except ImportError:
    np = None

CACHE_OK = True


# ================= 内嵌缓存层（原 stock_cache.py，单文件化）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "stock_cache.db")
INI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "stock_gui.ini")

# ---- 日志（#10）：文件 INFO+（滚动5MBx2），控制台 WARNING+ ----
from logging.handlers import RotatingFileHandler  # noqa: E402

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "stock_gui.log")
log = logging.getLogger("stock")


def setup_logging():
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(name)s %(message)s")
    try:
        fh = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024,
                                 backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        log.addHandler(fh)
    except OSError:
        pass                      # 文件不可写时退化为仅控制台
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING)
    log.addHandler(sh)
    return log


setup_logging()

KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
# 急救箱(stock_firstaid.py)写入的备用源覆盖默认值：
# ini [data] kline_url = 完整接口URL（须以http(s)开头，否则忽略）
try:
    import configparser as _cp_firstaid
    _fa_cp = _cp_firstaid.ConfigParser()
    _fa_cp.read(INI_PATH, encoding="utf-8")
    _fa_url = _fa_cp.get("data", "kline_url", fallback="").strip()
    if _fa_url.startswith(("http://", "https://")):
        KLINE_URL = _fa_url
except Exception:
    pass
UT = "fa5fd1943c7b386f172d6893dbfba10b"
TIERS = ("大盘", "中盘", "小盘")
STOCKS_TTL = 7 * 86400          # 全市场代码表缓存7天
INIT_LOCK = threading.Lock()
REFRESH_LOCK = threading.Lock()

# 默认池大小（可被 analyze 的参数覆盖）
L2_DEFAULT_N = 50               # 同行业同伴数
L3_DEFAULT_N = 100              # 同市值层抽样数

# 全局共享可变状态锁：所有缓存字典的读-改-写必须持有本锁
_STATE_LOCK = threading.RLock()

# 共享线程池：预取/分析/后台任务复用，避免每次新建线程池开销
_SHARED_EX = ThreadPoolExecutor(max_workers=8, thread_name_prefix="data")
_BG_EX = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bg")


class CFG:
    """集中调参（原散落各处的魔数收敛于此；模块级别名保持兼容）。"""
    W_WINDOW = 20                       # 形态匹配窗口长度（扫描：W20 IC+0.016
                                        # vs W10≈0，两次独立一致，自适应兼容短历史）
    TOPK = 10                           # Top-K 相似样本数（显示用）
    CANDIDATE_TOPK = 50                 # 候选样本数（扩大后再筛选）
    LV_W = {"L1": 0.6, "L2": 0.3, "L3": 0.1}    # 三级样本池权重
    SIGNAL_SCORE_BUY = 2                # 多头信号触发分
    SIGNAL_SCORE_SELL = -2              # 空头信号触发分
    SIGNAL_COOLDOWN = 5                 # 相邻信号最小间隔(交易日)
    WEAK_IDX_TH = -1.5                  # 大盘弱势阈值(%)
    WEAK_SEC_TH = -2.0                  # 板块弱势阈值(%)
    BAND_FIT_MIN = 60.0                 # 波段适合度门槛
    PRED_MAX_DAYS = 10                  # 多日预测天数
    
    # 样本质量筛选与加权参数
    SIMILARITY_WEIGHTING = False        # 指数相似度加权（消融回测证实拖后腿：
                                        # 关闭后 命中50.4%→52.4%, IC -0.043→+0.020）
    QUALITY_FILTER = True               # 是否启用样本质量筛选
    MAX_DAILY_CHANGE = 10.0             # 单日涨跌停阈值(%) - 超过视为异常
    MIN_SAMPLES_REQUIRED = 3            # 最少样本数要求 - 少于则降低置信度
    SIMILARITY_CUTOFF = 2.5             # 相似度截断阈值 - 超过则降低权重
    TIME_DECAY_ENABLED = True           # 是否启用时间衰减
    TIME_DECAY_DAYS = 90                # 时间衰减天数 - 超过此天数的样本权重衰减
    TIME_DECAY_RATE = 0.3               # 时间衰减率 - 超过天数的样本权重乘数
    
    # 置信度系统参数
    CONFIDENCE_ENABLED = True           # 是否启用置信度系统
    LOW_CONFIDENCE_SCORE = 0.3          # 低置信度阈值
    MEDIUM_CONFIDENCE_SCORE = 0.6       # 中置信度阈值

    # 多维形态匹配扩展（K线结构/波动率/RSI/量变/周线环境）
    STRUCT_W = 0.50                     # K线结构距离权重
    VOLA_W = 0.80                       # 波动率距离权重
    RSI_W = 0.50                        # RSI距离权重
    VOLCHG_W = 0.40                     # 量变(近5日/前5日)距离权重
    WEEKLY_W = 0.60                     # 周线环境距离权重
    WEEKLY_N = 4                        # 周线环境回看周数

    # 动态三级权重
    DYNAMIC_LV_W = True                 # 是否按各层最优相似度动态调整 L1/L2/L3 权重
    DYN_LV_STRENGTH = 0.5               # 动态混合强度(0=固定先验,1=完全按样本质量)
    # 层级消融回测(n=225)：L1+L2 命中56.9%/IC+0.057 最优；
    # L3(同市值层)拖后腿(并入后IC降至-0.001)，默认关闭
    ENABLE_L3 = False
    # 区间校准系数（n=4500实测：样本分位区间系统性过窄41%/70%，
    # 偏离P50放大1.3倍后 P25-P75→51.8%(名义50)、P10-P90→80.0%(名义80)）
    INTERVAL_K = 1.3
    # T+5 区间校准系数（n=4500标定 1.4最优：51.6%/79.2%）
    INTERVAL_K5 = 1.4

    # ---- 风险偏好（三级·网格寻优后参数）----
    # 保守=信号严(评分3+冷却8)+止损紧(ATR1.5/回落4%即走) → 胜率40.9%·年化中位-0.9%（最优）
    # 激进=捕捉机会(评分1+冷却3)+止损松(ATR2.5/回落10%) → 胜率31.7%·-6.0%，波动大机会多
    # 数据源：n=6000回测网格，详见 报告_买卖点收益回测.md
    RISK_MODE = "稳健"
    RISK_PARAMS = {
        "保守": {"atr_mult": 1.5, "trail_trigger": 1.01,
                 "trail_ratio": 0.96, "buy_th": 3, "cooldown": 8},
        "稳健": {"atr_mult": 1.5, "trail_trigger": 1.02,
                 "trail_ratio": 0.94, "buy_th": 2, "cooldown": 5},
        "激进": {"atr_mult": 2.5, "trail_trigger": 1.05,
                 "trail_ratio": 0.90, "buy_th": 1, "cooldown": 3},
    }

    def risk_params():
        return CFG.RISK_PARAMS.get(CFG.RISK_MODE, CFG.RISK_PARAMS["稳健"])
    
    # 各技术维度在信号打分中的权重（1.0=标准；<1 降权、>1 升权）
    IND_W = {
        "MACD": 1.1,        # 趋势主指标，加权
        "KDJ": 0.9,         # 摆动指标，略降权（横盘易钝化）
        "RSI": 0.9,         # 同上
        "量价": 1.0,
        "MA20": 1.0,
        "MA趋势": 1.2,      # MA20/60趋势状态（v3.3全A实证 IC 0.228，最强规则信号）
        "形态": 1.2,        # L1形态上行概率（IC 0.265，全A实证最强）
        "爆发力": 1.0,      # 20日动量（10%~35%强势区加分，>35%过热减分：bias20 极端延伸 IC 为负）
        "量能": 0.9,        # 量能扩张（5日均量/20日均量，突破期特征）
        "板块": 1.0,        # 板块轮动（行业5日收益强势/前20%领先，v4.0.2 全A实证 Rot 变体显著增益）
        "筹码": 0.8,
        "布林带": 0.8,      # 均值回归维度，震荡市才准，降权
        "ADX": 0.8,         # 趋势强度过滤器维度
    }


def _load_predict_cfg():
    """从 stock_gui.ini [predict] 读取用户调过的预测参数（带范围钳制）。
    必须在模块级别名 W_WINDOW/TOPK 赋值之前执行。"""
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        if not cp.has_section("predict"):
            cp.add_section("predict")   # 缺节时 get 才不会抛 NoSectionError

        def gi(key, dflt, lo, hi):
            try:
                return max(lo, min(hi, int(cp.get("predict", key,
                                                  fallback=dflt))))
            except (ValueError, TypeError):
                return dflt

        def gf(key, dflt, lo, hi):
            try:
                return max(lo, min(hi, float(cp.get("predict", key,
                                                    fallback=dflt))))
            except (ValueError, TypeError):
                return dflt

        CFG.W_WINDOW = gi("w_window", CFG.W_WINDOW, 5, 30)
        CFG.TOPK = gi("topk", CFG.TOPK, 3, 30)
        CFG.CANDIDATE_TOPK = gi("candidate_topk", CFG.CANDIDATE_TOPK, 10, 100)
        CFG.TIME_DECAY_DAYS = gi("time_decay_days", CFG.TIME_DECAY_DAYS,
                                 0, 1095)
        CFG.TIME_DECAY_RATE = gf("time_decay_rate", CFG.TIME_DECAY_RATE,
                                 0.0, 1.0)
        CFG.DYN_LV_STRENGTH = gf("dyn_lv_strength", CFG.DYN_LV_STRENGTH,
                                 0.0, 1.0)
        CFG.WEEKLY_N = gi("weekly_n", CFG.WEEKLY_N, 2, 8)
        CFG.STRUCT_W = gf("struct_w", CFG.STRUCT_W, 0.0, 3.0)
        CFG.VOLA_W = gf("vola_w", CFG.VOLA_W, 0.0, 3.0)
        CFG.RSI_W = gf("rsi_w", CFG.RSI_W, 0.0, 3.0)
        CFG.VOLCHG_W = gf("volchg_w", CFG.VOLCHG_W, 0.0, 3.0)
        CFG.WEEKLY_W = gf("weekly_w", CFG.WEEKLY_W, 0.0, 3.0)
        rm = cp.get("predict", "risk_mode", fallback=CFG.RISK_MODE)
        if rm in CFG.RISK_PARAMS:
            CFG.RISK_MODE = rm
        CFG.ENABLE_L3 = bool(gi("enable_l3", 0 if not CFG.ENABLE_L3 else 1,
                                0, 1))
    except Exception:
        log.exception("读取预测参数失败(使用默认)")


_load_predict_cfg()


W_WINDOW: int = CFG.W_WINDOW
TOPK: int = CFG.TOPK


# ================= 基础 =================

def _cx():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db_conn(commit: bool = False):
    """SQLite 连接上下文管理器：保证提交/回滚并关闭，杜绝连接泄露。"""
    conn = _cx()
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            log.exception("db rollback failed")
        raise
    finally:
        conn.close()


def init_db() -> None:
    with INIT_LOCK:
        with db_conn(commit=True) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS daily_bars(
                    code TEXT NOT NULL, date TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL, vol REAL,
                    PRIMARY KEY(code, date));
                CREATE INDEX IF NOT EXISTS idx_bars_code
                    ON daily_bars(code, date);
                CREATE TABLE IF NOT EXISTS stocks(
                    code TEXT PRIMARY KEY, name TEXT, industry TEXT,
                    mktcap REAL, tier TEXT, updated TEXT);
                CREATE INDEX IF NOT EXISTS idx_stocks_industry
                    ON stocks(industry);
                CREATE INDEX IF NOT EXISTS idx_stocks_tier
                    ON stocks(tier);
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS failed(
                    code TEXT PRIMARY KEY, ts REAL, reason TEXT);
            """)
            # 迁移：旧库 failed 表若无 reason 列则补列
            try:
                cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(failed)").fetchall()]
                if cols and "reason" not in cols:
                    conn.execute("ALTER TABLE failed ADD COLUMN reason TEXT")
            except Exception:
                log.exception("failed 表迁移失败(忽略)")


init_db()


def _get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                 (key, str(value)))


# ================= 代理 =================

_PROXY_OPENER = None            # 仅K线等 stock_cache 内部请求走代理


def set_proxy(url):
    """设置K线数据源代理（只影响 stock_cache 的请求；
    行情快照/板块等国内接口保持直连）。url 形如 http://127.0.0.1:7890。"""
    global _PROXY_OPENER
    url = (url or "").strip()
    try:
        if not url:
            _PROXY_OPENER = None
            return ""
        if not url.startswith("http"):
            url = "http://" + url
        handler = urllib.request.ProxyHandler({"http": url, "https": url})
        _PROXY_OPENER = urllib.request.build_opener(handler)
        return url
    except Exception:
        _PROXY_OPENER = None
        return ""


def _load_proxy_ini():
    try:
        import configparser
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        return cp.get("proxy", "url", fallback="")
    except Exception:
        return ""


set_proxy(_load_proxy_ini())    # 导入即生效（GUI/CLI通用）


# ================= AI 模型配置 =================

AI_MODEL_DEFAULT = "deepseek-v4-pro"


def _load_ai_model() -> str:
    """从 stock_gui.ini [deepseek] model 读取AI分析模型名。"""
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        return cp.get("deepseek", "model", fallback=AI_MODEL_DEFAULT)
    except Exception:
        return AI_MODEL_DEFAULT


AI_MODEL = _load_ai_model()


def set_ai_model(model: str) -> None:
    """运行时切换AI模型并持久化到 ini。"""
    global AI_MODEL
    AI_MODEL = (model or "").strip() or AI_MODEL_DEFAULT
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        if not cp.has_section("deepseek"):
            cp.add_section("deepseek")
        cp.set("deepseek", "model", AI_MODEL)
        with open(INI_PATH, "w", encoding="utf-8") as f:
            cp.write(f)
    except Exception:
        log.exception("保存AI模型设置失败")


# ================= 数据源熔断器（针对503限流） =================

_SRC_CB = {}                    # 源名 -> [连续失败数, 熔断截止时间戳]
_CB_LOCK = threading.Lock()
_CB_THRESHOLD = 2               # 连续失败N次触发熔断
_CB_BASE_COOLDOWN = 60.0        # 首次熔断冷却60秒
_CB_MAX_COOLDOWN = 600.0        # 冷却上限10分钟


def _cb_ok(name):
    """源当前是否可用（未熔断）。"""
    st = _SRC_CB.get(name)
    return not (st and st[1] > 0 and time.time() < st[1])


def _cb_record(name, ok, err=None):
    """上报数据源一次请求结果。

    - 成功 → 计数清零，立即结束熔断（半开探测成功）；
    - 失败且属限流类(503等) → 连续失败数+1，达到阈值按
      cooldown = min(BASE * 2^(n-THRESHOLD), MAX) 指数延长熔断时间；
    - 普通网络错误只累计失败数，不单独延长冷却。"""
    ratelimited = (not ok) and _is_ratelimit_err(err) if err is not None \
        else False
    with _CB_LOCK:
        st = _SRC_CB.setdefault(name, [0, 0.0])
        if ok:
            st[0], st[1] = 0, 0.0
            return
        st[0] += 1
        if ratelimited and st[0] >= _CB_THRESHOLD:
            cd = min(_CB_BASE_COOLDOWN *
                     (2 ** (st[0] - _CB_THRESHOLD)), _CB_MAX_COOLDOWN)
            st[1] = max(st[1], time.time() + cd)


def _is_ratelimit_err(e):
    """识别服务端限流/过载类错误：HTTP 429/502/503/504。"""
    code = getattr(e, "code", None)
    if code is not None:
        return code in (429, 502, 503, 504)
    s = str(e)
    return any(c in s for c in ("503", "502", "504", "429",
                                "Service Unavailable"))


def _backoff_delay(attempt, base=0.5, cap=6.0):
    """指数退避 + 抖动：base * 2^attempt，上限cap，±25%随机抖动防雪崩。"""
    d = min(base * (2 ** attempt), cap)
    import random
    return d * (0.75 + random.random() * 0.5)


def _http_get(url, retries=3, timeout=15, decode="utf-8", headers=None,
              src_name=None):
    """HTTP GET（带限流感知重试）。

    - 普通错误：指数退避+抖动后原URL重试；
    - 503/429等限流：只做最多1次退避重试就抛出，
      让上层多源切换/熔断机制接管，避免反复撞同一限流IP。
    - src_name 非空时向数据源熔断器上报成败。"""
    last = None
    hdr = {"User-Agent": "Mozilla/5.0"}
    if headers:
        hdr.update(headers)
    ok_flag = False
    try:
        for a in range(retries):
            with _THROTTLE_LOCK:
                wait = _MIN_INTERVAL - (time.time() - _LAST_REQ[0])
                if wait > 0:
                    time.sleep(wait)
                _LAST_REQ[0] = time.time()
            try:
                req = urllib.request.Request(url, headers=hdr)
                txt = None
                if _PROXY_OPENER is not None:
                    try:
                        with _PROXY_OPENER.open(req, timeout=timeout) as r:
                            txt = r.read().decode(decode, errors="ignore")
                    except Exception as pe:
                        # 代理不可用（软件未开/节点故障）时自动回退直连，
                        # 避免配置代理后一个源都拉不到
                        log.debug("代理请求失败，回退直连 %s: %s",
                                  url[:80], pe)
                if txt is None:
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        txt = r.read().decode(decode, errors="ignore")
                ok_flag = True
                return txt
            except Exception as e:
                last = e
                # 限流类错误：退避后仅再试一次即放弃（快速切源）
                eff_retries = min(retries, 2) if _is_ratelimit_err(e) \
                    else retries
                if a + 1 >= eff_retries:
                    break
                time.sleep(_backoff_delay(a))
        raise RuntimeError(f"网络请求失败: {last}")
    finally:
        if src_name:
            _cb_record(src_name, ok_flag, last)


# ================= 交易日辅助 =================

def _dstr(d):
    return d.strftime("%Y-%m-%d")


def _prev_weekday(d):
    import datetime
    d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def last_completed_td():
    """库中最后一天日K应为的日期 = 今天之前的最近工作日。
    注意：今日bar永远不入库（盘中未收盘，由实时快照在analyze里合成），
    所以即使已收盘，新鲜度基准也是上一个工作日。"""
    import datetime
    return _dstr(_prev_weekday(datetime.date.today()))


# ================= 日K增量缓存 =================

def _db_rows(conn, code):
    rows = conn.execute(
        "SELECT date,open,high,low,close,vol FROM daily_bars "
        "WHERE code=? ORDER BY date", (code,)).fetchall()
    return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "vol": r[5] or 0.0} for r in rows]


def db_hist_count(code: str) -> int:
    """快速返回该股缓存的日K总根数（仅查库，不联网）。"""
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM daily_bars WHERE code=?",
                (code,)).fetchone()
            return row[0] if row else 0
    except Exception:
        log.exception("db_hist_count failed: %s", code)
        return 0


def _db_rows_batch(conn, codes):
    """批量读取多只股票缓存，返回 {code: [row_dict,...]}。单次连接。"""
    placeholders = ",".join("?" for _ in codes)
    cur = conn.execute(
        f"SELECT code,date,open,high,low,close,vol FROM daily_bars "
        f"WHERE code IN ({placeholders}) ORDER BY code,date", codes)
    out = {}
    for r in cur:
        code = r[0]
        if code not in out:
            out[code] = []
        out[code].append({"date": r[1], "open": r[2], "high": r[3],
                          "low": r[4], "close": r[5], "vol": r[6] or 0.0})
    return out


def _bar_ok(r):
    """K线数据结构合法性校验：剔除脏数据（缺失/非正价/高低颠倒）。
    影线比例不再作为剔除依据——低价股分值效应下正常K线会被大量误杀
    （实测老数据误杀率30%+），价格异常由 _bars_anomalous 涨跌幅校验兜底。"""
    o, h, l, c = r["open"], r["high"], r["low"], r["close"]
    if None in (o, h, l, c):
        return False
    if min(o, h, l, c) <= 0:
        return False
    if h < l:
        return False
    return True


def sanitize_daily_db() -> None:
    """历史脏数据清理（仅手动调用；缓存默认不自动清理，保持大数据量提升准头）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code,date,open,high,low,close FROM daily_bars").fetchall()
        bad = [(c, d) for c, d, o, h, l, cl in rows
               if not _bar_ok({"open": o, "high": h, "low": l, "close": cl})]
        if bad:
            conn.executemany(
                "DELETE FROM daily_bars WHERE code=? AND date=?", bad)
            conn.commit()


def _limit_pct(code, name, date):
    """该股当日允许的涨跌幅限制(%)，无限制返回None。
    - 主板普通股10%；主板ST在2026-07-06新规实施前为5%，之后统一10%
    - 创业板2020-08-24注册制改革后20%（含ST）、科创板20%（含ST）
    - 北交所30%
    - 1996-12-16涨跌停板制度实施前不限"""
    if date < "1996-12-16":
        return None
    if code.startswith("bj"):
        return 30.0
    board = code[2:4] if len(code) >= 4 else ""
    if board == "68":              # 科创板
        return 20.0
    if board == "30":              # 创业板
        return 20.0 if date >= "2020-08-24" else 10.0
    if (name and "ST" in name.upper()
            and date < "2026-07-06"):   # 主板ST旧规5%
        return 5.0
    return 10.0


def _is_etf(code):
    """是否ETF/LOF代码：沪 51/56/58，深 15/16/18 开头。"""
    pre = code[2:4] if len(code) >= 4 else ""
    return pre in ("51", "56", "58", "15", "16", "18")


def _bars_anomalous(rows, code, name=""):
    """相邻日涨跌幅超出涨跌停允许范围即视为数据异常。

    - 个股：容差 涨跌停+3pp。
    - ETF/LOF：除权除息、份额折算/拆分会产生单根K线的大跳变
      （分红 ±10%+、折算可能 ±66%/±200%），但折算通常只影响单日，
      跳变后价格恢复连续。因此对 ETF 采用「孤立跳变放行」策略：
      只有**连续**出现超阈值大跳变（≥2次相邻）才判为数据错误；
      单次孤立大跳变视为合法除权/折算。
    - 历史不足30根的新股跳过检查。
    """
    if len(rows) < 30:
        return False
    is_etf = _is_etf(code)
    threshold_extra = 12.0 if is_etf else 0.0
    # 记录每根是否超阈值
    flags = []
    for prev, r in zip(rows, rows[1:]):
        pc = prev.get("close")
        c = r.get("close")
        if not pc or pc <= 0 or not c or c <= 0:
            flags.append(False)
            continue
        lim = _limit_pct(code, name, r["date"])
        if lim is None:
            flags.append(False)
            continue
        chg = abs(c / pc - 1) * 100
        flags.append(chg > lim + 3.0 + threshold_extra)
    if not any(flags):
        return False
    # 非ETF：任一超阈值即异常
    if not is_etf:
        return True
    # ETF：连续超阈值（相邻两根都异常）才判异常；孤立单次跳变放行
    for i in range(1, len(flags)):
        if flags[i] and flags[i - 1]:
            return True
    return False


# ================= 多源日K获取 =================

def _code_to_163(full):
    """腾讯代码 → 网易163代码：sh600519 → 0600519, sz002241 → 1002241"""
    if full.startswith("sh"):
        return "0" + full[2:]
    if full.startswith("sz"):
        return "1" + full[2:]
    return None


def _code_to_em(full):
    """腾讯代码 → 东财代码：sh600519 → 1.600519, sz002241 → 0.002241"""
    if full.startswith("sh"):
        return "1." + full[2:]
    if full.startswith("sz"):
        return "0." + full[2:]
    return None


def _fetch_tencent(full, count=600, host=None):
    """腾讯K线。host 可换备用域名（主域被限流时走代理域/HTTP域）。"""
    host = host or KLINE_URL
    txt = _http_get(host + f"?param={full},day,,,{count},qfq",
                    decode="utf-8", retries=1, timeout=8)
    kd = json.loads(txt)
    d = (kd.get("data") or {}).get(full) or {}
    bars = d.get("qfqday") or d.get("day") or []
    out = []
    for b in bars:
        try:
            if float(b[2]) <= 0:
                continue
            out.append({"date": b[0], "open": float(b[1]),
                        "close": float(b[2]), "high": float(b[3]),
                        "low": float(b[4]), "vol": float(b[5])})
        except (ValueError, IndexError):
            continue
    return out


def _fetch_163(full, count=600):
    """网易163财经（免费，稳定性好，返回CSV）"""
    code163 = _code_to_163(full)
    if not code163:
        return []
    import datetime
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=count * 2)
             ).strftime("%Y%m%d")
    url = (f"http://quotes.money.163.com/service/chddata.html"
           f"?code={code163}&start={start}&end={end}"
           f"&fields=TCLOSE;HIGH;LOW;TOPEN;VOTURNOVER")
    txt = _http_get(url, retries=2, timeout=15, decode="gbk")
    out = []
    for line in txt.strip().split("\n"):
        if not line.strip() or line.startswith("日期"):
            continue
        parts = line.strip().split(",")
        if len(parts) < 7:
            continue
        try:
            date = parts[0].strip().strip("'")
            close = float(parts[3]) if parts[3].strip() else 0
            high = float(parts[4]) if parts[4].strip() else 0
            low = float(parts[5]) if parts[5].strip() else 0
            opn = float(parts[6]) if parts[6].strip() else 0
            vol = float(parts[11]) if len(parts) > 11 and parts[11].strip() else 0
            if close <= 0:
                continue
            out.append({"date": date, "open": opn, "close": close,
                        "high": high, "low": low, "vol": vol})
        except (ValueError, IndexError):
            continue
    out.reverse()  # 网易返回倒序，翻转
    return out[:count]


def _fetch_eastmoney(full, count=600):
    """东方财富K线（免费，JSON格式）。多 host 轮询防限流。"""
    secid = _code_to_em(full)
    if not secid:
        return []
    hosts = ("push2his.eastmoney.com",
             "92.push2his.eastmoney.com",
             "93.push2his.eastmoney.com",
             "97.push2his.eastmoney.com")
    last_err = None
    for host in hosts:
        url = (f"https://{host}/api/qt/stock/kline/get"
               f"?secid={secid}&fields1=f1,f2,f3"
               f"&fields2=f51,f52,f53,f54,f55,f56"
               f"&klt=101&fqt=1&beg=0&end=20500101&lmt={count}")
        try:
            txt = _http_get(url, retries=2, timeout=20,
                            headers={"Referer": "https://quote.eastmoney.com/"})
            kd = json.loads(txt)
            klines = (kd.get("data") or {}).get("klines") or []
            out = []
            for line in klines:
                parts = line.split(",")
                if len(parts) < 6:
                    continue
                try:
                    close = float(parts[2])
                    if close <= 0:
                        continue
                    out.append({"date": parts[0], "open": float(parts[1]),
                                "close": close, "high": float(parts[3]),
                                "low": float(parts[4]), "vol": float(parts[5])})
                except (ValueError, IndexError):
                    continue
            if out:
                # 东财 lmt 实际返回全量，取最近 count 根
                return out[-count:]
            last_err = RuntimeError("东财返回空K线")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"东财所有host均失败: {last_err}")


def _fetch_sina(full, count=600):
    """新浪财经K线（免费，稳定，返回JSON）"""
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/"
           f"json_v2.php/CN_MarketData.getKLineData"
           f"?symbol={full}&scale=240&ma=no&datalen={count}")
    txt = _http_get(url, retries=2, timeout=15, decode="utf-8",
                   headers={"Referer": "https://finance.sina.com.cn/"})
    # 新浪返回的不是标准JSON（key没引号），做简单修复
    import re
    txt = re.sub(r'(?<=[{,])(\w+):', r'"\1":', txt)
    bars = json.loads(txt)
    out = []
    for b in bars:
        try:
            close = float(b.get("close", 0))
            if close <= 0:
                continue
            out.append({"date": b["day"], "open": float(b["open"]),
                        "close": close, "high": float(b["high"]),
                        "low": float(b["low"]),
                        "vol": float(b.get("volume", 0))})
        except (ValueError, KeyError):
            continue
    return out


def _fetch_remote_rows(full, count=600):
    """多源自动切换 + 熔断调度：腾讯ifzq → 东财 → 新浪 → 网易163。

    2026-08 实测：web.ifzq.gtimg.cn 已下线(501)，proxy.finance.qq.com
    返回404，现役主域为 ifzq.gtimg.cn；东财 push2his 整域故障期间
    自动降级；网易163服务端502时段排到最后。
    运行逻辑：
    1. 按优先级遍历数据源，跳过处于熔断冷却期的源；
    2. 若所有源都在冷却（极端503风暴），退化为「半开探测」：
       选冷却结束最早的源强行试一次，成功即重置熔断；
    3. 单次调用内只对一个源做至多2次限流重试，
       失败立刻切下一源，避免整体请求被单源拖死。"""
    sources = [
        ("腾讯", lambda: _fetch_tencent(full, count)),
        ("腾讯代理", lambda: _fetch_tencent(
            full, count,
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
            "fqkline/get")),
        ("腾讯HTTP", lambda: _fetch_tencent(
            full, count,
            "http://ifzq.gtimg.cn/appstock/app/fqkline/get")),
        ("东财", lambda: _fetch_eastmoney(full, count)),
        ("新浪", lambda: _fetch_sina(full, count)),
        ("网易163", lambda: _fetch_163(full, count)),
    ]
    last_err = None
    usable = [(n, f) for n, f in sources if _cb_ok(n)]
    if not usable:
        # 半开探测：挑最早解禁的源
        probe = min(sources,
                    key=lambda nf: _SRC_CB.get(nf[0], [0, 0.0])[1])
        usable = [probe]
    for name, fetcher in usable:
        try:
            rows = fetcher()
            _cb_record(name, True)
            # 新股可能只有几根K线：>=5 即视为有效（分析层另有30根门槛）
            if rows and len(rows) >= 5:
                return rows
            last_err = RuntimeError(f"{name}返回空K线")
        except Exception as e:
            _cb_record(name, False, e)
            last_err = e
            continue
    _auto_heal_kline()          # 全灭时自动探测候选域并切换
    _maybe_ai_rescue()          # 仍无解：弹窗询问是否让AI找源(GUI)
    raise RuntimeError(f"所有数据源均失败: {last_err}")


FAIL_TTL = 3600                 # 拉取失败记忆期1小时
_MIN_INTERVAL = 0.16            # 全局HTTP最小间隔，防腾讯限速
_THROTTLE_LOCK = threading.Lock()
_LAST_REQ = [0.0]


# ---- K线源自动容灾（急救箱逻辑内嵌，无人工参与）----
_KLINE_HEAL_TS = [0.0]          # 上次自愈探测时间（10分钟限频）
_KLINE_HEAL_LOCK = threading.Lock()
_KLINE_DEFAULTS = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "http://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)


def _probe_kline_url(base):
    """实测一个K线接口是否真的有数据（近5根日K + JSON合法）。"""
    try:
        txt = _http_get(base + "?param=sz002241,day,,,5,qfq",
                        retries=1, timeout=6)
        d0 = (json.loads(txt).get("data") or {}).get("sz002241") or {}
        bars = d0.get("qfqday") or d0.get("day") or []
        return len(bars) >= 5
    except Exception:
        return False


def _persist_kline_url(u):
    """把可用K线源写入内存与 ini（重启后仍生效）。"""
    globals()["KLINE_URL"] = u
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        if not cp.has_section("data"):
            cp.add_section("data")
        cp.set("data", "kline_url", u)
        cp.set("data", "kline_url_updated", time.strftime("%Y-%m-%d %H:%M"))
        with open(INI_PATH, "w", encoding="utf-8") as f:
            cp.write(f)
    except Exception:
        log.exception("kline_url 持久化失败(忽略)")


def _auto_heal_kline():
    """全部K线源失败时自动探测候选域并切换（内存+ini持久化）。
    10分钟限频，探测失败不影响原流程。"""
    with _KLINE_HEAL_LOCK:
        if time.time() - _KLINE_HEAL_TS[0] < 600:
            return False
        _KLINE_HEAL_TS[0] = time.time()
    cands = [u for u in _KLINE_DEFAULTS if u != KLINE_URL]
    for u in cands:
        if _probe_kline_url(u):
            _persist_kline_url(u)
            log.warning("K线源自动切换: %s", u)
            return True
    return False


# ---- AI 找源（需用户弹窗确认；AI只提议URL，程序实测验证后才启用）----
_AI_RESCUE_HOOK = None          # App.__init__ 注册的GUI确认回调
_AI_RESCUE_TS = [0.0]           # 30分钟内不重复触发
_AI_RESCUE_LOCK = threading.Lock()
_AI_URL_RE = re.compile(r"https?://[^\s\"'<>)\\]]+", re.I)


def ai_rescue_kline(api_key, model=None):
    """让 DeepSeek 提议候选K线接口URL，逐个实测验证，采用第一个通过者。
    返回 (ok, 提示消息)。绝不执行模型输出的代码，只做 GET 探测。"""
    import re as _re
    if not api_key:
        return False, "未配置DeepSeek Key，无法AI找源"
    prompt = (
        "我有一个A股工具，所有已知K线数据接口都失效了。"
        f"请给我最多5个【可以直接HTTP GET】获取A股 sz002241 日K线数据的"
        "候选接口完整URL（免费、无需key，返回JSON或CSV均可），"
        "一行一个URL，不要解释，不要markdown代码块，只输出URL列表。")
    try:
        txt = deepseek_chat(api_key, prompt, model=model, timeout=60)
    except Exception as e:
        return False, f"AI调用失败: {e}"
    urls = []
    for u in _AI_URL_RE.findall(txt or ""):
        u = u.rstrip(".,;，。；")
        if u not in urls:
            urls.append(u)
    urls = urls[:5]
    if not urls:
        return False, "AI未给出可用URL"
    log.info("AI找源: 验证 %d 个候选", len(urls))
    for u in urls:
        if _probe_kline_url(u):
            _persist_kline_url(u)
            log.warning("K线源已采用AI提议: %s", u)
            return True, f"已采用AI提议源: {u}"
    return False, f"AI提议{len(urls)}个均未通过验证(已丢弃)"


def _maybe_ai_rescue():
    """全灭后触发AI找源确认弹窗（经App注册的钩子转到主线程）。
    30分钟限频；无钩子（CLI）或无Key时只在日志提示。"""
    global _AI_RESCUE_HOOK
    with _AI_RESCUE_LOCK:
        if time.time() - _AI_RESCUE_TS[0] < 1800:
            return False
        _AI_RESCUE_TS[0] = time.time()
    if _AI_RESCUE_HOOK is None:
        log.info("所有K线源失效；可运行 python stock_firstaid.py --ai 手动找源")
        return False
    try:
        _AI_RESCUE_HOOK()
    except Exception:
        log.exception("AI找源弹窗触发失败")
    return True


def get_daily(full: str, min_bars: int = 100, tail=None):
    """带缓存的日K：本地够新且无异常直接返回，否则增量爬一次并入库。
    加载缓存后校验每日涨跌幅是否超出该股允许的涨跌停范围，
    数据异常则删除本地缓存全量重新下载。
    有缓存数据的股票永远返回数据（即使过期），不抛异常。
    只有从未成功获取过的代码才会触发网络请求和负缓存。
    tail: 非空时只返回最近 tail 根（用于启动快速预览，走缓存秒开）。"""
    today = time.strftime("%Y-%m-%d")
    fresh = last_completed_td()
    with db_conn() as conn:
        rows = _db_rows(conn, full)
        nrow = conn.execute("SELECT name FROM stocks WHERE code=?",
                            (full,)).fetchone()
    name = nrow[0] if nrow else ""
    bad_cache = bool(rows) and _bars_anomalous(rows, full, name)
    # 1) 有数据、够新且涨跌幅无异常 → 直接返回
    if rows and rows[-1]["date"] >= fresh and not bad_cache:
        return rows[-tail:] if (tail and len(rows) > tail) else rows
    # 2) 有数据但过期或涨幅异常 → 拉远端（异常时清空全量替换）
    if rows:
        try:
            remote = [r for r in _fetch_remote_rows(full, count=1100)
                      if r["date"] < today and _bar_ok(r)]
            # 基期一致性校验：前复权序列每次分红整体重定基，增量合并会在
            # 缓存接缝处留下人造跳空。重叠日期收盘价偏差>0.5% → 全量替换。
            rebase = False
            if rows and remote:
                newmap = {r["date"]: r["close"] for r in remote}
                checked = 0
                for r in rows[-200:]:
                    c2 = newmap.get(r["date"])
                    if c2 and r["close"]:
                        checked += 1
                        if abs(c2 / r["close"] - 1) > 0.005:
                            rebase = True
                            break
                rebase = rebase and checked >= 5
            with db_conn(commit=True) as conn2:
                # 只有新数据足够长才允许全量替换，防止短源摧毁深历史
                if (bad_cache or rebase) and len(remote) >= min(400, len(rows) // 2):
                    conn2.execute("DELETE FROM daily_bars WHERE code=?",
                                  (full,))
                if remote:
                    conn2.executemany(
                        "INSERT OR REPLACE INTO daily_bars"
                        "(code,date,open,high,low,close,vol) "
                        "VALUES(?,?,?,?,?,?,?)",
                        [(full, r["date"], r["open"], r["high"], r["low"],
                          r["close"], r["vol"]) for r in remote])
            if rebase:
                log.info("%s 前复权基期漂移，已全量重定基", full)
            # 重新读取合并后的数据
            with db_conn() as conn3:
                rows = _db_rows(conn3, full)
        except Exception:
            log.warning("get_daily 增量拉取失败 %s，回退本地缓存",
                        full, exc_info=True)  # 网络失败就用旧缓存，不报错
        return rows[-tail:] if (tail and len(rows) > tail) else rows
    # 3) 无数据 → 检查负缓存
    with db_conn() as conn:
        frow = conn.execute("SELECT ts, reason FROM failed WHERE code=?",
                            (full,)).fetchone()
        if frow and time.time() - frow[0] < FAIL_TTL:
            reason = (frow[1] or "网络失败") if len(frow) > 1 else "网络失败"
            raise RuntimeError(f"{full} 近期拉取失败(负缓存中) [{reason}]")

    # 4) 从未获取过 → 网络请求
    try:
        remote = _fetch_remote_rows(full, count=1100)
    except Exception as e:
        # 未上市/无数据识别：行情快照也拿不到有效价 → 静默记为未上市
        listed = True
        try:
            qq = fetch_quote(full)
            listed = bool(qq and qq.get("price") and qq["price"] > 0)
        except Exception:
            listed = False
        if not listed:
            log.info("%s 未上市或无行情数据，跳过", full)
            with db_conn(commit=True) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO failed(code,ts,reason) "
                    "VALUES(?,?,?)",
                    (full, time.time(), "未上市或无数据"))
            raise RuntimeError(f"{full} 未上市或无行情数据")
        log.warning("get_daily 首次拉取失败 %s: %s", full, e)
        # 失败退避：已有负缓存记录的代码（反复失败），时长翻倍，
        # 上限24h，避免样本池里拉不到的代码每天反复撞限流
        new_ttl = FAIL_TTL
        try:
            with db_conn() as conn:
                row = conn.execute("SELECT ts FROM failed WHERE code=?",
                                   (full,)).fetchone()
            if row:
                prev_ttl = max(FAIL_TTL - (time.time() - row[0]), FAIL_TTL)
                new_ttl = min(prev_ttl * 2, 86400)
        except Exception:
            log.exception("负缓存退避计算失败(忽略)")
        with db_conn(commit=True) as conn:
            # 存 ts 使 ts+FAIL_TTL = now+new_ttl，无需改表结构
            conn.execute(
                "INSERT OR REPLACE INTO failed(code,ts,reason) VALUES(?,?,?)",
                (full, time.time() - (FAIL_TTL - new_ttl), str(e)[:120]))
        raise
    with db_conn(commit=True) as conn:
        conn.execute("DELETE FROM failed WHERE code=?", (full,))
        conn.executemany(
            "INSERT OR REPLACE INTO daily_bars"
            "(code,date,open,high,low,close,vol) VALUES(?,?,?,?,?,?,?)",
            [(full, r["date"], r["open"], r["high"], r["low"],
              r["close"], r["vol"]) for r in remote
             if r["date"] < today and _bar_ok(r)])
        rows = [r for r in _db_rows(conn, full)]
    return rows[-tail:] if (tail and len(rows) > tail) else rows


def prefetch(codes, workers=6, progress=None):
    """并发预取一批代码的日K入库（首次回填用）。"""
    # 先排除已知失败的代码（10分钟内不再重试），记录原因
    with db_conn() as conn:
        now = time.time()
        failed = {r[0] for r in
                  conn.execute("SELECT code FROM failed WHERE ts > ?",
                               (now - FAIL_TTL,)).fetchall()}
        fail_reasons = {}
        try:
            for r in conn.execute(
                    "SELECT code, reason FROM failed WHERE ts > ?",
                    (now - FAIL_TTL,)).fetchall():
                fail_reasons[r[0]] = r[1] or "网络失败"
        except Exception:
            log.exception("failed 表读取失败(忽略)")
    codes = [c for c in codes if c not in failed]
    if failed and progress:
        # 显示前3个失败原因，避免刷屏
        sample = [f"{c}({fail_reasons.get(c, '网络失败')})"
                  for c in list(failed)[:3]]
        progress(f"跳过{len(failed)}个近期失败代码: " + "; ".join(sample))
    done = [0]
    total = len(codes)
    if total == 0:
        return
    # 进度上报粒度：大批量约每5%报一次，小批量每只都报
    step = max(1, min(10, total // 20 or 1))

    def one(c):
        try:
            get_daily(c)
        except Exception:
            log.debug("prefetch 跳过 %s", c, exc_info=True)
        done[0] += 1
        if progress and (done[0] % step == 0 or done[0] == total):
            progress(f"缓存回填 {done[0]}/{total} "
                     f"({done[0] * 100 // total}%)")

    ex = _SHARED_EX                # 全局共享线程池，不再每次新建
    list(ex.map(one, codes))


# ================= 全市场深历史回填（集成版，原 backfill_full.py） =================
# 腾讯单次上限800根 → 两页翻取1600根(≥1000目标)；三域名轮换；
# 免费源有IP配额(东财批量~500只断连、腾讯每窗口~200请求501)：
# 501 全局暂停自愈 + 断点续传，数晚跑满全市场。CLI: --backfill

_BF_TX_HOSTS = [
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
]
_BF_TX_I = [0]
_BF_PAUSE = [0.0, 0]            # [暂停截止时间, 连续限流次数]


def _bf_tx_fetch(full, end, count):
    """腾讯K线单页：三域名轮换，全部501才抛（触发全局暂停）。"""
    param = (f"?param={full},day,,{end},{count},qfq" if end
             else f"?param={full},day,,,{count},qfq")
    last = None
    for k in range(len(_BF_TX_HOSTS)):
        u = _BF_TX_HOSTS[(_BF_TX_I[0] + k) % len(_BF_TX_HOSTS)]
        try:
            txt = _http_get(u + param, decode="utf-8", retries=1, timeout=8)
            _BF_TX_I[0] = (_BF_TX_I[0] + k + 1) % len(_BF_TX_HOSTS)
            d = (json.loads(txt).get("data") or {}).get(full) or {}
            bars = d.get("qfqday") or d.get("day") or []
            out = []
            for b in bars:
                try:
                    if float(b[2]) <= 0:
                        continue
                    out.append({"date": b[0], "open": float(b[1]),
                                "close": float(b[2]), "high": float(b[3]),
                                "low": float(b[4]), "vol": float(b[5])})
                except (ValueError, IndexError):
                    continue
            return out
        except Exception as e:
            last = e
    raise last


def _bf_fetch_one(full, page=800):
    """单只：腾讯翻页为主源，东财一次性全量兜底。"""
    rows1 = _bf_tx_fetch(full, "", page)
    if not rows1:
        raise RuntimeError("腾讯空数据")
    if len(rows1) >= page - 10:                 # 触顶 → 翻页补历史
        import datetime
        d0 = datetime.date.fromisoformat(rows1[0]["date"])
        end = (d0 - datetime.timedelta(days=1)).isoformat()
        try:
            rows2 = _bf_tx_fetch(full, end, page)
            have = {r["date"] for r in rows1}
            rows1 = [r for r in rows2 if r["date"] not in have] + rows1
        except Exception:
            pass                                # 第二页失败就只装第一页
    if len(rows1) >= 300:
        return rows1
    try:
        rows = _fetch_eastmoney(full, count=1100)
        if len(rows) >= 300:
            return rows
    except Exception:
        pass
    raise RuntimeError("有效数据不足300根")


def backfill_full_market(progress=None, force=False, limit=0,
                         workers=6, throttle=0.45, min_bars=950):
    """全市场日K批量回填（≥min_bars根，断点续传）。返回统计dict。"""
    global _MIN_INTERVAL
    old_iv = _MIN_INTERVAL
    _MIN_INTERVAL = throttle
    try:
        today = time.strftime("%Y-%m-%d")
        import datetime
        fresh = (datetime.date.today()
                 - datetime.timedelta(days=6)).isoformat()
        with db_conn() as conn:
            codes = [r[0] for r in conn.execute(
                "SELECT code FROM stocks WHERE code NOT LIKE 'bj%' "
                "ORDER BY code").fetchall()]
            if not codes:
                refresh_all_codes(progress=progress)
                with db_conn() as conn:
                    codes = [r[0] for r in conn.execute(
                        "SELECT code FROM stocks WHERE code NOT LIKE 'bj%' "
                        "ORDER BY code").fetchall()]
        if limit:
            codes = codes[:limit]
        have = {}
        if not force:
            with db_conn() as conn:
                for c, n, d in conn.execute(
                        "SELECT code, COUNT(*), MAX(date) FROM daily_bars "
                        "GROUP BY code"):
                    have[c] = (n, d or "")
        todo = [c for c in codes
                if not (have.get(c, (0, ""))[0] >= min_bars
                        and have.get(c, (0, ""))[1] >= fresh)]
        stat = {"total": len(codes), "todo": len(todo), "ok": 0,
                "fail": 0, "codes": len(have)}
        if not todo:
            if progress:
                progress(f"回填：全市场已达标，无需继续")
            return stat
        if progress:
            progress(f"回填：待处理 {len(todo)}/{len(codes)} 只")
        t0 = time.time()
        done_n = [0]

        def work(c):
            if time.time() < _BF_PAUSE[0]:
                time.sleep(_BF_PAUSE[0] - time.time())
            try:
                rows = [r for r in _bf_fetch_one(c)
                        if r["date"] < today and _bar_ok(r)]
                if not rows:
                    raise RuntimeError("过滤后无有效数据")
                with db_conn(commit=True) as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO daily_bars"
                        "(code,date,open,high,low,close,vol) "
                        "VALUES(?,?,?,?,?,?,?)",
                        [(c, r["date"], r["open"], r["high"], r["low"],
                          r["close"], r["vol"]) for r in rows])
                _BF_PAUSE[1] = 0
                stat["ok"] += 1
            except Exception as e:
                msg = str(e)
                if "501" in msg or "429" in msg or "503" in msg:
                    _BF_PAUSE[1] = min(_BF_PAUSE[1] + 1, 4)
                    wait = 120 if _BF_PAUSE[1] < 3 else 300
                    _BF_PAUSE[0] = max(_BF_PAUSE[0], time.time() + wait)
                stat["fail"] += 1
            done_n[0] += 1
            if progress and (done_n[0] % 20 == 0
                             or done_n[0] == len(todo)):
                el = time.time() - t0
                eta = el / done_n[0] * (len(todo) - done_n[0])
                progress(f"全市场回填 {done_n[0]}/{len(todo)} "
                         f"({done_n[0] * 100 // len(todo)}%) "
                         f"成功{stat['ok']} 失败{stat['fail']} "
                         f"ETA {eta / 60:.0f}分")

        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=workers) as ex2:
            list(ex2.map(work, todo))
        if progress:
            progress(f"回填完成：成功{stat['ok']} 失败{stat['fail']}"
                     f"（失败的下次运行自动续传）")
        return stat
    finally:
        _MIN_INTERVAL = old_iv


# ================= 数据清洗（集成版；独立版见 data_clean.py） =================

def _clean_bar_valid(r):
    """单根bar结构校验（只查硬错误，不查影线比例——低价股分值效应会误杀）。"""
    o, h, l, c = r[1], r[2], r[3], r[4]
    if None in (o, h, l, c) or min(x for x in (o, h, l, c)) <= 0:
        return False
    if h < l or h < max(o, c) or l > min(o, c):
        return False
    return True


def clean_daily_db(fix=True, progress=None):
    """扫描并（可选）修复全库日K：结构异常/涨跌幅越界(除权残留)/
    停牌缺口/退市/价格粘性。返回统计dict。与 data_clean.py 同规则。"""
    import datetime as _dt
    _today = _dt.date.today()
    stats = {"codes": 0, "bad_bars": 0, "refetch": 0, "suspend": 0,
             "delisted": 0, "stale": 0, "deleted": 0, "refetched": 0}
    issues = {}
    with db_conn(commit=bool(fix)) as conn:
        names = {r[0]: (r[1] or "") for r in
                 conn.execute("SELECT code, name FROM stocks").fetchall()}
        if fix:
            conn.execute("CREATE TABLE IF NOT EXISTS delisted("
                         "code TEXT PRIMARY KEY, last_date TEXT, ts REAL)")
        rows = conn.execute(
            "SELECT code,date,open,high,low,close,vol FROM daily_bars "
            "ORDER BY code,date").fetchall()
        by = {}
        for c, d, o, h, l, cl, v in rows:
            by.setdefault(c, []).append((d, o, h, l, cl, v or 0.0))
        stats["codes"] = len(by)
        for ci, (c, bars) in enumerate(by.items()):
            if progress and ci % 300 == 0:
                progress(f"清洗扫描 {ci}/{len(by)}")
            n = len(bars)
            bad = [b for b in bars if not _clean_bar_valid(b)]
            if bad:
                issues.setdefault(c, []).append("bad")
                stats["bad_bars"] += len(bad)
            flags = []
            name = names.get(c, "")
            for prev, cur in zip(bars, bars[1:]):
                pc, cl = prev[4], cur[4]
                lim = _limit_pct(c, name, cur[0])
                if not pc or not cl or lim is None:
                    flags.append(False)
                    continue
                flags.append(abs(cl / pc - 1) * 100 > lim + 3.0)
            viol = any(flags) if not _is_etf(c) else any(
                a and b for a, b in zip(flags, flags[1:]))
            if viol:
                issues.setdefault(c, []).append("refetch")
                stats["refetch"] += 1
            gaps = 0
            for a, b in zip(bars, bars[1:]):
                try:
                    da = _dt.datetime.strptime(a[0], "%Y-%m-%d").date()
                    db2 = _dt.datetime.strptime(b[0], "%Y-%m-%d").date()
                    if (db2 - da).days > 20:
                        gaps += 1
                except ValueError:
                    continue
            if gaps:
                stats["suspend"] += 1
            d1 = bars[-1][0]
            try:
                age = (_today - _dt.datetime.strptime(
                    d1, "%Y-%m-%d").date()).days
            except ValueError:
                age = 0
            if age > 180:
                issues.setdefault(c, []).append(f"delisted:{d1}")
                stats["delisted"] += 1
            run = 1
            for a, b in zip(bars, bars[1:]):
                run = run + 1 if a[4] == b[4] and a[4] else 1
                if run >= 20:
                    issues.setdefault(c, []).append("stale")
                    stats["stale"] += 1
                    break
        if fix:
            for c, kinds in issues.items():
                kinds_set = set(kinds)
                if "bad" in kinds_set:
                    bars = conn.execute(
                        "SELECT date,open,high,low,close FROM daily_bars "
                        "WHERE code=? ORDER BY date", (c,)).fetchall()
                    dels = [(c, b[0]) for b in bars
                            if not _clean_bar_valid(b)]
                    if dels:
                        conn.executemany(
                            "DELETE FROM daily_bars WHERE code=? AND date=?",
                            dels)
                        stats["deleted"] += len(dels)
                if ("refetch" in kinds_set or "stale" in kinds_set) \
                        and not c.startswith("bj"):
                    try:
                        fresh = _bf_fetch_one(c)
                        fd = [r for r in fresh
                              if r["date"] < time.strftime("%Y-%m-%d")]
                        if len(fd) >= 200:
                            conn.execute(
                                "DELETE FROM daily_bars WHERE code=?", (c,))
                            conn.executemany(
                                "INSERT OR REPLACE INTO daily_bars"
                                "(code,date,open,high,low,close,vol) "
                                "VALUES(?,?,?,?,?,?,?)",
                                [(c, r["date"], r["open"], r["high"],
                                  r["low"], r["close"], r["vol"])
                                 for r in fd])
                            stats["refetched"] += 1
                    except Exception:
                        pass            # 源不可用时保留原数据，下次再修
                dl = next((k.split(":", 1)[1] for k in kinds
                           if k.startswith("delisted:")), None)
                if dl:
                    conn.execute(
                        "INSERT OR REPLACE INTO delisted VALUES(?,?,?)",
                        (c, dl, time.time()))
    return stats


# ================= 全市场代码表 / 分层 =================

def stocks_age() -> float:
    with db_conn() as conn:
        ts = _get_meta(conn, "stocks_updated")
        if not ts:
            return 1e18
        try:
            return time.time() - float(ts)
        except ValueError:
            return 1e18


def refresh_all_codes(progress=None):
    """拉取全A代码表（代码/名称/总市值/东财行业），按市值三分位分层。"""
    with REFRESH_LOCK:
        if stocks_age() < STOCKS_TTL:
            if progress:
                progress("代码表仍新鲜，跳过")
            return False
        # 不含北交所(m:0+t:81)，腾讯K线不支持且用户不需要
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
        hosts = ("https://push2delay.eastmoney.com",
                 "https://push2.eastmoney.com",
                 "http://push2.eastmoney.com")
        items = []
        pn = 1
        while pn <= 90:
            u = (f"{hosts[(pn - 1) % len(hosts)]}/api/qt/clist/get"
                 f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invariant=0"
                 f"&fields=f12,f14,f20,f100&fs={fs}&ut={UT}")
            got = False
            for host in hosts:
                uu = u.replace(u.split("/api/")[0], host)
                try:
                    data = json.loads(_http_get(
                        uu, retries=3, timeout=20,
                        headers={"Referer":
                                 "https://quote.eastmoney.com/"})
                    ).get("data") or {}
                    got = True
                    break
                except Exception:
                    time.sleep(1.5)
            if not got:
                if len(items) >= 500 or pn > 1:
                    break           # 已拿到足够数据，容忍个别页失败
                raise RuntimeError("代码表首页拉取失败")
            diff = data.get("diff") or {}
            batch = list(diff.values()) if isinstance(diff, dict) else diff
            if not batch:
                break
            for it in batch:
                code, name = it.get("f12"), it.get("f14")
                cap = it.get("f20")
                ind = it.get("f100")
                if not code or len(code) != 6 or not isinstance(cap, (int, float)):
                    continue
                if code.startswith(("4", "8", "92")):
                    full = "bj" + code
                elif code[0] in "69" or code[:2] in ("51", "56", "58"):
                    full = "sh" + code
                else:
                    full = "sz" + code
                items.append((full, name or "", ind if isinstance(ind, str) else None,
                              float(cap)))
            if progress:
                progress(f"代码表 {len(items)} 只 (第{pn}页)")
            pn += 1
            time.sleep(0.6)
        if len(items) < 500:
            raise RuntimeError(f"代码表异常: 仅{len(items)}只")

        # 市值三分位分层
        caps = sorted(it[3] for it in items)
        q1, q2 = caps[len(caps) // 3], caps[2 * len(caps) // 3]

        def tier_of(cap):
            if cap >= q2:
                return TIERS[0]
            if cap >= q1:
                return TIERS[1]
            return TIERS[2]

        today = time.strftime("%Y-%m-%d")
        with db_conn(commit=True) as conn:
            conn.execute("DELETE FROM stocks")
            conn.executemany(
                "INSERT OR REPLACE INTO stocks"
                "(code,name,industry,mktcap,tier,updated) "
                "VALUES(?,?,?,?,?,?)",
                [(c, n, i, cap, tier_of(cap), today)
                 for c, n, i, cap in items])
            _set_meta(conn, "stocks_updated", repr(time.time()))
        if progress:
            progress(f"代码表完成: {len(items)}只, 分界 "
                     f"{q1/1e8:.0f}/{q2/1e8:.0f}亿")
        return True


def ensure_codes(progress=None) -> None:
    """代码表过期则自动刷新。"""
    if stocks_age() >= STOCKS_TTL:
        try:
            refresh_all_codes(progress)
        except Exception:
            log.exception("ensure_codes 刷新失败")
            if stocks_age() >= STOCKS_TTL * 4:
                raise           # 完全没有可用代码表时才向上抛


def get_stock_info(full: str):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT name,industry,mktcap,tier FROM stocks WHERE code=?",
            (full,)).fetchone()
        return {"name": row[0], "industry": row[1],
                "mktcap": row[2], "tier": row[3]} if row else None


def industry_peers(full: str, limit: int = L2_DEFAULT_N):
    """L2池：同行业、市值最接近目标股的 N 只（不含自身）。"""
    info = get_stock_info(full)
    if not info or not info.get("industry"):
        return [], None
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code,name,mktcap FROM stocks "
            "WHERE industry=? AND code!=? AND code NOT LIKE 'bj%'",
            (info["industry"], full)).fetchall()
    if not rows:
        return [], info["industry"]
    my_cap = info.get("mktcap") or 0.0
    lg = math.log(max(my_cap, 1e8))

    def near(r):
        return abs(math.log(max(r[2] or 1e8, 1e8)) - lg)

    rows.sort(key=near)
    return [r[0] for r in rows[:limit]], info["industry"]


_TIER_POOL_CACHE = {}       # (tier, 日期) -> L3样本代码列表（同日共享，避免重复拉取）
_TIER_POOL_TS = {}


def tier_sample(full: str, n: int = 0, exclude_industries=()):
    """L3池：同市值层样本，**缓存优先、不固定数量**。

    - 命中本地缓存的同层代码排前面（分析立刻可用）；
    - 未缓存的排后面，由后台渐进回填，拉到多少用多少；
    - 不再固定截取 N 只（n<=0 时返回全层）。
    缓存读写全程持有 _STATE_LOCK，多线程下不会互相污染。"""
    info = get_stock_info(full)
    if not info or not info.get("tier"):
        return [], None
    tier = info["tier"]
    today = time.strftime("%Y%m%d")
    key = (tier, today)
    now = time.time()
    pool = None
    with _STATE_LOCK:
        cached = _TIER_POOL_CACHE.get(key)
        if cached is not None and now - _TIER_POOL_TS.get(key, 0) <= 86400:
            pool = cached
        else:
            # 内存缓存跨天失效后重新计算（锁内查库+回写，保证原子性）
            with db_conn() as conn:
                q = ("SELECT code,industry FROM stocks "
                     "WHERE tier=? AND code NOT LIKE 'bj%'")
                rows = conn.execute(q, (tier,)).fetchall()
                have = {r[0] for r in conn.execute(
                    "SELECT DISTINCT code FROM daily_bars").fetchall()}
            # 缓存优先：已回填过日K的排前面，其余排后面
            rnd = random.Random(today + tier)
            head = [r for r in rows if r[0] in have]
            tail = [r for r in rows if r[0] not in have]
            rnd.shuffle(head)
            rnd.shuffle(tail)
            pool = head + tail
            _TIER_POOL_CACHE[key] = pool
            _TIER_POOL_TS[key] = now
    # 排除自身与 L2 已覆盖的行业（industry 名字，非代码）
    exclude = set(exclude_industries or ())
    if exclude:
        pool = [r for r in pool if (r[1] or "") not in exclude]
    out = [c for c, _ in pool if c != full]
    if n and n > 0:
        out = out[:n]
    return out, tier


# 题材行业（交易回测证实：这些行业L2无增益，只用L1）+ 传统行业ETF池
THEME_KW = ("软件", "计算机", "半导体", "元件", "电子", "通信", "光电",
            "IT", "互联网", "游戏", "传媒", "数字", "消费电子", "光学",
            "医药", "中药", "生物", "医疗", "制药", "疫苗")
ETF_POOL = ("sh510300", "sh510500", "sz159915", "sh588000", "sh510050",
            "sh512100", "sh510880", "sz159922", "sh512880", "sh512690")


def _is_theme_industry(industry):
    """科技/医药等题材行业：L2跨股池无增益，只参考L1自身历史。"""
    return any(k in (industry or "") for k in THEME_KW)


def pool_codes(full, l2_n=L2_DEFAULT_N, l3_n=0):
    """一次拿到两级样本池。l3_n<=0 表示 L3 不限量（缓存优先排序）。

    - 题材行业/ETF 目标：返回空池（只用 L1）
    - 传统行业：L2 = 精确同行业 + 宽基/行业ETF池（交易回测胜率+2.2pp）"""
    if _is_etf(full):
        return {"l2": [], "l3": [], "industry": None, "tier": None}
    peers, industry = industry_peers(full, l2_n)
    if _is_theme_industry(industry):
        return {"l2": [], "l3": [], "industry": industry, "tier": None}
    l3, tier = tier_sample(full, l3_n, exclude_industries=(industry,))
    seen = {full}
    l3 = [c for c in l3 if c not in seen and not seen.add(c)]
    # 传统行业：L2 并入 ETF 池（市场beta形态样本）
    if peers:
        eseen = set(peers) | {full}
        peers = peers + [e for e in ETF_POOL if e not in eseen]
    return {"l2": peers, "l3": l3,
            "industry": industry, "tier": tier}


# 注意：KLINE_URL / INI_PATH 等常量统一定义在文件头部内嵌缓存层，此处不再重复
QT_URL = "https://qt.gtimg.cn/q="
AUTHOR = "獨白"
AUTHOR_EMAIL = "kingrux106@gmail.com"
AUTHOR_QQ = "2180287399"
DISCLAIMER = ("免责声明：本程序所有输出仅为历史数据的技术统计与研究用途，"
              "不构成任何投资建议或收益承诺。股市有风险，据此操作盈亏自负。")
INDEX_CODES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("bj899050", "北证50"),
]

UP, DOWN, PRED_C = "#ff5252", "#26c281", "#4da3ff"
TPRED_C = "#ffffff"   # 暗色主题下白色虚线；亮色主题自动切换为黑色
MA_COLORS = {5: "#ffa94d", 10: "#74c0fc", 20: "#e599f7", 30: "#69db7c", 60: "#d5a021"}
BG = "#14181e"
GRID_C = "#232b34"
GUIDE_C = "#39434e"
AXIS_TXT = "#8fa0ad"
TITLE_TXT = "#aebccb"
CROSS_C = "#9fb3c8"
DARK_BG = "#101418"
PANEL_BG = "#171c22"
FIELD_BG = "#1c232b"
FG_MAIN = "#d7dee6"
BTN_BG = "#222a33"
BTN_FG = "#d7dee6"
BTN_HOVER = "#2b3540"
BTN_BORDER = "#333e4a"

# ---- 可切换主题 ----
THEMES = {
    "dark": dict(
        UP="#ff5252", DOWN="#26c281", PRED_C="#4da3ff", TPRED_C="#ffffff",
        BG="#14181e", GRID_C="#232b34", GUIDE_C="#39434e",
        AXIS_TXT="#8fa0ad", TITLE_TXT="#aebccb", CROSS_C="#9fb3c8",
        DARK_BG="#101418", PANEL_BG="#171c22", FIELD_BG="#1c232b",
        FG_MAIN="#d7dee6",
        BTN_BG="#222a33", BTN_FG="#d7dee6", BTN_HOVER="#2b3540",
        BTN_BORDER="#333e4a",
    ),
    "light": dict(
        UP="#e03131", DOWN="#0ca678", PRED_C="#1971c2", TPRED_C="#111111",
        BG="#ffffff", GRID_C="#ececec", GUIDE_C="#f1f3f5",
        AXIS_TXT="#777777", TITLE_TXT="#444444", CROSS_C="#999999",
        DARK_BG="#f2f4f7", PANEL_BG="#ffffff", FIELD_BG="#ffffff",
        FG_MAIN="#1f2933",
        BTN_BG="#ffffff", BTN_FG="#1f2933", BTN_HOVER="#eef1f4",
        BTN_BORDER="#bbbbbb",
    ),
}


def apply_theme(theme, updown):
    """按设置重写模块级颜色常量；绘图函数读取全局值。"""
    t = dict(THEMES.get(theme, THEMES["dark"]))
    if updown == "green_up":
        t["UP"], t["DOWN"] = t["DOWN"], t["UP"]
    for k, v in t.items():
        globals()[k] = v


# ================= 数据获取 =================

def http_get(url: str, retries: int = 3) -> str:
    """腾讯行情文本接口（GBK）。统一走带熔断上报的 _http_get。"""
    return _http_get(url, retries=retries, timeout=15, decode="gbk",
                     src_name="腾讯行情")


def normalize_code(code):
    code = code.strip().lower()
    # 触摸屏/输入法常见：全角字符折叠为半角
    code = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
        for ch in code)
    # 只保留ASCII字母数字：零宽/NBSP/控制字符/中文标点一次性清除
    code = "".join(ch for ch in code if ch.isascii() and ch.isalnum())
    # 兼容 002241sz / sz.002241 / sh-600519 / 600519.SH 等变体
    if len(code) == 8 and code[-2:] in ("sh", "sz", "bj"):
        code = code[-2:] + code[:6]    # 002241sz → sz002241
    # 触摸屏 O/I/L 与 0/1 误触：'O00725'→'000725'（仅当整体为纯数字时替换）
    if any(ch in code for ch in "oil"):
        code2 = (code.replace("o", "0").replace("i", "1")
                     .replace("l", "1"))
        if code2.isdigit() or code2[:2] in ("sh", "sz", "bj"):
            code = code2
    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            return p + code[2:]
    d = "".join(ch for ch in code if ch.isdigit())
    if len(d) != 6:
        # !r 显示原始输入（含隐藏字符），便于远程排查
        raise ValueError(
            f"代码格式不对: {code!r}\n"
            "支持：002241 / 600519 / sh600519 / sz002241 / 002241.sz")
    if d[0] in "69" or d[:2] in ("51", "56", "58"):      # 沪股/沪ETF
        return "sh" + d
    if d[0] in "03" or d[:2] in ("15", "16", "18"):      # 深股/深ETF/LOF
        return "sz" + d
    if d[0] in "48":
        return "bj" + d
    raise ValueError(
        f"不支持的代码: {code!r}\n"
        "支持：沪深A股/ETF（0/3/6/5开头）与北交所（4/8开头）")


def vol_ratio_at(vols, i):
    """截至第 i 日（含）的量能状态：近5日均量 / 前15日均量。"""
    if i < 19:
        return None
    r5 = sum(vols[i - 4:i + 1]) / 5
    p15 = sum(vols[i - 19:i - 4]) / 15
    return (r5 / p15) if p15 > 0 else None


def vol_regime(vr):
    if vr is None:
        return "?"
    if vr > 1.2:
        return "放量"
    if vr < 0.8:
        return "缩量"
    return "平量"


def fmt_vol_cn(v):
    if v >= 1e8:
        return f"{v/1e8:.2f}亿"
    if v >= 1e4:
        return f"{v/1e4:.1f}万"
    return f"{v:.0f}"


_SECTOR_CACHE = {}          # code -> (timestamp, 结果三元组)
_SECTOR_CACHE_TTL = 1800    # 30 分钟
_BK_LIST_CACHE = {}         # 板块名 -> BK代码（全局，行业名单变化慢）
_BK_LIST_TS = 0.0

_IDX_QUOTE_CACHE = {}       # 上证指数行情快照，跨多股共享（避免重复拉取）
_IDX_QUOTE_TTL = 60         # 60 秒

_TOP_SECTORS_CACHE = None
_TOP_SECTORS_TS = 0.0
_TOP_SECTORS_LAST = ([], [])    # 最近一次成功结果（网络全挂时回退用）
_TOP_SECTORS_WARN_TS = 0.0      # 失败警告降噪：10分钟内只警告一次


def fetch_top_sectors():
    """获取今日行业板块涨跌幅排行（Top3涨/Top3跌）。
    返回 [(name, pct), ...] 的两个列表，带10分钟缓存。
    缓存读写持有 _STATE_LOCK；请求统一走熔断 _http_get。
    网络全挂时回退最近一次成功结果（可能滞后，但好于空白）。"""
    global _TOP_SECTORS_CACHE, _TOP_SECTORS_TS, _TOP_SECTORS_WARN_TS
    now = time.time()
    with _STATE_LOCK:
        if _TOP_SECTORS_CACHE and now - _TOP_SECTORS_TS < 600:
            return _TOP_SECTORS_CACHE
    UT = "fa5fd1943c7b386f172d6893dbfba10b"
    hdr = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    # HTTPS/HTTP × 主域/延迟域：连接被重置时逐个轮换
    hosts = ("https://push2delay.eastmoney.com",
             "https://push2.eastmoney.com",
             "http://push2delay.eastmoney.com",
             "http://push2.eastmoney.com")
    items = []
    vals = []
    fail_cnt = 0
    for pn in (1, 2, 3):
        u = (f"/api/qt/clist/get"
             f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invariant=0"
             f"&fields=f12,f14,f3&fs=m:90+t:2&ut={UT}")
        done = False
        for host in hosts:
            try:
                data = json.loads(_http_get(
                    host + u, retries=3, timeout=8, headers=dict(hdr),
                    src_name="东财板块"))
                diff = data.get("data", {}).get("diff") or {}
                vals = list(diff.values()) if isinstance(diff, dict) else diff
                for it in vals:
                    name = it.get("f14", "")
                    pct = it.get("f3")
                    if name and pct is not None:
                        items.append((name, float(pct)))
                done = True
                break
            except Exception as e:
                fail_cnt += 1
                if now - _TOP_SECTORS_WARN_TS > 600:
                    log.warning("板块排行第%d页 %s 失败: %s", pn, host, e)
                else:
                    log.debug("板块排行第%d页 %s 失败: %s", pn, host, e)
                continue
        if not done:
            break
        if len(vals) < 100:
            break
    if now - _TOP_SECTORS_WARN_TS > 600:
        _TOP_SECTORS_WARN_TS = now
    if items:
        items.sort(key=lambda x: x[1], reverse=True)
        result = (items[:3], items[-3:][::-1])
        with _STATE_LOCK:
            _TOP_SECTORS_CACHE = result
            _TOP_SECTORS_TS = now
            _TOP_SECTORS_LAST = result
        return result
    # 全部失败：回退最近一次成功结果，避免界面板块栏空白
    log.info("板块排行本轮全部失败(%d次请求)，回退上次结果", fail_cnt)
    return _TOP_SECTORS_LAST


def fetch_sector_context(full):
    """个股所属行业板块指数上下文。

    返回 (板块名, {date: 当日涨跌%}, 板块今日涨跌%)；失败返回 (None, {}, None)。
    带 30 分钟缓存：板块日内变化不大，命中缓存零耗时。
    """
    now = time.time()
    with _STATE_LOCK:
        hit = _SECTOR_CACHE.get(full)
        if hit and now - hit[0] < _SECTOR_CACHE_TTL:
            return hit[1]
    UT = "fa5fd1943c7b386f172d6893dbfba10b"
    hdr = {"User-Agent": "Mozilla/5.0",
           "Referer": "https://quote.eastmoney.com/"}

    def get(u, timeout=4):
        # 东财接口对部分网络直连被重置：按 URL 类型做主机轮询容灾；
        # 有些主机会返回 200 的反爬 HTML 页，需校验内容是 JSON
        if "push2his" in u:
            hosts = ("push2delay.eastmoney.com", "push2his.eastmoney.com",
                     "92.push2his.eastmoney.com")
            base = "push2his.eastmoney.com"
        else:
            hosts = ("push2delay.eastmoney.com", "push2.eastmoney.com",
                     "push2his.eastmoney.com")
            base = "push2.eastmoney.com"
        last = None
        for host in hosts:
            uu = u.replace(base, host)
            try:
                txt = _http_get(uu, retries=1, timeout=timeout,
                                headers=dict(hdr), src_name="东财板块")
                txt = txt.lstrip("\ufeff")
                if txt.lstrip().startswith("{"):
                    return txt
                last = RuntimeError("反爬HTML页")
                log.debug("板块接口 %s 返回非JSON", host)
            except Exception as e:
                last = e
                log.debug("板块接口 %s 失败: %s", host, e)
        raise last

    try:
        global _BK_LIST_CACHE, _BK_LIST_TS
        code = full[2:]
        mkt = "1" if full.startswith("sh") else "0"
        # 1) 个股行业名
        u = (f"https://push2.eastmoney.com/api/qt/stock/get"
             f"?secid={mkt}.{code}&fields=f127&ut={UT}")
        ind_name = json.loads(get(u))["data"].get("f127")
        if not ind_name:
            return None, {}, None
        # 2) 行业板块列表（分页，全局缓存 30 分钟），按名称匹配 BK 代码
        bk_code = None
        with _STATE_LOCK:
            if _BK_LIST_CACHE and now - _BK_LIST_TS < _SECTOR_CACHE_TTL:
                bk_code = _BK_LIST_CACHE.get(ind_name)
            else:
                new_bk = {}
                for pn in (1, 2, 3):
                    u = (f"https://push2.eastmoney.com/api/qt/clist/get"
                         f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invariant=0"
                         f"&fields=f12,f14&fs=m:90+t:2&ut={UT}")
                    diff = json.loads(get(u)).get("data", {}).get("diff") or {}
                    items = list(diff.values()) if isinstance(diff, dict) else diff
                    for it in items:
                        new_bk[it.get("f14")] = it.get("f12")
                    if len(items) < 100:
                        break
                _BK_LIST_CACHE = new_bk
                _BK_LIST_TS = now
                bk_code = _BK_LIST_CACHE.get(ind_name)
        if not bk_code:
            return ind_name, {}, None
        # 3) 板块日K（收盘价）—— 必须带 ut，否则部分主机返回反爬页
        u = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
             f"?secid=90.{bk_code}&fields1=f1,f2,f3&fields2=f51,f53"
             f"&klt=101&fqt=0&beg=20240101&end=20500101&ut={UT}")
        kl = json.loads(get(u, timeout=8))["data"]["klines"]
        bars = [(s.split(",")[0], float(s.split(",")[1])) for s in kl]
        if not bars:
            # 部分板块指数日K在部分主机返回空：降级取今日板块涨跌幅
            # （历史留空，评分按缺数据处理）
            u3 = (f"https://push2.eastmoney.com/api/qt/clist/get"
                  f"?pn=1&pz=100&po=1&np=1&fltt=2&invariant=0"
                  f"&fields=f12,f14,f3&fs=m:90+t:2&ut={UT}")
            diff = (json.loads(get(u3, timeout=8)).get("data") or {}).get(
                "diff") or {}
            items = list(diff.values()) if isinstance(diff, dict) else diff
            today_chg = None
            for it in items:
                if it.get("f12") == bk_code and it.get("f3") is not None:
                    today_chg = float(it["f3"])
                    break
            log.info("板块 %s(%s) 日K为空，降级仅用今日涨跌 %s",
                     ind_name, bk_code, today_chg)
            with _STATE_LOCK:
                _SECTOR_CACHE[full] = (time.time(),
                                       (ind_name, {}, today_chg))
            return ind_name, {}, today_chg
        if len(bars) < 2:
            log.info("板块 %s(%s) 日K数据不足2天", ind_name, bk_code)
            return ind_name, {}, None
        chg_by_date = {
            b[0]: (b[1] / a[1]) * 100 - 100
            for a, b in zip(bars, bars[1:])
        }
        today_chg = chg_by_date.get(bars[-1][0])
        with _STATE_LOCK:
            _SECTOR_CACHE[full] = (time.time(),
                                   (ind_name, chg_by_date, today_chg))
        return ind_name, chg_by_date, today_chg
    except Exception:
        log.warning("fetch_sector_context 失败 %s", full, exc_info=True)
        return None, {}, None


def fetch_quote(full):
    f = http_get(QT_URL + full).split("~")
    if len(f) < 35 or not f[3]:
        raise ValueError("未查询到该股票")
    return {"name": f[1], "price": float(f[3]), "prev_close": float(f[4]),
            "open": float(f[5]), "high": float(f[33]), "low": float(f[34]),
            "time": f[30]}


def fetch_quote_cached(full: str, ttl=_IDX_QUOTE_TTL):
    """带短时缓存的行情快照：多只股票共享同一份大盘/指数数据。"""
    if full == "sh000001":
        now = time.time()
        with _STATE_LOCK:
            hit = _IDX_QUOTE_CACHE.get(full)
            if hit and now - hit[0] < ttl:
                return hit[1]
            q = fetch_quote(full)
            _IDX_QUOTE_CACHE[full] = (now, q)
            return q
    return fetch_quote(full)


def fetch_daily(full):
    kd = json.loads(http_get(KLINE_URL + f"?param={full},day,,,500,qfq"))
    d = kd.get("data", {}).get(full)
    if not d:
        raise ValueError("K线数据获取失败")
    bars = d.get("qfqday") or d.get("day")
    rows = [{"date": b[0], "open": float(b[1]), "close": float(b[2]),
             "high": float(b[3]), "low": float(b[4]), "vol": float(b[5])}
            for b in bars if float(b[2]) > 0]
    if len(rows) < 100:
        raise ValueError("上市时间太短，样本不足")
    return rows


# ================= 指标计算 =================

def sma_period(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def calc_macd(closes):
    dif = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    dea_raw = ema(dif, 9)
    dea = [None] * 8 + dea_raw[8:]
    hist = [None if dd is None else 2 * (a - dd) for a, dd in zip(dif, dea)]
    return dif, dea, hist


def calc_kdj(rows, n=9):
    ks, ds = [], []
    k = d = 50.0
    for i in range(len(rows)):
        seg = rows[max(0, i - n + 1):i + 1]
        lo = min(r["low"] for r in seg)
        hi = max(r["high"] for r in seg)
        rsv = (rows[i]["close"] - lo) / (hi - lo) * 100 if hi > lo else 50.0
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        ds.append(d)
    return ks, ds, [3 * a - 2 * b for a, b in zip(ks, ds)]


def calc_rsi(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def calc_chips(rows, cur_price=None, nbin=120):
    """筹码分布：逐日按换手衰减历史筹码，当日成交量在[低,高]区间均匀摊分。
    无流通股本数据，换手率用 量/中位量*2% 启发式近似（限幅）。"""
    bars = [r for r in rows
            if r.get("vol") and r.get("low") and r["low"] > 0
            and r["high"] >= r["low"]]
    if len(bars) < 30:
        return None
    lo_p = min(r["low"] for r in bars)
    hi_p = max(r["high"] for r in bars)
    if hi_p <= lo_p:
        return None
    step = (hi_p - lo_p) / nbin
    chips = [0.0] * (nbin + 1)
    med_vol = sorted(r["vol"] for r in bars)[len(bars) // 2] or 1.0
    for r in bars:
        t = min(0.20, max(0.002, 0.02 * (r["vol"] / med_vol)))
        chips = [c * (1.0 - t) for c in chips]
        b_lo = max(0, int((r["low"] - lo_p) / step))
        b_hi = min(nbin, int((r["high"] - lo_p) / step))
        if b_hi <= b_lo:
            chips[b_hi] += r["vol"]
        else:
            share = r["vol"] / (b_hi - b_lo + 1)
            for k in range(b_lo, b_hi + 1):
                chips[k] += share
    tot = sum(chips)
    if tot <= 0:
        return None
    mids = [lo_p + step * (k + 0.5) for k in range(nbin + 1)]
    if cur_price is None or cur_price <= 0:
        cur_price = bars[-1]["close"]
    avg_cost = sum(m * w for m, w in zip(mids, chips)) / tot
    profit = sum(w for m, w in zip(mids, chips) if m <= cur_price) / tot
    p5 = p95 = None
    cum = 0.0
    for m, w in zip(mids, chips):
        cum += w
        if p5 is None and cum >= tot * 0.05:
            p5 = m
        if cum >= tot * 0.95:
            p95 = m
            break

    # 支撑/压力：在筹码分布中找“局部峰”（局部极大值），再取现价上下方
    # 最密集的峰，作为支撑位/压力位。相比原“单根最密集bin”，局部峰能
    # 避免选中噪声尖刺，且更贴近“最密集筹码峰”的语义。
    def _peaks():
        out = []
        for k in range(1, nbin):
            w = chips[k]
            if w > chips[k - 1] and w >= chips[k + 1] and w > 0:
                out.append((mids[k], w))
        return out

    def _strongest(below):
        pk = _peaks()
        if below:
            cand = [(m, w) for m, w in pk if m < cur_price]
        else:
            cand = [(m, w) for m, w in pk if m > cur_price]
        if not cand:
            # 退化为最密集单bin（避免无峰时返回空）
            best_w, best_m = -1.0, None
            for w, m in zip(chips, mids):
                if (m < cur_price) == below and w > best_w:
                    best_w, best_m = w, m
            return best_m
        return max(cand, key=lambda x: x[1])[0]   # 最密集峰

    return {"bins": list(zip(mids, chips)),
            "avg_cost": round(avg_cost, 3), "profit": profit,
            "p5": p5, "p95": p95,
            "peak": max(zip(chips, mids))[1],
            "sup": _strongest(True), "res": _strongest(False),
            "cur": cur_price}


def calc_adx(rows, n=14):
    """DMI/ADX：+DI、-DI、ADX(n=14)。
    返回 (pdi, mdi, adx) 三条序列，预热期(2n左右)为 None。"""
    m = len(rows)
    pdi = [None] * m
    mdi = [None] * m
    adx = [None] * m
    if m < 2 * n + 1:
        return pdi, mdi, adx
    tr_s = pdm_s = ndm_s = 0.0
    dxs = []
    for i in range(1, m):
        h, l = rows[i]["high"], rows[i]["low"]
        hp, lp = rows[i - 1]["high"], rows[i - 1]["low"]
        pc = rows[i - 1]["close"]
        up = h - hp
        dn = lp - l
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if i <= n:
            tr_s += tr
            pdm_s += pdm
            ndm_s += ndm
        else:
            # Wilder 平滑
            tr_s = tr_s - tr_s / n + tr
            pdm_s = pdm_s - pdm_s / n + pdm
            ndm_s = ndm_s - ndm_s / n + ndm
        if i >= n and tr_s > 0:
            pdi[i] = 100.0 * pdm_s / tr_s
            mdi[i] = 100.0 * ndm_s / tr_s
            s = pdi[i] + mdi[i]
            dxs.append(100.0 * abs(pdi[i] - mdi[i]) / s if s > 0 else 0.0)
            if len(dxs) >= n:
                if adx[i - 1] is None:
                    adx[i] = sum(dxs[-n:]) / n
                else:
                    adx[i] = (adx[i - 1] * (n - 1) + dxs[-1]) / n
    return pdi, mdi, adx


def calc_boll(closes, n=20, k=2.0):
    """布林带：中轨=n日SMA，上下轨=中轨±k倍标准差。
    返回 (mid, up, low) 三条序列，预热期为 None。"""
    mid = sma_period(closes, n)
    up = [None] * len(closes)
    low = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        seg = closes[i - n + 1:i + 1]
        m = mid[i]
        sd = (sum((x - m) ** 2 for x in seg) / n) ** 0.5
        up[i] = m + k * sd
        low[i] = m - k * sd
    return mid, up, low


def _band_fit_score(rows, mas, vr_arr):
    """波段适合度评分（0-100）：用实时技术特征判断该股是否适合做短线波段。

    适合波段的特征：波动率适中、趋势明确、量能活跃、非单边阴跌。
    返回分越高越适合波段（≥60 判为适合）。
    """
    try:
        if len(rows) < 60:
            return 50.0
        closes = [r["close"] for r in rows]
        c = closes[-1]
        # 1) 波动率（近20日日收益标准差年化）：适中偏高加分
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        r20 = rets[-20:]
        import statistics
        vol = statistics.stdev(r20) if len(r20) > 1 else 0
        vol_score = 0.0
        # 日均波动 1%~3% 视为波段友好区间
        if 0.008 <= vol <= 0.04:
            vol_score = 25.0
        elif vol < 0.008:
            vol_score = 5.0          # 太死水，无波段空间
        else:
            vol_score = 15.0          # 波动过大，风险高
        # 2) 趋势强度：MA20 斜率 + 价格相对 MA20 位置
        ma20 = mas[20][-1] if mas[20][-1] else c
        ma20p = mas[20][-6] if len(mas[20]) > 6 and mas[20][-6] else ma20
        slope = (ma20 - ma20p) / ma20p if ma20p else 0
        dist = (c - ma20) / ma20 if ma20 else 0
        trend = 0.0
        if slope > 0.002 and 0 < dist < 0.06:      # 温和上行且未过度偏离
            trend = 30.0
        elif slope > 0 and dist > -0.03:
            trend = 20.0
        elif slope < -0.002:
            trend = 5.0                             # 单边下行，不适合波段
        else:
            trend = 15.0
        # 3) 量能活跃度：近期量比
        vr = vr_arr[-1] if vr_arr else None
        vol_active = 0.0
        if vr is None:
            vol_active = 15.0
        elif 0.8 <= vr <= 2.5:
            vol_active = 25.0                       # 量能活跃且未过度
        elif vr > 2.5:
            vol_active = 12.0                       # 放量过猛，注意见顶
        else:
            vol_active = 8.0                        # 缩量，波段乏力
        # 4) 非单边阴跌：近60日整体趋势
        ma60 = mas[60][-1] if mas[60][-1] else c
        long_trend = 0.0
        if c >= ma60:
            long_trend = 20.0
        else:
            long_trend = 5.0
        return round(vol_score + trend + vol_active + long_trend, 1)
    except Exception:
        return 50.0


def _trend_track_signals(disp_rows, mas, idx_chg_by_date, idx_chg_today):
    """长周期趋势跟踪信号（用于不适合波段的标的）。

    以 MA20 与 MA60 的金叉/死叉为主信号，叠加 MA20 斜率与大盘环境过滤；
    信号少而稳，持有周期长，避免阴跌中被频繁套牢。
    返回与现有多维信号相同格式的列表 [(index, date, "BUY"/"SELL", 理由)]。
    """
    signals = []
    if len(disp_rows) < 60:
        return signals
    closes = [r["close"] for r in disp_rows]
    ma20 = mas[20]
    ma60 = mas[60]
    prev_state = None          # 0=空仓/无趋势, 1=多头持有
    # 从近 120 根开始扫描
    start = max(1, len(disp_rows) - 120)
    for i in range(start, len(disp_rows)):
        m20 = ma20[i]
        m60 = ma60[i]
        if m20 is None or m60 is None or m20 <= 0 or m60 <= 0:
            continue
        c = closes[i]
        # MA20 斜率
        m20p = ma20[i - 1] if i >= 1 and ma20[i - 1] else m20
        slope20 = (m20 - m20p) / m20p if m20p else 0
        # MA60 前值
        m60p = ma60[i - 1] if i >= 1 and ma60[i - 1] else m60
        # 大盘环境过滤（只用当日大盘涨跌，避免引用“今日”数据造成前视）
        ic = idx_chg_by_date.get(disp_rows[i]["date"])
        idx_ok = True
        if ic is not None:
            idx_ok = ic > -1.2     # 当日大盘未大幅走弱
        # 金叉：MA20 上穿 MA60 且斜率向上
        if (m20 > m60 and m20p <= m60p
                and slope20 > 0 and idx_ok):
            if prev_state != 1:
                signals.append((i, disp_rows[i]["date"], "BUY",
                                "趋势金叉 MA20上穿MA60 且斜率向上"))
                prev_state = 1
        # 死叉：MA20 下穿 MA60，或趋势破坏
        elif m20 < m60 and m20p >= m60p:
            if prev_state == 1:
                signals.append((i, disp_rows[i]["date"], "SELL",
                                "趋势死叉 MA20下穿MA60"))
                prev_state = 0
    return signals


def backtest_signals(rows, signals, rp=None):
    """按买卖点信号模拟交易（信号日收盘价成交，无手续费）。
    BUY开仓/SELL平仓，带ATR动态止损+移动止盈。
    返回 胜率、区间收益、年化收益、最大回撤。"""
    try:
        if not signals or len(rows) < 30:
            return None
        sig_map = {s[0]: s[2] for s in signals}
        
        # 计算ATR(14)用于止损
        atrs = [0.0] * len(rows)
        for i in range(14, len(rows)):
            h, l, pc = rows[i]["high"], rows[i]["low"], rows[i-1]["close"]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            atrs[i] = sum(max(rows[j]["high"] - rows[j]["low"], 
                           abs(rows[j]["high"] - rows[j-1]["close"]),
                           abs(rows[j]["low"] - rows[j-1]["close"])) 
                      for j in range(i-13, i+1)) / 14
        
        rp = rp or CFG.risk_params()
        eq = 1.0
        entry = None
        highest = None  # 持仓期间最高价
        trades = []
        curve = []

        for i, r in enumerate(rows):
            c = r["close"]
            h = r["high"]
            l = r["low"]
            typ = sig_map.get(i)

            if entry is not None:
                highest = max(highest, h) if highest else h
                atr_stop = entry - rp["atr_mult"] * atrs[i] if atrs[i] > 0 \
                    else entry * 0.95
                trail_stop = highest * rp["trail_ratio"] \
                    if highest > entry * rp["trail_trigger"] else atr_stop
                
                # 止损触发（日内最低触及止损价）
                if l <= trail_stop:
                    exit_price = trail_stop
                    trades.append(exit_price / entry - 1)
                    eq *= exit_price / entry
                    entry = None
                    highest = None
                    curve.append(eq)
                    continue
            
            if typ == "BUY" and entry is None and c:
                entry = c
                highest = h
            elif typ == "SELL" and entry:
                trades.append(c / entry - 1)
                eq *= c / entry
                entry = None
                highest = None
            curve.append(eq * (c / entry) if entry else eq)
        
        # 未平仓按最后收盘价计算
        floating = rows[-1]["close"] / entry - 1 if entry else None
        
        wins = len([t for t in trades if t > 0])
        losses = len([t for t in trades if t <= 0])
        
        import datetime
        d0 = datetime.date.fromisoformat(rows[signals[0][0]]["date"])
        d1 = datetime.date.fromisoformat(rows[-1]["date"])
        years = max((d1 - d0).days / 365.25, 1e-9)
        total = curve[-1] if curve else 1.0
        ann = total ** (1 / years) - 1 if total > 0 else -1.0
        
        peak = 0.0
        mdd = 0.0
        for v in curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = min(mdd, v / peak - 1)
        
        # 平均盈利/平均亏损
        avg_win = sum(t for t in trades if t > 0) / wins if wins else 0
        avg_loss = sum(t for t in trades if t <= 0) / losses if losses else 0
        
        return {
            "trades": len(trades) + (1 if floating is not None else 0),
            "closed": len(trades),
            "wins": wins,
            "losses": losses,
            "winrate": wins / len(trades) if trades else None,
            "total": total - 1,
            "ann": ann,
            "mdd": mdd,
            "floating": floating,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_loss": avg_win / abs(avg_loss) if avg_loss != 0 else float('inf'),
        }
    except Exception:
        log.exception("backtest_signals 回测失败")
        return None


def _l1_up_prob_last(rows):
    """最新一日的 L1 形态上行概率（与 v4 因子 l1_up 完全同口径，防前视）。
    样本不足返回 None。"""
    n = len(rows)
    closes = [r["close"] for r in rows]
    L = logret(closes)
    W = W_WINDOW
    t = n - 1
    if len(L) < 2 * W + 3 or t - W < W:
        return None
    cur = znorm(L[t - W:t])
    d_arr = _px_distances(L[:t], cur, W)    # 仅用 t 以前窗口（防前视）
    ks = [k for k in range(len(d_arr)) if k + W <= t - W]
    if len(ks) < 6:
        return None
    ks.sort(key=lambda k: d_arr[k])
    ups = tot = 0.0
    for k in ks[:CFG.TOPK]:
        j = k + W
        if j + 1 < n:
            tot += 1.0
            ups += 1.0 if closes[j + 1] > closes[j] else 0.0
    if tot < 5:
        return None
    return ups / tot


def _picks_ind_ctx(days=14):
    """最新行业5日等权收益（板块轮动荐股用）。
    返回 (map={industry: ret5}, med=全行业中位数, lead=前20%行业集合)。"""
    with db_conn() as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_bars ORDER BY date DESC LIMIT ?",
            (days + 6,))]
        if len(dates) < 7:
            return {}, 0.0, set()
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
        acc = {}
        for c, d, cl in conn.execute(
                "SELECT code, date, close FROM daily_bars "
                "WHERE date >= ? AND date <= ? ORDER BY code, date",
                (min(dates), dates[0])):
            acc.setdefault(c, []).append((d, cl))
    latest = dates[0]
    rets = {}
    for c, seq in acc.items():
        dd = [x[0] for x in seq]
        if latest not in dd:
            continue
        k = dd.index(latest)
        if k < 5:
            continue
        p0, p5 = seq[k][1], seq[k - 5][1]
        x = ind_of.get(c, "")
        if p0 and p5 and p0 > 0 and p5 > 0 and x:
            rets.setdefault(x, []).append(p0 / p5 - 1.0)
    m = {x: sum(v) / len(v) for x, v in rets.items() if v}
    vals = sorted(m.values())
    med = vals[len(vals) // 2] if vals else 0.0
    lead = set()
    if m:
        srt = sorted(m.items(), key=lambda kv: -kv[1])
        lead = {x for x, _ in srt[:max(1, len(srt) // 5)]}
    return m, med, lead


def daily_pick_score(rows, ind_ctx=None):
    """对最新一根K线做多维打分（与买卖点信号同构，权重同 CFG.IND_W）。
    v4.0.1 荐股优化（依据 v3.3/v4 全A实证，定位"以小博大"）：
    - RSI 改动量口径：超卖反弹假设不成立（反向状态 IC -0.098，仅13.8%个股为正），
      超卖不再加分，强势状态加分；
    - 新增 MA20/60 趋势维度（IC 0.228）与 L1 形态上行概率维度（IC 0.265）；
    - 新增爆发力（20日动量，10%~35%强势区加分、>35%过热减分）与量能扩张维度；
    - ind_ctx 非 None 时新增板块轮动维度（行业5日收益强势/前20%领先）；
    - 返回第4元素 gates：{"ma_trend": ±2/0} 供 daily_picks 做空头趋势闸门。
    返回 (score, reasons, band_fit_score, gates)。"""
    n = len(rows)
    if n < 60:
        return None
    closes = [r["close"] for r in rows]
    dif, dea, _ = calc_macd(closes)
    k_, d_, _ = calc_kdj(rows)
    r6 = calc_rsi(closes, 6)
    b_mid, b_up, b_low = calc_boll(closes)
    pdi_a, mdi_a, adx_a = calc_adx(rows)
    mas = {20: sma_period(closes, 20), 60: sma_period(closes, 60)}
    vols_d = [r.get("vol") or 0.0 for r in rows]
    vr_arr = [vol_ratio_at(vols_d, k) for k in range(n)]
    i = n - 1
    if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
        return None
    sc = 0
    reasons = []

    def _wadd(dim, pts, reason=None):
        nonlocal sc
        sc += int(round(pts * CFG.IND_W.get(dim, 1.0)))
        if reason and pts:
            reasons.append(reason)

    if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
        _wadd("MACD", 2, "MACD金叉")
    elif dif[i] > dea[i]:
        _wadd("MACD", 1, "DIF>DEA")
    elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
        _wadd("MACD", -2, "MACD死叉")
    else:
        _wadd("MACD", -1)
    if k_[i - 1] <= d_[i - 1] and k_[i] > d_[i] and k_[i] < 45:
        _wadd("KDJ", 2, "KDJ低位金叉")
    elif k_[i] > d_[i]:
        _wadd("KDJ", 1)
    elif k_[i - 1] >= d_[i - 1] and k_[i] < d_[i] and k_[i] > 65:
        _wadd("KDJ", -2, "KDJ高位死叉")
    else:
        _wadd("KDJ", -1)
    if r6[i] is not None and r6[i - 1] is not None:
        # v4.0.1：动量口径（超卖=弱势延续不加分，强势=延续加分）
        if r6[i] < 30:
            _wadd("RSI", -1, "RSI超卖弱势(动量)")
        elif r6[i] > 70:
            _wadd("RSI", 1, "RSI强势(动量)")
    c, cp = rows[i]["close"], rows[i - 1]["close"]
    v5 = sum(vols_d[max(0, i - 5):i]) / max(1, min(5, i))
    vr_d = vols_d[i] / v5 if v5 > 0 else 0.0
    if vr_d > 1.5 and c > cp:
        _wadd("量价", 1, "放量上涨")
    elif vr_d > 1.5 and c < cp:
        _wadd("量价", -1, "放量下跌")
    ma20, ma20p = mas[20][i], mas[20][i - 1]
    if ma20 and ma20p:
        if c > ma20 and ma20 > ma20p:
            _wadd("MA20", 1)
        elif c < ma20 and ma20 < ma20p:
            _wadd("MA20", -1)
    ma60, ma60p = mas[60][i], mas[60][i - 1]
    ma_trend = 0
    if ma20 and ma20p and ma60 and ma60p:
        # v3.3 全A实证：MA20/60趋势状态 IC 0.228（87%个股为正）
        if c > ma20 > ma60 and ma20 > ma20p:
            ma_trend = 2
            _wadd("MA趋势", 2, "MA20/60多头趋势")
        elif c < ma20 < ma60 and ma20 < ma20p:
            ma_trend = -2
            _wadd("MA趋势", -2, "MA20/60空头趋势")
    # 爆发力：20日动量（以小博大核心；极端过热反向，bias20 极端延伸 IC 为负）
    if i >= 20 and closes[i - 20]:
        c20 = c / closes[i - 20] - 1.0
        if 0.10 <= c20 < 0.35:
            _wadd("爆发力", 2, "20日强势+%.0f%%" % (c20 * 100))
        elif 0.05 <= c20 < 0.10:
            _wadd("爆发力", 1, "20日强势+%.0f%%" % (c20 * 100))
        elif c20 >= 0.35:
            _wadd("爆发力", -2, "20日过热+%.0f%%" % (c20 * 100))
        elif c20 <= -0.15:
            _wadd("爆发力", -1, "20日弱势%.0f%%" % (c20 * 100))
    # 量能扩张：5日均量显著放大且收涨（突破期特征）
    if i >= 20:
        v20m = sum(vols_d[i - 19:i + 1]) / 20.0
        v5m = sum(vols_d[max(0, i - 4):i + 1]) / max(1, min(5, i + 1))
        if v20m > 0 and v5m / v20m > 1.5 and c > cp:
            _wadd("量能", 1, "量能扩张")
    # 板块轮动：行业5日收益强势 / 前20%领先
    if ind_ctx:
        r5 = ind_ctx.get("r5")
        if r5 is not None:
            if r5 > ind_ctx.get("med", 0.0):
                _wadd("板块", 1, "板块强势")
            if ind_ctx.get("lead"):
                _wadd("板块", 1, "板块前20%")
    try:
        # 只用尾部160根算筹码快照：全量算1600只需数分钟且饿死GIL卡界面
        snap = chip_snapshots(rows[-160:], tail=1).get(rows[i]["date"])
        if snap:
            sup_i, res_i = snap[0], snap[1]
            if sup_i and c <= sup_i * 1.01:
                _wadd("筹码", 1, "贴近支撑")
            elif res_i and c >= res_i * 0.99:
                _wadd("筹码", -1, "贴近压力")
    except Exception:
        pass
    if None not in (b_up[i], b_low[i]):
        if c < b_low[i]:
            _wadd("布林带", 1, "布林下轨超卖")
        elif c > b_up[i]:
            _wadd("布林带", -1, "布林上轨超买")
    a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
    if None not in (a_i, p_i, m_i) and a_i >= 20:
        if p_i > m_i:
            _wadd("ADX", 1, "ADX趋势偏多" if a_i >= 25 else None)
        elif m_i > p_i:
            _wadd("ADX", -1)
    try:
        # L1 形态上行概率（v4 因子同口径，IC 0.265 全A最强）
        p_up = _l1_up_prob_last(rows)
        if p_up is not None:
            if p_up >= 0.6:
                _wadd("形态", 2, "L1形态上行%d%%" % round(p_up * 100))
            elif p_up <= 0.4:
                _wadd("形态", -2, "L1形态上行%d%%" % round(p_up * 100))
    except Exception:
        pass
    band = _band_fit_score(rows, mas, vr_arr)
    return sc, reasons, band, {"ma_trend": ma_trend}


def daily_picks(progress=None, top_n=20, min_bars=120):
    """每日荐股：纯本地缓存扫描，不联网。
    返回 [(code, name, close, chg_pct, score, reasons, band)] 按 score 降序。
    深历史库（全市场×1000+根）不能整库载入内存：先按根数初筛，
    再分块只取每只尾部400根。"""
    with db_conn() as conn:
        names = {r[0]: r[1] for r in conn.execute(
            "SELECT code, name FROM stocks").fetchall()}
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM daily_bars GROUP BY code "
            "HAVING COUNT(*) >= ?", (min_bars,)).fetchall()]
    ind5_map, ind5_med, ind5_lead = _picks_ind_ctx()
    CH = 500
    cands = []
    for i in range(0, len(codes), CH):
        chunk = codes[i:i + CH]
        with db_conn() as conn:
            ph = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT code,date,open,high,low,close,vol FROM ("
                f"  SELECT code,date,open,high,low,close,vol,"
                f"         ROW_NUMBER() OVER (PARTITION BY code "
                f"                            ORDER BY date DESC) rn"
                f"  FROM daily_bars WHERE code IN ({ph})"
                f") WHERE rn<=400 ORDER BY code, date", chunk).fetchall()
        by = {}
        for c, d, o, h, l, cl, v in rows:
            by.setdefault(c, []).append(
                {"date": d, "open": o, "high": h, "low": l,
                 "close": cl, "vol": v or 0.0})
        cands += [(c, r) for c, r in by.items()
                  if len(r) >= min_bars and not _is_etf(c)
                  and not c.startswith('bj')          # 北交所K线源不支持
                  and r[-1]['close'] and r[-1]['close'] >= 2]   # 剔除仙股
        if progress:
            progress(f"荐股载入 {min(i + CH, len(codes))}/{len(codes)}")
    picks = []
    total = len(cands)
    t_start = time.time()
    for k, (code, rws) in enumerate(cands):
        if progress and k % 40 == 0:
            progress(f"荐股扫描 {k}/{total}")
            time.sleep(0.01)          # 让出GIL，防止界面卡死
        if time.time() - t_start > 120:
            break                     # 2分钟硬熔断：宁可少扫不卡界面
        try:
            _ic = ind_of.get(code, "")
            r = daily_pick_score(rws[-400:], ind_ctx={
                "r5": ind5_map.get(_ic), "med": ind5_med,
                "lead": _ic in ind5_lead})
        except Exception:
            continue
        if r is None:
            continue
        score, reasons, band, gates = r
        if "ST" in names.get(code, "").upper():
            continue             # ST：退市/流动性风险，以小博大不碰
        if gates.get("ma_trend", 0) <= -2:
            continue             # 空头趋势闸门（MA20/60趋势 IC 0.228，最强信号）
        if score < CFG.risk_params()["buy_th"]:
            continue
        chg = (rws[-1]["close"] / rws[-2]["close"] - 1) * 100 \
            if rws[-1]["close"] and rws[-2]["close"] else 0.0
        picks.append((code, names.get(code, code[-6:]),
                      rws[-1]["close"], chg, score,
                      " ".join(reasons) or "-", band))
    picks.sort(key=lambda x: -x[4])
    if progress:
        progress(f"荐股完成：{len(picks)} 只入围，取Top{top_n}")
    return picks[:top_n]


def chip_snapshots(rows, nbin=80, tail=120):
    """逐日演化筹码分布，返回 {日期: (支撑价, 压力价, 获利比例)}（仅尾部tail天）。"""
    bars = [r for r in rows
            if r.get("vol") and r.get("low") and r["low"] > 0
            and r["high"] >= r["low"]]
    if len(bars) < 30:
        return {}
    lo_p = min(r["low"] for r in bars)
    hi_p = max(r["high"] for r in bars)
    if hi_p <= lo_p:
        return {}
    step = (hi_p - lo_p) / nbin
    mids = [lo_p + step * (k + 0.5) for k in range(nbin + 1)]
    chips = [0.0] * (nbin + 1)
    med_vol = sorted(r["vol"] for r in bars)[len(bars) // 2] or 1.0
    out = {}
    rec_from = len(bars) - min(tail, len(bars))
    for idx, r in enumerate(bars):
        t = min(0.20, max(0.002, 0.02 * (r["vol"] / med_vol)))
        chips = [c * (1.0 - t) for c in chips]
        b_lo = max(0, int((r["low"] - lo_p) / step))
        b_hi = min(nbin, int((r["high"] - lo_p) / step))
        if b_hi <= b_lo:
            chips[b_hi] += r["vol"]
        else:
            share = r["vol"] / (b_hi - b_lo + 1)
            for k in range(b_lo, b_hi + 1):
                chips[k] += share
        if idx < rec_from:
            continue
        tot = sum(chips)
        if tot <= 0:
            continue
        c = r["close"]
        profit = sum(w for m, w in zip(mids, chips) if m <= c) / tot

        def _peaks():
            return [(mids[k], chips[k])
                    for k in range(1, nbin)
                    if chips[k] > chips[k - 1] and chips[k] >= chips[k + 1]
                    and chips[k] > 0]

        def _strongest(below):
            pk = _peaks()
            cand = [(m, w) for m, w in pk
                    if (m < c) == below]
            if not cand:
                bw, bm = -1.0, None
                for w, m in zip(chips, mids):
                    if (m < c) == below and w > bw:
                        bw, bm = w, m
                return bm
            return max(cand, key=lambda x: x[1])[0]

        out[r["date"]] = (_strongest(True), _strongest(False), profit)
    return out


def pct(vals, p):
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    f = k - lo
    return s[lo] * (1 - f) + s[hi] * f


def znorm(win):
    m = sum(win) / len(win)
    sd = (sum((x - m) ** 2 for x in win) / len(win)) ** 0.5 or 1e-12
    return [(x - m) / sd for x in win]


def logret(seq, is_etf=False):
    """计算对数收益率序列。
    对于ETF，除权除息会导致前复权价格跳变，对异常跳变进行平滑处理。"""
    rets = [math.log(seq[i + 1] / seq[i]) for i in range(len(seq) - 1)]
    if is_etf and len(rets) > 0:
        # 计算收益率的中位数和标准差
        import statistics
        median = statistics.median(rets)
        # 计算绝对偏差的中位数（MAD），比标准差更稳健
        mad = statistics.median([abs(r - median) for r in rets])
        # 对超过5倍MAD的异常收益率进行平滑（截断到±5倍MAD）
        threshold = 5 * mad if mad > 0 else 0.1
        rets = [max(min(r, median + threshold), median - threshold) for r in rets]
    return rets


W_WINDOW, TOPK = 10, 10

# ---- 三级样本池：L1自身 / L2同行业 / L3同市值层，融合权重 ----
LV_W = dict(CFG.LV_W)
LV_LABEL = {"L1": "自身历史", "L2": "同行业", "L3": "同市值层"}


def wpct(pairs, p):
    """加权分位数：pairs=[(值,权重)]，p∈{10..90}。"""
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs) or 1.0
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= p / 100 * tot:
            return v
    return pairs[-1][0]


# ---- 多维匹配扩展特征 ----

def rsi_at(closes, i, n=14):
    """截至第 i 日（含）的简单RSI。样本不足返回 None。"""
    if i < n or n <= 0:
        return None
    gains = losses = 0.0
    for k in range(i - n + 1, i + 1):
        ch = closes[k] - closes[k - 1]
        if ch > 0:
            gains += ch
        else:
            losses -= ch
    if gains + losses <= 0:
        return None
    if losses <= 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def vola_at(rets, i, n=10):
    """截至第 i 日（含）近 n 日对数收益标准差（日波动率）。"""
    if i < n - 1 or n <= 1:
        return None
    seg = rets[i - n + 1:i + 1]
    m = sum(seg) / n
    return (sum((x - m) ** 2 for x in seg) / n) ** 0.5


def candle_feats(rows, i):
    """第 i 根K线结构特征：(实体方向占比, 上影占比, 下影占比, 收盘位置)。
    全部归一到 -1..1 / 0..1，量纲无关。"""
    r = rows[i]
    o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
    if None in (o, h, l, c) or o <= 0 or c <= 0:
        return None
    rng = h - l
    if rng <= 1e-9:
        return (0.0, 0.0, 0.0, 0.5)
    body = (c - o) / rng
    up_sh = (h - max(o, c)) / rng
    dn_sh = (min(o, c) - l) / rng
    pos = (c - l) / rng
    return (body, up_sh, dn_sh, pos)


def volchg_at(vols, i, n=5):
    """量变特征：log(近n日均量 / 前n日均量)。"""
    if i < 2 * n - 1:
        return None
    cur = sum(vols[i - n + 1:i + 1]) / n
    prev = sum(vols[i - 2 * n + 1:i - n + 1]) / n
    if prev <= 0 or cur <= 0:
        return None
    return math.log(cur / prev)


def weekly_ctx(rows, i, n=4):
    """周线环境：截至第 i 日最近 n 周（每周5根K线近似）的周收益列表，
    最近一周在前。样本不足返回 None。"""
    if i + 1 < n * 5:
        return None
    out = []
    for k in range(n):
        e = i + 1 - 5 * k
        s = e - 5
        if s < 0:
            return None
        c0 = rows[s].get("close")
        c1 = rows[e - 1].get("close")
        if not c0 or c0 <= 0 or not c1:
            return None
        out.append(c1 / c0 - 1)
    return out


def _cur_context(rows, rets, vols, closes):
    """计算「今日」的多维匹配特征向量（供 L1/L2/L3 共用）。"""
    i = len(rows) - 1
    return {
        "struct": candle_feats(rows, i),
        "vola": vola_at(rets, i),
        "rsi": rsi_at(closes, i),
        "volchg": volchg_at(vols, i),
        "weekly": weekly_ctx(rows, i, CFG.WEEKLY_N),
    }


def _dist_extra(d_cur, d_i):
    """样本与今日的扩展特征距离（结构+波动率+RSI+量变+周线）。
    任一特征缺失时该项取中性惩罚值，保证不同样本分数可比。
    d_cur 为 None（如渐进加载早期未计算今日特征）时全部按缺失处理。"""
    d = 0.0
    d_cur = d_cur or {}
    # K线结构（4维欧氏距离/4）
    if d_cur.get("struct") is not None and d_i.get("struct") is not None:
        a, b = d_cur["struct"], d_i["struct"]
        d += CFG.STRUCT_W * (sum((x - y) ** 2 for x, y in zip(a, b)) / len(a)) ** 0.5
    else:
        d += CFG.STRUCT_W * 0.5
    # 波动率（对数比，封顶1.5）
    if d_cur.get("vola") is not None and d_i.get("vola") is not None \
            and d_cur["vola"] > 0 and d_i["vola"] > 0:
        d += CFG.VOLA_W * min(abs(math.log(d_cur["vola"] / d_i["vola"])), 1.5)
    else:
        d += CFG.VOLA_W * 0.5
    # RSI（0-100 差 / 100）
    if d_cur.get("rsi") is not None and d_i.get("rsi") is not None:
        d += CFG.RSI_W * abs(d_cur["rsi"] - d_i["rsi"]) / 100.0
    else:
        d += CFG.RSI_W * 0.5
    # 量变（对数比，封顶1.5）
    if d_cur.get("volchg") is not None and d_i.get("volchg") is not None:
        d += CFG.VOLCHG_W * min(abs(d_cur["volchg"] - d_i["volchg"]), 1.5)
    else:
        d += CFG.VOLCHG_W * 0.5
    # 周线环境（周收益欧氏距离，×5 放大到日级别量纲附近）
    if d_cur.get("weekly") is not None and d_i.get("weekly") is not None:
        a, b = d_cur["weekly"], d_i["weekly"]
        d += CFG.WEEKLY_W * min((sum((x - y) ** 2 for x, y in zip(a, b))
                                 / len(a)) ** 0.5 * 5, 1.5)
    else:
        d += CFG.WEEKLY_W * 0.5
    return d


def _dynamic_lv_weights(levels):
    """按层级的有效样本质量动态分配权重，使用Top-K中位距离而非单个best。"""
    keys = ("L1", "L2", "L3")
    pri_tot = sum(max(0.0, LV_W.get(k, 0.0)) for k in keys) or 1.0
    pri = {k: max(0.0, LV_W.get(k, 0.0)) / pri_tot for k in keys}
    if not CFG.DYNAMIC_LV_W:
        return pri
    raw = {}
    for k, smp in levels:
        scores = sorted(float(s.get("similarity_score", 9.0)) for s in smp
                        if math.isfinite(float(s.get("similarity_score", 9.0))))
        if not scores:
            raw[k] = 0.0
            continue
        med = scores[len(scores) // 2]
        quality = math.exp(-min(med, 6.0) / 1.25)
        n_factor = min(1.0, math.sqrt(len(scores) / 6.0))
        raw[k] = quality * (0.65 + 0.35 * n_factor)
    tot = sum(raw.values())
    if tot <= 0:
        return pri
    dyn = {k: raw.get(k, 0.0) / tot for k in keys}
    st = min(max(CFG.DYN_LV_STRENGTH, 0.0), 1.0)
    return {k: (1.0 - st) * pri[k] + st * dyn.get(k, 0.0) for k in keys}


def _pool_match(pool_rows, cur, vr_now, idx_chg_by_date, idx_chg_today,
                topk=TOPK, candidate_topk=None, cur_ctx=None):
    """多股票池历史窗口匹配；只用样本T及以前的信息，目标收益绝不参与筛选。"""
    W = W_WINDOW
    candidate_topk = candidate_topk or CFG.CANDIDATE_TOPK
    info, sims = {}, []
    for code, rows in pool_rows:
        closes = [r["close"] for r in rows]
        rets = logret(closes, is_etf=_is_etf(code))
        if len(rets) < W + 3:
            continue
        vols = [r.get("vol") or 0.0 for r in rows]
        vr_arr = [vol_ratio_at(vols, k) for k in range(len(vols))]
        info[code] = (rows, vr_arr)
        smp_ctx = {
            "struct": [candle_feats(rows, k) for k in range(len(rows))],
            "vola": [vola_at(rets, k) for k in range(len(rets))],
            "rsi": [rsi_at(closes, k) for k in range(len(closes))],
            "volchg": [volchg_at(vols, k) for k in range(len(vols))],
            "weekly": [weekly_ctx(rows, k, CFG.WEEKLY_N) for k in range(len(rows))],
        }
        last_i = len(rets) - W
        # d_px 向量化（_px_distances 内部 numpy 可用时快约50倍）；
        # s_v/s_i 廉价可全量算；d_x 较贵 → 先按 d_px+s_v+s_i 预筛
        # top(4K+80) 再补算 d_x（d_x 上界约3.6，截断误差可忽略）
        d_px_arr = _px_distances(rets, cur, W)
        pre = []
        for k, d_px in enumerate(d_px_arr):
            i = k + W
            if i > last_i:
                break
            vr_i = vr_arr[i]
            s_v = 0.6 * min(abs(math.log(max(vr_now,1e-6)/max(vr_i,1e-6))),2.5) if vr_now is not None and vr_i is not None else 0.30
            ic = idx_chg_by_date.get(rows[i]["date"])
            s_i = min(1.5,0.3*abs(ic-idx_chg_today)) if ic is not None and idx_chg_today is not None else 0.40
            pre.append((d_px + s_v + s_i, i, d_px, s_v, s_i))
        pre.sort(key=lambda x: x[0])
        for base_s, i, d_px, s_v, s_i in pre[:candidate_topk * 4 + 80]:
            d_x = _dist_extra(cur_ctx,{k:smp_ctx[k][i] for k in smp_ctx})
            sims.append((base_s+d_x,i,code,d_px,s_v,s_i))
    if not info:
        return []
    candidate = heapq.nsmallest(candidate_topk,sims,key=lambda x:x[0])
    if not candidate:
        return []
    best = candidate[0][0]
    out=[]
    for score,i,code,d_px,s_v,s_i in candidate:
        rows,vr_arr=info[code]; r=rows[i]; ic=idx_chg_by_date.get(r["date"])
        # 消融回测(n=1480)：指数加权命中50.4%/IC-0.043 → 等权52.4%/IC+0.020，
        # 默认关闭；如需启用把 CFG.SIMILARITY_WEIGHTING 改回 True
        weight = math.exp(-min(max(0.0, score - best), 6.0) / 0.9) \
            if CFG.SIMILARITY_WEIGHTING else 1.0
        if CFG.QUALITY_FILTER and d_px>CFG.SIMILARITY_CUTOFF:
            weight*=0.35
        # 时间衰减（消融回测：保留衰减 IC+0.020 vs 无衰减 -0.024）
        if CFG.TIME_DECAY_ENABLED:
            try:
                age=(time.mktime(time.strptime(time.strftime("%Y-%m-%d"),"%Y-%m-%d"))
                     -time.mktime(time.strptime(r["date"],"%Y-%m-%d")))/86400.0
                if age>CFG.TIME_DECAY_DAYS:
                    weight*=max(CFG.TIME_DECAY_RATE,
                                1.0-(age-CFG.TIME_DECAY_DAYS)/365.0*0.5)
            except (ValueError, TypeError):
                pass
        s={"t_date":r["date"],"vr":vr_arr[i],
           "idx_chg":ic-idx_chg_today if ic is not None and idx_chg_today is not None else None,
           "gap":rows[i+1]["open"]/r["close"]-1 if i+1<len(rows) else None,
           "code":code,"_match_i":i,"similarity_score":score,
           "distance_px":d_px,"distance_vol":s_v,"distance_idx":s_i,"weight":weight}
        for d in range(1,11):
            if i+d<len(rows):
                nd=rows[i+d]; prev_c=rows[i+d-1]["close"]; op=nd.get("open") or prev_c
                s[f"n{d}_date"]=nd["date"]; s[f"n{d}_cl"]=nd["close"]/prev_c-1
                s[f"n{d}_hi"]=nd["high"]/prev_c-1; s[f"n{d}_lo"]=nd["low"]/prev_c-1
                s[f"n{d}_oc"]=nd["close"]/op-1 if op>0 else None
                s[f"n{d}_oh"]=nd["high"]/op-1 if op>0 else None
                s[f"n{d}_ol"]=nd["low"]/op-1 if op>0 else None
            else:
                for suf in ("date","cl","hi","lo","oc","oh","ol"): s[f"n{d}_{suf}"]=None
        out.append(s)
    selected=[]; per_code={}; min_gap=max(3,W//2)
    for s in sorted(out,key=lambda x:x["similarity_score"]):
        c=s["code"]; i=s["_match_i"]
        if any(abs(i-j)<min_gap for j in per_code.get(c,[])): continue
        per_code.setdefault(c,[]).append(i); selected.append(s)
        if len(selected)>=topk: break
    return selected


def market_phase_text(time_str):
    """行情快照时间(YYYYMMDDHHMMSS...) -> 'HH:MM 市场阶段'。"""
    try:
        hhmm = int((time_str or "")[8:12])
    except ValueError:
        return "时间未知"
    hm = f"{hhmm // 100}:{hhmm % 100:02d}"
    if hhmm < 915:
        return hm + " 盘前"
    if hhmm < 925:
        return hm + " 集合竞价"
    if hhmm < 1130 or 1300 <= hhmm < 1500:
        return hm + " 盘中交易"
    if hhmm < 1300:
        return hm + " 午间休市"
    return hm + " 已收盘"


def _load_pools(pool_info, cur, vr_now, idx_chg_by_date, idx_chg_today,
                progress=None, cur_ctx=None):
    """取 L2/L3 池K线（走缓存增量）并跑匹配，返回 {"L2":[...], "L3":[...]}。"""
    out = {}
    t0 = time.time()
    lv_keys = ("L2", "L3") if CFG.ENABLE_L3 else ("L2",)
    for key in lv_keys:
        if time.time() - t0 > 60:
            break
        codes = pool_info.get(key.lower()) or []
        if not codes:
            continue
        try:
            if progress:
                progress(f"回填{LV_LABEL[key]}池 {len(codes)}只...")
            prefetch(codes, workers=10, progress=progress)
            # 批量读缓存（单次连接）
            with db_conn() as conn:
                cached = _db_rows_batch(conn, codes)
            pool_rows = [(c, r) for c, r in cached.items() if len(r) >= 130]
            smp = _pool_match(pool_rows, cur, vr_now, idx_chg_by_date,
                              idx_chg_today, cur_ctx=cur_ctx)
            if smp:
                out[key] = smp
        except Exception:
            log.warning("加载%s池失败(跳过)", LV_LABEL[key], exc_info=True)
            continue
    return out


def load_pools_progressive(full, ctx, progress=None, batch=12):
    """逐步加载 L2/L3 样本池并实时产出融合预测。

    ctx 为 analyze 返回的 _ctx（含 o_today/pre_open/live/src/cur/vr_now/
    idx_chg_by_date/idx_chg_today/gap_today/prev_close 等）。
    每加载完一批 L2 或 L3，就用已累计的样本重算一次预测并 yield
    (level_map, t_pred, pred, clamped, tpred_bar, pool_note)；
    调用方在 GUI 主线程据此刷新预测K线，实现“边跑边更新”。
    """
    o_today = ctx["o_today"]
    pre_open = ctx["pre_open"]
    live = ctx["live"]
    src = ctx["src"]
    # 取样本池代码（复用缓存/失败记忆）
    pool_info = None
    try:
        pool_info = pool_codes(full)
    except Exception:
        log.warning("load_pools_progressive: pool_codes 失败 %s",
                    full, exc_info=True)
        pool_info = None
    if not pool_info:
        return
    level_map = {}
    order = ([("L2", pool_info.get("l2") or [])]
             + ([("L3", pool_info.get("l3") or [])] if CFG.ENABLE_L3 else []))
    for key, codes in order:
        if not codes:
            continue
        t0 = time.time()
        acc_rows = []       # 本级的累计池K线，逐批变大，匹配随之变准
        matched_n = 0       # 上次跑匹配时的池大小（自适应降频）
        n_batches = (len(codes) + batch - 1) // batch
        for bi, i in enumerate(range(0, len(codes), batch), 1):
            chunk = codes[i:i + batch]
            if progress:
                progress(f"后台加载{LV_LABEL[key]}样本 "
                         f"{min(i + batch, len(codes))}/{len(codes)}只 "
                         f"({min((i + batch) * 100 // len(codes), 100)}%) "
                         f"第{bi}/{n_batches}批...")
            try:
                # 进度由上面的批次消息统一汇报，避免内层"缓存回填"刷屏
                prefetch(chunk, workers=8)
            except Exception:
                log.warning("样本池回填失败 %s(跳过)", chunk, exc_info=True)
            # 批量读缓存
            try:
                with db_conn() as conn:
                    cached = _db_rows_batch(conn, chunk)
            except Exception:
                cached = {}
            acc_rows += [(c, r) for c, r in cached.items() if len(r) >= 130]
            if progress:
                progress(f"后台加载{LV_LABEL[key]}样本 "
                         f"{len(acc_rows)}/{len(codes)}只有效 "
                         f"(第{bi}/{n_batches}批，"
                         f"{bi * 100 // n_batches}%)")
            # 池大了以后每批全量匹配太慢：新增≥24只有效K线才重跑一次
            if len(acc_rows) < matched_n + 24:
                continue
            matched_n = len(acc_rows)
            # 用已累计的全部本池K线跑匹配，样本随加载增多而变准
            try:
                smp = _pool_match(acc_rows, ctx["cur"], ctx["vr_now"],
                                  ctx["idx_chg_by_date"], ctx["idx_chg_today"],
                                  cur_ctx=ctx.get("cur_ctx"))
                if smp:
                    level_map[key] = smp
            except Exception:
                log.warning("池匹配失败 %s %s(跳过)", key, full,
                            exc_info=True)
            # 每次有新增样本就产出一次更新
            if level_map.get(key):
                levels = [("L1", src)] + [
                    (k, v) for k, v in sorted(level_map.items()) if v]
                t_pred, pred, clamped, tpred_bar = _fusion_prediction(
                    o_today, pre_open, live, levels)
                # 多日预测
                multi_pred = _multi_day_prediction(o_today, levels, max_days=CFG.PRED_MAX_DAYS)
                parts = [f"{LV_LABEL['L1']}{len(src)}"]
                parts += [f"{LV_LABEL[k]}{len(v)}"
                          for k, v in sorted(level_map.items()) if v]
                pool_note = "样本池: " + "+".join(parts)
                yield level_map, t_pred, pred, clamped, tpred_bar, pool_note, multi_pred
            if time.time() - t0 > 60:   # 单级超时保护（L3不限量后池更大）
                if progress:
                    progress(f"{LV_LABEL[key]}池加载超时(60s)，"
                             f"已用{len(acc_rows)}只K线继续")
                break
        if progress:
            progress(f"{LV_LABEL[key]}池完成: {len(level_map.get(key, []))}个匹配样本")


# ================= 策略消融引擎（多算法回测+防过拟合选型） =================
# 每次分析对该股近1000交易日做一次多算法消融回测：
#   候选 = L1形态up_prob / MACD / KDJ / RSI / 布林带 / MA20-60趋势 / 多维评分×3风险档
# 防过拟合：前~75%训练集选策略，后~25%验证集只报告不参与选择（前视零容忍：
# 信号只用 T 日及以前数据，信号日收盘成交）。结果缓存 meta 表，5日过期。

STRAT_TTL = 5 * 86400


def _strat_key(full):
    return "strategy:" + full


def load_strategy(full):
    """读策略缓存（meta表），5日过期返回 None。"""
    try:
        with db_conn() as conn:
            v = _get_meta(conn, _strat_key(full))
        if not v:
            return None
        d = json.loads(v)
        if time.time() - float(d.get("ts", 0)) > STRAT_TTL:
            return None
        return d
    except Exception:
        log.exception("load_strategy 失败(忽略)")
        return None


def save_strategy(full, strat):
    try:
        strat = dict(strat)
        strat["ts"] = time.time()
        with db_conn(commit=True) as conn:
            _set_meta(conn, _strat_key(full),
                      json.dumps(strat, ensure_ascii=False))
    except Exception:
        log.exception("save_strategy 失败(忽略)")


# ---- 各算法信号发生器（只用 T 日及以前数据，杜绝前视）----

def _sig_macd(rows):
    closes = [r["close"] for r in rows]
    dif, dea, _ = calc_macd(closes)
    out = []
    for i in range(9, len(rows)):
        if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            continue
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            out.append((i, rows[i]["date"], "BUY", "MACD金叉"))
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            out.append((i, rows[i]["date"], "SELL", "MACD死叉"))
    return out


def _sig_kdj(rows):
    k, d, _ = calc_kdj(rows)
    out = []
    for i in range(3, len(rows)):
        if None in (k[i], d[i], k[i - 1], d[i - 1]):
            continue
        if k[i - 1] <= d[i - 1] and k[i] > d[i] and k[i] < 45:
            out.append((i, rows[i]["date"], "BUY", "KDJ低位金叉"))
        elif k[i - 1] >= d[i - 1] and k[i] < d[i] and k[i] > 65:
            out.append((i, rows[i]["date"], "SELL", "KDJ高位死叉"))
    return out


def _sig_rsi(rows):
    closes = [r["close"] for r in rows]
    r6 = calc_rsi(closes, 6)
    out = []
    for i in range(1, len(rows)):
        if r6[i] is None or r6[i - 1] is None:
            continue
        if r6[i - 1] < 20 <= r6[i]:
            out.append((i, rows[i]["date"], "BUY", "RSI超卖回升"))
        elif r6[i - 1] > 80 >= r6[i]:
            out.append((i, rows[i]["date"], "SELL", "RSI超买回落"))
    return out


def _sig_boll(rows):
    closes = [r["close"] for r in rows]
    _, up, low = calc_boll(closes)
    out = []
    for i in range(1, len(rows)):
        if None in (up[i], low[i], up[i - 1], low[i - 1]):
            continue
        pc = rows[i - 1]["close"]
        c = rows[i]["close"]
        if pc <= low[i - 1] and c > low[i]:
            out.append((i, rows[i]["date"], "BUY", "布林下轨回升"))
        elif pc >= up[i - 1] and c < up[i]:
            out.append((i, rows[i]["date"], "SELL", "布林上轨回落"))
    return out


def _sig_ma_trend(rows):
    closes = [r["close"] for r in rows]
    ma20 = sma_period(closes, 20)
    ma60 = sma_period(closes, 60)
    out = []
    state = 0
    for i in range(60, len(rows)):
        if None in (ma20[i], ma60[i], ma20[i - 1], ma60[i - 1]):
            continue
        if ma20[i - 1] <= ma60[i - 1] and ma20[i] > ma60[i] and state != 1:
            out.append((i, rows[i]["date"], "BUY", "MA20上穿MA60"))
            state = 1
        elif ma20[i - 1] >= ma60[i - 1] and ma20[i] < ma60[i] and state != -1:
            out.append((i, rows[i]["date"], "SELL", "MA20下穿MA60"))
            state = -1
    return out


def _composite_signals(rows, rp, idx_chg_by_date=None, chip_tail=400,
                       use_chips=True):
    """多维评分信号（消融用，与GUI打分同构；筹码维度限尾段提速）。
    rp: 风险参数（buy_th/cooldown）。返回 [(i,date,"BUY"/"SELL",reason)]。"""
    n = len(rows)
    if n < 60:
        return []
    closes = [r["close"] for r in rows]
    dif, dea, _ = calc_macd(closes)
    k_, d_, _ = calc_kdj(rows)
    r6 = calc_rsi(closes, 6)
    _, b_up, b_low = calc_boll(closes)
    pdi_a, mdi_a, adx_a = calc_adx(rows)
    ma20 = sma_period(closes, 20)
    vols_d = [r.get("vol") or 0.0 for r in rows]
    try:
        chip_snaps = chip_snapshots(rows, tail=chip_tail) if use_chips \
            else {}
    except Exception:
        chip_snaps = {}
    weak = idx_chg_by_date or {}
    buy_th = rp["buy_th"]
    out = []
    prev_dir = 0
    cooldown = 0
    start = max(1, n - 1000)
    for i in range(start, n):
        if cooldown > 0:
            cooldown -= 1
            continue
        if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            continue
        sc = 0

        def _wadd(dim, pts):
            return int(round(pts * CFG.IND_W.get(dim, 1.0)))

        sc += _wadd("MACD", 2 if (dif[i - 1] <= dea[i - 1] and dif[i] > dea[i])
                    else -2 if (dif[i - 1] >= dea[i - 1] and dif[i] < dea[i])
                    else 1 if dif[i] > dea[i] else -1)
        sc += _wadd("KDJ", 2 if (k_[i - 1] <= d_[i - 1] and k_[i] > d_[i]
                                 and k_[i] < 45)
                    else -2 if (k_[i - 1] >= d_[i - 1] and k_[i] < d_[i]
                                and k_[i] > 65)
                    else 1 if k_[i] > d_[i] else -1)
        if r6[i] is not None and r6[i - 1] is not None:
            sc += _wadd("RSI", 2 if (r6[i - 1] < 20 and r6[i] >= 20)
                        else -2 if (r6[i - 1] > 80 and r6[i] <= 80)
                        else 1 if r6[i] < 30 else -1 if r6[i] > 70 else 0)
        c, cp = rows[i]["close"], rows[i - 1]["close"]
        v5 = sum(vols_d[max(0, i - 5):i]) / max(1, min(5, i))
        vr_d = vols_d[i] / v5 if v5 > 0 else 0.0
        sc += _wadd("量价", 1 if (vr_d > 1.5 and c > cp)
                    else -1 if (vr_d > 1.5 and c < cp) else 0)
        m20, m20p = ma20[i], ma20[i - 1]
        if m20 and m20p:
            sc += _wadd("MA20", 1 if (c > m20 and m20 > m20p)
                        else -1 if (c < m20 and m20 < m20p) else 0)
        snap = chip_snaps.get(rows[i]["date"])
        if snap:
            sup_i, res_i = snap[0], snap[1]
            if sup_i and c <= sup_i * 1.01:
                sc += _wadd("筹码", 1)
            elif res_i and c >= res_i * 0.99:
                sc += _wadd("筹码", -1)
        if None not in (b_up[i], b_low[i]):
            sc += _wadd("布林带", 1 if c < b_low[i]
                        else -1 if c > b_up[i] else 0)
        a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
        if None not in (a_i, p_i, m_i) and a_i >= 20:
            sc += _wadd("ADX", 1 if p_i > m_i else -1)
        day_weak = (weak.get(rows[i]["date"]) is not None
                    and weak[rows[i]["date"]] < CFG.WEAK_IDX_TH)
        th = buy_th + 1 if day_weak else buy_th
        if sc >= th and prev_dir <= 0:
            out.append((i, rows[i]["date"], "BUY", f"多维偏多({sc})"))
            prev_dir = 1
            cooldown = rp["cooldown"]
        elif sc <= CFG.SIGNAL_SCORE_SELL and prev_dir >= 0:
            out.append((i, rows[i]["date"], "SELL", f"多维偏空({sc})"))
            prev_dir = -1
            cooldown = rp["cooldown"]
    return out


ALGO_LABEL = {
    "l1_pattern": "L1形态上行概率",
    "macd": "MACD金叉/死叉",
    "kdj": "KDJ金叉/死叉",
    "rsi": "RSI超买超卖",
    "boll": "布林带回归",
    "ma_trend": "MA20/60趋势",
    "composite": "多维评分",
}


def _px_distances(rets, cur, W):
    """滑窗z-normalize后与cur的欧氏距离：返回数组（窗口k=rets[k:k+W]）。
    numpy可用时向量化（快约50倍），否则退回纯Python循环。"""
    n = len(rets)
    if n < W:
        return []
    if np is not None:
        a = np.asarray(rets, dtype=np.float64)
        wins = np.lib.stride_tricks.sliding_window_view(a, W)
        mu = wins.mean(axis=1, keepdims=True)
        sd = wins.std(axis=1, keepdims=True)
        zn = (wins - mu) / np.maximum(sd, 1e-12)
        d = np.sqrt(((zn - np.asarray(cur)) ** 2).sum(axis=1))
        return d.tolist()
    out = []
    for k in range(n - W + 1):
        w = znorm(rets[k:k + W])
        out.append(math.sqrt(sum((a - b) ** 2 for a, b in zip(cur, w))))
    return out


def _sig_l1_pattern(rows, W=None, step=5, up_th=0.6, dn_th=0.4):
    """L1形态信号：每隔step日做一次自身历史形态匹配（只用该日以前数据），
    Top-K样本次日上行概率≥60%→BUY，≤40%→SELL。"""
    W = W or W_WINDOW
    closes = [r["close"] for r in rows]
    rets = logret(closes)
    if len(rets) < 2 * W + 3:
        return []
    out = []
    for i in range(W, len(rets) - W + 1, step):
        cur = znorm(rets[i - W:i])
        d_arr = _px_distances(rets[:i], cur, W)   # 仅用 i 以前窗口（防前视）
        cand = [(d_arr[k], k) for k in range(len(d_arr))
                if k + W <= i - W]                # 样本窗口完整结束于 i 之前
        if len(cand) < 6:
            continue
        cand.sort()
        tops = cand[:CFG.TOPK]
        ups = tot = 0
        for _, k in tops:
            j = k + W                              # 窗口次日起算结果
            if j < len(rows) - 1:
                tot += 1
                if rows[j + 1]["close"] > rows[j]["close"]:
                    ups += 1
        if tot < 5:
            continue
        p = ups / tot
        if p >= up_th:
            out.append((i, rows[i]["date"], "BUY",
                        f"L1形态上行{p*100:.0f}%"))
        elif p <= dn_th:
            out.append((i, rows[i]["date"], "SELL",
                        f"L1形态上行{p*100:.0f}%"))
    return out


# ---- 区间事件回测（信号日收盘成交 + ATR止损/移动止盈，防前视）----

def _bt_events(rows, signals, rp, i0=0, i1=None, atrs=None):
    """在 rows[i0:i1] 上模拟交易。返回指标dict；交易数不足返回 None。
    atrs 可外部预计算加速。"""
    i1 = len(rows) if i1 is None else min(i1, len(rows))
    if i1 - i0 < 30:
        return None
    sig_map = {s[0]: s[2] for s in signals if i0 <= s[0] < i1}
    # ATR(14) 预计算（若未传入）
    if atrs is None:
        atrs = [0.0] * len(rows)
        for i in range(i0 + 14, i1):
            s = 0.0
            for j in range(i - 13, i + 1):
                h, l, pc = rows[j]["high"], rows[j]["low"], rows[j - 1]["close"]
                s += max(h - l, abs(h - pc), abs(l - pc))
            atrs[i] = s / 14
    eq = 1.0
    entry = None
    highest = None
    trades = []
    curve = []
    for i in range(i0, i1):
        r = rows[i]
        c, h, l = r["close"], r["high"], r["low"]
        typ = sig_map.get(i)
        if entry is not None:
            highest = max(highest, h) if highest else h
            atr_stop = (entry - rp["atr_mult"] * atrs[i]) if atrs[i] > 0 \
                else entry * 0.95
            trail_stop = (highest * rp["trail_ratio"]
                          if highest > entry * rp["trail_trigger"]
                          else atr_stop)
            if l <= trail_stop:
                exit_px = min(trail_stop, h)
                trades.append(exit_px / entry - 1)
                eq *= exit_px / entry
                entry = None
                highest = None
        if typ == "BUY" and entry is None and c:
            entry = c
            highest = h
        elif typ == "SELL" and entry:
            trades.append(c / entry - 1)
            eq *= c / entry
            entry = None
            highest = None
        curve.append(eq * (c / entry) if entry else eq)
    if len(trades) < 2:
        return None
    wins = len([t for t in trades if t > 0])
    import datetime
    try:
        d0 = datetime.date.fromisoformat(rows[i0]["date"])
        d1 = datetime.date.fromisoformat(rows[i1 - 1]["date"])
        years = max((d1 - d0).days / 365.25, 0.25)
    except Exception:
        years = max((i1 - i0) / 250.0, 0.25)
    total = curve[-1] if curve else 1.0
    ann = total ** (1 / years) - 1 if total > 0 else -1.0
    peak, mdd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return {"trades": len(trades), "wins": wins,
            "winrate": wins / len(trades),
            "total": total - 1, "ann": ann, "mdd": mdd,
            "curve": curve, "i0": i0}


def _regime_map(idx_rows, n):
    """牛市/熊市日历：指数收盘 ≥ MA120 视为牛市。返回 {date: bool}。"""
    closes = [r["close"] for r in (idx_rows or [])]
    if len(closes) < 130:
        return {}
    ma = sma_period(closes, 120)
    out = {}
    for r, m in zip(idx_rows, ma):
        if m:
            out[r["date"]] = r["close"] >= m
    return out


def _bull_bear_score(rows, curve, i0, regime):
    """分段年化收益：牛市段/熊市段（无数据段返回 None）。"""
    if not regime or not curve:
        return None, None
    bull_r, bear_r = [], []
    prev = None
    for k, i in enumerate(range(i0, min(i0 + len(curve), len(rows)))):
        day = rows[i]["date"]
        reg = regime.get(day)
        if prev is not None and prev[1] > 0:
            r = curve[k] / prev[1] - 1
            if reg is True:
                bull_r.append(r)
            elif reg is False:
                bear_r.append(r)
        prev = (day, curve[k])
    def _ann(rets):
        if len(rets) < 20:
            return None
        s = 0.0
        for r in rets:
            s += math.log1p(max(-0.95, min(r, 0.95)))
        n = len(rets)
        return math.expm1(s * 252.0 / n)
    return _ann(bull_r), _ann(bear_r)


def _precompute_atr(rows, i0=0, i1=None):
    """预计算 ATR(14)，供 run_ablation 批量回测复用。"""
    i1 = len(rows) if i1 is None else min(i1, len(rows))
    atrs = [0.0] * len(rows)
    for i in range(i0 + 14, i1):
        s = 0.0
        for j in range(i - 13, i + 1):
            h, l, pc = rows[j]["high"], rows[j]["low"], rows[j - 1]["close"]
            s += max(h - l, abs(h - pc), abs(l - pc))
        atrs[i] = s / 14
    return atrs


def run_ablation(full, rows, idx_rows=None, progress=None):
    """多算法消融回测（近1000交易日）。训练集选策略/验证集验证，防过拟合。
    v2026-09-12: 预计算ATR + 线程池并行候选评估，速度提升。

    返回 {"mode_candidates": {保守:strat, 稳健:strat, 激进:strat},
          "ts": ..., "bars": n, "train_n":, "val_n":} 或 None。
    strat = {"algo","mode","params","train","val","bull","bear","label"}"""
    rows = [r for r in rows if r.get("close") and r["close"] > 0]
    if len(rows) > 1000:
        rows = rows[-1000:]
    if len(rows) < 200:
        return None
    n = len(rows)
    val_n = max(200, n // 4)
    split = n - val_n
    regime = _regime_map(idx_rows, n)
    # 预计算ATR，所有候选复用
    atrs = _precompute_atr(rows, 0, n)
    if progress:
        progress("策略消融回测中(近1000交易日)...")

    # 生成所有基础信号（只生成一次）
    sig_cache = {}
    gens = {
        "macd": lambda: _sig_macd(rows),
        "kdj": lambda: _sig_kdj(rows),
        "rsi": lambda: _sig_rsi(rows),
        "boll": lambda: _sig_boll(rows),
        "ma_trend": lambda: _sig_ma_trend(rows),
        "l1_pattern": lambda: _sig_l1_pattern(rows),
    }
    for algo, gen in gens.items():
        try:
            sig_cache[algo] = gen()
        except Exception:
            log.warning("消融信号生成失败 %s", algo, exc_info=True)
            sig_cache[algo] = []

    # 任务列表：(algo, mode, rp, is_composite)
    tasks = []
    for algo in gens:
        for mode, rp in CFG.RISK_PARAMS.items():
            tasks.append((algo, mode, rp, False))
    for mode, rp in CFG.RISK_PARAMS.items():
        tasks.append(("composite", mode, rp, True))

    def _eval_task(task):
        algo, mode, rp, is_comp = task
        if is_comp:
            try:
                sigs = _composite_signals(rows, rp, idx_chg_by_date=None)
            except Exception:
                log.warning("composite信号生成失败 %s", mode, exc_info=True)
                return None
        else:
            sigs = sig_cache.get(algo)
            if not sigs:
                return None
        tr = _bt_events(rows, sigs, rp, 0, split, atrs=atrs)
        va = _bt_events(rows, sigs, rp, split, n, atrs=atrs)
        if not tr:
            return None
        bull, bear = _bull_bear_score(rows, tr["curve"], tr["i0"], regime)
        label = (f"多维评分·{mode}" if is_comp
                 else f"{ALGO_LABEL.get(algo, algo)}·{mode}")
        return {"algo": algo, "mode": mode, "params": dict(rp),
                "label": label,
                "train": {k: v for k, v in tr.items() if k != "curve"},
                "val": ({k: v for k, v in va.items() if k != "curve"}
                        if va else None),
                "bull": bull, "bear": bear}

    cands = []
    # 使用线程池并行评估候选（I/O轻、计算密集，GIL会部分释放）
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as exe:
        for r in exe.map(_eval_task, tasks):
            if r is not None:
                cands.append(r)

    if not cands:
        return None
    # 训练集选型（验证集绝不参与选择），每档最少3笔交易
    def _pick(key):
        pool = [c for c in cands if c["train"]["trades"] >= 3]
        if not pool:
            pool = cands
        comp = [c for c in pool if c["algo"] == "composite"
                and c["mode"] == "稳健"]
        if key == "保守":
            pool.sort(key=lambda c: (c["train"]["mdd"],
                                     -c["train"]["winrate"]))
        elif key == "激进":
            pool.sort(key=lambda c: -c["train"]["ann"])
        else:   # 稳健：收益回撤比
            pool.sort(key=lambda c: -(c["train"]["ann"]
                                      / max(abs(c["train"]["mdd"]), 0.05)))
        return dict(pool[0]) if pool[0] else dict(comp[0])

    out = {"mode_candidates": {"保守": _pick("保守"), "稳健": _pick("稳健"),
                               "激进": _pick("激进")},
           "ts": time.time(), "bars": n, "train_n": split,
           "val_n": n - split}
    if progress:
        progress(f"策略消融完成：{len(cands)}个候选 (训练{split}/验证{n - split})")
    return out


# ================= 分析 =================

def _fusion_prediction(o_today, pre_open, live, levels):
    """三级融合。盘中用open->close分布；盘前/收盘后用close->next-close分布。"""
    LW=_dynamic_lv_weights(levels); tot_w=sum(LW.get(k,0.0) for k,_ in levels) or 1.0
    target=("oc","oh","ol") if live is not None else ("cl","hi","lo")
    def _wfield(field):
        pairs=[]
        for k,smp in levels:
            lw=LW.get(k,0.0)/tot_w; valid=[s for s in smp if s.get(field) is not None]
            tw=sum(max(1e-9,s.get("weight",1.0)) for s in valid) or 1.0
            pairs.extend((s[field],lw*s.get("weight",1.0)/tw) for s in valid)
        return pairs
    PS=(10,25,50,75,90); cf,hf,lf=(f"n1_{x}" for x in target)
    cp,hp,lp=_wfield(cf),_wfield(hf),_wfield(lf)
    if not cp:
        # 无有效样本：输出平线预测而非空区间，保证报告/图表不崩
        ps=(10,25,50,75,90)
        flat={"cl":{p:o_today for p in ps},"hi":{p:o_today for p in ps},
              "lo":{p:o_today for p in ps},"up_prob":0.5,"confidence":None}
        return (flat,
                {"date":"T+1预测","open":o_today,"close":o_today,
                 "high":o_today,"low":o_today,"vol":None,
                 "confidence":None},
                False, None)
    # 区间校准（INTERVAL_K，n=4500标定）：分位偏离P50放大，消除过窄
    K=CFG.INTERVAL_K
    c50=wpct(cp,50); h50=wpct(hp or cp,50); l50=wpct(lp or cp,50)
    t_pred={"cl":{p:o_today*(1+c50+K*(wpct(cp,p)-c50)) for p in PS},
            "hi":{p:o_today*(1+h50+K*(wpct(hp or cp,p)-h50)) for p in PS},
            "lo":{p:o_today*(1+l50+K*(wpct(lp or cp,p)-l50)) for p in PS},
            "up_prob":sum(w for v,w in cp if v>0)}
    confidence=_calculate_confidence(levels); t_pred["confidence"]=confidence
    # ---- T+5 累计预测：样本 close→close 5日累计（n1..n5全有才计入）----
    def _cum5(s):
        f=[s.get(f"n{d}_cl") for d in range(1,6)]
        if any(x is None for x in f):
            return None
        c=1.0
        for x in f:
            c*=(1+x)
        return c-1.0
    t5pairs=[]
    for k,smp in levels:
        lw=LW.get(k,0.0)/tot_w
        v=[(c,s.get("weight",1.0)) for s in smp if (c:=_cum5(s)) is not None]
        if not v:
            continue
        tw=sum(w for _,w in v) or 1.0
        t5pairs.extend((c, lw*w/tw) for c,w in v)
    if t5pairs:
        K5=CFG.INTERVAL_K5
        c50=wpct(t5pairs,50)
        t5={"cl":{p:o_today*(1+c50+K5*(wpct(t5pairs,p)-c50)) for p in PS},
            "up_prob":sum(w for v,w in t5pairs if v>0),"n":len(t5pairs)}
        t_pred["t5"]=t5
    else:
        t_pred["t5"]=None
    clamped=False
    if live is not None:
        clamped=True
        for pp in PS:
            t_pred["hi"][pp]=round(max(t_pred["hi"][pp],live["high"]),2); t_pred["lo"][pp]=round(min(t_pred["lo"][pp],live["low"]),2)
    pred={"date":"T日预测" if live is not None else ("今日(T)" if pre_open else "T+1预测"),"open":o_today,"close":t_pred["cl"][50],"high":t_pred["hi"][50],"low":t_pred["lo"][50],"vol":None,"confidence":confidence}
    tpred_bar={"date":"T日预测","open":o_today,"close":t_pred["cl"][50],"high":t_pred["hi"][50],"low":t_pred["lo"][50],"vol":None,"confidence":confidence} if live is not None else None
    return t_pred,pred,clamped,tpred_bar


def _calculate_confidence(levels):
    """计算预测置信度，基于多个维度评估样本质量。"""
    if not CFG.CONFIDENCE_ENABLED:
        return None
    
    confidence_score = 1.0
    total_samples = sum(len(smp) for _, smp in levels)
    
    # 1. 样本数量因子
    if total_samples < CFG.MIN_SAMPLES_REQUIRED:
        confidence_score *= 0.5
    elif total_samples < CFG.MIN_SAMPLES_REQUIRED * 2:
        confidence_score *= 0.8
    
    # 2. 相似度一致性因子（所有样本的相似度分数方差）
    if CFG.SIMILARITY_WEIGHTING and total_samples > 0:
        all_scores = []
        for _, smp in levels:
            all_scores.extend([s.get("similarity_score", float('inf')) for s in smp])
        
        if len(all_scores) > 1:
            mean_score = sum(all_scores) / len(all_scores)
            variance = sum((s - mean_score) ** 2 for s in all_scores) / len(all_scores)
            std_dev = variance ** 0.5
            
            # 相似度方差越小，置信度越高
            consistency_factor = max(0.6, 1.0 - min(std_dev / 2.0, 0.4))
            confidence_score *= consistency_factor
    
    # 3. 权重分布因子（有效权重占比）
    if CFG.SIMILARITY_WEIGHTING and total_samples > 0:
        all_weights = []
        for _, smp in levels:
            all_weights.extend([s.get("weight", 1.0) for s in smp])
        
        # 计算高权重样本占比（权重>1.0的样本）
        high_weight_count = sum(1 for w in all_weights if w > 1.0)
        weight_quality = high_weight_count / len(all_weights) if all_weights else 0.5
        confidence_score *= (0.7 + 0.3 * weight_quality)  # 范围0.7-1.0
    
    # 4. 层级完整性因子（三个层级是否都有样本）
    active_levels = sum(1 for _, smp in levels if smp)
    if active_levels == 3:
        confidence_score *= 1.0  # 完整的三级样本
    elif active_levels == 2:
        confidence_score *= 0.9  # 缺少一个层级
    elif active_levels == 1:
        confidence_score *= 0.7  # 只有一个层级
    
    # 归一化置信度分数到0-1范围
    confidence_score = max(0.0, min(1.0, confidence_score))
    
    # 根据置信度分数返回等级
    if confidence_score >= CFG.MEDIUM_CONFIDENCE_SCORE:
        confidence_level = "高"
    elif confidence_score >= CFG.LOW_CONFIDENCE_SCORE:
        confidence_level = "中"
    else:
        confidence_level = "低"
    
    return {
        "score": confidence_score,
        "level": confidence_level,
        "total_samples": total_samples,
        "active_levels": active_levels,
    }


def _multi_day_prediction(o_today, levels, max_days=10):
    """多日预测：基于样本的统计分布，预测T+1到T+max_days的走势。
    带均值回归修正：长期预测向零回归，减少累积误差。
    使用动态三级权重 + 样本质量权重。"""
    LW = _dynamic_lv_weights(levels)
    tot_w = sum(LW.get(k, 0.0) for k, _ in levels) or 1.0

    def _wfield(field):
        pairs = []
        for k, smp in levels:
            lw = LW.get(k, 0.0) / tot_w
            if CFG.SIMILARITY_WEIGHTING and smp:
                tw = sum(s.get("weight", 1.0) for s in smp
                         if s.get(field) is not None) or 1.0
                for s in smp:
                    if s.get(field) is not None:
                        pairs.append((s[field], lw * s.get("weight", 1.0) / tw))
            else:
                w = lw / len(smp) if smp else 0
                pairs.extend((s[field], w) for s in smp if s.get(field) is not None)
        return pairs
    
    PS = (10, 25, 50, 75, 90)
    multi_pred = []
    
    for d in range(1, max_days + 1):
        cl_field = f"n{d}_cl"
        hi_field = f"n{d}_hi"
        lo_field = f"n{d}_lo"
        
        cl_pairs = _wfield(cl_field)
        hi_pairs = _wfield(hi_field)
        lo_pairs = _wfield(lo_field)
        
        if not cl_pairs:
            break
        
        # 区间校准：分位偏离P50放大 INTERVAL_K 倍
        K = CFG.INTERVAL_K
        c50 = wpct(cl_pairs, 50)
        h50 = wpct(hi_pairs or cl_pairs, 50)
        l50 = wpct(lo_pairs or cl_pairs, 50)
        day_pred = {
            "day": d,
            "label": f"T+{d}",
            "cl": {p: c50 + K * (wpct(cl_pairs, p) - c50) for p in PS},
            "hi": {p: h50 + K * (wpct(hi_pairs or cl_pairs, p) - h50)
                   for p in PS},
            "lo": {p: l50 + K * (wpct(lo_pairs or cl_pairs, p) - l50)
                   for p in PS},
            "up_prob": len([p for p, w in cl_pairs if p > 0]) / len(cl_pairs) if cl_pairs else 0.5,
        }
        
        # 均值回归修正：预测天数越多，向零回归越强
        decay = 1.0 - 0.04 * (d - 1)
        decay = max(0.6, decay)
        
        # 计算累计涨跌幅
        if d == 1:
            day_pred["cum_cl"] = day_pred["cl"][50]
            day_pred["cum_hi"] = day_pred["hi"][75]
            day_pred["cum_lo"] = day_pred["lo"][25]
        else:
            prev = multi_pred[-1]
            day_pred["cum_cl"] = (1 + prev["cum_cl"]) * (1 + day_pred["cl"][50]) - 1
            day_pred["cum_hi"] = (1 + prev["cum_hi"]) * (1 + day_pred["hi"][75]) - 1
            day_pred["cum_lo"] = (1 + prev["cum_lo"]) * (1 + day_pred["lo"][25]) - 1
        
        # 应用均值回归修正
        day_pred["cum_cl_raw"] = day_pred["cum_cl"]
        day_pred["cum_cl"] = day_pred["cum_cl"] * decay
        day_pred["cum_hi"] = day_pred["cum_hi"] * decay
        day_pred["cum_lo"] = day_pred["cum_lo"] * decay
        
        # 预测价格
        day_pred["price_cl"] = o_today * (1 + day_pred["cum_cl"])
        day_pred["price_hi"] = o_today * (1 + day_pred["cum_hi"])
        day_pred["price_lo"] = o_today * (1 + day_pred["cum_lo"])
        
        multi_pred.append(day_pred)
    
    return multi_pred


def analyze(full, progress=None, quick=False):
    """全量分析，切片交给GUI。
    quick=True 只做快速预览（本股缓存 + L1预测，秒开），完整历史/样本池
    由后台增量加载器继续补齐并实时更新预测K线。"""
    W = W_WINDOW
    # ---- 并发拉取全部数据源（个股行情/K线、上证行情/K线、板块、样本池）----
    now_ts = time.time()
    with _STATE_LOCK:
        sec_cached = _SECTOR_CACHE.get(full)
        sec_hit = bool(sec_cached and now_ts - sec_cached[0] < _SECTOR_CACHE_TTL)
    ex = _SHARED_EX              # 全局共享线程池
    f_q = ex.submit(fetch_quote, full)
    if CACHE_OK:
        f_rows = ex.submit(get_daily, full)
    else:
        f_rows = ex.submit(fetch_daily, full)
    f_iq = ex.submit(fetch_quote_cached, "sh000001")
    if CACHE_OK:
        f_ir = ex.submit(get_daily, "sh000001")
    else:
        f_ir = ex.submit(fetch_daily, "sh000001")
    f_sec = None if sec_hit else ex.submit(fetch_sector_context, full)
    pool_info = None
    if CACHE_OK and not quick and stocks_age() < STOCKS_TTL * 4:
        try:
            pool_info = ex.submit(pool_codes, full).result(timeout=15)
        except Exception:
            log.warning("analyze: pool_codes 失败 %s", full, exc_info=True)
            pool_info = None
    q = f_q.result()
    rows = f_rows.result()

    # 识别今日盘中bar（缓存库只存已收盘日K，实时bar由快照合成）
    today_str = time.strftime("%Y-%m-%d")
    today_compact = today_str.replace("-", "")
    live = None
    snap_full = (q.get("time") or "")
    snap_d = snap_full[:8]
    try:
        hhmm = int(snap_full[8:12])
    except ValueError:
        hhmm = 0
    if (snap_d == today_compact and q["price"] > 0 and hhmm >= 925):
        pc0 = q["prev_close"] or (rows[-1]["close"] if rows else 0)
        lo0 = q["low"] if q["low"] > 0 else min(q["price"], q["open"] or q["price"])
        live = {"date": today_str, "open": q["open"] or pc0,
                "close": q["price"],
                "high": max(q["high"], q["price"]),
                "low": min(lo0, q["price"]), "vol": 0.0}
    had_today_bar = live is not None
    if len(rows) < 30:
        raise ValueError(
            f"该股上市不足30个交易日(现仅{len(rows)}根日K)，"
            "暂无法统计预测，请过阵子再来")

    try:
        iq = f_iq.result()
        idx_chg_today = ((iq["price"] / iq["prev_close"]) * 100 - 100
                         if iq["prev_close"] else 0.0)
    except Exception:
        idx_chg_today = None
    try:
        idx_rows = f_ir.result()
        idx_chg_by_date = {
            b["date"]: (b["close"] / a["close"]) * 100 - 100
            for a, b in zip(idx_rows, idx_rows[1:])
        }
    except Exception:
        idx_chg_by_date = {}
    try:
        if sec_hit:
            sec_name, sec_chg_by_date, sec_chg_today = sec_cached[1]
        else:
            sec_name, sec_chg_by_date, sec_chg_today = f_sec.result(
                timeout=8)
            if sec_name:
                _SECTOR_CACHE[full] = (
                    time.time(),
                    (sec_name, sec_chg_by_date, sec_chg_today))
    except Exception:
        sec_name, sec_chg_by_date, sec_chg_today = None, {}, None

    closes_m = [r["close"] for r in rows]
    rets = logret(closes_m, is_etf=_is_etf(full))

    vols_m = [r.get("vol") or 0.0 for r in rows]
    vr_arr = [vol_ratio_at(vols_m, k) for k in range(len(vols_m))]
    vr_now = vr_arr[-1]
    cur_regime = vol_regime(vr_now)

    # 新股自适应：历史不足时缩短匹配窗口（最低5日）。
    # 不重叠匹配要求窗口数 = len(rets) - 2W + 1，至少保留2个样本窗口
    while W > 5 and len(rets) - 2 * W + 1 < 2:
        W -= 1
    if len(rets) - 2 * W + 1 < 1:
        raise ValueError("历史K线过短，暂无法统计预测")

    def _dist_vol(i):
        vr_i = vr_arr[i]
        if vr_now is None or vr_i is None:
            return None
        return abs(math.log(max(vr_now, 1e-6) / max(vr_i, 1e-6)))

    def _dist_idx(i):
        ic = idx_chg_by_date.get(rows[i]["date"])
        if ic is None or idx_chg_today is None:
            return None
        return abs(ic - idx_chg_today)          # 百分点差

    def _dist_sec(i):
        sc = sec_chg_by_date.get(rows[i]["date"])
        if sc is None or sec_chg_today is None:
            return None
        return abs(sc - sec_chg_today)

    cur = znorm(rets[-W:])
    # 今日侧多维特征（供 L1/L2/L3 匹配共用）
    cur_ctx = _cur_context(rows, rets, vols_m, closes_m)
    # 样本侧特征预计算（O(1)查表，避免逐日重复计算）
    _l1_rsi = [rsi_at(closes_m, k) for k in range(len(closes_m))]
    _l1_vola = [vola_at(rets, k) for k in range(len(rets))]
    _l1_volchg = [volchg_at(vols_m, k) for k in range(len(vols_m))]
    _l1_weekly = [weekly_ctx(rows, k, CFG.WEEKLY_N) for k in range(len(rows))]
    _l1_struct = [candle_feats(rows, k) for k in range(len(rows))]
    sims = []
    d_px_arr = _px_distances(rets, cur, W)   # numpy向量化（可用时）
    for i in range(W, len(rets) - W + 1):
        d_px = d_px_arr[i - W]
        d_v = _dist_vol(i)
        d_i = _dist_idx(i)
        d_s = _dist_sec(i)
        d_x = _dist_extra(cur_ctx, {
            "struct": _l1_struct[i], "vola": _l1_vola[i],
            "rsi": _l1_rsi[i], "volchg": _l1_volchg[i],
            "weekly": _l1_weekly[i]})
        score = (d_px
                 + (0.6 * min(d_v, 2.5) if d_v is not None else 0.30)
                 + (min(1.5, 0.3 * d_i) if d_i is not None else 0.40)
                 + (min(1.2, 0.25 * d_s) if d_s is not None else 0.30)
                 + d_x)
        sims.append((score, i))
    top = heapq.nsmallest(CFG.CANDIDATE_TOPK, sims, key=lambda x: x[0])

    prev_close = q["prev_close"] or closes_m[-1]
    # 盘前检测：快照日期已切到今日，但日K还没有今日bar → 尚未开盘，
    # 无今开可锚，改锚昨收（否则会把上一交易日的开价误当"今开"）
    snap_d = (q.get("time") or "")[:8].replace("-", "")
    today_compact = today_str.replace("-", "")
    pre_open = (not had_today_bar
                and snap_d >= today_compact)
    if pre_open:
        o_today = prev_close
        anchor = "昨收(未开盘)"
    elif live is not None:
        o_today = q["open"] or prev_close
        anchor = "今开"
    else:
        o_today = prev_close
        anchor = "今收"
    gap_today = (o_today / prev_close - 1) * 100

    # 市场阶段（按行情快照时间）
    phase = market_phase_text(q.get("time"))
    next_label = "今日(T)" if pre_open else "次日(T+1)"

    samples = []
    max_pred_days = 10  # 最多预测10天
    for score, i in top:
        r = rows[i]
        ic = idx_chg_by_date.get(r["date"])
        sc = sec_chg_by_date.get(r["date"])
        sample = {
            "t_date": r["date"],
            "vr": vr_arr[i],
            "idx_chg": (ic - idx_chg_today
                        if (ic is not None and idx_chg_today is not None)
                        else None),
            "sec_d": (sc - sec_chg_today
                      if (sc is not None and sec_chg_today is not None)
                      else None),
            "gap": rows[i + 1]["open"] / r["close"] - 1 if i + 1 < len(rows) else None,
            "similarity_score": score,
            "weight": (math.exp(-min(max(0.0, score - top[0][0]), 6.0) / 0.9)
                       if CFG.SIMILARITY_WEIGHTING else 1.0),
            "_match_i": i,        }
        # 时间衰减（L1样本；与_pool_match同款，消融回测证实有益）
        if CFG.TIME_DECAY_ENABLED:
            try:
                age = (time.mktime(time.strptime(
                    time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
                    - time.mktime(time.strptime(r["date"], "%Y-%m-%d"))) / 86400.0
                if age > CFG.TIME_DECAY_DAYS:
                    sample["weight"] *= max(
                        CFG.TIME_DECAY_RATE,
                        1.0 - (age - CFG.TIME_DECAY_DAYS) / 365.0 * 0.5)
            except (ValueError, TypeError):
                pass
        # 扩展样本：记录T+1到T+max_pred_days的相对前日收盘涨跌幅
        # 与 Open→Close/High/Low（盘中预测用，逻辑与今开锚定一致）
        for d in range(1, max_pred_days + 1):
            if i + d < len(rows):
                nd = rows[i + d]
                prev_c = rows[i + d - 1]["close"] if i + d - 1 >= 0 else r["close"]
                op = nd.get("open") or prev_c
                sample[f"n{d}_date"] = nd["date"]
                sample[f"n{d}_cl"] = nd["close"] / prev_c - 1
                sample[f"n{d}_hi"] = nd["high"] / prev_c - 1
                sample[f"n{d}_lo"] = nd["low"] / prev_c - 1
                sample[f"n{d}_oc"] = nd["close"] / op - 1 if op > 0 else None
                sample[f"n{d}_oh"] = nd["high"] / op - 1 if op > 0 else None
                sample[f"n{d}_ol"] = nd["low"] / op - 1 if op > 0 else None
            else:
                for suf in ("date", "cl", "hi", "lo", "oc", "oh", "ol"):
                    sample[f"n{d}_{suf}"] = None
        samples.append(sample)
    # 样本分层筛选：① 量能状态+大盘涨跌接近 ② 开盘缺口接近 ③ 全部
    for s in samples:
        s["regime"] = vol_regime(s.get("vr"))
    sel_ctx = [
        s for s in samples
        if s["regime"] == cur_regime
        and s["idx_chg"] is not None and abs(s["idx_chg"]) <= 0.8
        and (s["sec_d"] is None or abs(s["sec_d"]) <= 1.2)
    ]
    sel_gap = [s for s in samples if abs(s["gap"] * 100 - gap_today) <= 1.0]
    if len(sel_ctx) >= 3:
        src, filter_note = sel_ctx, f"量能({cur_regime})+大盘(±0.8pp)筛选"
    elif len(sel_gap) >= 3:
        src, filter_note = sel_gap, "按开盘缺口筛选"
    else:
        src, filter_note = samples, "使用全部样本"
    # 去重挑选：间隔 W//2 → 不足则分级放宽(3/1)，避免样本被砍光
    src_sorted = sorted(src, key=lambda x: x.get("similarity_score", 9.0))
    picked = []

    def _pick(gap):
        pl, used = [], []
        for s in src_sorted:
            ii = s.get("_match_i")
            if ii is not None and any(abs(ii - j) < gap for j in used):
                continue
            pl.append(s)
            if ii is not None:
                used.append(ii)
            if len(pl) >= TOPK:
                break
        return pl

    for gap in (max(3, W // 2), 3, 1):
        picked = _pick(gap)
        if len(picked) >= min(TOPK, 6):
            break
    if picked:
        src = picked

    # ---- 二三级样本池：L2 同行业(传统行业+ETF) / L3 已删 ----
    # 题材行业/ETF 只用 L1（交易回测：题材L2无增益，ETF L2劣于持有）
    level_map = {}
    pool_note = ""
    my_info = get_stock_info(full) or {}
    solo_l1 = _is_etf(full) or _is_theme_industry(my_info.get("industry"))
    if solo_l1:
        pool_note = "样本池: 题材/ETF仅L1"
    elif CACHE_OK and pool_info:
        try:
            if progress:
                progress("拉取同行/同市值层K线(首次回填较慢)...")
            level_map = _load_pools(pool_info, cur, vr_now,
                                    idx_chg_by_date, idx_chg_today,
                                    progress, cur_ctx=cur_ctx)
            parts = [f"{LV_LABEL['L1']}{len(src)}"]
            parts += [f"{LV_LABEL[k]}{len(v)}"
                      for k, v in sorted(level_map.items()) if v]
            pool_note = "样本池: " + "+".join(parts)
        except Exception as e:
            pool_note = f"样本池不可用({e.__class__.__name__})"

    # ---- 三级加权融合：L1 0.6 / L2 0.3 / L3 0.1 ----
    levels = [("L1", src)] + [(k, v) for k, v in sorted(level_map.items())
                              if v]
    t_pred, pred, clamped, tpred_bar = _fusion_prediction(
        o_today, pre_open, live, levels)
    
    # ---- 多日预测：T+1到T+10 ----
    multi_pred = _multi_day_prediction(o_today, levels, max_days=CFG.PRED_MAX_DAYS)

    # 指标基于 匹配历史(+今日盘中) 计算
    disp_rows = rows + ([live] if live else [])
    closes_i = [r["close"] for r in disp_rows]
    dif, dea, mhist = calc_macd(closes_i)
    k_, d_, j_ = calc_kdj(disp_rows)
    r6, r12 = calc_rsi(closes_i, 6), calc_rsi(closes_i, 12)
    b_mid, b_up, b_low = calc_boll(closes_i)
    pdi_a, mdi_a, adx_a = calc_adx(disp_rows)
    mas = {n: sma_period(closes_i, n) for n in MA_COLORS}

    # ---- 所选策略（meta缓存，5日过期；无缓存用默认多维·稳健）----
    strat = load_strategy(full)
    rp = dict(strat["params"]) if (strat and strat.get("params")) \
        else CFG.risk_params()
    sel_algo = (strat or {}).get("algo", "composite")
    if sel_algo not in ALGO_LABEL:
        sel_algo = "composite"

    signals = []
    # ---- 指标型策略：直接按该算法规则生成历史买卖点（近250根，同策略）----
    if sel_algo in ("macd", "kdj", "rsi", "boll", "ma_trend", "l1_pattern"):
        try:
            raw = {"macd": _sig_macd, "kdj": _sig_kdj, "rsi": _sig_rsi,
                   "boll": _sig_boll, "ma_trend": _sig_ma_trend,
                   "l1_pattern": _sig_l1_pattern}[sel_algo](disp_rows)
            cut = max(1, len(disp_rows) - 250)
            signals = [s for s in raw if s[0] >= cut]
        except Exception:
            log.exception("策略%s信号生成失败(回退多维评分)", sel_algo)
            sel_algo = "composite"
    # ---- 多维打分：每日综合评分，方向切换时生成买卖信号 ----
    # 评分维度：MACD趋势、KDJ状态、RSI超买超卖、量价配合、MA20趋势、
    #          筹码位置、统计偏多/偏空；合计≥2→多头信号，≤-2→空头信号
    _bull_scores = []   # (index, date, score, reasons)
    start = max(1, len(disp_rows) - 120) if sel_algo == "composite" \
        else len(disp_rows)      # 指标型策略跳过多维打分循环
    vols_d = [r.get("vol") or 0.0 for r in disp_rows]
    try:
        chip_snaps = chip_snapshots(disp_rows, tail=120)
    except Exception:
        chip_snaps = {}

    for i in range(start, len(disp_rows)):
        if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            _bull_scores.append((i, disp_rows[i]["date"], 0, []))
            continue
        sc = 0
        reasons = []

        def _wadd(dim, pts, reason=None):
            """按 CFG.IND_W 权重加权计分（四舍五入取整，保留符号）。"""
            nonlocal sc
            sc += int(round(pts * CFG.IND_W.get(dim, 1.0)))
            if reason and pts:
                reasons.append(reason)

        # MACD（权重1.1：趋势主指标）
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            _wadd("MACD", 2, "MACD金叉")
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            _wadd("MACD", -2, "MACD死叉")
        elif dif[i] > dea[i]:
            _wadd("MACD", 1, "DIF>DEA")
        else:
            _wadd("MACD", -1, "DIF<DEA")
        # KDJ（权重0.9：摆动指标，横盘易钝化）
        if k_[i - 1] <= d_[i - 1] and k_[i] > d_[i] and k_[i] < 45:
            _wadd("KDJ", 2, "KDJ低位金叉")
        elif k_[i - 1] >= d_[i - 1] and k_[i] < d_[i] and k_[i] > 65:
            _wadd("KDJ", -2, "KDJ高位死叉")
        elif k_[i] > d_[i]:
            _wadd("KDJ", 1)
        else:
            _wadd("KDJ", -1)
        # RSI（权重0.9）
        if r6[i] is not None and r6[i - 1] is not None:
            if r6[i - 1] < 20 and r6[i] >= 20:
                _wadd("RSI", 2, "RSI超卖回升")
            elif r6[i - 1] > 80 and r6[i] <= 80:
                _wadd("RSI", -2, "RSI超买回落")
            elif r6[i] < 30:
                _wadd("RSI", 1)
            elif r6[i] > 70:
                _wadd("RSI", -1)
        # 量价（权重1.0）
        c, cp = disp_rows[i]["close"], disp_rows[i - 1]["close"]
        v5 = sum(vols_d[max(0, i - 5):i]) / max(1, min(5, i))
        vr_d = vols_d[i] / v5 if v5 > 0 else 0.0
        if vr_d > 1.5 and c > cp:
            _wadd("量价", 1, "放量上涨")
        elif vr_d > 1.5 and c < cp:
            _wadd("量价", -1, "放量下跌")
        # MA20趋势（权重1.0）
        ma20, ma20p = mas[20][i], mas[20][i - 1]
        if ma20 and ma20p:
            if c > ma20 and ma20 > ma20p:
                _wadd("MA20", 1)
            elif c < ma20 and ma20 < ma20p:
                _wadd("MA20", -1)
        # 筹码（权重0.8）
        snap = chip_snaps.get(disp_rows[i]["date"])
        if snap:
            sup_i, res_i = snap[0], snap[1]
            if sup_i and c <= sup_i * 1.01:
                _wadd("筹码", 1, "贴近支撑")
            elif res_i and c >= res_i * 0.99:
                _wadd("筹码", -1, "贴近压力")
        # 布林带（权重0.8：均值回归参考，震荡市才准）
        bu_i, bl_i = b_up[i], b_low[i]
        if None not in (bu_i, bl_i):
            if c < bl_i:
                _wadd("布林带", 1, "布林下轨超卖")
            elif c > bu_i:
                _wadd("布林带", -1, "布林上轨超买")
            elif (disp_rows[i - 1]["close"] <= (b_low[i - 1] or 0)
                    and c > bl_i):
                _wadd("布林带", 1, "布林下轨回升")
            elif (disp_rows[i - 1]["close"] >= (b_up[i - 1] or 1e18)
                    and c < bu_i):
                _wadd("布林带", -1, "布林上轨回落")
        # ADX（权重0.8：趋势强度过滤——只有 ADX≥20 趋势成立时，
        # DI 方向才计分；横盘时不贡献分数）
        a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
        if None not in (a_i, p_i, m_i) and a_i >= 20:
            if p_i > m_i:
                _wadd("ADX", 1, "ADX趋势偏多" if a_i >= 25 else None)
            elif m_i > p_i:
                _wadd("ADX", -1, "ADX趋势偏空" if a_i >= 25 else None)
        # 统计样本维度不放历史打分：今日匹配样本不能用于标注过去（防前视）
        # 最新一天的样本倾向已由综合评估中的"统计预测"维度体现
        _bull_scores.append((i, disp_rows[i]["date"], sc, reasons))

    # 方向切换触发：多头得分≥2且前一次信号为空头→BUY；空头得分≤-2且前一次为多头→SELL
    prev_dir = 0   # 0=无信号, 1=多头, -1=空头
    cooldown = 0
    _bear_words = {"DIF<DEA", "放量下跌", "贴近压力",
                   "布林上轨超买", "布林上轨回落"}
    _bull_words = {"DIF>DEA", "放量上涨", "贴近支撑",
                   "布林下轨超卖", "布林下轨回升"}

    def _weak_day(day):
        """该交易日的市场是否弱势——只用当日(及以前)的大盘/板块数据，
        不再引用 idx_chg_today/sec_chg_today 全局值，消除历史信号前视。"""
        ic = idx_chg_by_date.get(day)
        sc = sec_chg_by_date.get(day)
        weak = False
        if ic is not None and ic < CFG.WEAK_IDX_TH:
            weak = True
        if sc is not None and sc < CFG.WEAK_SEC_TH:
            weak = True
        return weak

    for idx_i, day, sc, reasons in _bull_scores:
        if cooldown > 0:
            cooldown -= 1
            continue

        # 弱势行情过滤：当日大盘弱势时 BUY 加严；买入阈值/冷却按所选策略
        buy_threshold = (rp["buy_th"] + 1) if _weak_day(day) else rp["buy_th"]
        sell_threshold = CFG.SIGNAL_SCORE_SELL

        if sc >= buy_threshold and prev_dir <= 0:
            bull_r = [r for r in reasons if r not in _bear_words]
            reason_str = "多维偏多 " + " ".join(bull_r or reasons)
            if _weak_day(day):
                reason_str += " [弱势谨慎]"
            signals.append((idx_i, day, "BUY", reason_str))
            prev_dir = 1
            cooldown = rp["cooldown"]
        elif sc <= sell_threshold and prev_dir >= 0:
            bear_r = [r for r in reasons if r not in _bull_words]
            signals.append((idx_i, day, "SELL",
                            "多维偏空 " + " ".join(bear_r or reasons)))
            prev_dir = -1
            cooldown = CFG.SIGNAL_COOLDOWN

    # ---- 波段适合度路由：仅多维评分策略时替换信号；指标型策略尊重用户选择 ----
    band_score = _band_fit_score(disp_rows, mas, vr_arr)
    band_fit = band_score >= CFG.BAND_FIT_MIN
    if sel_algo != "composite":
        band_algo = f"策略·{ALGO_LABEL.get(sel_algo, sel_algo)}"
    elif band_fit:
        band_algo = "波段·多维融合"
    else:
        # 不适合波段：改用长周期 MA20/MA60 趋势跟踪，信号少而稳
        t_signals = _trend_track_signals(disp_rows, mas,
                                         idx_chg_by_date, idx_chg_today)
        if t_signals:
            signals = t_signals
            band_algo = "趋势跟踪·MA20/60"
        else:
            # 趋势跟踪零信号（震荡股无MA金叉）→ 回退多维信号，避免无买卖点
            band_algo = "趋势跟踪·MA20/60（无信号→回退多维）"
    band_note = f"波段适合度 {band_score:.0f}/100 → {band_algo}"

    vols = [r["vol"] for r in disp_rows]
    cur_px = q["price"] if q and q.get("price") else disp_rows[-1]["close"]
    chips = None
    try:
        chips = calc_chips(disp_rows, cur_px)
    except Exception:
        pass

    # ---- 综合评估：多维打分，作为买卖点综合参考 ----
    action = None
    try:
        i = len(disp_rows) - 1
        c = disp_rows[i]["close"]
        pc = disp_rows[i - 1]["close"] if i else c
        items = []
        ma20, ma20p = mas[20][i], mas[20][i - 1] if i else None
        if ma20 and ma20p:
            if c > ma20 and ma20 > ma20p:
                items.append(("MA20趋势", 1, "价站上MA20且MA20向上"))
            elif c < ma20 and ma20 < ma20p:
                items.append(("MA20趋势", -1, "价跌破MA20且MA20向下"))
            else:
                items.append(("MA20趋势", 0, "MA20方向不明"))
        dif_i, dea_i = dif[i], dea[i]
        mh_i, mh_p = mhist[i], mhist[i - 1] if i else None
        if None not in (dif_i, dea_i, mh_i, mh_p):
            if dif_i > dea_i and mh_i >= mh_p:
                items.append(("MACD", 1, "DIF>DEA且柱体走强"))
            elif dif_i < dea_i and mh_i <= mh_p:
                items.append(("MACD", -1, "DIF<DEA且柱体走弱"))
            else:
                items.append(("MACD", 0, "多空转换中"))
        k_i, d_i, j_i = k_[i], d_[i], j_[i]
        if None not in (k_i, d_i):
            if k_i > d_i and j_i < 90:
                items.append(("KDJ", 1, f"K{k_i:.0f}>D{d_i:.0f}"))
            elif k_i < d_i and j_i > 10:
                items.append(("KDJ", -1, f"K{k_i:.0f}<D{d_i:.0f}"))
            else:
                items.append(("KDJ", 0, "超买超卖区待修复"))
        r6_i = r6[i]
        if r6_i is not None:
            if r6_i < 30:
                items.append(("RSI", 1, f"RSI6={r6_i:.0f} 超卖"))
            elif r6_i > 70:
                items.append(("RSI", -1, f"RSI6={r6_i:.0f} 超买"))
            else:
                items.append(("RSI", 0, f"RSI6={r6_i:.0f} 中性"))
        v_i = vols_d[i] if disp_rows[i].get("vol") else 0.0
        v5 = (sum(vols_d[max(0, i - 5):i]) / 5) if i >= 5 else 0.0
        if v_i and v5 and v_i > v5 * 1.2:
            if c > pc:
                items.append(("量价", 1, "放量上涨"))
            else:
                items.append(("量价", -1, "放量下跌"))
        else:
            items.append(("量价", 0, "量能平稳"))
        if chips:
            sup_i, res_i = chips.get("sup"), chips.get("res")
            if sup_i and c <= sup_i * 1.01:
                items.append(("筹码", 1, f"贴近支撑{sup_i:.2f}"))
            elif res_i and c >= res_i * 0.99:
                items.append(("筹码", -1, f"贴近压力{res_i:.2f}"))
            elif chips["p5"] <= c <= chips["p95"]:
                items.append(("筹码", 0, "处于筹码密集区中部"))
        # 布林带：位置 + 中轨方向
        bu_i, bl_i, bm_i = b_up[i], b_low[i], b_mid[i]
        if None not in (bu_i, bl_i, bm_i):
            bm_p = b_mid[i - 1] if i else None
            mid_up = (bm_p is not None and bm_i > bm_p)
            if c > bu_i:
                items.append(("布林带", -1,
                              f"高于上轨{bu_i:.2f} 超买注意回落"))
            elif c < bl_i:
                items.append(("布林带", 1,
                              f"低于下轨{bl_i:.2f} 超卖关注反弹"))
            elif c > bm_i and mid_up:
                items.append(("布林带", 1,
                              f"中轨{bm_i:.2f}上方且中轨向上"))
            elif c < bm_i and not mid_up:
                items.append(("布林带", -1,
                              f"中轨{bm_i:.2f}下方且中轨向下"))
            else:
                items.append(("布林带", 0,
                              f"中轨{bm_i:.2f}附近 方向不明"))
        # ADX：趋势强度
        a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
        if None not in (a_i, p_i, m_i):
            if a_i >= 25:
                items.append(("ADX", 1 if p_i > m_i else -1,
                              f"ADX={a_i:.0f} 强趋势"
                              f"{'偏多' if p_i > m_i else '偏空'}"))
            elif a_i >= 20:
                items.append(("ADX", 1 if p_i > m_i else -1 if p_i != m_i else 0,
                              f"ADX={a_i:.0f} 趋势形成中"))
            else:
                items.append(("ADX", 0, f"ADX={a_i:.0f} 无趋势震荡"))
        up_p = t_pred["up_prob"]
        if up_p >= 0.55:
            items.append(("统计预测", 1, f"上行概率{up_p*100:.0f}%"))
        elif up_p <= 0.45:
            items.append(("统计预测", -1, f"上行概率{up_p*100:.0f}%"))
        else:
            items.append(("统计预测", 0, f"上行概率{up_p*100:.0f}%"))
        # 多日预测趋势评估
        if multi_pred and len(multi_pred) >= 3:
            short_trend = multi_pred[2]["cum_cl"] if len(multi_pred) >= 3 else 0
            mid_trend = multi_pred[min(4, len(multi_pred) - 1)]["cum_cl"] if len(multi_pred) >= 5 else short_trend
            
            if short_trend > 0.02 and mid_trend > 0.03:
                items.append(("多日预测", 1, f"短期+{short_trend*100:.1f}% 中期+{mid_trend*100:.1f}% 看涨"))
            elif short_trend < -0.02 and mid_trend < -0.03:
                items.append(("多日预测", -1, f"短期{short_trend*100:.1f}% 中期{mid_trend*100:.1f}% 看跌"))
            elif short_trend > 0.01:
                items.append(("多日预测", 1, f"短期+{short_trend*100:.1f}% 偏多"))
            elif short_trend < -0.01:
                items.append(("多日预测", -1, f"短期{short_trend*100:.1f}% 偏空"))
            else:
                items.append(("多日预测", 0, f"短期{short_trend*100:+.1f}% 震荡"))
        if samples:
            avg1 = sum(x["n1_cl"] for x in samples if x.get("n1_cl") is not None) / len(samples)
            items.append(("相似样本", 1 if avg1 > 0 else -1,
                          f"次日均涨跌{avg1*100:+.1f}%"))
        # 各维度加权求和（权重与信号打分共用 CFG.IND_W）
        score = sum(int(round(s * CFG.IND_W.get(lab, 1.0)))
                    for lab, s, _ in items)
        if score >= 4:
            verdict = "多维共振偏多·买点参考"
        elif score >= 2:
            verdict = "略偏多·轻仓试探"
        elif score > -2:
            verdict = "多空交织·观望"
        elif score > -4:
            verdict = "略偏空·减仓留意"
        else:
            verdict = "多维共振偏空·卖点参考"
        action = {"score": score, "verdict": verdict, "items": items,
                  "band_fit": band_fit, "band_score": band_score,
                  "band_note": band_note}
    except Exception:
        log.exception("综合评估计算失败(action=None)")

    # ---- 回测统计：基于全部历史信号计算胜率/盈亏/年化（按所选策略参数）----
    bt_stats = None
    if signals and len(signals) >= 2:
        try:
            bt_stats = backtest_signals(disp_rows, signals, rp=rp)
        except Exception:
            log.exception("回测统计失败(bt_stats=None)")

    # ---- 幽灵K线：T+1 / T+5 / T+10（白色虚线边框，随所选策略融合预测）----
    def _ghost(day_pred, label):
        if not day_pred:
            return None
        o = o_today
        if label != "T+1" and multi_pred:
            idx = int(label[2:]) - 2
            if 0 <= idx < len(multi_pred):
                o = multi_pred[idx]["price_cl"]      # 前一预测日收盘为开
        hi = day_pred.get("price_hi") or day_pred["close"]
        lo = day_pred.get("price_lo") or day_pred["close"]
        cl = day_pred.get("price_cl") or day_pred["close"]
        hi = max(hi, o, cl)
        lo = min(lo, o, cl)
        return {"date": f"{label}预测", "open": round(o, 2),
                "close": round(cl, 2), "high": round(hi, 2),
                "low": round(lo, 2), "vol": None}
    # T+1 已由 pred 承担（slice_view 追加），幽灵只补 T+5 / T+10
    ghosts = []
    for dd in (5, 10):
        if multi_pred and len(multi_pred) >= dd:
            g = _ghost(multi_pred[dd - 1], f"T+{dd}")
            if g:
                ghosts.append(g)

    return {
        "quote": q, "full_code": full, "disp_rows": disp_rows,
        "anchor": anchor, "pre_open": pre_open,
        "phase": phase, "next_label": next_label,
        "tpred_bar": tpred_bar,
        "t5_pred": t_pred.get("t5"),
        "pred": pred, "t_pred": t_pred, "multi_pred": multi_pred,
        "ghosts": ghosts,
        "strategy": strat,
        "risk_mode": (strat or {}).get("mode",
                                       CFG.RISK_MODE if sel_algo == "composite"
                                       else "稳健"),
        "sel_algo": sel_algo,
        "samples": samples, "src_n": len(src),
        "filtered": src is not samples,
        "filter_note": filter_note,
        "levels": [{"key": k, "label": LV_LABEL[k], "n": len(smp),
                    "samples": smp,
                    "up_prob": (len([s for s in smp if s.get("n1_cl") is not None and s["n1_cl"] > 0])
                                / len(smp)) if smp else 0.5}
                   for k, smp in levels],
        "pool_note": pool_note,
        "idx_chg_today": idx_chg_today, "vr_now": vr_now,
        "cur_regime": cur_regime,
        "sector_name": sec_name, "sector_chg_today": sec_chg_today,
        "ind": {"ma": mas, "dif": dif, "dea": dea, "mhist": mhist,
                "k": k_, "d": d_, "j": j_, "rsi6": r6, "rsi12": r12,
                "boll_mid": b_mid, "boll_up": b_up, "boll_low": b_low, "pdi": pdi_a, "mdi": mdi_a, "adx": adx_a},
        "vols": vols,
        "chips": chips,
        "action": action,
        "signals": signals,
        "bt_stats": bt_stats,
        "band_fit": band_fit, "band_score": band_score,
        "band_algo": band_algo, "band_note": band_note,
        "gap_today": gap_today, "prev_close": prev_close,
        "has_live": bool(live),
        "live_high": live["high"] if live else None,
        "live_low": live["low"] if live else None,
        "clamped": clamped,
        "quick": bool(quick),
        "_ctx": {
            "full": full, "o_today": o_today, "pre_open": pre_open,
            "live": live, "src": src, "cur": cur,
            "cur_ctx": cur_ctx,
            "vr_now": vr_now, "idx_chg_by_date": idx_chg_by_date,
            "idx_chg_today": idx_chg_today, "gap_today": gap_today,
            "prev_close": prev_close, "pool_info": pool_info,
        },
    }


# ================= v4.0 自适应ML研究引擎 =================
# 设计约束（防数据泄漏）：
#   1. 特征只用 T 日及以前数据；标签 = Close[T+H]/Close[T]-1 仅作训练目标
#   2. StandardScaler / Lasso 因子筛选 / Horizon 选择 全部只看训练段
#   3. Walk-Forward 扩展窗：每折用折前全部数据训练，折内预测，折间不重叠
#   4. LightGBM 超参先验固定（浅树/少叶/强正则/子采样），不用 Test 调参
#   5. ATR/移动止盈风控保留为对照退出（消融 + 极端行情防火墙），不删除

_V4_FACTORS = ("l1_up", "l1_ret", "bias20", "bias60", "ret5", "ret10",
               "ret20", "rsi14", "atr_pct", "vola20", "volchg",
               "mkt5", "ind5")
_V4_HORIZONS = (1, 5, 10)
_V4_FOLD = 40            # Walk-Forward 折大小（交易日）
_V4_WARMUP = 60          # 因子预热期
_V4_TRAIN_MIN = 180      # 首折最少训练样本
_V4_QTS = (10, 25, 50, 75, 90)
# v4.0.1：去掉佣金/印花税（记 0），只保留滑点——用户实际交易成本以滑点为主；
# 键名保留以兼容旧报告结构，数值为 0 时乘法天然退化
_V4_COST = {"slip": 0.001, "commission": 0.0, "stamp": 0.0}
_V4_CAPITAL = 1_000_000.0
# 预测缓存版本：**改因子/模型/折参数代码后必须 +1**，否则旧缓存被误用
_V4_CACHE_VER = "1"

# 三档风险：同一套 v4 模型输出上的不同决策层参数（不分别训练）
# a_th 为自适应分数的 σ 阈值（训练段标准化后）
_V4_TIERS = {
    "保守": {"p_th": 0.62, "r_th": 0.012, "a_th": 1.00,
             "frac": 0.12, "max_pos": 10, "exit_p": 0.50},
    "平衡": {"p_th": 0.55, "r_th": 0.006, "a_th": 0.70,
             "frac": 0.20, "max_pos": 6, "exit_p": 0.45},
    "激进": {"p_th": 0.52, "r_th": 0.000, "a_th": 0.40,
             "frac": 0.33, "max_pos": 4, "exit_p": 0.45},
}

# LightGBM 先验超参（防过拟合：浅树/少叶/强正则/子采样，不按Test调）
_V4_LGBM = {"objective": "regression", "num_leaves": 15, "max_depth": 4,
            "learning_rate": 0.05, "n_estimators": 200,
            "min_child_samples": 40, "subsample": 0.8, "subsample_freq": 1,
            "colsample_bytree": 0.8, "reg_lambda": 1.0, "reg_alpha": 0.0,
            "random_state": 42, "n_jobs": 1, "verbose": -1}


def _v4_deps():
    """依赖探测：缺库时如实记录，不偷偷换模型。"""
    out = {}
    for m in ("numpy", "sklearn", "lightgbm", "scipy"):
        try:
            mod = __import__(m)
            out[m] = getattr(mod, "__version__", "?")
        except ImportError:
            out[m] = None
    return out


def _v4_rankic(a, b):
    """Spearman 秩相关（numpy 实现）。样本不足/无差异返回 None。"""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    m = np.isfinite(a) & np.isfinite(b)
    a, b = a[m], b[m]
    if len(a) < 25:
        return None
    ra = a.argsort().argsort().astype(np.float64)
    rb = b.argsort().argsort().astype(np.float64)
    ra -= ra.mean()
    rb -= rb.mean()
    den = math.sqrt(float((ra * ra).sum()) * float((rb * rb).sum()))
    return float((ra * rb).sum() / den) if den > 0 else None


def _v4_roll_mean(a, w):
    n = len(a)
    out = np.full(n, np.nan)
    if n >= w:
        c = np.cumsum(np.insert(np.asarray(a, float), 0, 0.0))
        out[w - 1:] = (c[w:] - c[:-w]) / w
    return out


def _v4_roll_std(a, w):
    m = _v4_roll_mean(a, w)
    m2 = _v4_roll_mean(np.asarray(a, float) ** 2, w)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


# ---- 市场/行业日收益上下文（worker 进程初始化时注入，只读） ----
_V4_MKT_CTX = None      # (dates, cumsum, bisect_right)
_V4_IND_CTX = None      # {industry: (dates, cumsum, bisect_right)}


def _v4_worker_init(mkt, ind):
    global _V4_MKT_CTX, _V4_IND_CTX
    from bisect import bisect_right as _br

    def _ctx(dct):
        if not dct:
            return None
        dates = sorted(dct)
        rets = np.array([dct[x] for x in dates], float)
        c = np.cumsum(np.insert(rets, 0, 0.0))
        return (dates, c, _br)

    _V4_MKT_CTX = _ctx(mkt)
    _V4_IND_CTX = {k: _ctx(v) for k, v in (ind or {}).items() if v}


def _v4_roll5_ret(ctx, d):
    """截至日期 d（含）近5个交易日的等权累计收益；不足按实际天数；缺→0。"""
    if ctx is None:
        return 0.0
    dates, c, br = ctx
    i = br(dates, d) - 1
    if i < 0:
        return 0.0
    j0 = max(0, i - 4)
    return float((c[i + 1] - c[j0]) / (i + 1 - j0))


def _v4_factor_matrix(bars, industry):
    """T 日因子矩阵（仅用 T 及以前数据）。返回 (F[n×13], aux dict)。

    因子：L1形态上行概率/相似样本收益（逐日匹配，与 _sig_l1_pattern 同口径）、
    MA20/60 乖离、5/10/20 日收益、RSI14、ATR14/Close、20日波动、量变(5/20)、
    大盘5日收益、行业5日收益（市场日历口径，停牌日不产生缺口）。"""
    n = len(bars)
    dates = [b["date"] for b in bars]
    close = np.array([(b["close"] if b["close"] else np.nan)
                      for b in bars], float)
    high = np.array([(b["high"] if b["high"] else np.nan)
                     for b in bars], float)
    low = np.array([(b["low"] if b["low"] else np.nan)
                    for b in bars], float)
    vol = np.array([(b.get("vol") or 0.0) for b in bars], float)
    F = np.full((n, len(_V4_FACTORS)), np.nan)
    fi = {f: k for k, f in enumerate(_V4_FACTORS)}

    lr = np.zeros(n)                    # lr[t]=log(c[t]/c[t-1])，与logret同口径
    lr[1:] = np.diff(np.log(np.maximum(close, 1e-9)))
    ret1 = np.zeros(n)
    ret1[1:] = close[1:] / close[:-1] - 1.0

    ma20 = _v4_roll_mean(close, 20)
    ma60 = _v4_roll_mean(close, 60)
    F[:, fi["bias20"]] = close / np.maximum(ma20, 1e-9) - 1.0
    F[:, fi["bias60"]] = close / np.maximum(ma60, 1e-9) - 1.0
    for k, f in ((5, "ret5"), (10, "ret10"), (20, "ret20")):
        F[k:, fi[f]] = close[k:] / np.maximum(close[:-k], 1e-9) - 1.0

    d1 = np.diff(close, prepend=close[:1])
    up = _v4_roll_mean(np.where(d1 > 0, d1, 0.0), 14)
    dn = _v4_roll_mean(np.where(d1 < 0, -d1, 0.0), 14)
    rsi = 100.0 - 100.0 / (1.0 + up / np.maximum(dn, 1e-12))
    rsi[(up + dn) <= 0] = 50.0
    F[:, fi["rsi14"]] = rsi

    tr = np.zeros(n)
    tr[1:] = np.maximum(high[1:] - low[1:], np.maximum(
        np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    atr = _v4_roll_mean(tr, 14)
    F[:, fi["atr_pct"]] = atr / np.maximum(close, 1e-9)
    F[:, fi["vola20"]] = _v4_roll_std(lr, 20)
    v5 = _v4_roll_mean(vol, 5)
    v20 = _v4_roll_mean(vol, 20)
    F[:, fi["volchg"]] = v5 / np.maximum(v20, 1e-9) - 1.0

    if _V4_MKT_CTX is not None:
        F[:, fi["mkt5"]] = [_v4_roll5_ret(_V4_MKT_CTX, x) for x in dates]
    ictx = _V4_IND_CTX.get(industry) if _V4_IND_CTX else None
    if ictx is not None:
        F[:, fi["ind5"]] = [_v4_roll5_ret(ictx, x) for x in dates]
    else:
        F[:, fi["ind5"]] = 0.0

    # L1 形态两因子：逐日自身历史匹配（只用当日以前窗口，防前视）
    W = W_WINDOW
    L = lr[1:]                          # 与 logret(closes) 完全同口径
    for t in range(2 * W + 1, n - 1):
        cur = znorm(L[t - W:t])
        d_arr = _px_distances(L[:t], cur, W)
        ks = [k for k in range(len(d_arr)) if k + W <= t - W]
        if len(ks) < 6:
            continue
        ks.sort(key=lambda k: d_arr[k])
        ups = tot = sret = 0.0
        for k in ks[:CFG.TOPK]:
            j = k + W
            if j + 1 < n:
                tot += 1.0
                o = close[j + 1] / close[j] - 1.0
                sret += o
                ups += 1.0 if o > 0 else 0.0
        if tot >= 5:
            F[t, fi["l1_up"]] = ups / tot
            F[t, fi["l1_ret"]] = sret / tot

    aux = {"dates": dates, "close": close, "high": high, "low": low,
           "open": np.array([(b["open"] if b["open"] else np.nan)
                             for b in bars], float),
           "atr": atr, "ret1": ret1}
    return F, aux


def _v4_mkt_ind_ctx(ind_of):
    """市场/行业等权日收益（一遍扫描全库）：
    返回 (mkt={date: ret}, ind={date: {industry: ret}})。"""
    mkt_acc, ind_acc, prev_px = {}, {}, {}
    with db_conn() as conn:
        cur = conn.execute(
            "SELECT code, date, close FROM daily_bars ORDER BY date")
        for code, d, cl in cur:
            pv = prev_px.get(code)
            if pv and pv > 0 and cl and cl > 0:
                r = cl / pv - 1.0
                a = mkt_acc.get(d)
                if a is None:
                    mkt_acc[d] = [r, 1]
                else:
                    a[0] += r
                    a[1] += 1
                b = ind_acc.setdefault(d, {}).setdefault(
                    ind_of.get(code, ""), [0.0, 0])
                b[0] += r
                b[1] += 1
            if cl:
                prev_px[code] = cl
    mkt = {d: s / c for d, (s, c) in mkt_acc.items() if c}
    ind = {d: {k: s / c for k, (s, c) in v.items() if c}
           for d, v in ind_acc.items()}
    return mkt, ind


def _v4_walkforward_one(job):
    """多进程 worker：单股 Walk-Forward 训练+预测（顶层函数可 pickle）。"""
    code, bars, industry = job
    try:
        return _v4_wf_impl(code, bars, industry)
    except Exception:
        log.exception("v4 worker 失败 %s", code)
        return None


def _v4_wf_impl(code, bars, industry):
    from sklearn.linear_model import (Lasso, LogisticRegression,
                                      QuantileRegressor)
    try:
        from lightgbm import LGBMRegressor
        has_lgbm = True
    except ImportError:
        has_lgbm = False

    n = len(bars)
    if n < _V4_WARMUP + _V4_TRAIN_MIN + _V4_FOLD + max(_V4_HORIZONS):
        return None
    F, aux = _v4_factor_matrix(bars, industry)
    dates, close, high, low, open_, atr, ret1 = (
        aux["dates"], aux["close"], aux["high"], aux["low"], aux["open"],
        aux["atr"], aux["ret1"])
    y = {}
    for H in _V4_HORIZONS:
        a = np.full(n, np.nan)
        a[:n - H] = close[H:] / close[:n - H] - 1.0
        y[H] = a

    # 有效区间：因子全 finite 的最长连续尾段
    finite = np.all(np.isfinite(F), axis=1)
    t0 = _V4_WARMUP
    last_ok = n - 1 - max(_V4_HORIZONS)
    while t0 <= last_ok and not finite[t0]:
        t0 += 1
    t1 = last_ok
    while t1 >= t0 and not finite[t1]:
        t1 -= 1
    S = t1 - t0 + 1
    if S < _V4_TRAIN_MIN + _V4_FOLD:
        return None
    n_folds = max(1, int(S * 0.4) // _V4_FOLD)
    if S - n_folds * _V4_FOLD < _V4_TRAIN_MIN:
        n_folds -= 1
    if n_folds < 1:
        return None
    fa = S - n_folds * _V4_FOLD
    TS = S - fa

    Fc = F[t0:t1 + 1].copy()
    bad = ~np.isfinite(Fc)
    if bad.any():
        Fc[bad] = 0.0                   # 中段孤立缺失中性填充
    Yc = {H: y[H][t0:t1 + 1] for H in _V4_HORIZONS}
    base_sigs = {}
    try:
        for i, d_, typ, _rs in _composite_signals(
                bars, CFG.RISK_PARAMS["稳健"], use_chips=False):
            if i - t0 >= fa:
                base_sigs[d_] = typ
    except Exception:
        base_sigs = {}

    ml = {H: np.full(TS, np.nan) for H in _V4_HORIZONS}
    ml_dyn = np.full(TS, np.nan)        # 逐日按 h_choice 取对应H的预测
    p_up = np.full(TS, np.nan)
    adaptive = np.full(TS, np.nan)
    qpred = {q: np.full(TS, np.nan) for q in _V4_QTS}
    hch = np.zeros(TS, dtype=np.int32)
    ins_ic = []                         # LGBM 训练段内IC（过拟合检查）
    q_cross_raw = [0, 0]                # 违反序的相邻对数 / 总对数
    factor_table = None
    feat_imp = None
    per_stock_ic = {H: [] for H in _V4_HORIZONS}

    for j in range(n_folds):
        b = S - j * _V4_FOLD
        a = b - _V4_FOLD
        ta, tb = a - fa, b - fa         # 测试段输出数组内的偏移索引
        tr = np.arange(0, a)
        mu = Fc[tr].mean(axis=0)
        sd = Fc[tr].std(axis=0)
        sd[sd < 1e-9] = 1.0
        Z = (Fc - mu) / sd              # 仅训练段统计量，测试行同缩放
        # 1) Horizon 选择：训练段内层 75/25 时序切分的 LGBM IC（不看测试）
        cut = int(len(tr) * 0.75)
        h_ic = {}
        for H in _V4_HORIZONS:
            if not has_lgbm or cut < 60 or len(tr) - cut < 30:
                h_ic[H] = 0.0
                continue
            try:
                m_ = LGBMRegressor(**_V4_LGBM).fit(Z[tr[:cut]],
                                                   Yc[H][tr[:cut]])
                h_ic[H] = _v4_rankic(m_.predict(Z[tr[cut:]]),
                                     Yc[H][tr[cut:]]) or 0.0
            except Exception:
                h_ic[H] = 0.0
        h_star = max(_V4_HORIZONS, key=lambda H: h_ic[H])
        hch[ta:tb] = h_star
        yh = Yc[h_star][tr]
        # 2) Lasso 因子筛选/权重（标准化后，训练段 only）
        las = Lasso(alpha=0.005, max_iter=5000).fit(Z[tr], yh)
        coef = np.asarray(las.coef_, float)
        ic_m = np.zeros(len(_V4_FACTORS))
        icir = np.zeros(len(_V4_FACTORS))
        segs = np.array_split(tr, 4)
        for i in range(len(_V4_FACTORS)):
            ics = [_v4_rankic(Z[s_, i], yh[s_]) for s_ in segs]
            ics = [x for x in ics if x is not None]
            if ics:
                ic_m[i] = float(np.mean(ics))
                icir[i] = ic_m[i] / max(float(np.std(ics)), 1e-6)
        sel = np.array([
            (coef[i] != 0.0) or (abs(ic_m[i]) > 0.02 and icir[i] > 0.2)
            for i in range(len(_V4_FACTORS))])
        if sel.sum() < 3:
            sel = np.ones(len(_V4_FACTORS), bool)
        # 自适应权重：Lasso 稀疏权重优先；若被全量收缩为零，
        # 退回 IC×稳定性 加权（同样只看训练段）
        if np.any(coef != 0.0):
            w_eff = coef.copy()
        else:
            w_eff = np.array([
                ic_m[i] * min(icir[i], 3.0)
                if (abs(ic_m[i]) > 0.02 and icir[i] > 0.2) else 0.0
                for i in range(len(_V4_FACTORS))])
        Xs = Z[:, sel]
        # 3) LightGBM：全训练段拟合，折内预测（各 H 都要，Horizon 实验用）
        if has_lgbm:
            for H in _V4_HORIZONS:
                try:
                    m_ = LGBMRegressor(**_V4_LGBM).fit(Xs[tr], Yc[H][tr])
                    ml[H][ta:tb] = m_.predict(Xs[a:b])
                    if H == h_star:
                        ml_dyn[ta:tb] = ml[H][ta:tb]
                        ins_ic.append(_v4_rankic(m_.predict(Xs[tr]),
                                                 yh) or 0.0)
                        if j == 0:
                            feat_imp = sorted(
                                zip([f for i_, f in enumerate(_V4_FACTORS)
                                     if sel[i_]],
                                    m_.booster_.feature_importance("gain")),
                                key=lambda x: -x[1])
                except Exception:
                    pass
            for H in _V4_HORIZONS:
                ic = _v4_rankic(ml[H][ta:tb], Yc[H][a:b])
                if ic is not None:
                    per_stock_ic[H].append(ic)
        else:
            for H in _V4_HORIZONS:
                ml[H][ta:tb] = 0.0      # 缺库占位（如实标记，不偷偷换模型）
            ml_dyn[ta:tb] = 0.0
        # 4) Logistic 方向概率
        try:
            lg = LogisticRegression(C=1.0, max_iter=500).fit(
                Xs[tr], (yh > 0))
            p_up[ta:tb] = lg.predict_proba(Xs[a:b])[:, 1]
        except Exception:
            pass
        # 5) Quantile Regression（训练段；排序消除交叉并记录原始交叉率）
        try:
            qs = []
            for q in _V4_QTS:
                qm = QuantileRegressor(quantile=q / 100.0, alpha=0.01,
                                       solver="highs").fit(Xs[tr], yh)
                qs.append(qm.predict(Xs[a:b]))
            Q = np.column_stack(qs)
            for r_ in range(Q.shape[0]):
                for c_ in range(Q.shape[1] - 1):
                    q_cross_raw[1] += 1
                    if Q[r_, c_] > Q[r_, c_ + 1] + 1e-12:
                        q_cross_raw[0] += 1
            Q = np.sort(Q, axis=1)      # 投影到非交叉（保守修复，如实记录）
            for qi, q in enumerate(_V4_QTS):
                qpred[q][ta:tb] = Q[:, qi]
        except Exception:
            pass
        adaptive[ta:tb] = (Z[a:b] @ w_eff) / max(float((Z[tr] @ w_eff).std()),
                                                 1e-9)  # 训练段σ归一
        if j == 0:
            factor_table = [{"factor": f, "weight": float(w_eff[i]),
                             "train_ic": float(ic_m[i]),
                             "stability": float(icir[i]),
                             "selected": bool(sel[i])}
                            for i, f in enumerate(_V4_FACTORS)]

    if not np.isfinite(p_up).any() and not any(
            np.isfinite(ml[H]).any() for H in _V4_HORIZONS):
        return None
    out = {
        "code": code,
        "dates": dates[t0 + fa:t0 + S],
        "close": close[t0 + fa:t0 + S].astype(np.float32),
        "open": open_[t0 + fa:t0 + S].astype(np.float32),
        "high": high[t0 + fa:t0 + S].astype(np.float32),
        "low": low[t0 + fa:t0 + S].astype(np.float32),
        "atr": atr[t0 + fa:t0 + S].astype(np.float32),
        "ret1": ret1[t0 + fa:t0 + S].astype(np.float32),
        "l1_up": F[t0 + fa:t0 + S, 0].astype(np.float32),
        "l1_ret": F[t0 + fa:t0 + S, 1].astype(np.float32),
        "y": {H: Yc[H][fa:].astype(np.float32) for H in _V4_HORIZONS},
        "ml": {H: ml[H].astype(np.float32) for H in _V4_HORIZONS},
        "ml_dyn": ml_dyn.astype(np.float32),
        "p_up": p_up.astype(np.float32),
        "adaptive": adaptive.astype(np.float32),
        "q": {str(q): qpred[q].astype(np.float32) for q in _V4_QTS},
        "h_choice": hch,
        "base_sigs": base_sigs,
        "ins_ic": float(np.mean(ins_ic)) if ins_ic else None,
        "q_cross": (q_cross_raw[0] / q_cross_raw[1]) if q_cross_raw[1] else 0.0,
        "factor_table": factor_table,
        "feat_imp": feat_imp,
        "per_stock_ic": per_stock_ic,
        "n_folds": n_folds,
        "train_last": int(t0 + fa),
    }
    return out


# ---- v4 组合级回测（统一：初始资金/成本/滑点/成交时点/涨跌停/停牌） ----

def _v4_limit_pct(code):
    """涨跌停幅度：创业板/科创板20%，主板10%（北交所不参与回测）。"""
    if code.startswith(("sz30", "sh68")):
        return 0.20
    return 0.10


def _v4_metrics(eq_curve, dates, trades, stock_days=0):
    """组合绩效：总收益/年化/最大回撤/Calmar/Sharpe/胜率/盈亏比/交易数/
    平均持仓天数/最大连续亏损/收益波动率。"""
    import datetime as _dt
    eq = np.asarray(eq_curve, float)
    n = len(eq)
    out = {"total": 0.0, "ann": 0.0, "mdd": 0.0, "calmar": None,
           "sharpe": None, "vol": 0.0, "winrate": None, "pf": None,
           "trades": len(trades), "avg_hold": None,
           "max_consec_loss": 0, "stock_days": stock_days, "days": n}
    if n < 2 or eq[0] <= 0:
        return out
    total_mult = float(eq[-1] / eq[0])
    out["total"] = total_mult - 1.0
    try:
        d0 = _dt.date.fromisoformat(dates[0])
        d1 = _dt.date.fromisoformat(dates[-1])
        years = max((d1 - d0).days / 365.25, 1e-6)
    except Exception:
        years = max(n / 252.0, 1e-6)
    out["ann"] = total_mult ** (1.0 / years) - 1.0 if total_mult > 0 else -1.0
    daily = np.diff(eq) / eq[:-1]
    sd = float(daily.std())
    out["vol"] = sd * math.sqrt(252.0) if n > 2 else 0.0
    if sd > 1e-12:
        out["sharpe"] = float(daily.mean()) / sd * math.sqrt(252.0)
    peak = eq[0]
    mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    out["mdd"] = float(mdd)
    if out["mdd"] < -1e-9:
        out["calmar"] = out["ann"] / abs(out["mdd"])
    if trades:
        wins = [t for t in trades if t["ret"] > 0]
        losses = [t for t in trades if t["ret"] <= 0]
        out["winrate"] = len(wins) / len(trades)
        gp = sum(t["pnl"] for t in wins)
        gl = -sum(t["pnl"] for t in losses)
        out["pf"] = (gp / gl) if gl > 1e-9 else (None if gp <= 0 else 99.0)
        out["avg_hold"] = float(np.mean([t["hold"] for t in trades]))
        consec = worst = 0
        for t in trades:
            consec = consec + 1 if t["ret"] <= 0 else 0
            worst = max(worst, consec)
        out["max_consec_loss"] = worst
    return out


# 消融变体（决策层开关；同一套 Walk-Forward 预测输出，不重训）
_V4_VARIANTS = {
    "Full v4": {},
    "Full - Adaptive": {"use_adaptive": False},
    "Full - Horizon": {"adaptive_h": False},
    "Full - Logistic": {"use_logistic": False},
    "Full - LightGBM": {"use_lgbm": False},
    "Full - Quantile": {"use_quantile": False, "use_dist_exit": False},
    "Full - DistributionExit": {"use_dist_exit": False},
    "Adaptive+Horizon": {"use_logistic": False, "use_lgbm": False},
    "Adaptive+LightGBM": {"use_logistic": False, "adaptive_h": False,
                          "use_dist_exit": False},
    "Logistic+LightGBM": {"use_adaptive": False, "use_dist_exit": False},
    "LightGBM+Quantile": {"use_logistic": False, "use_adaptive": False},
    "Adaptive+Horizon+LightGBM": {"use_logistic": False,
                                  "use_dist_exit": False},
    # v4.0.1 退出结构消融（hybrid = Q10棘轮 + 移动止盈 + p_up）：
    "Exit: Dist(v4.0)": {"exit_mode": "dist", "cooldown": 0,
                         "min_hold": 0},  # 忠实复现 v4.0 纯分布退出（含旧高频）
    "Hybrid - Trailing": {"use_trailing": False},    # 去移动止盈
    "Hybrid - Q10Stop": {"use_q10_stop": False},     # 去Q10棘轮
    "Hybrid - Cooldown5": {"cooldown": 5, "min_hold": 5},  # 降频实验（全策略下有害）
    "Hybrid - T10only": {"h_only": 10, "cooldown": 5,
                         "min_hold": 5},  # 只交易 T+10+冷却：低频低回撤首选
    # 以小博大 / 交易后升档再入场（reentry_tier 已设为 full 模式默认开）：
    "Reentry-Off": {"reentry_tier": False},   # 隔离升档再入场的贡献
    "TrailSlow": {"trail_slow": True},
    "T10only+Reentry": {"h_only": 10, "cooldown": 5,
                        "min_hold": 5, "reentry_tier": True},
    "Dist+Reentry": {"exit_mode": "dist", "reentry_tier": True},
    "Dist+Reentry+Cd5": {"exit_mode": "dist", "reentry_tier": True,
                         "cooldown": 5, "min_hold": 5},
    "Hybrid (v4.0.1)": {"exit_mode": "hybrid"},   # 隔离默认 dist vs hybrid
    # 板块轮动（行业5日收益动量门槛）：
    "Rot-Top30": {"rot_top": 0.70},               # 只买行业强度前30%的个股
    "Rot-Top50": {"rot_top": 0.50},
    "Rot-Strong": {"rot_strong": True},           # 只买跑赢大盘的行业
    "Rot-T10only": {"h_only": 10, "cooldown": 5, "min_hold": 5,
                    "rot_top": 0.70},             # T10王牌+板块轮动叠加
    "RotT10+DispHi": {"h_only": 10, "cooldown": 5, "min_hold": 5,
                      "rot_top": 0.70,
                      "disp_min": 0.5},           # regime：仅高分化（轮动富集）环境
}


def _v4_entry_score(mats, rules):
    """与入场逻辑一致的 (score, label) pooled IC/MAE（矩阵向量化）。"""
    cal, M, codes = mats
    if rules.get("use_lgbm", True):
        x, y = M["ml_dyn"], M["y_dyn"]
    elif rules.get("use_quantile", True):
        x, y = M["q50"], M["y5"]
    else:
        return {"ic": None, "mae": None, "n": 0}
    return _v4_pool_eval(x, y)


def _v4_pool_eval(x, y, th=None):
    """矩阵 pooled IC / MAE / 方向命中率（finite 掩码）。"""
    m = np.isfinite(x) & np.isfinite(y)
    n = int(m.sum())
    out = {"ic": None, "mae": None, "hit": None, "n": n, "ic_ir": None}
    if n < 25:
        return out
    xs = x[m].astype(np.float64)
    ys = y[m].astype(np.float64)
    out["ic"] = _v4_rankic(xs, ys)
    out["mae"] = float(np.mean(np.abs(xs - ys)))
    if th is not None:
        out["hit"] = float(np.mean((xs > th) == (ys > 0)))
    return out


def _v4_stack(preds):
    """把逐股测试段堆叠为日历对齐矩阵（一次构建，指标/回测共用）。

    返回 (cal, M, codes)。M 各键为 (n_stocks × n_days) 矩阵；
    无 bar 处为 NaN。"""
    cal = sorted({d for r in preds for d in r["dates"]})
    idx = {d: k for k, d in enumerate(cal)}
    ns, nc = len(preds), len(cal)

    def mk():
        return np.full((ns, nc), np.nan, np.float32)

    keys = ("close", "open", "high", "low", "ret1", "atr", "p_up",
            "adaptive", "ml_dyn", "l1_up", "l1_ret")
    M = {k: mk() for k in keys}
    for H in _V4_HORIZONS:
        M["ml%d" % H] = mk()
        M["y%d" % H] = mk()
    for q in _V4_QTS:
        M["q%d" % q] = mk()
    M["h_choice"] = np.full((ns, nc), -1, np.int16)
    M["base_buy"] = np.zeros((ns, nc), bool)
    M["base_sell"] = np.zeros((ns, nc), bool)
    codes = []
    lim_rows = np.full(ns, 0.10, np.float64)
    for k, r in enumerate(preds):
        codes.append(r["code"])
        cols = np.array([idx[d] for d in r["dates"]], np.int64)
        for key in keys:
            M[key][k, cols] = r[key]
        for H in _V4_HORIZONS:
            M["ml%d" % H][k, cols] = r["ml"][H]
            M["y%d" % H][k, cols] = r["y"][H]
        for q in _V4_QTS:
            M["q%d" % q][k, cols] = r["q"][str(q)]
        M["h_choice"][k, cols] = r["h_choice"]
        for i, d in enumerate(r["dates"]):
            typ = r["base_sigs"].get(d)
            if typ == "BUY":
                M["base_buy"][k, idx[d]] = True
            elif typ == "SELL":
                M["base_sell"][k, idx[d]] = True
        lim_rows[k] = _v4_limit_pct(r["code"])
    # 动态 H 的标签 y_dyn
    M["y_dyn"] = mk()
    for H in _V4_HORIZONS:
        m = M["h_choice"] == H
        M["y_dyn"][m] = M["y%d" % H][m]
    # 涨跌停掩码
    with np.errstate(invalid="ignore"):
        M["limit_up"] = M["ret1"] >= (lim_rows[:, None] - 0.005)
        M["limit_dn"] = M["ret1"] <= -(lim_rows[:, None] - 0.005)
    M["has_bar"] = np.isfinite(M["close"])
    return cal, M, codes


def _v4_stack_subset(preds_sub):
    """为时间对齐子样本重建堆叠矩阵（日历轴也重建，避免旧日期稀释）。"""
    return _v4_stack(preds_sub)


def _v4_icir_rows(Mx, My, min_n=30):
    """逐行 IC 序列 → IC_IR。"""
    ics = []
    for i in range(Mx.shape[0]):
        ic = _v4_rankic(Mx[i], My[i])
        if ic is not None:
            ics.append(ic)
    return _v4_icir(ics)


def _v4_icir(ics):
    if not ics:
        return None
    m = float(np.mean(ics))
    s = float(np.std(ics))
    return m / max(s, 1e-6)


def _v4_quantile_diag_m(M):
    """Pinball / 覆盖率 / 交叉率（矩阵向量化）。"""
    y = M["y_dyn"]
    out = {}
    for q in _V4_QTS:
        pred = M["q%d" % q]
        m = np.isfinite(pred) & np.isfinite(y)
        if m.sum() < 25:
            out["pinball_%d" % q] = None
            continue
        a = y[m].astype(np.float64) - pred[m].astype(np.float64)
        loss = np.where(a > 0, a * (q / 100.0), -a * (1 - q / 100.0))
        out["pinball_%d" % q] = float(np.mean(loss))
    m10 = np.isfinite(M["q10"]) & np.isfinite(M["q90"]) & np.isfinite(y)
    out["coverage_10_90"] = (float(np.mean(
        (M["q10"][m10] <= y[m10]) & (y[m10] <= M["q90"][m10])))
        if int(m10.sum()) else None)
    out["n"] = int(m10.sum())
    return out


def _v4_factor_agg(preds):
    """跨股聚合因子表：中位 train_ic / 稳定性 / 入选率 / 中位|权重|。"""
    agg = {f: {"ic": [], "stab": [], "sel": 0, "w": [], "n": 0}
           for f in _V4_FACTORS}
    n_st = 0
    for r in preds:
        ft = r.get("factor_table")
        if not ft:
            continue
        n_st += 1
        for row in ft:
            a = agg[row["factor"]]
            a["n"] += 1
            a["ic"].append(row["train_ic"])
            a["stab"].append(row["stability"])
            if row["selected"]:
                a["sel"] += 1
                a["w"].append(abs(row["weight"]))
    out = {}
    for f, a in agg.items():
        if not a["n"]:
            continue
        out[f] = {
            "train_ic_med": float(np.median(a["ic"])),
            "stability_med": float(np.median(a["stab"])),
            "sel_pct": a["sel"] / a["n"],
            "weight_med": float(np.median(a["w"])) if a["w"] else 0.0,
        }
    out["_n_stocks"] = n_st
    return out


def _v4_entry_mask(M, rules, tier):
    """入场资格矩阵（向量化；NaN 比较为 False）。"""
    mode = rules.get("mode", "full")
    with np.errstate(invalid="ignore"):
        if mode == "baseline":
            ok = M["base_buy"].copy()
        else:
            ok = M["has_bar"].copy()
            if rules.get("use_logistic", True):
                ok &= (M["p_up"] >= tier["p_th"])
            if rules.get("use_lgbm", True):
                ok &= (M["ml_dyn"] >= tier["r_th"])
                if rules.get("use_quantile", True) \
                        and rules.get("q50_entry", True):
                    ok &= (M["q50"] > 0)
            elif rules.get("use_quantile", True):
                ok &= (M["q50"] >= tier["r_th"])
            if rules.get("use_adaptive", True):
                ok &= (M["adaptive"] >= tier["a_th"])
            h_only = rules.get("h_only")        # 只交易指定 Horizon（如 T+10）
            if h_only:
                ok &= (M["h_choice"] == int(h_only))
            rot_top = rules.get("rot_top")      # 板块轮动：行业5日收益排名前 N%
            if rot_top and "ind_rank5" in M:
                with np.errstate(invalid="ignore"):
                    ok &= (M["ind_rank5"] >= float(rot_top))
            if rules.get("rot_strong") and "ind5" in M and "mkt5" in M:
                with np.errstate(invalid="ignore"):
                    ok &= (M["ind5"] > M["mkt5"])   # 行业跑赢大盘
            disp_min = rules.get("disp_min")    # regime：离散度分位门槛
            if disp_min and "disp_rank" in M:
                with np.errstate(invalid="ignore"):
                    ok &= (M["disp_rank"] >= float(disp_min))
            disp_max = rules.get("disp_max")    # regime：只在高/低分化环境交易
            if disp_max is not None and "disp_rank" in M:
                with np.errstate(invalid="ignore"):
                    ok &= (M["disp_rank"] <= float(disp_max))
        ok &= ~M["limit_up"]
    return ok & M["has_bar"]


def _v4_entry_ok_cell(M, ks, t, tier, rules):
    """单格入场判定（与 _v4_entry_mask 同逻辑，供平仓后升档再入场用）。"""
    if not M["has_bar"][ks, t] or M["limit_up"][ks, t]:
        return False
    if rules.get("use_logistic", True) \
            and not (M["p_up"][ks, t] >= tier["p_th"]):
        return False
    if rules.get("use_lgbm", True):
        if not (M["ml_dyn"][ks, t] >= tier["r_th"]):
            return False
        if rules.get("use_quantile", True) and rules.get("q50_entry", True) \
                and not (M["q50"][ks, t] > 0):
            return False
    elif rules.get("use_quantile", True) \
            and not (M["q50"][ks, t] >= tier["r_th"]):
        return False
    if rules.get("use_adaptive", True) \
            and not (M["adaptive"][ks, t] >= tier["a_th"]):
        return False
    h_only = rules.get("h_only")
    if h_only and M["h_choice"][ks, t] != int(h_only):
        return False
    rot_top = rules.get("rot_top")
    if rot_top and "ind_rank5" in M:
        v = M["ind_rank5"][ks, t]
        if not np.isfinite(v) or v < float(rot_top):
            return False
    if rules.get("rot_strong") and "ind5" in M and "mkt5" in M:
        if not (M["ind5"][ks, t] > M["mkt5"][ks, t]):
            return False
    disp_min = rules.get("disp_min")
    if disp_min and "disp_rank" in M:
        v = M["disp_rank"][ks, t]
        if not np.isfinite(v) or v < float(disp_min):
            return False
    disp_max = rules.get("disp_max")
    if disp_max is not None and "disp_rank" in M:
        v = M["disp_rank"][ks, t]
        if not np.isfinite(v) or v > float(disp_max):
            return False
    return True


def _v4_attach_rotation(M, cal, codes_s, ind_of, mkt, ind):
    """往堆叠矩阵注入板块轮动上下文：
    ind_rank5（行业5日收益当日横截面百分位 0~1）、ind5、mkt5。"""
    ind_names = [ind_of.get(c, "") for c in codes_s]
    uniq = sorted({x for x in ind_names if x})
    if not uniq:
        return
    jdx = {x: j for j, x in enumerate(uniq)}
    didx = {d: k for k, d in enumerate(cal)}
    IR = np.zeros((len(cal), len(uniq)))
    CNT = np.zeros((len(cal), len(uniq)))
    for d, dmap in ind.items():
        k = didx.get(d)
        if k is None:
            continue
        for x, r in dmap.items():
            j = jdx.get(x)
            if j is not None:
                IR[k, j] = r
                CNT[k, j] = 1.0
    c = np.cumsum(IR, 0)
    cc = np.cumsum(CNT, 0)
    S5 = c.copy()
    S5[5:] -= c[:-5]
    N5 = cc.copy()
    N5[5:] -= cc[:-5]
    R5 = np.where(N5 > 0, S5 / np.maximum(N5, 1.0), np.nan)
    marr = np.array([mkt.get(d, np.nan) for d in cal], float)
    m0 = np.where(np.isfinite(marr), marr, 0.0)
    cm = np.cumsum(m0)
    MS5 = cm.copy()
    MS5[5:] -= cm[:-5]
    mn = np.isfinite(marr).astype(float)
    ccn = np.cumsum(mn)
    NN5 = ccn.copy()
    NN5[5:] -= ccn[:-5]
    MR5 = np.where(NN5 > 0, MS5 / np.maximum(NN5, 1.0), np.nan)
    RANK = np.full(R5.shape, np.nan)
    for k in range(R5.shape[0]):
        row = R5[k]
        m = np.isfinite(row)
        n = int(m.sum())
        if n >= 3:
            v = row[m]
            RANK[k, m] = v.argsort().argsort() / max(n - 1, 1)
    # 行业动量离散度（轮动富集度）：行业5日收益横截面 std 的窗口内分位（0~1）
    DISP = np.array([np.nanstd(R5[k]) if np.isfinite(R5[k]).sum() >= 3
                     else np.nan for k in range(R5.shape[0])])
    dm = np.isfinite(DISP)
    DRANK = np.full(len(DISP), np.nan)
    if dm.sum() >= 5:
        DRANK[dm] = DISP[dm].argsort().argsort() / max(dm.sum() - 1, 1)
    ns, nc = M["close"].shape
    M["ind_rank5"] = np.full((ns, nc), np.nan)
    M["ind5"] = np.full((ns, nc), np.nan)
    M["mkt5"] = np.broadcast_to(MR5[None, :], (ns, nc)).copy()
    M["disp_rank"] = np.broadcast_to(DRANK[None, :], (ns, nc)).copy()
    for k, x in enumerate(ind_names):
        j = jdx.get(x)
        if j is not None:
            M["ind_rank5"][k] = RANK[:, j]
            M["ind5"][k] = R5[:, j]


def _v4_portfolio_sim(mats, tier, rules, initial=_V4_CAPITAL):
    """组合级事件回测（矩阵版，唯一实现）。

    - 信号日收盘成交；买入=收盘×(1+滑点)(1+佣金)；卖出=×(1-滑点)(1-佣金-印花税)
      （v4.0.1 起佣金/印花税记 0，仅保留滑点）
    - 涨停禁买、跌停顺延；停牌持仓顺延；期末强平
    - dist 分布退出（默认）：Q10棘轮止损（只收紧）+ Q75目标 + p_up 信号退出
    - hybrid 混合退出（消融对照）：Q10棘轮 + 移动止盈棘轮 + p_up（修正 bug 后实证劣于 dist）
    - 对照退出：ATR止损/移动止盈（v3.3 稳健参数，highest 逐日更新）
    - baseline：v3.3 多维评分信号进出
    """
    cal, M, codes = mats
    rp = CFG.RISK_PARAMS["稳健"]
    mode = rules.get("mode", "full")
    use_dist = rules.get("use_dist_exit", True) \
        and rules.get("use_quantile", True)
    # 默认 dist（v4.0 纯分布退出）：修正 highest 逐日更新 bug 后的全量实证
    # 显示 1.02/0.94 移动止盈在组合级是最大拖累（-13pp vs dist），dist 全场最优
    exit_mode = rules.get("exit_mode", "dist")
    # 降频开关（默认关闭！300只实测：冷却5根反而把年化 +10.6%→-3.1%——
    # 退出后信号仍有效时快速再入场是收益来源之一，勿硬压频率）。
    # cooldown=平仓后同股再入场冷却根数；min_hold=p_up 信号退出前最少持仓根数
    # （止损/移动止盈不受 min_hold 限制）。baseline 信号自带冷却，不重复加。
    cd = 0 if mode == "baseline" else int(rules.get("cooldown", 0))
    mh = 0 if mode == "baseline" else int(rules.get("min_hold", 0))
    cool = {}                           # code_idx -> 最后一卖出的 t
    # 以小博大选项：stop_q=止损参考分位(10/25, 越小越宽/越大越紧)；
    # trail_slow=移动止盈用激进参数(触发1.05/回落10%, 让盈利跑更久)；
    # reentry_tier=平仓后 reentry_bars 根内按更高一档阈值再入场（质量门槛替代时间门槛）
    qstop = M["q25"] if int(rules.get("stop_q", 10)) == 25 else M["q10"]
    trp = CFG.RISK_PARAMS["激进"] if rules.get("trail_slow") else rp
    _strict = _V4_TIERS.get({"平衡": "保守", "激进": "平衡"}
                            .get(rules.get("tier_name", "")))
    re_bars = int(rules.get("reentry_bars", 8) or 8)
    cost = _V4_COST
    buy_mult = (1 + cost["slip"]) * (1 + cost["commission"])
    sell_mult = (1 - cost["slip"]) * (1 - cost["commission"]
                                      - cost["stamp"])
    ns, nc = M["close"].shape
    ok_entry = _v4_entry_mask(M, rules, tier)
    cash = initial
    pos = {}
    last_px = np.zeros(ns, np.float64)
    eq_curve = []
    trades = []
    for t in range(nc):
        col = M["close"][:, t]
        upd = np.isfinite(col)
        last_px[upd] = col[upd].astype(np.float64)
        # ---- 退出 ----
        for ks in sorted(pos):
            if not M["has_bar"][ks, t]:
                continue                # 停牌顺延
            p = pos[ks]
            px_c = float(col[ks])
            px_o = float(M["open"][ks, t])
            hi = float(M["high"][ks, t])
            lo = float(M["low"][ks, t])
            p["highest"] = max(p["highest"], hi)   # 逐日更新最高价（v3.3 同口径；此前缺失→移动止盈失效）
            sold = False
            if not M["limit_dn"][ks, t]:
                if mode == "baseline" or p["atr_fallback"] or not use_dist:
                    atr_t = float(M["atr"][ks, t])
                    if p["highest"] > p["entry"] * rp["trail_trigger"]:
                        stop = p["highest"] * rp["trail_ratio"]
                    else:
                        stop = p["entry"] - rp["atr_mult"] * max(atr_t, 1e-9)
                    if lo <= stop:
                        px = px_o if px_o <= stop else min(stop, hi)
                        sold = True
                    elif mode == "baseline" and M["base_sell"][ks, t]:
                        px = px_c
                        sold = True
                else:
                    # Q 棘轮止损（只收紧不放宽；stop_q 可选 10/25 分位）
                    if rules.get("use_q10_stop", True):
                        qs = qstop[ks, t]
                        if np.isfinite(qs):
                            p["stop"] = max(p["stop"], p["entry"]
                                            * (1.0 + float(qs)))
                    # 移动止盈棘轮：浮盈触发后随最高价上移（拉长持仓）
                    if exit_mode == "hybrid" \
                            and rules.get("use_trailing", True) \
                            and p["highest"] > p["entry"] * trp["trail_trigger"]:
                        p["stop"] = max(p["stop"],
                                        p["highest"] * trp["trail_ratio"])
                    if lo <= p["stop"]:
                        px = px_o if px_o <= p["stop"] else min(p["stop"], hi)
                        sold = True
                    elif exit_mode == "dist" and hi >= p["target"]:
                        px = px_o if px_o >= p["target"] else p["target"]
                        sold = True
                    elif t - p["t_in"] >= mh \
                            and rules.get("use_logistic", True) \
                            and np.isfinite(M["p_up"][ks, t]) \
                            and float(M["p_up"][ks, t]) < tier["exit_p"]:
                        px = px_c
                        sold = True
            if sold:
                cool[ks] = t
                net = px * sell_mult
                cash += p["shares"] * net
                trades.append({"code": codes[ks],
                               "ret": net / p["buy_net"] - 1.0,
                               "pnl": p["shares"] * (net - p["buy_net"]),
                               "hold": t - p["t_in"]})
                del pos[ks]
        # ---- 入场 ----
        if len(pos) < tier["max_pos"]:
            for ks in np.nonzero(ok_entry[:, t])[0]:
                if len(pos) >= tier["max_pos"]:
                    break
                if ks in pos:
                    continue
                if t - cool.get(ks, -10**9) < cd:
                    continue             # 平仓冷却，防同股频繁进出
                if mode == "full" \
                        and rules.get("reentry_tier", True) \
                        and _strict is not None \
                        and t - cool.get(ks, -10**9) < re_bars \
                        and not _v4_entry_ok_cell(M, ks, t, _strict, rules):
                    continue             # 交易后再入场：按更高一档信号要求
                px_c = float(M["close"][ks, t])
                buy_net = px_c * buy_mult
                eq0 = cash + sum(pp["shares"] * last_px[k2]
                                 for k2, pp in pos.items())
                shares = int(eq0 * tier["frac"] / buy_net / 100.0) * 100
                if shares <= 0 or shares * buy_net > cash:
                    continue
                cash -= shares * buy_net
                p = {"t_in": t, "shares": shares, "buy_net": buy_net,
                     "entry": px_c,
                     "highest": max(px_c, float(M["high"][ks, t]))}
                if use_dist and np.isfinite(qstop[ks, t]) \
                        and np.isfinite(M["q75"][ks, t]):
                    if rules.get("use_q10_stop", True):
                        p["stop"] = p["entry"] * (1.0 + float(qstop[ks, t]))
                    else:
                        p["stop"] = 0.0     # 无初始止损，随棘轮/移动止盈上移
                    p["target"] = p["entry"] * (1.0 + float(M["q75"][ks, t]))
                    p["atr_fallback"] = False
                else:
                    p["stop"] = None
                    p["target"] = None
                    p["atr_fallback"] = True
                pos[ks] = p
        # ---- 收盘权益 ----
        eq = cash + sum(pp["shares"] * last_px[k2]
                        for k2, pp in pos.items())
        eq_curve.append(eq)
    # 期末强平
    n_forced = 0
    for ks in sorted(pos):
        pp = pos[ks]
        px = last_px[ks] if last_px[ks] > 0 else pp["entry"]
        net = px * sell_mult
        cash += pp["shares"] * net
        trades.append({"code": codes[ks], "ret": net / pp["buy_net"] - 1.0,
                       "pnl": pp["shares"] * (net - pp["buy_net"]),
                       "hold": nc - 1 - pp["t_in"]})
        n_forced += 1
    stock_days = int(M["has_bar"].sum())
    m = _v4_metrics(eq_curve, cal, trades, stock_days=stock_days)
    m["forced_closes"] = n_forced
    return m


def run_v4_research(min_bars=400, limit=0, progress=None):
    """v4.0 全A研究：Walk-Forward 自适应ML + 三档风险回测 + 消融。

    返回 report dict（同时落盘 research/v4_report.json 与
    research/v4_factors.json）。"""
    from concurrent.futures import ProcessPoolExecutor
    p = progress or (lambda s: None)
    deps = _v4_deps()
    if np is None or deps.get("numpy") is None:
        raise RuntimeError("v4.0 需要 numpy：pip install numpy")
    if deps.get("sklearn") is None:
        raise RuntimeError("v4.0 需要 scikit-learn：pip install scikit-learn")
    p("v4.0：加载股票池与市场/行业上下文 ...")
    with db_conn() as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM daily_bars GROUP BY code "
            "HAVING COUNT(*) >= ? AND (code LIKE 'sh60%' OR code LIKE 'sh68%'"
            " OR code LIKE 'sz00%' OR code LIKE 'sz30%')",
            (min_bars,)).fetchall()]
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
    if limit:
        codes = codes[:limit]
    # ---- 预测缓存：Walk-Forward 结果与退出规则/成本无关，命中则跳过重训练 ----
    # sig 绑定 股票池+min_bars+数据指纹+因子版本，任一变化自动失效；
    # V4_NO_CACHE=1 强制重算
    _cache = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "research", "v4_preds.pkl")
    _sig = None
    preds = None
    if os.environ.get("V4_NO_CACHE") != "1":
        try:
            with db_conn() as _c:
                _dmax = _c.execute(
                    "SELECT MAX(date) FROM daily_bars").fetchone()[0] or ""
                _nbars = _c.execute(
                    "SELECT COUNT(*) FROM daily_bars").fetchone()[0]
            _sig = {"codes": codes, "min_bars": min_bars,
                    "dmax": _dmax, "nbars": _nbars, "ver": _V4_CACHE_VER}
            if os.path.exists(_cache):
                import pickle as _pk
                with open(_cache, "rb") as f:
                    _blob = _pk.load(f)
                if _blob.get("sig") == _sig and _blob.get("preds"):
                    preds = _blob["preds"]
                    p("v4.0：命中预测缓存 research/v4_preds.pkl（%d 只，"
                      "跳过重训练）" % len(preds))
        except Exception:
            log.exception("v4 预测缓存读取失败（忽略，重新计算）")
            preds = None
    # 市场/行业等权日收益（一遍扫描；无论命中缓存与否都要——板块轮动门槛用）
    mkt, ind = _v4_mkt_ind_ctx(ind_of)
    while preds is None:            # 未命中缓存：完整重算（最多执行一次）
        # ---- 分块多进程 Walk-Forward ----
        preds = []
        n_done = 0
        CH = 120
        workers = max(1, min(8, os.cpu_count() or 2))
        p(f"v4.0：Walk-Forward 全A计算（{len(codes)}只 × {len(_V4_HORIZONS)}周期, "
          f"{workers}进程）...")
        with ProcessPoolExecutor(max_workers=workers,
                                 initializer=_v4_worker_init,
                                 initargs=(mkt, ind)) as ex:
            for ci in range(0, len(codes), CH):
                chunk = codes[ci:ci + CH]
                bars_map = {}
                with db_conn() as conn:
                    ph = ",".join("?" for _ in chunk)
                    rws = conn.execute(
                        f"SELECT code,date,open,high,low,close,vol FROM ("
                        f" SELECT *, ROW_NUMBER() OVER (PARTITION BY code "
                        f" ORDER BY date DESC) rn FROM daily_bars "
                        f" WHERE code IN ({ph})"
                        f") WHERE rn<=1000 ORDER BY code, date", chunk).fetchall()
                for c, d, o, h, l, cl, v in rws:
                    bars_map.setdefault(c, []).append(
                        {"date": d, "open": o, "high": h, "low": l,
                         "close": cl, "vol": v or 0.0})
                jobs = [(c, b, ind_of.get(c, ""))
                        for c, b in bars_map.items() if len(b) >= min_bars]
                for res in ex.map(_v4_walkforward_one, jobs):
                    if res:
                        preds.append(res)
                        n_done += 1
                p(f"v4.0 进度 {min(ci + CH, len(codes))}/{len(codes)}"
                  f"（有效 {n_done}）")
        if not preds:
            raise RuntimeError("v4.0：无有效股票（缓存不足或依赖缺失）")
        try:
            os.makedirs(os.path.dirname(_cache), exist_ok=True)
            import pickle as _pk
            with open(_cache, "wb") as f:
                _pk.dump({"sig": _sig, "preds": preds}, f, protocol=4)
            p("v4.0：预测已缓存 research/v4_preds.pkl（同股票池/数据下规则迭代秒级重跑）")
        except Exception:
            log.exception("v4 预测缓存落盘失败（忽略）")
        break

    # ---- 堆叠矩阵（一次构建，全部指标/回测共用） ----
    p("v4.0：汇总 Horizon / 模型 / 分位数 指标 ...")
    mats = _v4_stack(preds)
    cal, M, codes_s = mats
    _v4_attach_rotation(M, cal, codes_s, ind_of, mkt, ind)

    # Horizon 实验（矩阵向量化 + 逐股IC序列）
    horizon = {}
    for H in _V4_HORIZONS:
        e = _v4_pool_eval(M["ml%d" % H], M["y%d" % H])
        e["ic_ir"] = _v4_icir_rows(M["ml%d" % H], M["y%d" % H])
        horizon[str(H)] = e
    hh = M["h_choice"][M["h_choice"] > 0]
    cnt = np.bincount(hh.astype(np.int64), minlength=11)
    horizon["h_choice_dist"] = {str(H): int(cnt[H]) for H in _V4_HORIZONS}

    # 模型比较（vs 当日所选 Horizon 标签）
    models = {
        "ml": _v4_pool_eval(M["ml_dyn"], M["y_dyn"], th=0.0),
        "q50": _v4_pool_eval(M["q50"], M["y_dyn"], th=0.0),
        "adaptive": _v4_pool_eval(M["adaptive"], M["y_dyn"], th=0.0),
        "p_up": _v4_pool_eval(M["p_up"], M["y_dyn"], th=0.5),
        "l1_up": _v4_pool_eval(M["l1_up"], M["y_dyn"], th=0.5),
        "l1_ret": _v4_pool_eval(M["l1_ret"], M["y_dyn"], th=0.0),
    }
    models["p_up"]["mae"] = None           # 概率模型 MAE 无意义
    models["l1_up"]["mae"] = None
    # v3.3 多维评分信号状态 pooled IC（事件日，vs T+1 收益）
    mev = M["base_buy"] | M["base_sell"]
    if int(mev.sum()) >= 25:
        xs_e = np.where(M["base_buy"][mev], 1.0, -1.0)
        models["baseline_composite"] = _v4_pool_eval(
            xs_e.astype(np.float32), M["y1"][mev], th=0.0)
    else:
        models["baseline_composite"] = {"ic": None, "mae": None,
                                        "hit": None, "n": int(mev.sum()),
                                        "ic_ir": None}
    models["baseline_composite"]["mae"] = None

    # Quantile 诊断 / 过拟合检查
    qdiag = _v4_quantile_diag_m(M)
    cross = [r["q_cross"] for r in preds if r.get("q_cross") is not None]
    qdiag["crossing_raw_med"] = float(np.median(cross)) if cross else None
    ins = [r["ins_ic"] for r in preds if r.get("ins_ic") is not None]
    overfit = {
        "lgbm_train_ic_med": float(np.median(ins)) if ins else None,
        "lgbm_test_ic": models["ml"]["ic"],
        "flag": None, "note": "Train IC 显著高于 Test IC → 过拟合标记",
    }
    if overfit["lgbm_train_ic_med"] is not None \
            and models["ml"]["ic"] is not None:
        overfit["flag"] = bool(
            overfit["lgbm_train_ic_med"] - models["ml"]["ic"] > 0.15)
    feat_imp_top = {}
    for r in preds:
        if r.get("feat_imp"):
            for f, g in r["feat_imp"]:
                feat_imp_top[f] = feat_imp_top.get(f, 0.0) + float(g)
    feat_imp_top = dict(sorted(feat_imp_top.items(), key=lambda x: -x[1]))

    # 因子聚合
    factors = _v4_factor_agg(preds)

    # ---- 组合回测：Baseline / Adaptive / Full 三档 + 消融 ----
    # 时间对齐子样本：测试段结束距全局最新日 ≤45 自然日；
    # 退市股旧窗口只参与 IC/模型统计（对齐隐含幸存者偏差，如实记录）
    import datetime as _dt4
    last_d = max(r["dates"][-1] for r in preds)
    _ld = _dt4.date.fromisoformat(last_d)
    rows_bt = [k for k, r in enumerate(preds)
               if (_ld - _dt4.date.fromisoformat(r["dates"][-1])).days <= 45]
    mats_bt = _v4_stack_subset([preds[k] for k in rows_bt])
    _v4_attach_rotation(mats_bt[1], mats_bt[0], mats_bt[2], ind_of, mkt, ind)
    p("v4.0：组合级回测（三档风险 × 策略 × 消融，"
      f"{len(rows_bt)}/{len(preds)} 只时间对齐）...")
    sims = {}
    tier_of_mode = {"保守": "保守", "稳健": "平衡", "激进": "激进"}
    for mode in ("保守", "稳健", "激进"):
        rules = {"mode": "baseline", "use_logistic": False,
                 "use_adaptive": False, "use_lgbm": False,
                 "use_quantile": False, "use_dist_exit": False}
        sims["baseline:" + mode] = _v4_portfolio_sim(
            mats_bt, _V4_TIERS[tier_of_mode[mode]], rules)
    for tn in ("保守", "平衡", "激进"):
        rules = {"mode": "adaptive", "use_logistic": False,
                 "use_lgbm": False, "use_quantile": False,
                 "use_dist_exit": False}
        sims["adaptive:" + tn] = _v4_portfolio_sim(
            mats_bt, _V4_TIERS[tn], rules)
        sims["full:" + tn] = _v4_portfolio_sim(
            mats_bt, _V4_TIERS[tn], {"mode": "full", "tier_name": tn})
    for vname, vr in _V4_VARIANTS.items():
        for tn in ("保守", "平衡", "激进"):
            rules = {"mode": "full", "tier_name": tn}
            rules.update(vr)
            sims["abl:%s:%s" % (vname, tn)] = _v4_portfolio_sim(
                mats_bt, _V4_TIERS[tn], rules)

    def _strip(m):
        return {k: v for k, v in m.items()
                if k not in ("equity", "dates", "trade_list")}

    strategies = {k: _strip(v) for k, v in sims.items()
                  if not k.startswith("abl:")}
    ablation = {}
    for vname, vr in _V4_VARIANTS.items():
        rules = {"mode": "full"}
        rules.update(vr)
        ic_m = _v4_entry_score(mats, rules)
        ablation[vname] = {
            "tier": {tn: _strip(sims["abl:%s:%s" % (vname, tn)])
                     for tn in ("保守", "平衡", "激进")},
            "entry_score_ic": ic_m["ic"],
            "entry_score_mae": ic_m["mae"],
        }

    # ---- 元信息 / 泄漏检查 ----
    all_dates = [d for r in preds for d in (r["dates"][0], r["dates"][-1])]
    spans = [len(r["dates"]) for r in preds]
    report = {
        "meta": {
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "deps": deps,
            "n_codes_pool": len(codes),
            "n_valid": len(preds),
            "n_valid_bt": len(rows_bt),
            "bt_align_note": "组合回测使用测试段结束距最新日≤45自然日的"
                             "时间对齐子样本（隐含幸存者偏差，如实记录）",
            "min_bars": min_bars,
            "date_min": min(all_dates),
            "date_max": max(all_dates),
            "span_med": int(np.median(spans)),
            "fold": _V4_FOLD,
            "warmup": _V4_WARMUP,
            "train_min": _V4_TRAIN_MIN,
            "cost": _V4_COST,
            "capital": _V4_CAPITAL,
            "lgbm_params": _V4_LGBM,
            "note_data": "回测范围为本地缓存可用数据；"
                         "见 n_codes_pool/n_valid。",
        },
        "horizon": horizon,
        "models": models,
        "quantile": qdiag,
        "overfit": overfit,
        "feat_imp_top": feat_imp_top,
        "factors": factors,
        "strategies": strategies,
        "ablation": ablation,
        "leakage_check": [
            "特征仅用 T 日及以前数据（形态匹配样本窗口结束于 T-W 之前）",
            "标签 Close[T+H]/Close[T]-1 仅作训练目标，不进特征",
            "StandardScaler/Lasso筛选/Horizon选择均只在训练段完成",
            "Walk-Forward 扩展窗：每折仅用折前数据训练，折间不重叠",
            "LightGBM 超参先验固定，未用 Test 调参",
            "三档风险为同一模型输出上的决策层参数，未分别训练",
            "组合回测统一初始资金/手续费/滑点/成交时点/涨跌停/停牌规则",
        ],
    }

    # ---- 落盘 ----
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "research")
    try:
        os.makedirs(out_dir, exist_ok=True)

        def _jb(o):
            if isinstance(o, dict):
                return {str(k): _jb(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [_jb(v) for v in o]
            if isinstance(o, (np.floating, np.integer)):
                return o.item()
            if isinstance(o, float) and not np.isfinite(o):
                return None
            if isinstance(o, np.bool_):
                return bool(o)
            return o

        with open(os.path.join(out_dir, "v4_report.json"), "w",
                  encoding="utf-8") as f:
            json.dump(_jb(report), f, ensure_ascii=False, indent=1)
        with open(os.path.join(out_dir, "v4_factors.json"), "w",
                  encoding="utf-8") as f:
            json.dump(_jb({r["code"]: r["factor_table"] for r in preds
                           if r.get("factor_table")}),
                      f, ensure_ascii=False, indent=1)
        p("v4.0：报告已写入 research/v4_report.json")
    except Exception:
        log.exception("v4 报告落盘失败")
    return report


def _v4_print_report(r):
    """控制台摘要（CLI --v4）。"""
    m = r["meta"]
    print("=" * 76)
    print(f"v4.0 全A研究  {m['ts']}  股票池 {m['n_codes_pool']} 只 / "
          f"有效 {m['n_valid']} 只  "
          f"区间 {m['date_min']} ~ {m['date_max']}（中位{m['span_med']}日）")
    print(f"依赖: numpy={m['deps'].get('numpy')} "
          f"sklearn={m['deps'].get('sklearn')} "
          f"lightgbm={m['deps'].get('lightgbm')}")
    print("-" * 76)
    print("Horizon 实验（LightGBM 预测 vs 真实收益）")
    print(f"{'H':>4}{'IC':>9}{'IC_IR':>8}{'MAE':>9}{'方向命中':>9}{'样本':>9}")
    for H in ("1", "5", "10"):
        e = r["horizon"].get(H, {})
        f = lambda v, k=100: "-" if v is None else f"{v*k:+.3f}"
        print(f"{H:>4}{f(e.get('ic'), 1):>9}{f(e.get('ic_ir'), 1):>8}"
              f"{f(e.get('mae'), 1):>9}"
              f"{f(e.get('hit')):>9}{e.get('n', 0):>9}")
    print("Horizon 自适应选择分布: " + str(r["horizon"].get("h_choice_dist")))
    print("-" * 76)
    print("模型比较（vs 当日所选 Horizon 真实收益）")
    print(f"{'模型':<20}{'IC':>9}{'MAE':>9}{'方向命中':>9}{'样本':>9}")
    NM = {"ml": "LightGBM", "q50": "Quantile Q50", "adaptive": "Lasso得分",
          "p_up": "Logistic p_up", "l1_up": "L1形态up_prob",
          "l1_ret": "L1相似样本收益", "baseline_composite": "v3.3多维评分"}
    for k, e in r["models"].items():
        f = lambda v, k2=100: "-" if v is None else f"{v*k2:+.3f}"
        print(f"{NM.get(k, k):<20}{f(e.get('ic'), 1):>9}"
              f"{f(e.get('mae'), 1):>9}{f(e.get('hit')):>9}{e['n']:>9}")
    q = r["quantile"]
    print("-" * 76)
    print("Quantile 诊断: " + "  ".join(
        f"P{qk.split('_')[1]}={v:.4f}" if v is not None else "-"
        for qk, v in q.items() if qk.startswith("pinball"))
        + f"  覆盖率[10,90]={q.get('coverage_10_90')}"
        + f"  原始交叉率={q.get('crossing_raw_med')}")
    o = r["overfit"]
    print(f"LightGBM 过拟合检查: Train IC 中位 {o['lgbm_train_ic_med']} "
          f"vs Test IC {o['lgbm_test_ic']} → "
          f"{'⚠️疑似过拟合' if o['flag'] else '未见明显过拟合'}")
    print("-" * 76)
    print("因子有效性（跨股聚合）")
    print(f"{'因子':<10}{'IC中位':>9}{'稳定性':>8}{'入选率':>8}{'中位|权重|':>10}")
    for f_, a in r["factors"].items():
        if f_.startswith("_"):
            continue
        print(f"{f_:<10}{a['train_ic_med']:+9.3f}{a['stability_med']:8.2f}"
              f"{a['sel_pct']*100:7.0f}%{a['weight_med']:10.4f}")
    print("-" * 76)
    _c = _V4_COST
    print("组合回测（成本：滑点%.2f%%/佣金%.3f%%/印花税%.2f%%；初始%d万）"
          % (_c["slip"] * 100, _c["commission"] * 100, _c["stamp"] * 100,
             int(_V4_CAPITAL / 10000)))
    print(f"{'策略':<24}{'年化':>9}{'回撤':>9}{'Calmar':>8}{'Sharpe':>8}"
          f"{'胜率':>7}{'盈亏比':>7}{'交易':>6}{'均持仓':>7}")
    for k, v in r["strategies"].items():
        g = lambda v_, d=1: "-" if v_ is None else f"{v_*100:+.{d}f}%"
        g2 = lambda v_: "-" if v_ is None else f"{v_:.2f}"
        hold = "-" if v["avg_hold"] is None else f"{v['avg_hold']:.1f}"
        print(f"{k:<24}{g(v['ann']):>9}{g(v['mdd']):>9}"
              f"{g2(v['calmar']):>8}{g2(v['sharpe']):>8}"
              f"{g(v['winrate']):>7}{g2(v['pf']):>7}{v['trades']:>6}"
              f"{hold:>7}")
    print("-" * 76)
    print("消融实验（平衡档；IC=该变体入场分数 pooled IC）")
    print(f"{'变体':<28}{'年化':>9}{'回撤':>9}{'Calmar':>8}{'Sharpe':>8}"
          f"{'胜率':>7}{'交易':>6}{'IC':>8}")
    for vn, a in r["ablation"].items():
        v = a["tier"]["平衡"]
        g = lambda v_, d=1: "-" if v_ is None else f"{v_*100:+.{d}f}%"
        g2 = lambda v_: "-" if v_ is None else f"{v_:.2f}"
        ic = a.get("entry_score_ic")
        print(f"{vn:<28}{g(v['ann']):>9}{g(v['mdd']):>9}"
              f"{g2(v['calmar']):>8}{g2(v['sharpe']):>8}{g(v['winrate']):>7}"
              f"{v['trades']:>6}{('-' if ic is None else f'{ic:+.3f}'):>8}")
    print("=" * 76)
    print("注：全部为历史统计研究，不构成投资建议。")


def slice_view(res, show_n, pan=0):
    n_total = len(res["disp_rows"])
    off = max(0, n_total - show_n - pan)
    end = min(n_total, off + show_n)
    vis = res["disp_rows"][off:end]
    pd = res["pred"]["date"]
    # 可见区间筹码：平移后筹码随区间变化
    vis_chips = None
    try:
        vis_chips = calc_chips(vis, vis[-1]["close"]) if vis else None
    except Exception:
        vis_chips = None
    # 幽灵K线追加：T+1预测 + T+5/T+10（白色虚线边框）
    ghosts = [g for g in (res.get("ghosts") or []) if g]
    view = {
        "bars": vis + [res["pred"]] + ghosts,
        "dates": ([r["date"] for r in vis]
                  + ["T+1" if pd.startswith("T+") else "T日"]
                  + [g["date"] for g in ghosts]),
        "off": off,
        "tpred": res.get("tpred_bar"),
        "chips": vis_chips or res.get("chips"),
        "phase": (res.get("phase", "")
                  + (" · 预测锚定昨收" if res.get("pre_open") else "")),
        "pred_label": "T+1" if pd.startswith("T+") else "T日",
        "ma": {nn: vals[off:end] for nn, vals in res["ind"]["ma"].items()},
        "dif": res["ind"]["dif"][off:end], "dea": res["ind"]["dea"][off:end],
        "mhist": res["ind"]["mhist"][off:end],
        "k": res["ind"]["k"][off:end], "d": res["ind"]["d"][off:end],
        "j": res["ind"]["j"][off:end],
        "rsi6": res["ind"]["rsi6"][off:end], "rsi12": res["ind"]["rsi12"][off:end],
        "boll_mid": res["ind"]["boll_mid"][off:end],
        "boll_up": res["ind"]["boll_up"][off:end],
        "boll_low": res["ind"]["boll_low"][off:end], "pdi": res["ind"]["pdi"][off:end], "mdi": res["ind"]["mdi"][off:end], "adx": res["ind"]["adx"][off:end],
        "vols": res["vols"][off:end] + [None] * (1 + len(ghosts)),
        "signals": [(i - off, dt, t, txt) for i, dt, t, txt in res["signals"]
                    if off <= i < end],
    }
    view["ghost_n"] = len(ghosts)
    return view


# ================= 邮件发送（授权码来自香橙派 ai-quant 日报配置） =================

EMAIL_SMTP_HOST = "smtp.163.com"
EMAIL_SMTP_PORT = 465
EMAIL_SENDER = "languangxunlh@163.com"
EMAIL_AUTH_CODE = "KYVmx5RTa7s4zUcA"
EMAIL_RECIPIENTS = ("19526719996@163.com", "2180287399@qq.com")


def _load_email_cfg():
    """ini [email] 可覆盖默认值。"""
    global EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, EMAIL_SENDER, EMAIL_AUTH_CODE
    global EMAIL_RECIPIENTS
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        if cp.has_section("email"):
            EMAIL_SMTP_HOST = cp.get("email", "host", fallback=EMAIL_SMTP_HOST)
            EMAIL_SMTP_PORT = cp.getint("email", "port", fallback=EMAIL_SMTP_PORT)
            EMAIL_SENDER = cp.get("email", "sender", fallback=EMAIL_SENDER)
            EMAIL_AUTH_CODE = cp.get("email", "auth_code",
                                     fallback=EMAIL_AUTH_CODE)
            rcpt = cp.get("email", "recipients", fallback="")
            if rcpt:
                EMAIL_RECIPIENTS = tuple(
                    r.strip() for r in rcpt.replace("；", ";").split(";")
                    if r.strip())
    except Exception:
        log.exception("读取邮箱配置失败(使用默认)")
    return (EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, EMAIL_SENDER, EMAIL_AUTH_CODE,
            EMAIL_RECIPIENTS)


def send_email_report(subject, body, recipients=None):
    """发送文本邮件（纯标准库）。返回实际收件人元组。"""
    import smtplib
    import ssl
    from email.header import Header
    from email.mime.text import MIMEText
    host, port, sender, auth, rcpts = _load_email_cfg()
    rcpts = tuple(recipients) if recipients else rcpts
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = ", ".join(rcpts)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(sender, auth)
        s.sendmail(sender, rcpts, msg.as_string())
    log.info("邮件已发送: %s -> %s", subject, rcpts)
    return rcpts


# ================= GUI =================

class Chart(tk.Canvas):
    def __init__(self, master, height):
        super().__init__(master, height=height, bg=BG, highlightthickness=0)


def deepseek_chat(api_key: str, prompt: str, model=None, timeout: int = 90):
    """单轮调用 DeepSeek chat 接口（纯标准库）。model 缺省用 AI_MODEL。"""
    return _deepseek_chat(api_key, [{"role": "user", "content": prompt}],
                          model, timeout)


def _deepseek_chat(api_key, messages, model=None, timeout=90):
    """多轮调用 DeepSeek chat 接口。messages 为 [{role,content},...]，
    首条 user 消息应携带完整共享数据上下文，后续追问只追加新问题，
    从而复用同一份数据（不重复拼装）。model 缺省用 ini 配置的 AI_MODEL
    （默认 deepseek-v4-pro）。"""
    body = json.dumps({
        "model": model or AI_MODEL,
        "messages": [
            {"role": "system",
             "content": "你是专业A股分析师，回答简洁直接，给出可操作建议并附风险提示。"},
        ] + list(messages),
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        opener = _PROXY_OPENER or urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        if e.code == 401:
            raise RuntimeError("API Key 无效 (401)，请在设置中检查")
        raise RuntimeError(f"HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}")
    return d["choices"][0]["message"]["content"]


class App:
    PANEL_H = {"main": 400, "vol": 110, "ind": 160}
    REFRESH_MS = 15 * 60 * 1000     # 完整重分析间隔
    TICK_MS = 60 * 1000             # 行情快照刷新（1分钟）

    def __init__(self, root):
        self.root = root
        root.title("股票形态相似度预测工具 · 增强版")
        # 窗口尺寸自适应屏幕分辨率（不超出屏幕可用区域）
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        # 小屏模式：2.4寸等触摸小屏（宽≤700 或 高≤500）自动全屏 + 精简布局
        self.compact = (sw <= 700 or sh <= 500)
        if self.compact:
            w, h = sw, sh
            root.attributes("-fullscreen", True)
            self.PANEL_H = {"main": int(sh * 0.44), "vol": int(sh * 0.13),
                            "ind": int(sh * 0.18)}
        else:
            w = max(900, min(1280, sw - 24))
            h = max(600, min(810, sh - 60))
        root.geometry(f"{w}x{h}")
        self.settings = {"theme": "dark", "updown": "red_up"}
        self.api_key = ""
        self.watchlist = []
        self.ai_text = ""
        self.idx_data = {}
        self._prog_running = False
        self._ai_msgs = []          # LLM 多轮对话历史 [{role,content},...]
        self._load_config()
        apply_theme(self.settings["theme"], self.settings["updown"])
        root.configure(bg=DARK_BG)
        self._style_ttk()
        self.res = None
        self.view = None
        self.scales = {}
        self.show_n = tk.IntVar(value=30 if self.compact else 60)
        self.ind_name = tk.StringVar(value="MACD")
        self.show_chips = tk.BooleanVar(value=not self.compact)
        self.view_pan = 0       # 平移偏移：0=最新，正=往左看更早
        self.ma_on = {nn: tk.BooleanVar(value=True) for nn in MA_COLORS}

        self._build_toolbar()
        # 注册AI找源确认钩子：数据源全灭时弹窗询问（主线程弹窗，后台执行）
        globals()["_AI_RESCUE_HOOK"] = self._ai_rescue_flow
        if getattr(self, "_last_code", ""):
            self.code_var.set(self._last_code)
        self._build_body()
        if not self.compact:
            self._refresh_names()       # 启动即拉取自选池名称（后台）
        if CACHE_OK:
            threading.Thread(target=self._warm_cache, daemon=True).start()
        if self.code_var.get():
            self.run()
        self._update_indices()          # 立即刷新指数
        if not self.compact:
            self._update_sectors()      # 行业排行（小屏无该面板，跳过）
        self._safe_after(30000, self._index_loop)
        self._safe_after(self.REFRESH_MS, self._auto_refresh)   # 15分钟完整重分析
        self._safe_after(self.TICK_MS, self._tick)              # 5秒行情快照

    def _style_ttk(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=DARK_BG, foreground=FG_MAIN,
                        fieldbackground=FIELD_BG, bordercolor="#2a3340",
                        lightcolor=PANEL_BG, darkcolor="#12171d",
                        troughcolor=DARK_BG)
        style.configure("TFrame", background=DARK_BG)
        style.configure("TLabelframe", background=DARK_BG,
                        bordercolor="#2a3340")
        style.configure("TLabelframe.Label", background=DARK_BG,
                        foreground=TITLE_TXT)
        style.configure("TLabel", background=DARK_BG, foreground=FG_MAIN)
        style.configure("TButton", background=BTN_BG, foreground=BTN_FG,
                        bordercolor=BTN_BORDER)
        style.map("TButton", background=[("active", BTN_HOVER)])
        style.configure("Tool.TButton", background=BTN_BG, foreground=BTN_FG,
                        bordercolor=BTN_BORDER,
                        font=("Microsoft YaHei", 10, "bold"), padding=(16, 4))
        style.map("Tool.TButton",
                  background=[("pressed", BTN_HOVER), ("active", BTN_HOVER)],
                  foreground=[("disabled", AXIS_TXT)])
        style.configure("TEntry", fieldbackground=FIELD_BG, foreground=FG_MAIN)
        style.configure("TCombobox", fieldbackground=FIELD_BG,
                        foreground=FG_MAIN, background=BTN_BG, arrowcolor=FG_MAIN)
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD_BG)],
                  foreground=[("readonly", FG_MAIN)])
        style.configure("TScrollbar", background=BTN_BG,
                        troughcolor=DARK_BG)
        style.configure("TNotebook", background=DARK_BG,
                        bordercolor=BTN_BORDER, tabmargins=[4, 4, 4, 0])
        style.configure("TNotebook.Tab", background=BTN_BG,
                        foreground=FG_MAIN, bordercolor=BTN_BORDER,
                        padding=[12, 6])
        style.map("TNotebook.Tab",
                  background=[("selected", BTN_HOVER), ("active", BTN_HOVER)],
                  foreground=[("selected", "#ffffff")])
        style.configure("Checkbutton", background=DARK_BG, foreground=FG_MAIN)
        style.map("Checkbutton", background=[("active", DARK_BG)])

    # ---------- 布局 ----------

    def _build_toolbar(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="代码:").pack(side="left")
        self.code_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.code_var,
                        width=9 if self.compact else 14)
        ent.pack(side="left", padx=3)
        ent.bind("<Return>", lambda e: self.run())
        self.btn_run = ttk.Button(top, text="分析预测", command=self.run)
        self.btn_run.pack(side="left", padx=3)

        if self.compact:
            # 两行工具条，保证 320px 宽全部可见（label/entry/分析预测已在上）
            ttk.Button(top, text="⚙", width=3,
                       command=self.open_settings).pack(side="right", padx=2)
            top2 = ttk.Frame(self.root)
            top2.pack(fill="x")
            ttk.Label(top2, text="周期:").pack(side="left")
            cb = ttk.Combobox(top2, textvariable=self.show_n, width=4,
                              state="readonly", values=[15, 30, 45, 60])
            cb.pack(side="left", padx=2)
            cb.bind("<<ComboboxSelected>>", lambda e: self._rerender())
            ttk.Label(top2, text="副图:").pack(side="left")
            ci = ttk.Combobox(top2, textvariable=self.ind_name, width=5,
                              state="readonly", values=["MACD", "KDJ", "RSI", "BOLL", "ADX"])
            ci.pack(side="left", padx=2)
            ci.bind("<<ComboboxSelected>>", lambda e: self._rerender())
            # 小屏：报告/样本/缓存/指数 全部收进【工具】菜单
            ttk.Button(top2, text="工具", width=4,
                       command=lambda: self._tools_menu(top2)
                       ).pack(side="right", padx=2)
            ttk.Button(top2, text="荐股", width=4,
                       command=self._open_picks).pack(side="right", padx=2)
        else:
            ttk.Separator(top, orient="vertical").pack(side="left", fill="y",
                                                       padx=8)
            ttk.Label(top, text="周期:").pack(side="left")
            cb = ttk.Combobox(top, textvariable=self.show_n, width=5,
                              state="readonly", values=[30, 60, 90, 120])
            cb.pack(side="left", padx=3)
            cb.bind("<<ComboboxSelected>>", lambda e: self._rerender())
            ttk.Label(top, text="副图指标:").pack(side="left")
            ci = ttk.Combobox(top, textvariable=self.ind_name, width=6,
                              state="readonly",
                              values=["MACD", "KDJ", "RSI", "BOLL", "ADX"])
            ci.pack(side="left", padx=3)
            ci.bind("<<ComboboxSelected>>", lambda e: self._rerender())

        if self.compact:
            pass
        else:
            ttk.Separator(top, orient="vertical").pack(side="left", fill="y",
                                                       padx=8)
            for nn in sorted(MA_COLORS):
                tk.Checkbutton(top, text=f"MA{nn}", variable=self.ma_on[nn],
                               command=self._rerender, font=("Consolas", 8),
                               bg=DARK_BG, fg=FG_MAIN, activebackground=DARK_BG,
                               activeforeground=FG_MAIN,
                               selectcolor=FIELD_BG).pack(side="left")
            tk.Checkbutton(top, text="筹码峰", variable=self.show_chips,
                           command=self._rerender, font=("Consolas", 8),
                           bg=DARK_BG, fg=FG_MAIN, activebackground=DARK_BG,
                           activeforeground=FG_MAIN,
                           selectcolor=FIELD_BG).pack(side="left")
            ttk.Separator(top, orient="vertical").pack(side="left", fill="y",
                                                       padx=8)
            ttk.Button(top, text="每日荐股", command=self._open_picks
                       ).pack(side="left", padx=2)
            ttk.Button(top, text="复制报告",
                       command=self.copy_report).pack(side="left", padx=2)
            ttk.Button(top, text="导出报告",
                       command=self.export_report).pack(side="left", padx=2)
            ttk.Button(top, text="样本明细",
                       command=self.show_samples).pack(side="left", padx=2)
            if CACHE_OK:
                ttk.Button(top, text="更新缓存",
                           command=self.refresh_cache).pack(side="left", padx=2)
            self.btn_ai = ttk.Button(top, text="工具", command=self.open_tools)
            self.btn_ai.pack(side="left", padx=2)
            ttk.Button(top, text="⚙设置",
                       command=self.open_settings).pack(side="left", padx=2)

        # 状态信息：股票信息(左,常驻) + 加载进度(中) + 悬停信息(右)，互不覆盖
        info_row = tk.Frame(self.root, bg=DARK_BG)
        info_row.pack(fill="x", padx=8, pady=(0, 2))
        self.info_var = tk.StringVar(value="输入代码如 002241 / 600519，点击【分析预测】")
        info_lbl = tk.Label(info_row, textvariable=self.info_var,
                            fg=TITLE_TXT, bg=DARK_BG, anchor="w",
                            font=("Microsoft YaHei", 9))
        info_lbl.pack(side="left")
        self.progress_var = tk.StringVar(value="")
        progress_lbl = tk.Label(info_row, textvariable=self.progress_var,
                                fg="#4da3ff", bg=DARK_BG, anchor="w",
                                font=("Microsoft YaHei", 9))
        progress_lbl.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.hover_var = tk.StringVar(value="")
        hk = tk.Label(info_row, textvariable=self.hover_var, foreground="#4da3ff",
                      bg=DARK_BG, font=("Consolas", 9))
        hk.pack(side="right", padx=(10, 0))

        # 窗口缩放 / 文本更新时压缩股票信息文本，避免溢出
        self._info_lbl = info_lbl
        self._info_row = info_row
        # 状态栏（进度）同样截断：小屏上长文本会溢出
        self._prog_lbl = progress_lbl

        def _fit_info(_e=None):
            try:
                avail = info_row.winfo_width() - 120
                if avail > 10:
                    est = max(6, int(avail / 9))
                    # 股票信息
                    text = self.info_var.get()
                    info_lbl.config(text=text[:est] + "…" if len(text) > est
                                    else text)
                    # 进度文本：给右侧hover留空间
                    ptext = self.progress_var.get()
                    pest = max(6, est - 10)
                    self._prog_lbl.config(
                        text=ptext[:pest] + "…" if len(ptext) > pest else ptext)
            except Exception:
                pass
        info_row.bind("<Configure>", _fit_info)
        # 变量变化时也重新适配
        self.info_var.trace_add("write", lambda *a: self._fit_info())
        self.progress_var.trace_add("write", lambda *a: self._fit_info())
        self._fit_info = _fit_info
        self._fit_info()

    def _build_body(self):
        body = tk.Frame(self.root)
        body.pack(fill="both", expand=True)

        # ---- 右侧：预测参考（先pack，防止遮挡图表；小屏省略）----
        right = None
        if not self.compact:
            right = ttk.LabelFrame(body, text=" 预测参考 ", padding=4)
            right.pack(side="right", fill="y", padx=(2, 8), pady=6)
            self._w_right = right

            rt = tk.Frame(right, bg=DARK_BG)
            rt.pack(fill="x")
            self._rt_fold = tk.BooleanVar(value=True)
            self._rt_btn = tk.Label(rt, text="▼", font=("Consolas", 9),
                                    fg=TITLE_TXT, bg=DARK_BG, cursor="hand2")
            self._rt_btn.pack(side="right")
            self._rt_btn.bind("<Button-1>", lambda e: self._toggle_rt())

            self._rt_content = tk.Frame(right, bg=DARK_BG)
            self._rt_content.pack(fill="both", expand=True)

            self.side_txt = tk.Text(self._rt_content, width=38,
                                    font=("Microsoft YaHei", 9),
                                    relief="flat", bg=PANEL_BG, fg=FG_MAIN,
                                    insertbackground=FG_MAIN,
                                    selectbackground="#2b3540")
            self.side_txt.pack(fill="both", expand=True)
        self._w_right = getattr(self, "_w_right", None)

        # ---- 左侧：自选池（固定宽度；小屏省略）----
        if not self.compact:
            wf = ttk.LabelFrame(body, text=" 自选池 ", padding=4)
            wf.pack(side="left", fill="y", padx=(8, 2), pady=2)
            wf.pack_propagate(False)
            wf.config(width=170)

            self._wf_content = tk.Frame(wf, bg=DARK_BG)
            self._wf_content.pack(fill="both", expand=True)

            self.watch_list = tk.Listbox(self._wf_content, width=12,
                                         font=("Consolas", 9),
                                         exportselection=False, bg=PANEL_BG,
                                         fg=FG_MAIN, selectbackground="#2b3540",
                                         selectforeground="#ffffff",
                                         relief="flat", highlightthickness=0)
            self.watch_list.pack(fill="both", expand=True)
            self.watch_list.bind("<Double-Button-1>", self._on_pick)
            bf = ttk.Frame(self._wf_content)
            bf.pack(fill="x", pady=(4, 0))
            ttk.Button(bf, text="+ 加自选", width=8,
                       command=self.add_watch).pack(side="left", padx=1)
            ttk.Button(bf, text="- 删除", width=7,
                       command=self.del_watch).pack(side="left", padx=1)

            sf = ttk.LabelFrame(self._wf_content, text=" 今日行业 ", padding=2)
            sf.pack(fill="both", expand=True, pady=(6, 0))
            self.sector_txt = tk.Text(sf, height=8, wrap="word",
                                      font=("Consolas", 9),
                                      bg=PANEL_BG, fg=FG_MAIN, relief="flat",
                                      state="disabled", cursor="arrow")
            self.sector_txt.pack(fill="both", expand=True)

        # ---- 中部：图表 + 指数条 ----
        left = tk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self._w_left = left
        self.cv_main = Chart(left, self.PANEL_H["main"])
        self.cv_vol = Chart(left, self.PANEL_H["vol"])
        self.cv_ind = Chart(left, self.PANEL_H["ind"])
        # grid 布局：窗口高度变化时三个面板按权重自适应缩放
        left.grid_rowconfigure(0, weight=6)     # 主图
        left.grid_rowconfigure(1, weight=2)     # 成交量
        left.grid_rowconfigure(2, weight=3)     # 指标副图
        for col in (0,):
            left.grid_columnconfigure(col, weight=1)
        self.cv_main.grid(row=0, column=0, sticky="nsew", padx=(2, 2))
        self.cv_vol.grid(row=1, column=0, sticky="nsew", padx=(2, 2))
        self.cv_ind.grid(row=2, column=0, sticky="nsew", padx=(2, 2))
        idxbar = ttk.LabelFrame(left, text=" 五大指数 ", padding=(6, 3))
        idxbar.grid(row=3, column=0, sticky="ew", padx=(2, 2), pady=(4, 0))
        self.idx_labels = {}
        # 小屏：指数条不占主界面，收进【工具 → 五大指数】弹窗
        if not getattr(self, "compact", False):
            for col, (code, name) in enumerate(INDEX_CODES):
                idxbar.columnconfigure(col, weight=1, uniform="idx")
                cell = tk.Frame(idxbar, bg=DARK_BG)
                cell.grid(row=0, column=col, sticky="nsew", padx=4)
                tk.Label(cell, text=name, font=("Microsoft YaHei", 9),
                         fg=AXIS_TXT, bg=DARK_BG).grid(row=0, column=0,
                                                       sticky="w")
                lc = tk.Label(cell, text="-", font=("Consolas", 9),
                              bg=DARK_BG)
                lc.grid(row=0, column=1, sticky="e", padx=(6, 0))
                lp = tk.Label(cell, text="-", font=("Consolas", 10, "bold"),
                              fg=FG_MAIN, bg=DARK_BG)
                lp.grid(row=1, column=0, columnspan=2, sticky="w")
                self.idx_labels[code] = (lp, lc)
        idxbar.columnconfigure(len(INDEX_CODES), weight=0)
        if getattr(self, "compact", False):
            idxbar.grid_remove()
        self._w_idxbar = idxbar

        bottom = tk.Frame(self.root, bg=DARK_BG)
        bottom.pack(fill="both", padx=8, pady=(2, 6))
        self._w_bottom = bottom
        self.txt = tk.Text(bottom, height=5 if self.compact else 8,
                           font=("Consolas", 9),
                           bg="#12171d", fg="#cfd8e0",
                           insertbackground=FG_MAIN, relief="flat",
                           selectbackground="#2b3540")
        self.txt.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(bottom, command=self.txt.yview)
        sb.pack(side="right", fill="y")
        self.txt.config(yscrollcommand=sb.set)

        # 十字光标事件（元素在各面板内部，仅坐标移动，无整图重绘）
        self.chart_keys = [("main", self.cv_main), ("vol", self.cv_vol),
                           ("ind", self.cv_ind)]
        for key, cv in self.chart_keys:
            cv.bind("<Motion>", lambda e, k=key: self._on_motion(e, k))
            cv.bind("<Leave>", lambda e, k=key: self._on_leave(e, k))
            cv.bind("<Configure>", self._on_resize)
            cv.bind("<MouseWheel>", self._on_wheel)
            cv.bind("<Button-4>", self._on_wheel)
            cv.bind("<Button-5>", self._on_wheel)
            cv.bind("<ButtonPress-1>", self._drag_start)
            cv.bind("<B1-Motion>", self._drag_move)
            cv.bind("<ButtonRelease-1>", self._drag_end)
            cv.bind("<Double-Button-1>", self._drag_reset)
    # ---------- 折叠 ----------

    def _open_picks(self):
        """每日荐股：扫本地缓存多维打分，Top-N 入围。"""
        win = tk.Toplevel(self.root)
        win.title(f"每日荐股 · 风险偏好{CFG.RISK_MODE} · 纯本地缓存")
        win.configure(bg=DARK_BG)
        win.geometry("520x460" if not self.compact else
                     f"{self.root.winfo_screenwidth()}x"
                     f"{self.root.winfo_screenheight()}+0+0")
        if self.compact:
            win.attributes("-fullscreen", True)
            win.bind("<Escape>", lambda e: win.destroy())
        lb = tk.Listbox(win, font=("Consolas", 9), bg="#12171d",
                        fg="#cfd8e0", selectbackground="#2b3540")
        lb.pack(fill="both", expand=True, padx=6, pady=6)
        lb.insert("end", "扫描本地缓存中…")

        def worker():
            try:
                picks = daily_picks(progress=lambda s: self._safe_after(
                    0, lambda: self.progress_var.set(s)))
            except Exception as e:
                picks = []
                self._safe_after(0, lambda: lb.delete(0, "end") or
                                 lb.insert("end", f"失败: {e}"))
            self._safe_after(0, lambda: self._picks_fill(win, lb, picks))
        threading.Thread(target=worker, daemon=True).start()

    def _picks_fill(self, win, lb, picks):
        lb.delete(0, "end")
        if not picks:
            lb.insert("end", "今日缓存中无入围股票（无买入阈值以上评分）")
            return
        lb.insert("end", f"{'代码':<10}{'名称':<8}{'收盘':>8}{'涨跌':>7}"
                         f"{'评分':>5}  理由")
        self._picks_codes = []
        for code, name, close, chg, score, reasons, band in picks:
            lb.insert("end", f"{code:<10}{name[:6]:<8}{close:>8.2f}"
                             f"{chg:>+6.1f}%{score:>4}  {reasons[:30]}"
                             f"  波段{band:.0f}")
            self._picks_codes.append(code)
        lb.insert("end", "")
        lb.insert("end", "双击某行 → 直接分析该股")

        def pick(_e=None):
            sel = lb.curselection()
            if sel and sel[0] < len(self._picks_codes):
                code = self._picks_codes[sel[0]]
                win.destroy()
                self.code_var.set(code)
                self.run()
        lb.bind("<Double-Button-1>", pick)

    def _open_idx_window(self):
        """小屏：五大指数弹窗（打开期间由 _index_loop 每30秒自动刷新）。"""
        if getattr(self, "_idx_win", None) and self._idx_win.winfo_exists():
            self._idx_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("五大指数")
        win.configure(bg=DARK_BG)
        if getattr(self, "compact", False):
            win.attributes("-fullscreen", True)
            win.bind("<Escape>", lambda e: win.destroy())
        frm = tk.Frame(win, bg=DARK_BG)
        frm.pack(fill="both", expand=True, padx=6, pady=6)
        for col in range(len(INDEX_CODES)):
            frm.grid_columnconfigure(col, weight=1)
        self.idx_labels = {}
        for col, (code, name) in enumerate(INDEX_CODES):
            frm.grid_columnconfigure(col, weight=1, uniform="idx")
            cell = tk.Frame(frm, bg=DARK_BG)
            cell.grid(row=0, column=col, sticky="nsew", padx=3, pady=4)
            tk.Label(cell, text=name, font=("Microsoft YaHei", 9),
                     fg=AXIS_TXT, bg=DARK_BG).grid(row=0, column=0,
                                                   sticky="w")
            lc = tk.Label(cell, text="-", font=("Consolas", 9), bg=DARK_BG)
            lc.grid(row=0, column=1, sticky="e")
            lp = tk.Label(cell, text="-", font=("Consolas", 11, "bold"),
                          fg=FG_MAIN, bg=DARK_BG)
            lp.grid(row=1, column=0, columnspan=2, sticky="w")
            self.idx_labels[code] = (lp, lc)

        def _closed(_e=None):
            self.idx_labels = {}
            self._idx_win = None
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.bind("<Destroy>", _closed)
        self._idx_win = win
        self._update_indices()

    def _tools_menu(self, anchor_widget):
        """小屏：报告/样本/缓存/设置 收进弹出菜单（状态栏仍常驻）。"""
        m = tk.Menu(self.root, tearoff=0, bg=PANEL_BG, fg=FG_MAIN,
                    activebackground=BTN_HOVER, activeforeground=FG_MAIN,
                    font=("Microsoft YaHei", 10))
        m.add_command(label="复制报告", command=self.copy_report)
        m.add_command(label="导出报告", command=self.export_report)
        m.add_command(label="邮件发报告", command=self.mail_report)
        m.add_command(label="样本明细", command=self.show_samples)
        m.add_command(label="五大指数", command=self._open_idx_window)
        if CACHE_OK:
            m.add_separator()
            m.add_command(label="更新缓存", command=self.refresh_cache)
            m.add_command(label="v4.0 全A研究（三档风险+消融）",
                          command=self.run_v4_research_bg)
        m.add_separator()
        m.add_command(label="⚙ 设置", command=self.open_settings)
        x = anchor_widget.winfo_rootx()
        y = anchor_widget.winfo_rooty() + anchor_widget.winfo_height()
        m.tk_popup(x, y)

    def _toggle_rt(self):
        v = not self._rt_fold.get()
        self._rt_fold.set(v)
        self._rt_btn.config(text="▼" if v else "▶")
        if v:
            self._rt_content.pack(fill="both", expand=True)
        else:
            self._rt_content.pack_forget()

    # ---------- 运行分析 ----------

    def _progress(self, msg):
        """后台线程进度 -> 主线程状态栏（加载进度区）。"""
        self._safe_after(0, lambda: self.progress_var.set(msg))

    def _safe_after(self, ms, fn):
        """主线程调度：窗口已销毁或 root 失效时静默跳过（线程安全退出）。"""
        try:
            if not self.root.winfo_exists():
                return
            self.root.after(ms, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _warm_cache(self):
        """启动后台预热全市场代码表（过期才真正联网刷新）。"""
        try:
            ensure_codes(self._progress)
        except Exception:
            log.exception("后台预热代码表失败")

    def refresh_cache(self):
        self.progress_var.set("正在更新缓存数据库（全市场代码表）...")
        self._run_bg(lambda: refresh_all_codes(self._progress),
                     self._cache_done)

    def _cache_done(self, res, err):
        if err:
            self.progress_var.set(f"缓存更新失败: {err}")
            messagebox.showerror("更新缓存", str(err))
            return
        self.progress_var.set("缓存已就绪：日K/代码表/分层池 本地命中，不再重复爬取")

    def run_v4_research_bg(self):
        """工具菜单：后台跑 v4.0 全A研究（三档风险+消融），完成后弹摘要。"""
        if getattr(self, "_v4_running", False):
            messagebox.showinfo("v4.0 研究", "已在后台运行中，请稍候")
            return
        self._v4_running = True
        self.progress_var.set("v4.0 全A研究启动（后台，见状态栏进度）...")

        def _job():
            return run_v4_research(progress=self._progress)

        self._run_bg(_job, self._v4_done)

    def _v4_done(self, res, err):
        self._v4_running = False
        if err:
            self.progress_var.set(f"v4.0 研究失败: {err}")
            messagebox.showerror("v4.0 研究", str(err))
            return
        s = res.get("strategies", {})
        o = res.get("overfit", {})
        lines = [f"有效股票 {res['meta']['n_valid']} 只"
                 f"（池 {res['meta']['n_codes_pool']}），"
                 f"区间 {res['meta']['date_min']} ~ "
                 f"{res['meta']['date_max']}", ""]
        for key, tag in (("baseline:稳健", "v3.3基线(稳健)"),
                         ("full:保守", "v4.0 保守"),
                         ("full:平衡", "v4.0 平衡"),
                         ("full:激进", "v4.0 激进")):
            v = s.get(key)
            if v:
                lines.append(f"{tag}  年化 {v['ann']*100:+.1f}%  "
                             f"回撤 {v['mdd']*100:.1f}%  "
                             f"Calmar {v['calmar'] if v['calmar'] is not None else '-'}")
        if o.get("flag"):
            lines.append("")
            lines.append("⚠️ LightGBM 疑似过拟合（Train IC >> Test IC）")
        lines.append("")
        lines.append("完整结果见 research/v4_report.json 与 README")
        messagebox.showinfo("v4.0 全A研究完成", "\n".join(lines))
        self.progress_var.set("v4.0 全A研究完成（research/v4_report.json）")

    def _run_bg(self, fn, done):
        """后台线程执行 fn，完成后经 future 回调在主线程调用
        done(result|None, err|None)。不再轮询，无 CPU 空转。"""
        try:
            fut = _BG_EX.submit(fn)
        except RuntimeError:            # 解释器退出中
            return

        def _cb(f):
            try:
                r = f.result()
                err = None
            except Exception as e:
                log.exception("后台任务失败")
                r, err = None, e
            self._safe_after(0, lambda: done(r, err))

        fut.add_done_callback(_cb)

    def run(self):
        try:
            full = normalize_code(self.code_var.get())
        except ValueError as e:
            messagebox.showwarning("代码有误", str(e))
            return
        self.btn_run.config(state="disabled")
        # 启动先快速填最近K线（走缓存秒开），完整历史/样本池后台补齐
        self.progress_var.set("正在快速加载近期行情...")
        self._run_bg(lambda: analyze(full, self._progress, quick=True),
                     self._done_load)

    # ---------- AI 找源：确认弹窗 → 后台验证 → 自动重新分析 ----------

    def _ai_rescue_flow(self):
        """数据源全灭时由后台线程调用；转到主线程弹确认框。"""
        def ask():
            if not self.api_key:
                self.progress_var.set(
                    "所有K线源失效；在设置里配好DeepSeek Key后可AI自动找源")
                return
            yes = messagebox.askyesno(
                "数据源急救",
                "所有K线数据源均失效，自动容灾也未能恢复。\n\n"
                "是否让 AI(DeepSeek) 寻找替代接口？\n\n"
                "· AI 只提议候选URL，程序会逐个实测验证\n"
                "· 验证通过才会启用并记住，失败全部丢弃\n"
                "· 成功后将自动重新分析当前股票")
            if not yes:
                self.progress_var.set("已跳过AI找源；可稍后运行 "
                                      "python stock_firstaid.py --ai")
                return
            self.progress_var.set("AI正在寻找新数据源（提议→实测验证）...")
            key, model = self.api_key, AI_MODEL

            def work():
                ok, msg = ai_rescue_kline(key, model)
                self._safe_after(0, lambda: self._ai_rescue_done(ok, msg))
            threading.Thread(target=work, daemon=True).start()
        self._safe_after(0, ask)

    def _ai_rescue_done(self, ok, msg):
        self.progress_var.set("AI找源: " + msg)
        if ok:
            # 新源已生效，自动重新分析当前股票
            if self.code_var.get().strip():
                self.run()

    def _done_load(self, res, err):
        if err:
            self._fail(str(err))
            return
        self._loaded(res)
        if res.get("quick"):
            # 启动后后台逐步加载样本池，边加载边更新预测K线（不阻塞界面）
            self._start_progressive(res)
            # 策略缓存过期/缺失 → 后台消融回测 + 弹窗三选一
            self._ensure_strategy(res)

    def _start_progressive(self, res):
        ctx = res.get("_ctx")
        if not ctx:
            return
        if getattr(self, "_prog_running", False):
            return
        self._prog_running = True
        self.progress_var.set("已显示基础预测，正在后台加载样本池提升精度...")

        def worker():
            try:
                for lm, tp, pd, cl, tpb, note, mp in load_pools_progressive(
                        ctx["full"], ctx, self._progress):
                    self._safe_after(0, lambda lm=lm, tp=tp, pd=pd, cl=cl,
                                     tpb=tpb, note=note, mp=mp:
                                     self._apply_progressive(
                                         res, lm, tp, pd, cl, tpb, note, mp))
            except Exception:
                log.exception("样本池增量加载失败(预测保留基础版)")
            finally:
                self._prog_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _apply_progressive(self, res, level_map, t_pred, pred, clamped,
                           tpred_bar, pool_note, multi_pred):
        if self.res is not res:
            return                  # 已切换股票，丢弃过期更新
        res["t_pred"] = t_pred
        res["pred"] = pred
        res["clamped"] = clamped
        res["tpred_bar"] = tpred_bar
        res["pool_note"] = pool_note
        res["multi_pred"] = multi_pred
        res["t5_pred"] = t_pred.get("t5")
        res["levels"] = [
            {"key": k, "label": LV_LABEL[k], "n": len(smp),
             "samples": smp,
             "up_prob": (len([s for s in smp if s.get("n1_cl") is not None and s["n1_cl"] > 0]) / len(smp)) if smp else 0.5}
            for k, smp in sorted(level_map.items()) if smp]
        self._rerender()
        self.progress_var.set("样本池加载中，预测已更新: " + pool_note)

    # ---------- 策略消融：后台回测 → 弹窗选三档策略（缓存5日） ----------

    def _ensure_strategy(self, res):
        """无有效策略缓存时后台跑消融，完成后弹窗让用户三选一。"""
        if self.res is not res:
            return
        if res.get("strategy"):
            st = res["strategy"]
            self.progress_var.set(
                f"当前策略: {st.get('label', '?')} "
                f"(策略缓存{5 - (time.time() - st.get('ts', 0)) // 86400:.0f}日内有效)")
            return
        self.progress_var.set("后台运行多算法消融回测(L1/MACD/KDJ/RSI/布林/"
                              "MA/多维×3风险档, 近1000交易日)...")
        full = res["full_code"]
        bars = res["disp_rows"][:-1] if res.get("has_live") \
            else res["disp_rows"]
        try:
            idx_rows = get_daily("sh000001")
        except Exception:
            idx_rows = None

        def work():
            return run_ablation(full, bars, idx_rows=idx_rows,
                                progress=self._progress)

        self._run_bg(work, lambda r, e: self._ablation_done(res, r, e))

    def _ablation_done(self, res, abl, err):
        if self.res is not res:
            return                  # 已切换股票，丢弃
        if err or not abl:
            self.progress_var.set("策略消融未完成"
                                  + (f": {err}" if err else "（历史过短）")
                                  + "，使用默认多维·稳健")
            return
        self._last_ablation = abl
        self._show_strategy_popup(res, abl)

    def _show_strategy_popup(self, res, abl):
        full = res["full_code"]
        win = tk.Toplevel(self.root)
        win.title(f"策略消融选择 - {full}")
        win.configure(bg=DARK_BG)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=(
            f"基于近{abl['bars']}个交易日回测选策略 "
            f"（训练{abl['train_n']}日选型 / 验证{abl['val_n']}日防过拟合，"
            f"验证集未参与选择）\n"
            f"牛熊分界：上证指数收盘 vs MA120。以下胜率/年化/回撤为"
            f"【验证集】样本外数据，牛/熊评分为对应行情段的年化收益。")
                  ).pack(anchor="w", padx=12, pady=(10, 4))
        box = ttk.Frame(win)
        box.pack(fill="both", expand=True, padx=12, pady=4)
        sel_var = tk.StringVar(value="稳健")
        order = ("保守", "稳健", "激进")

        def _fmt(v, pct=True):
            if v is None:
                return "-"
            return f"{v * 100:+.1f}%" if pct else f"{v:.2f}"

        for i, mode in enumerate(order):
            c = abl["mode_candidates"].get(mode)
            if not c:
                continue
            tr, va = c.get("train") or {}, c.get("val") or {}
            txt = (f"{mode}｜{c['label']}\n"
                   f"  验证期: 胜率{_fmt(va.get('winrate'))} "
                   f"年化{_fmt(va.get('ann'))} 回撤{_fmt(va.get('mdd'))} "
                   f"交易{va.get('trades', 0)}笔"
                   f"（训练期: 胜率{_fmt(tr.get('winrate'))} "
                   f"年化{_fmt(tr.get('ann'))} 回撤{_fmt(tr.get('mdd'))}）\n"
                   f"  牛市评分{_fmt(c.get('bull'))}  "
                   f"熊市评分{_fmt(c.get('bear'))}")
            rb = ttk.Radiobutton(box, text=txt, value=mode,
                                 variable=sel_var)
            rb.pack(anchor="w", pady=4)

        def apply():
            c = abl["mode_candidates"].get(sel_var.get())
            if not c:
                return
            save_strategy(full, {"algo": c["algo"], "mode": c["mode"],
                                 "params": c["params"], "label": c["label"],
                                 "ts": time.time()})
            win.destroy()
            self.progress_var.set(
                f"已选策略: {c['label']}（缓存5日，到期自动重新消融）")
            try:
                full2 = normalize_code(self.code_var.get())
                self._run_bg(lambda: analyze(full2), self._refresh_done)
            except ValueError:
                pass

        btns = ttk.Frame(win)
        btns.pack(pady=10)
        ttk.Button(btns, text="应用所选策略", command=apply).pack(
            side="left", padx=6)
        ttk.Button(btns, text="本次先用默认(多维·稳健)",
                   command=win.destroy).pack(side="left", padx=6)

    def _rerun_strategy(self):
        """手动重选：清除策略缓存并重新消融（工具菜单入口）。"""
        if not self.res:
            messagebox.showinfo("提示", "请先【分析预测】一只股票")
            return
        try:
            with db_conn(commit=True) as conn:
                conn.execute("DELETE FROM meta WHERE key=?",
                             (_strat_key(self.res["full_code"]),))
        except Exception:
            log.exception("清除策略缓存失败")
        self._ensure_strategy(self.res)

    # ---------- 数据维护：全市场回填 / 数据清洗（后台执行） ----------

    def run_backfill(self):
        if getattr(self, "_bf_busy", False):
            return
        self._bf_busy = True
        self.progress_var.set("后台全市场回填启动（进度见状态栏）...")

        def work():
            return backfill_full_market(progress=self._progress)

        def done(res, err):
            self._bf_busy = False
            if err:
                self.progress_var.set(f"全市场回填失败: {err}")
                return
            self.progress_var.set(
                f"全市场回填：本次成功{res['ok']} 失败{res['fail']}"
                f"（配额限制，下次运行自动续传）")

        self._run_bg(work, done)

    def run_clean(self):
        if getattr(self, "_clean_busy", False):
            return
        self._clean_busy = True
        self.progress_var.set("数据清洗中（扫描+修复）...")

        def work():
            return clean_daily_db(fix=True, progress=self._progress)

        def done(res, err):
            self._clean_busy = False
            if err:
                self.progress_var.set(f"数据清洗失败: {err}")
                return
            msg = (f"扫描 {res['codes']} 只代码：\n"
                   f"结构异常删除 {res['deleted']} 根\n"
                   f"整只重拉修复 {res['refetched']} 只"
                   f"（源不可用时自动跳过）\n"
                   f"停牌缺口 {res['suspend']} 只 · "
                   f"疑似退市 {res['delisted']} 只已登记\n"
                    f"价格粘性 {res['stale']} 只")
            self.progress_var.set("数据清洗完成")
            messagebox.showinfo("数据清洗", msg)
        self._run_bg(work, done)

    def _auto_refresh(self):
        """每15分钟静默重跑当前代码分析，刷新当日实时K线与预测。"""
        code = self.code_var.get()
        if code:
            try:
                full = normalize_code(code)
                self._run_bg(lambda: analyze(full), self._refresh_done)
            except ValueError:
                pass
        self._safe_after(self.REFRESH_MS, self._auto_refresh)

    def _refresh_done(self, res, err):
        if err or not res:
            return                      # 静默失败，下个周期再试
        ai = self.ai_text
        self._loaded(res)
        self.ai_text = ai               # 自动刷新不清空AI分析结果
        if ai:
            self._write_side()
        q = res["quote"]
        self.progress_var.set(f"[自动刷新 {time.strftime('%H:%M')}] 已完成")

    def _tick_done(self, q, err):
        self._tick_busy = False
        res = self.res
        if err or not q or not res:
            return
        old_snap = (res["quote"].get("time") or "")[:8]
        new_snap = (q.get("time") or "")[:8]
        res["quote"] = q
        if new_snap != old_snap and old_snap:
            # 跨快照日（如开盘后出现今日bar/新交易日）→ 触发完整重分析
            try:
                full = normalize_code(self.code_var.get())
                self._run_bg(lambda: analyze(full), self._refresh_done)
            except ValueError:
                pass
            return
        if res.get("has_live"):
            live = res["disp_rows"][-1]
            live["close"] = q["price"]
            live["high"] = max(live["high"], q["price"])
            live["low"] = min(live["low"], q["price"]) if q.get("low") and q["low"] > 0 else live["low"]
            tp = res["t_pred"]
            for pp in (10, 25, 50, 75, 90):     # 预测区间并入最新高低
                tp["hi"][pp] = round(max(tp["hi"][pp], live["high"]), 2)
                tp["lo"][pp] = round(min(tp["lo"][pp], live["low"]), 2)
            res["live_high"] = live["high"]
            res["live_low"] = live["low"]
            tb = res.get("tpred_bar")
            if tb:                               # T日预测叠加层同步外扩
                tb["high"] = max(tb["high"], live["high"])
                tb["low"] = min(tb["low"], live["low"])
        res["phase"] = market_phase_text(q.get("time"))
        self._set_info(q)
        self._rerender()

    def _tick(self):
        """秒级实时：只拉一次行情快照，更新现价/实时bar/预测区间/状态角标。"""
        self._safe_after(self.TICK_MS, self._tick)
        if not self.res or getattr(self, "_tick_busy", False):
            return
        code = self.code_var.get()
        if not code:
            return
        try:
            full = normalize_code(code)
        except ValueError:
            return
        self._tick_busy = True
        self._run_bg(lambda: fetch_quote(full), self._tick_done)

    def _fail(self, msg):
        self.btn_run.config(state="normal")
        self.progress_var.set("失败")
        messagebox.showerror("错误", msg)

    def _loaded(self, res):
        self.btn_run.config(state="normal")
        # 切换股票时重置多轮AI对话（自动刷新同股不重置）
        if self.res is not None and res["full_code"] != self.res["full_code"]:
            self._ai_msgs = []
        self.res = res
        self.ai_text = ""
        self._save_ini()
        self._set_info()
        self._rerender()

    def _set_info(self, q=None):
        """顶栏信息行（盘前不显示过期今开，锚定提示在图表右下角）。"""
        res = self.res
        q = q or res["quote"]
        chg = (q["price"] / res["prev_close"] - 1) * 100
        if res.get("pre_open"):
            self.info_var.set(
                f"{q['name']} ({res['full_code']})  "
                f"昨收{res['prev_close']:.2f}  "
                f"现价{q['price']:.2f}({chg:+.2f}%)  快照{q['time']}")
        else:
            self.info_var.set(
                f"{q['name']} ({res['full_code']})  昨收{res['prev_close']:.2f} "
                f"今开{q['open']:.2f}(缺口{res['gap_today']:+.2f}%) "
                f"现价{q['price']:.2f}({chg:+.2f}%)  快照{q['time']}"
                + ("  [含盘中实时bar]" if res["has_live"] else ""))

    def _rerender(self):
        self._drag_job = None
        if not self.res:
            return
        try:
            n = int(self.show_n.get())
        except Exception:
            n = 60
        n = max(20, min(n, 250))
        self.view = slice_view(self.res, n, pan=self.view_pan)
        self._draw_main()
        self._draw_vol()
        name = self.ind_name.get()
        if name == "MACD":
            self._draw_macd()
        elif name == "KDJ":
            self._draw_kdj()
        elif name == "RSI":
            self._draw_rsi()
        elif name == "BOLL":
            self._draw_bollpct()
        elif name == "ADX":
            self._draw_adx()
        if getattr(self, "side_txt", None):
            self._write_side()
        self._write_report()

    def _on_wheel(self, event):
        """滚轮：上下缩放，Shift+滚轮左右平移。"""
        if not self.res:
            return
        if event.state & 0x1:  # Shift held → pan
            step = 5 if (event.delta > 0 or getattr(event, "num", None) == 4) else -5
            self.view_pan = max(0, min(self.view_pan + step,
                                       len(self.res["disp_rows"]) - 20))
            self.progress_var.set(f"平移: 第{self.view_pan}根起")
        else:
            if getattr(event, "num", None) == 4:
                step = -10
            elif getattr(event, "num", None) == 5:
                step = 10
            else:
                step = -10 if event.delta > 0 else 10
            try:
                n = int(self.show_n.get()) + step
            except Exception:
                n = 60
            n = max(20, min(250, n))
            self.show_n.set(n)
            self.progress_var.set(f"K线根数: {n}")
        if getattr(self, "_wheel_job", None):
            self.root.after_cancel(self._wheel_job)
        self._wheel_job = self.root.after(60, self._rerender)

    def _on_resize(self, _event):
        if getattr(self, "_resize_job", None):
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(200, self._rerender)

    def _drag_start(self, event):
        self._drag_x = event.x
        self._drag_pan = self.view_pan
        self._drag_moved = False

    def _drag_move(self, event):
        if not self.res or not hasattr(self, "_drag_x") \
                or self._drag_x is None:
            return
        dx = event.x - self._drag_x
        if abs(dx) > 4:
            self._drag_moved = True   # 拖动中抑制十字光标重绘
        g = self.scales.get("main")
        if not g or g.get("bw", 0) <= 0:
            return
        bars_moved = int(dx / g["bw"])
        new_pan = max(0, min(self._drag_pan + bars_moved,
                             len(self.res["disp_rows"]) - 20))
        if new_pan != self.view_pan:
            self.view_pan = new_pan
            # 拖拽节流：armv6 小屏 60ms 一次重绘，跟手不卡
            if not getattr(self, "_drag_job", None):
                self._drag_job = self.root.after(60, self._rerender)

    def _drag_end(self, event):
        self._drag_x = None
        # 拖动结束时立即补一次最终位置重绘
        if getattr(self, "_drag_job", None):
            self.root.after_cancel(self._drag_job)
            self._drag_job = None
            self._rerender()

    def _drag_reset(self, _event):
        """双击图表：回到最新K线（清除平移）。"""
        if self.view_pan:
            self.view_pan = 0
            self.progress_var.set("已回到最新")
            self._rerender()

    # ---------- 绘图工具 ----------

    def _geom(self, cv, n_bars, chips=False):
        w = max(cv.winfo_width(), 240)
        h = int(cv["height"])
        # 小屏压缩左右留白，确保K线尽量占满屏幕
        compact = getattr(self, "compact", False)
        L = 34 if compact else 58
        R = 44 if (chips and compact) else (70 if chips else (28 if compact else 20))
        T, B = 14 if compact else 16, 18 if compact else 20
        pw, ph = w - L - R, h - T - B
        bw = pw / n_bars
        return {"w": w, "h": h, "L": L, "R": R, "T": T, "B": B,
                "pw": pw, "ph": ph, "bw": bw, "n": n_bars}

    @staticmethod
    def _pad_range(lo, hi, ratio=0.06):
        rng = (hi - lo) or 1.0
        pad = rng * ratio
        return lo - pad, hi + pad

    def _axes(self, cv, g, lo, hi, fmt="{:.2f}", ngrid=4):
        def ymap(v):
            return g["T"] + (hi - v) / (hi - lo) * g["ph"]
        for k in range(ngrid + 1):
            v = lo + (hi - lo) * k / ngrid
            y = ymap(v)
            cv.create_line(g["L"], y, g["w"] - g["R"], y, fill=GRID_C)
            txt = fmt(v) if callable(fmt) else fmt.format(v)
            cv.create_text(g["L"] - 4, y, text=txt, anchor="e",
                           font=("Consolas", 8), fill=AXIS_TXT)
        return ymap

    def _line(self, cv, xs_fn, vals, ymap, color, width=1):
        """整条折线一次绘制（None 断开），item 数从 N 段降为少数几条。"""
        pts = []
        segs = []
        for i, v in enumerate(vals):
            if v is None:
                if len(pts) >= 2:
                    segs.append(list(pts))
                pts = []
                continue
            x, yy = xs_fn(i), ymap(v)
            pts.extend((x, yy))
        if len(pts) >= 2:
            segs.append(list(pts))
        for s in segs:
            cv.create_line(*s, fill=color, width=width,
                           joinstyle="round", capstyle="round")

    def _finish_panel(self, cv, g, key, lo, hi, dates, fmt=None):
        """登记缩放信息并创建光标元素（层级创建时一次固定）。"""
        g["lo_v"], g["hi_v"] = lo, hi
        g["dates"] = dates
        g["fmt"] = fmt or (lambda v: f"{v:.2f}")
        g["key"] = key
        self.scales[key] = g
        g["vid"] = cv.create_line(0, 0, 0, 0, state="hidden", fill=CROSS_C,
                                  dash=(4, 3))
        g["hid"] = cv.create_line(0, 0, 0, 0, state="hidden", fill=CROSS_C,
                                  dash=(4, 3))
        g["pid"] = cv.create_text(0, 0, text="", state="hidden",
                                  fill="#ffffff",
                                  font=("Consolas", 9, "bold"))
        g["pbg"] = cv.create_rectangle(0, 0, 0, 0, state="hidden",
                                       fill="#1971c2", outline="")
        g["did"] = cv.create_text(0, 0, text="", state="hidden",
                                  fill="#ffffff",
                                  font=("Consolas", 9, "bold"))
        g["dbgd"] = cv.create_rectangle(0, 0, 0, 0, state="hidden",
                                        fill="#333c46", outline="")
        cv.tag_lower(g["dbgd"], g["did"])
        cv.tag_raise(g["pid"])
        g["_shown"] = False

    def _draw_main(self):
        cv, v = self.cv_main, self.view
        cv.delete("all")
        bars = v["bars"]
        has_chips = self.show_chips.get() and v.get("chips") and v["chips"].get("bins")
        g = self._geom(cv, len(bars), chips=has_chips)
        if not bars:
            return

        los = [b["low"] for b in bars]
        his = [b["high"] for b in bars]
        for nn, on in self.ma_on.items():
            if on.get():
                vals = [x for x in v["ma"][nn] if x is not None]
                if vals:
                    los.append(min(vals))
                    his.append(max(vals))
        if v.get("tpred"):
            los.append(v["tpred"]["low"])
            his.append(v["tpred"]["high"])
        # 布林带叠加：副图指标选 BOLL 时轨道纳入纵轴范围
        show_boll = (self.ind_name.get() == "BOLL"
                     and v.get("boll_up") is not None)
        if show_boll:
            for arr in (v["boll_up"], v["boll_low"]):
                vals = [x for x in arr if x is not None]
                if vals:
                    los.append(min(vals))
                    his.append(max(vals))
        if not los or not his:
            return
        lo, hi = self._pad_range(min(los), max(his))

        def ymap(val):
            return g["T"] + (hi - val) / (hi - lo) * g["ph"]

        def xs(i):
            return g["L"] + g["bw"] * (i + 0.5)
        self._axes(cv, g, lo, hi)

        for nn in sorted(MA_COLORS):
            if self.ma_on[nn].get():
                self._line(cv, xs, v["ma"][nn], ymap, MA_COLORS[nn])

        if show_boll:
            self._line(cv, xs, v["boll_up"], ymap, "#e8a838")
            self._line(cv, xs, v["boll_low"], ymap, "#e8a838")
            self._line(cv, xs, v["boll_mid"], ymap, "#9a76d0")
            cv.create_text(g["w"] - 6, g["T"] - 3, text="BOLL(20,2)",
                           fill="#e8a838", font=("Consolas", 8, "bold"),
                           anchor="e")

        # 筹码峰：独立右列，只画可见价格区间的bin，均匀排列
        if has_chips:
            cp_ = v["chips"]
            chip_w = g["R"] - 6
            cw = chip_w * 0.85
            xr = g["w"] - 3
            xl = xr - chip_w
            ybot = g["h"] - g["B"]
            # 只保留可见价格区间的bin
            vis_bins = [(m, w) for m, w in cp_["bins"]
                        if w > 0 and lo <= m <= hi]
            if vis_bins:
                maxw = max(w for _, w in vis_bins)
                n_vis = len(vis_bins)
                slot = (ybot - g["T"]) / max(n_vis, 1)  # 每个bin均匀占位
                for idx, (mid, wgt) in enumerate(vis_bins):
                    yy = g["T"] + slot * (idx + 0.5)
                    bar_len = cw * wgt / maxw
                    cv.create_line(xr - bar_len, yy, xr, yy,
                                   fill=UP if mid <= cp_["cur"] else DOWN,
                                   width=2)
            # 分隔线
            cv.create_line(xl, g["T"], xl, ybot, fill=GRID_C, dash=(2, 3))
            for k_, colr, lab in (("sup", UP, "支"), ("res", DOWN, "压")):
                pv = cp_.get(k_)
                if pv and lo < pv < hi:
                    yy = ymap(pv)
                    cv.create_line(g["L"], yy, xr, yy,
                                   fill=colr, dash=(6, 4))
                    cv.create_text(xl + 2, yy - 7, text=f"{lab} {pv:.2f}",
                                   anchor="w", fill=colr,
                                   font=("Microsoft YaHei", 8))

        yo = yc = None
        ghost_lab = {"T+1预测": "T+1", "T日预测": "T日",
                     "T+5预测": "T+5", "T+10预测": "T+10"}
        for i, b in enumerate(bars):
            x = xs(i)
            if b["date"] in ghost_lab:
                # 幽灵K线：预测蜡烛，白色虚线边框（亮色主题自动转黑）
                yo, yc = ymap(b["open"]), ymap(b["close"])
                bw2 = max(g["bw"] * 0.62, 2)
                ty, by2 = min(yo, yc), max(yo, yc)
                if by2 - ty < 1.5:
                    by2 = ty + 1.5
                cv.create_line(x, ymap(b["high"]), x, ty,
                               fill=TPRED_C, dash=(3, 2))
                cv.create_line(x, by2, x, ymap(b["low"]),
                               fill=TPRED_C, dash=(3, 2))
                cv.create_rectangle(x - bw2 / 2, ty, x + bw2 / 2, by2,
                                    fill="", outline=TPRED_C, dash=(4, 3))
                cv.create_text(x, ty - 9, text=ghost_lab[b["date"]],
                               fill=TPRED_C, font=("Consolas", 7, "bold"))
                continue
            up = b["close"] >= b["open"]
            color = UP if up else DOWN
            yo, yc = ymap(b["open"]), ymap(b["close"])
            cv.create_line(x, ymap(b["high"]), x, ymap(b["low"]),
                           fill=color)
            bw2 = max(g["bw"] * 0.62, 2)
            ty, by2 = min(yo, yc), max(yo, yc)
            if by2 - ty < 1:
                by2 = ty + 1
            cv.create_rectangle(x - bw2 / 2, ty, x + bw2 / 2, by2,
                                fill=color, outline=color)

        for i, day, typ, txt in v["signals"]:
            if i >= len(bars) - 1:
                continue
            x = xs(i)
            if typ == "BUY":
                y = ymap(bars[i]["low"]) + 5
                cv.create_polygon(x, y, x - 5, y + 9, x + 5, y + 9,
                                  fill=UP, outline="")
                cv.create_text(x, y + 15, text="B", fill=UP,
                               font=("Arial", 8, "bold"))
            else:
                y = ymap(bars[i]["high"]) - 5
                cv.create_polygon(x, y, x - 5, y - 9, x + 5, y - 9,
                                  fill=DOWN, outline="")
                cv.create_text(x, y - 15, text="S", fill=DOWN,
                               font=("Arial", 8, "bold"))

        if not bars:
            return

        # 现价标签挂在最后一根真实K线上（跳过幽灵K线）
        pb_i = len(bars) - 1
        while pb_i >= 0 and bars[pb_i]["date"] in ghost_lab:
            pb_i -= 1
        if pb_i >= 0:
            pb = bars[pb_i]
            cv.create_text(xs(pb_i), g["T"] - 3,
                           text=f"C:{pb['close']:.2f}",
                           fill=PRED_C,
                           font=("Microsoft YaHei", 8, "bold"))
        # T+5 预测标注（图表左上角，预测P50/区间/上行概率）
        t5v = self.res.get("t5_pred") if getattr(self, "res", None) else None
        if t5v:
            c5 = t5v["cl"]
            cv.create_text(g["L"] + 2, g["T"] + 11,
                           text=f"T+5: {c5[50]:.2f} "
                                f"({c5[25]:.2f}~{c5[75]:.2f}) "
                                f"↑{t5v['up_prob']*100:.0f}%",
                           fill=PRED_C, anchor="w",
                           font=("Microsoft YaHei", 8, "bold"))
        lx = g["L"] + 2
        for nn in sorted(MA_COLORS):
            if self.ma_on[nn].get():
                cv.create_text(lx, 6, text=f"MA{nn}",
                               fill=MA_COLORS[nn],
                               font=("Consolas", 8, "bold"), anchor="w")
                lx += 36
        step = max(1, len(bars) // 10)
        for i in range(0, len(bars), step):
            cv.create_text(xs(i), g["h"] - 7, text=v["dates"][i][5:],
                           font=("Consolas", 7), fill=AXIS_TXT)
        self._finish_panel(cv, g, "main", lo, hi, v["dates"])

    # ---------- 成交量 ----------

    def _draw_vol(self):
        cv, v = self.cv_vol, self.view
        cv.delete("all")
        vols = list(v["vols"])
        while vols and vols[-1] is None:   # 剔除尾部预测占位空槽
            vols.pop()
        n = len(v["bars"])
        g = self._geom(cv, n, chips=bool(self.show_chips.get() and v.get("chips")))
        vmax = max(vols) if vols else 1.0
        lo, hi = 0, vmax * 1.08

        def ymap(val):
            return g["T"] + (hi - val) / (hi - lo) * g["ph"]

        def xs(i):
            return g["L"] + g["bw"] * (i + 0.5)
        self._axes(cv, g, lo, hi, lambda x: f"{x/10000:.0f}万", 2)
        bw2 = max(g["bw"] * 0.62, 1)
        for i, vol in enumerate(vols):
            c = UP if v["bars"][i]["close"] >= v["bars"][i]["open"] else DOWN
            cv.create_rectangle(xs(i) - bw2 / 2, ymap(vol),
                                xs(i) + bw2 / 2, ymap(0),
                                fill=c, outline=c)
        if len(vols) >= 5:
            mv = sum(vols[-5:]) / 5
            cv.create_line(g["L"], ymap(mv), g["w"] - g["R"], ymap(mv),
                           fill="#e8890c", dash=(5, 3))
            cv.create_text(g["w"] - g["R"] - 4, ymap(mv) - 7,
                           text=f"5日均量 {mv/10000:.0f}万手",
                           anchor="e", font=("Consolas", 8), fill="#e8890c")
        cv.create_text(g["L"] + 2, g["T"] - 3, text="VOLUME(手)", anchor="w",
                       font=("Microsoft YaHei", 8), fill=TITLE_TXT)
        step = max(1, n // 10)
        for i in range(0, n, step):
            cv.create_text(xs(i), g["h"] - 7, text=v["dates"][i][5:],
                           font=("Consolas", 7), fill=AXIS_TXT)
        self._finish_panel(cv, g, "vol", lo, hi, v["dates"],
                           fmt=lambda v: fmt_vol_cn(v) + "手")

    # ---------- 可选指标 ----------

    def _draw_bollpct(self):
        """布林带 %B：收盘在带内的位置（0=下轨 100=上轨），20/80 为阈值。"""
        cv, v = self.cv_ind, self.view
        cv.delete("all")
        up, low, mid = v["boll_up"], v["boll_low"], v["boll_mid"]
        bars = v["bars"]
        n = len(bars)
        g = self._geom(cv, n, chips=bool(self.show_chips.get()
                                         and v.get("chips")))
        pct = []
        for i, b in enumerate(bars):
            if i >= len(up) or None in (up[i], low[i]) or up[i] <= low[i]:
                pct.append(None)
                continue
                pct.append(None)
                continue
            pct.append(max(-20.0, min(120.0,
                         (b["close"] - low[i]) / (up[i] - low[i]) * 100)))

        def ymap(val):
            return g["T"] + (100 - val) / 140 * g["ph"]

        def xs(i):
            return g["L"] + g["bw"] * (i + 0.5)
        lo, hi = -20, 120
        for gv in (0, 20, 50, 80, 100):
            col = GRID_C if gv in (0, 100) else GUIDE_C
            cv.create_line(g["L"], ymap(gv), g["w"] - g["R"], ymap(gv),
                           fill=col, dash=(2, 3) if gv in (20, 80) else ())
            cv.create_text(g["L"] - 4, ymap(gv), text=str(gv),
                           font=("Consolas", 7), fill=AXIS_TXT, anchor="e")
        self._line(cv, xs, pct, ymap, "#e8a838", width=1)
        lastv = next((x for x in reversed(pct) if x is not None), None)
        info = f"%B={lastv:.0f}" if lastv is not None else ""
        cv.create_text(g["L"] + 2, g["T"] - 3,
                       text=f"BOLL %B  橙线(0下轨/100上轨)    {info}",
                       anchor="w", font=("Microsoft YaHei", 8),
                       fill=TITLE_TXT)
        step = max(1, n // 10)
        for i in range(0, n, step):
            cv.create_text(xs(i), g["h"] - 7, text=v["dates"][i][5:],
                           font=("Consolas", 7), fill=AXIS_TXT)
        self._finish_panel(cv, g, "ind", -20, 120, v["dates"],
                           fmt=lambda x: f"{x:.0f}")

    def _draw_adx(self):
        """DMI/ADX 副图：+DI(橙) / -DI(绿) / ADX(蓝粗)。"""
        cv, v = self.cv_ind, self.view
        cv.delete("all")
        pdi, mdi, adx = v["pdi"], v["mdi"], v["adx"]
        n = len(v["bars"])
        g = self._geom(cv, n, chips=bool(self.show_chips.get()
                                         and v.get("chips")))
        vals = [x for x in pdi + mdi + adx if x is not None]
        lo, hi = (0, 60) if not vals else (0, max(60, max(vals) * 1.1))

        def ymap(val):
            return g["T"] + (hi - val) / (hi - lo) * g["ph"]

        def xs(i):
            return g["L"] + g["bw"] * (i + 0.5)
        for gv, sty in ((20, (2, 3)), (25, (4, 3))):
            cv.create_line(g["L"], ymap(gv), g["w"] - g["R"], ymap(gv),
                           fill=GUIDE_C, dash=sty)
            cv.create_text(g["L"] - 4, ymap(gv), text=str(gv),
                           font=("Consolas", 7), fill=AXIS_TXT, anchor="e")
        self._axes(cv, g, lo, hi, "{:.0f}", 2)
        self._line(cv, xs, pdi, ymap, "#e8890c")
        self._line(cv, xs, mdi, ymap, "#26c281")
        self._line(cv, xs, adx, ymap, "#1971c2", width=2)
        lv = lambda arr: [x for x in arr if x is not None]
        lp_, lm_, la_ = lv(pdi), lv(mdi), lv(adx)
        info = (f"+DI:{lp_[-1]:.0f} -DI:{lm_[-1]:.0f} ADX:{la_[-1]:.0f}"
                if lp_ and lm_ and la_ else "")
        cv.create_text(g["L"] + 2, g["T"] - 3,
                       text=("ADX/DMI  橙+DI / 绿-DI / 蓝ADX    " + info),
                       anchor="w", font=("Microsoft YaHei", 8),
                       fill=TITLE_TXT)
        step = max(1, n // 10)
        for i in range(0, n, step):
            cv.create_text(xs(i), g["h"] - 7, text=v["dates"][i][5:],
                           font=("Consolas", 7), fill=AXIS_TXT)
        self._finish_panel(cv, g, "ind", lo, hi, v["dates"],
                           fmt=lambda x: f"{x:.0f}")

    def _draw_macd(self):
        cv, v = self.cv_ind, self.view
        cv.delete("all")
        dif, dea, mh = v["dif"], v["dea"], v["mhist"]
        n = len(v["bars"])
        g = self._geom(cv, n, chips=bool(self.show_chips.get() and v.get("chips")))
        vals = [x for x in dif + dea + mh if x is not None]
        lo, hi = self._pad_range(min(vals + [0]), max(vals + [0]), 0.12)

        def ymap(val):
            return g["T"] + (hi - val) / (hi - lo) * g["ph"]

        def xs(i):
            return g["L"] + g["bw"] * (i + 0.5)
        zero = ymap(0)
        cv.create_line(g["L"], zero, g["w"] - g["R"], zero, fill=GRID_C)
        self._axes(cv, g, lo, hi, "{:.2f}", 2)
        mbw2 = max(g["bw"] * 0.3, 1.5)
        for i, hv in enumerate(mh):
            if hv is None:
                continue
            c = UP if hv >= 0 else DOWN
            y = ymap(hv)
            cv.create_rectangle(xs(i) - mbw2, min(y, zero),
                                xs(i) + mbw2, max(y, zero),
                                fill=c, outline=c)
        self._line(cv, xs, dif, ymap, "#e8890c")
        self._line(cv, xs, dea, ymap, "#1971c2")
        lv = lambda arr: [x for x in arr if x is not None]
        ld, la, lh = (lv(dif), lv(dea), lv(mh))
        info = f"DIF:{ld[-1]:.3f}  DEA:{la[-1]:.3f}  MACD:{lh[-1]:.3f}" \
            if ld and la and lh else ""
        cv.create_text(g["L"] + 2, g["T"] - 3,
                       text=f"MACD  橙DIF / 蓝DEA / 红绿柱    {info}",
                       anchor="w", font=("Microsoft YaHei", 8), fill=TITLE_TXT)
        self._finish_panel(cv, g, "ind", lo, hi, v["dates"],
                           fmt=lambda v: f"{v:.3f}")

    def _draw_kdj(self):
        cv, v = self.cv_ind, self.view
        cv.delete("all")
        k, d, j = v["k"], v["d"], v["j"]
        n = len(v["bars"])
        g = self._geom(cv, n, chips=bool(self.show_chips.get() and v.get("chips")))
        lo, hi = self._pad_range(min(j + [0]), max(j + [100]), 0.06)

        def ymap(val):
            return g["T"] + (hi - val) / (hi - lo) * g["ph"]

        def xs(i):
            return g["L"] + g["bw"] * (i + 0.5)
        for gv in (20, 50, 80):
            y = ymap(gv)
            cv.create_line(g["L"], y, g["w"] - g["R"], y,
                           fill=GUIDE_C if gv != 50 else GRID_C)
            cv.create_text(g["L"] - 4, y, text=str(gv), anchor="e",
                           font=("Consolas", 8), fill=AXIS_TXT)
        self._line(cv, xs, k, ymap, "#e8890c")
        self._line(cv, xs, d, ymap, "#1971c2")
        self._line(cv, xs, j, ymap, "#9c36b5")
        cv.create_text(g["L"] + 2, g["T"] - 3,
                       text=(f"KDJ  橙K / 蓝D / 紫J    "
                             f"K:{k[-1]:.1f}  D:{d[-1]:.1f}  J:{j[-1]:.1f}"),
                       anchor="w", font=("Microsoft YaHei", 8), fill=TITLE_TXT)
        self._finish_panel(cv, g, "ind", lo, hi, v["dates"],
                           fmt=lambda v: f"{v:.1f}")

    def _draw_rsi(self):
        cv, v = self.cv_ind, self.view
        cv.delete("all")
        n = len(v["bars"])
        g = self._geom(cv, n, chips=bool(self.show_chips.get() and v.get("chips")))
        lo, hi = self._pad_range(0, 100, 0.02)

        def ymap(val):
            return g["T"] + (hi - val) / (hi - lo) * g["ph"]

        def xs(i):
            return g["L"] + g["bw"] * (i + 0.5)
        for gv in (30, 50, 70):
            y = ymap(gv)
            cv.create_line(g["L"], y, g["w"] - g["R"], y,
                           fill=GUIDE_C if gv != 50 else GRID_C)
            cv.create_text(g["L"] - 4, y, text=str(gv), anchor="e",
                           font=("Consolas", 8), fill=AXIS_TXT)
        self._line(cv, xs, v["rsi6"], ymap, "#e8890c")
        self._line(cv, xs, v["rsi12"], ymap, "#1971c2")
        last6 = [x for x in v["rsi6"] if x is not None]
        last12 = [x for x in v["rsi12"] if x is not None]
        label = f"RSI  橙RSI6:{last6[-1]:.1f} / 蓝RSI12:{last12[-1]:.1f}" \
            if last6 and last12 else "RSI"
        cv.create_text(g["L"] + 2, g["T"] - 3, text=label, anchor="w",
                       font=("Microsoft YaHei", 8), fill=TITLE_TXT)
        self._finish_panel(cv, g, "ind", lo, hi, v["dates"],
                           fmt=lambda v: f"{v:.1f}")

    # ---------- 十字光标 ----------

    def _on_motion(self, event, key):
        if getattr(self, "_drag_x", None) is not None:
            return          # 拖拽平移中，不画十字光标（避免拖拽卡顿）
        """十字光标：线/标签连续跟随鼠标，数据读数吸附最近K线。"""
        if key not in self.scales or not self.view:
            return
        sg = self.scales[key]
        cv = {"main": self.cv_main, "vol": self.cv_vol,
              "ind": self.cv_ind}[key]

        # 轻操作：竖线/横线/价格标签逐像素跟随（仅悬停面板，缩小重绘区）
        xv = max(min(event.x, sg["w"] - sg["R"]), sg["L"])
        y = max(min(event.y, sg["T"] + sg["ph"]), sg["T"])
        cv.coords(sg["vid"], xv, sg["T"] + 2, xv, sg["h"] - sg["B"])
        cv.coords(sg["hid"], sg["L"], y, sg["w"] - sg["R"], y)
        price = sg["hi_v"] - (y - sg["T"]) / sg["ph"] * (
            sg["hi_v"] - sg["lo_v"])
        fmt = sg.get("fmt")
        txt = fmt(price) if fmt else f"{price:.2f}"
        px = sg["w"] - sg["R"] + 30
        cv.coords(sg["pid"], px, y)
        cv.itemconfigure(sg["pid"], text=txt)
        cv.coords(sg["pbg"], px - 27, y - 9, px + 29, y + 9)

        first = not sg["_shown"]
        if first:
            for it in ("vid", "hid", "pid", "pbg", "did", "dbgd"):
                cv.itemconfigure(sg[it], state="normal")
            cv.tag_raise(sg["pbg"])
            cv.tag_raise(sg["pid"])
            sg["_shown"] = True

        # 吸附数据：仅跨越K线时更新（重操作）
        idx = int((event.x - sg["L"]) / sg["bw"])
        idx = max(0, min(sg["n"] - 1, idx))
        sig = (key, idx)
        if getattr(self, "_mtn", None) == sig and not first:
            return
        self._mtn = sig
        cx = sg["L"] + sg["bw"] * (idx + 0.5)
        date = sg["dates"][idx]
        dl = date[:10]

        c2 = cv
        c2.coords(sg["did"], cx, sg["h"] - sg["B"] // 2 + 2)
        c2.itemconfigure(sg["did"], text=dl)
        w_bg = len(dl) * 7 + 10
        c2.coords(sg["dbgd"], cx - w_bg / 2,
                  sg["h"] - sg["B"] // 2 - 5,
                  cx + w_bg / 2, sg["h"] - sg["B"] // 2 + 11)

        bars = self.view["bars"]
        if idx >= len(bars):
            return
        b = bars[idx]
        v = self.view
        ind_txt = ""
        name = self.ind_name.get()
        if b["date"] != "T+1预测" and idx < len(v["dif"]) \
                and v["dif"][idx] is not None:
            if name == "MACD":
                if v["dea"][idx] is not None and v["mhist"][idx] is not None:
                    ind_txt = (f"  DIF:{v['dif'][idx]:.3f} "
                               f"DEA:{v['dea'][idx]:.3f} "
                               f"MACD:{v['mhist'][idx]:.3f}")
            elif name == "KDJ":
                ind_txt = (f"  K:{v['k'][idx]:.1f} D:{v['d'][idx]:.1f} "
                           f"J:{v['j'][idx]:.1f}")
            elif name == "BOLL":
                if None not in (v["boll_up"][idx], v["boll_low"][idx]):
                    ind_txt = (f"  上轨:{v['boll_up'][idx]:.2f} "
                               f"中轨:{v['boll_mid'][idx]:.2f} "
                               f"下轨:{v['boll_low'][idx]:.2f}")
            else:
                r6 = v["rsi6"][idx]
                r12 = v["rsi12"][idx]
                ind_txt = ("  RSI6:%.1f RSI12:%s"
                           % (r6, f"{r12:.1f}" if r12 is not None else "-"))
        if b["date"] in ("T+1预测", "T日预测", "T+5预测", "T+10预测"):
            tag = {"T+1预测": "预测T+1", "T日预测": "预测T日",
                   "T+5预测": "预测T+5(累计)", "T+10预测": "预测T+10(累计)"
                   }.get(b["date"], "预测")
            self.hover_var.set(
                f"[{tag}] 开{b['open']:.2f} 高{b['high']:.2f} "
                f"低{b['low']:.2f} 收{b['close']:.2f}")
            return
        sig_txt = ""
        for s_i, _day, s_t, s_txt in self.view["signals"]:
            if s_i == idx:
                sig_txt = f"  ◆[{'买' if s_t == 'BUY' else '卖'}] {s_txt}"
        pc = bars[idx - 1]["close"] if idx > 0 else (
            self.res["disp_rows"][self.view["off"] - 1]["close"]
            if self.view["off"] > 0 else self.res["prev_close"])
        chg = (b["close"] / pc - 1) * 100
        vol_s = (fmt_vol_cn(b["vol"]) + "手") if b.get("vol") else "-"
        self.hover_var.set(
            f"{b['date']} 开{b['open']:.2f} 高{b['high']:.2f} "
            f"低{b['low']:.2f} 收{b['close']:.2f} ({chg:+.2f}%) "
            f"量{vol_s}{ind_txt}{sig_txt}")

    def _on_leave(self, _event, _key=None):
        self.hover_var.set("")
        self._mtn = None
        for k2 in ("main", "vol", "ind"):
            sg2 = self.scales.get(k2)
            if not sg2:
                continue
            c2 = {"main": self.cv_main, "vol": self.cv_vol,
                  "ind": self.cv_ind}[k2]
            for it in ("vid", "hid", "pid", "pbg", "did", "dbgd"):
                c2.itemconfigure(sg2[it], state="hidden")
            sg2["_shown"] = False

    def _report_text(self):
        res, tp, pred, q = self.res, self.res["t_pred"], self.res["pred"], self.res["quote"]
        L = []
        L.append("=" * 64)
        st = res.get("strategy")
        L.append(f"当前策略: "
                 + (st.get("label", "?") if st else "多维评分·稳健(默认·未消融)")
                 + (f"（缓存至 {time.strftime('%m-%d %H:%M', time.localtime((st.get('ts') or 0) + STRAT_TTL))}，到期自动重新消融）"
                    if st else "（可在工具→重选策略运行消融回测）"))
        L.append("=" * 64)
        L.append(f"{q['name']} ({res['full_code']})  快照 {q['time']}  "
                 f"昨收 {res['prev_close']:.2f}")
        L.append(f"今开 {q['open']:.2f} (缺口 {res['gap_today']:+.2f}%)  "
                 f"现价 {q['price']:.2f}")
        idx_txt = (f"{res['idx_chg_today']:+.2f}%"
                   if res["idx_chg_today"] is not None else "未知")
        vr_txt = (f"量比 {res['vr_now']:.2f}（{res['cur_regime']}）"
                  if res["vr_now"] is not None else "数据不足")
        sec_txt = (f"{res['sector_name']} {res['sector_chg_today']:+.2f}%"
                   if res["sector_name"] and res["sector_chg_today"] is not None
                   else "未知")
        L.append(f"大盘(上证) {idx_txt} | 板块 {sec_txt} | 本股量能 {vr_txt}")
        L.append(f"-- 今日(T)预测 -- 锚定{res.get('anchor', '今开')}")
        for p in (10, 25, 50, 75, 90):
            L.append(f"P{p}: 收盘{tp['cl'][p]:.2f} 最高{tp['hi'][p]:.2f} "
                     f"最低{tp['lo'][p]:.2f}")
        L.append(f"开盘->收盘 上行概率 {tp['up_prob']*100:.0f}%   "
                 f"有效样本 {res['src_n']}/{len(res['samples'])}"
                 f"（{res['filter_note']}）")

        # T+5 累计预测（close→close 5日，区间校准k=1.4）
        t5 = res.get("t5_pred")
        if t5:
            c5 = t5["cl"]
            L.append(f"-- T+5预测(累计) -- n={t5['n']}")
            L.append(f"P10 {c5[10]:.2f} | P25 {c5[25]:.2f} | "
                     f"P50 {c5[50]:.2f} | P75 {c5[75]:.2f} | "
                     f"P90 {c5[90]:.2f}")
            L.append(f"5日上行概率 {t5['up_prob']*100:.0f}%")
        
        # 置信度信息
        confidence = tp.get("confidence")
        if confidence:
            conf_level = confidence["level"]
            conf_score = confidence["score"]
            L.append(f"预测置信度: {conf_level}(评分{conf_score:.2f})  "
                     f"总样本{confidence['total_samples']}只  "
                     f"有效层级{confidence['active_levels']}/3")
        else:
            L.append("预测置信度: 未启用")
        
        lv = res.get("levels") or []
        if len(lv) > 1:
            L.append("分层上行概率: " + " | ".join(
                f"{x['label']} {x['up_prob']*100:.0f}%(n={x['n']})"
                for x in lv))
            w = _dynamic_lv_weights([(x["key"], x.get("samples") or [])
                                     for x in lv])
            L.append(f"融合权重({('动态' if CFG.DYNAMIC_LV_W else '固定')}): " +
                     " ".join(f"{x['label']}{w.get(x['key'], 0):.2f}"
                              for x in lv))
        if res.get("pool_note"):
            L.append(res["pool_note"])
        if res["has_live"] and res["clamped"]:
            L.append(f"[盘中实时修正] 已实现最高 {res['live_high']:.2f} / "
                     f"最低 {res['live_low']:.2f}，已并入预测区间")
        L.append(f"-- {res.get('next_label', '次日(T+1)')}预测 --")
        L.append(f"开 {pred['open']:.2f} | 收 {pred['close']:.2f} | "
                 f"高 {pred['high']:.2f} | 低 {pred['low']:.2f}")
        # 布林带 / ADX 参考
        bl_up = next((x for x in reversed(res["ind"]["boll_up"])
                      if x is not None), None)
        bl_mid = next((x for x in reversed(res["ind"]["boll_mid"])
                      if x is not None), None)
        bl_low = next((x for x in reversed(res["ind"]["boll_low"])
                       if x is not None), None)
        if None not in (bl_up, bl_mid, bl_low):
            pos = ("上轨上方·超买" if q["price"] > bl_up
                   else "下轨下方·超卖" if q["price"] < bl_low
                   else "带内")
            L.append(f"-- 布林带(20,2) --")
            L.append(f"上轨 {bl_up:.2f} | 中轨 {bl_mid:.2f} | "
                     f"下轨 {bl_low:.2f} | 现价 {q['price']:.2f} 位于{pos}")
        _adx = next((x for x in reversed(res["ind"]["adx"])
                     if x is not None), None)
        _pdi = next((x for x in reversed(res["ind"]["pdi"])
                     if x is not None), None)
        _mdi = next((x for x in reversed(res["ind"]["mdi"])
                     if x is not None), None)
        if None not in (_adx, _pdi, _mdi):
            L.append("-- ADX/DMI(14) --")
            L.append(f"ADX {_adx:.0f}"
                     + ("（强趋势）" if _adx >= 25
                        else "（趋势形成）" if _adx >= 20 else "（无趋势·震荡）")
                     + f" | +DI {_pdi:.0f} vs -DI {_mdi:.0f}"
                     + (" 多头占优" if _pdi > _mdi else " 空头占优"))
        sigs = res["signals"]
        L.append(f"-- 近期买卖信号({min(len(sigs),12)}条) --")
        for i, day, typ, txt in sigs[-12:]:
            tag = "买" if typ == "BUY" else "卖"
            L.append(f"  {day} [{tag}] {txt}")
        if not sigs:
            L.append("  近期无")
        L.append("[提示] 历史统计推断仅供参考，不构成投资建议。")
        return "\n".join(L)

    def _write_report(self):
        self.txt.delete("1.0", "end")
        self.txt.insert("end", self._report_text())

    def _write_side(self):
        res = self.res
        tp, pred = res["t_pred"], res["pred"]
        t = self.side_txt
        t.delete("1.0", "end")

        def p(s=""):
            t.insert("end", s + "\n")

        def tag(s, c):
            t.insert("end", s + "\n", ("c",))
            t.tag_config("c", foreground=c)

        st = res.get("strategy")
        tag("■ 当前策略: " + (st.get("label", "?") if st
                              else "多维评分·稳健(默认·未消融)"),
            "#ffd34d")
        if not st:
            p("  (工具→重选策略 可运行多算法消融回测)")
        p(f"■ 今日(T)收盘预测  [锚定{res.get('anchor', '今开')}]")
        for pp in (10, 50, 90):
            p(f"  P{pp}: 收{tp['cl'][pp]:.2f} 高{tp['hi'][pp]:.2f} 低{tp['lo'][pp]:.2f}")
        p(f"  上行概率 {tp['up_prob']*100:.0f}%  "
          f"样本{res['src_n']}/{len(res['samples'])}")

        # T+5 累计预测
        t5 = res.get("t5_pred")
        if t5:
            c5 = t5["cl"]
            tag(f"  T+5: P50 {c5[50]:.2f}  区间 {c5[25]:.2f}~{c5[75]:.2f}  "
                f"上行{t5['up_prob']*100:.0f}%", PRED_C)
        
        # 置信度显示
        confidence = tp.get("confidence")
        if confidence:
            conf_color = UP if confidence["score"] >= CFG.MEDIUM_CONFIDENCE_SCORE else \
                        ("#ffd34d" if confidence["score"] >= CFG.LOW_CONFIDENCE_SCORE else DOWN)
            conf_level = confidence["level"]
            conf_score = confidence["score"]
            tag(f"  预测置信度: {conf_level}(评分{conf_score:.2f})  样本{confidence['total_samples']}只 层级{confidence['active_levels']}/3", conf_color)
        else:
            p("  预测置信度: 未启用")
        
        idx_txt = (f"{res['idx_chg_today']:+.2f}%"
                   if res["idx_chg_today"] is not None else "未知")
        vr_txt = (f"{res['vr_now']:.2f}({res['cur_regime']})"
                  if res["vr_now"] is not None else "-")
        p(f"  大盘 {idx_txt} | 量能 {vr_txt}")
        if res["sector_name"]:
            sec_txt = (f"{res['sector_chg_today']:+.2f}%"
                       if res["sector_chg_today"] is not None else "-")
            p(f"  板块 {res['sector_name']} {sec_txt}")
        p(f"  筛选: {res['filter_note']}")
        lv = res.get("levels") or []
        if len(lv) > 1:
            p("  分层上行概率: " + " | ".join(
                f"{x['label']}{x['up_prob']*100:.0f}%(n={x['n']})"
                for x in lv))
        if res.get("pool_note"):
            p(f"  {res['pool_note']}")
        if res["has_live"] and res["clamped"]:
            p(f"  [实时修正] 盘中已实现 高{res['live_high']:.2f} "
              f"低{res['live_low']:.2f}")
        p()
        p(f"■ {res.get('next_label', '次日(T+1)')}预测")
        p(f"  开{pred['open']:.2f} 收{pred['close']:.2f}")
        p(f"  高{pred['high']:.2f} 低{pred['low']:.2f}")
        cp_ = res.get("chips")
        if cp_:
            p()
            p("■ 筹码参考")
            p(f"  平均成本 {cp_['avg_cost']:.2f} | 现价 {cp_['cur']:.2f}")
            p(f"  获利盘 {cp_['profit']*100:.0f}%"
              f"（{'有获利抛压' if cp_['profit'] < 0.3 else '套牢盘较少' if cp_['profit'] > 0.7 else '筹码较均衡'}）")
            if cp_["p5"] and cp_["p95"]:
                p(f"  90%筹码区间 {cp_['p5']:.2f} ~ {cp_['p95']:.2f}")
            lv_txt = []
            if cp_["sup"]:
                lv_txt.append(f"支撑 {cp_['sup']:.2f}")
            if cp_["res"]:
                lv_txt.append(f"压力 {cp_['res']:.2f}")
            if lv_txt:
                p("  " + " | ".join(lv_txt))
        # 布林带参考
        bl_up = next((x for x in reversed(res["ind"]["boll_up"])
                      if x is not None), None)
        bl_mid = next((x for x in reversed(res["ind"]["boll_mid"])
                       if x is not None), None)
        bl_low = next((x for x in reversed(res["ind"]["boll_low"])
                       if x is not None), None)
        if None not in (bl_up, bl_mid, bl_low):
            p()
            p("■ 布林带(20,2)")
            p(f"  上轨 {bl_up:.2f} | 中轨 {bl_mid:.2f} | 下轨 {bl_low:.2f}")
            cur_px = res["quote"]["price"]
            pos = ("上轨上方·超买" if cur_px > bl_up
                   else "下轨下方·超卖" if cur_px < bl_low
                   else "带内")
            p(f"  现价 {cur_px:.2f} 位于{pos}")
        # ADX/DMI 参考
        _adx = next((x for x in reversed(res["ind"]["adx"])
                     if x is not None), None)
        _pdi = next((x for x in reversed(res["ind"]["pdi"])
                     if x is not None), None)
        _mdi = next((x for x in reversed(res["ind"]["mdi"])
                     if x is not None), None)
        if None not in (_adx, _pdi, _mdi):
            p()
            p("■ ADX/DMI(14)")
            p(f"  ADX {_adx:.0f}"
              + ("（强趋势）" if _adx >= 25
                 else "（趋势形成）" if _adx >= 20 else "（无趋势·震荡）")
              + f" | +DI {_pdi:.0f} vs -DI {_mdi:.0f}"
              + (" 多头占优" if _pdi > _mdi else " 空头占优"))
        act = res.get("action")
        if act:
            p()
            p("■ 综合评估(买卖点)")
            if act.get("band_note"):
                p("  ◆ " + act["band_note"])
            for lab, sc, note in act["items"]:
                mark = "+" if sc > 0 else ("-" if sc < 0 else "·")
                tg = f"act_{lab}"
                t.insert("end", f"  [{mark}] {lab} ", ("c",))
                t.tag_config(tg, foreground=UP if sc > 0 else
                             (DOWN if sc < 0 else AXIS_TXT))
                t.insert("end", f"{note}\n", (tg,))
            tg = "act_v"
            t.insert("end", f"  合计 {act['score']:+d} → {act['verdict']}\n",
                     ("c",))
            t.tag_config(tg, foreground="#ffd34d",
                         font=("Microsoft YaHei", 9, "bold"))
        # 多日预测
        multi = res.get("multi_pred")
        if multi:
            p()
            p("■ 多日预测趋势")
            p(f"{'周期':<8}{'收盘预测':>8}{'最高预测':>8}{'最低预测':>8}{'上行概率':>8}{'累计涨跌':>8}")
            for mp in multi:
                p(f"{mp['label']:<8}"
                  f"{mp['price_cl']:>8.2f}"
                  f"{mp['price_hi']:>8.2f}"
                  f"{mp['price_lo']:>8.2f}"
                  f"{mp['up_prob']*100:>7.0f}%"
                  f"{mp['cum_cl']*100:>+7.1f}%")
        p()
        p("■ 相似历史参考日期")
        p(f"(近{W_WINDOW}日形态匹配 候选{CFG.CANDIDATE_TOPK}→筛选{TOPK})")
        p(f"{'T日':<11}{'T+1日':<11}{'次日涨跌':>8}{'权重':>6}{'相似度':>6}")
        for s in res["samples"]:
            n1_cl = s.get("n1_cl")
            chg1 = n1_cl * 100 if n1_cl is not None else 0
            n1_date = s.get("n1_date", "N/A")
            mark = "*" if abs((s.get("gap") or 0) * 100 - res["gap_today"]) <= 1.0 else ""
            weight = s.get("weight", 1.0)
            similarity = s.get("similarity_score", 0)
            p(f"{s['t_date']:<11}{n1_date:<11}{chg1:>+7.1f}% {mark}{weight:>5.2f}{similarity:>6.2f}")
        p()
        p("* = 开盘缺口与今日接近  | 权重基于相似度/时间/质量  | 相似度越低越相似")
        p("──────────────────────")
        p("■ 最近买卖信号")
        sigs = res["signals"][-6:]
        if not sigs:
            p("  近期无")
        for n_i, (s_i, day, typ, txt) in enumerate(reversed(sigs)):
            tg = f"sig{n_i}"
            t.insert("end", f"  {day} [{'买' if typ == 'BUY' else '卖'}] ",
                     ("c",))
            t.tag_config(tg,
                         foreground=UP if typ == "BUY" else DOWN)
            t.insert("end", txt + "\n", (tg,))
        # 回测统计
        bt = res.get("bt_stats")
        if bt:
            p()
            p("■ 回测统计（样本内全部信号）")
            p(f"  总交易 {bt['trades']} 笔 | 已平仓 {bt['closed']} 笔 | 盈利 {bt['wins']} 笔")
            if bt.get("winrate") is not None:
                p(f"  胜率 {bt['winrate']*100:.1f}% | 区间收益 {bt['total']*100:+.1f}%"
                  f" | 年化收益 {bt['ann']*100:+.1f}%"
                  f" | 最大回撤 {bt['mdd']*100:.1f}%")
            if bt.get("floating") is not None:
                p(f"  未平仓浮盈 {bt['floating']*100:+.1f}%")
        p("提示：前复权价统计推断，")
        p("仅供参考，不构成投资建议")
        p("──────────────────────")
        p("■ 市场状态")
        tg_ph = "phase_tag"
        t.insert("end", f"  ● {res.get('phase', '时间未知')}"
                 + ("（未开盘·预测锚定昨收）" if res.get("pre_open") else "")
                 + "\n", (tg_ph,))
        t.tag_config(tg_ph, foreground="#4da3ff",
                     font=("Microsoft YaHei", 10, "bold"))
        if getattr(self, "ai_text", ""):
            p()
            p("══════════════════════")
            tag_cfg = self.side_txt.tag_config
            self.side_txt.insert("end", "■ DeepSeek AI 分析\n", ("h3",))
            self.side_txt.tag_config("h3", foreground="#4da3ff",
                                     font=("Microsoft YaHei", 10, "bold"))
            self.side_txt.insert("end", self.ai_text + "\n")

    # ---------- 自选池 ----------

    def _load_config(self):
        cp = configparser.ConfigParser()
        if os.path.exists(INI_PATH):
            try:
                cp.read(INI_PATH, encoding="utf-8")
                codes = [c.strip() for c in
                         cp.get("watchlist", "codes", fallback="").split(",")
                         if c.strip()]
                self.watchlist = codes
                self._last_code = cp.get("ui", "last", fallback="")
                self.settings["theme"] = cp.get("ui", "theme",
                                                fallback="dark")
                self.settings["updown"] = cp.get("ui", "updown",
                                                 fallback="red_up")
                self.api_key = cp.get("deepseek", "api_key", fallback="")
                self.proxy_url = cp.get("proxy", "url", fallback="")
            except Exception:
                pass

    def _save_ini(self):
        cp = configparser.ConfigParser()
        if not cp.has_section("watchlist"):
            cp.add_section("watchlist")
        cp.set("watchlist", "codes", ",".join(self.watchlist))
        if not cp.has_section("ui"):
            cp.add_section("ui")
        cp.set("ui", "last", self.code_var.get())
        cp.set("ui", "theme", self.settings["theme"])
        cp.set("ui", "updown", self.settings["updown"])
        if not cp.has_section("deepseek"):
            cp.add_section("deepseek")
        cp.set("deepseek", "api_key", self.api_key)
        if not cp.has_section("proxy"):
            cp.add_section("proxy")
        cp.set("proxy", "url", getattr(self, "proxy_url", ""))
        try:
            with open(INI_PATH, "w", encoding="utf-8") as f:
                cp.write(f)
        except OSError as e:
            print(f"[ini] 保存失败: {e}")

    def _render_watchlist(self):
        if getattr(self, "compact", False) or not hasattr(self, "watch_list"):
            return                  # 小屏无自选池面板
        self.watch_list.delete(0, "end")
        for c in self.watchlist:
            name = getattr(self, "_names", {}).get(c, "")
            self.watch_list.insert("end", f"{c} {name}")

    def add_watch(self):
        raw = self.code_var.get()
        if not raw:
            messagebox.showinfo("提示", "先在上方输入代码再点【加自选】")
            return
        try:
            full = normalize_code(raw)
        except ValueError as e:
            messagebox.showwarning("代码有误", str(e))
            return
        if full in self.watchlist:
            self.progress_var.set(f"{full} 已在自选池")
            return
        self.watchlist.append(full)
        self._save_ini()
        self._refresh_names()

    def del_watch(self):
        sel = self.watch_list.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选中一项")
            return
        real = self.watch_list.get(sel[0]).split()[0]
        if real in self.watchlist:
            self.watchlist.remove(real)
            self._save_ini()
            self._render_watchlist()

    def _on_pick(self, _event):
        sel = self.watch_list.curselection()
        if not sel:
            return
        code = self.watchlist[sel[0]].split()[0]
        self.code_var.set(code)
        self.run()

    def _refresh_names(self):
        """批量拉自选池名称后刷新列表。"""
        if not self.watchlist:
            self._render_watchlist()
            return

        def fn():
            names = {}
            raw = http_get(QT_URL + ",".join(self.watchlist))
            for seg in raw.split(";"):
                seg = seg.strip()
                if "=" not in seg or "~" not in seg:
                    continue
                var_name = seg.split("=")[0].strip()   # 如 v_sz002241
                code = var_name.replace("v_", "", 1).lower()
                f = seg.split("~")
                if len(f) > 1 and code:
                    names[code] = f[1]
            return names

        def done(names, err):
            if err is None:
                self._names = names
            self._render_watchlist()
        self._run_bg(fn, done)

    # ---------- 五大指数 ----------

    def _update_indices(self):
        if getattr(self, "compact", False) and not self.idx_labels:
            return              # 小屏：指数弹窗未打开时不拉取
        codes = [c for c, _ in INDEX_CODES]

        def fn():
            data = {}
            raw = http_get(QT_URL + ",".join(codes))
            for seg in raw.split(";"):
                seg = seg.strip()
                if "=" not in seg or "~" not in seg:
                    continue
                code = seg.split("=")[0].strip().replace("v_", "", 1).lower()
                f = seg.split("~")
                if len(f) < 34 or not f[3]:
                    continue
                try:
                    data[code] = {"name": f[1], "price": float(f[3]),
                                  "chg": float(f[32])}
                except ValueError:
                    continue
            return data

        def done(data, err):
            self.idx_data = data or {}
            for code, name in INDEX_CODES:
                lp, lc = self.idx_labels[code]
                info = (data or {}).get(code)
                if info:
                    lp.config(text=f"{info['price']:.2f}")
                    chg = info["chg"]
                    lc.config(text=f"{chg:+.2f}%",
                              fg=UP if chg >= 0 else DOWN)
                else:
                    lp.config(text="-")
                    lc.config(text="-", fg=AXIS_TXT)
        self._run_bg(fn, done)

    def _index_loop(self):
        self._update_indices()
        if not getattr(self, "compact", False):
            self._update_sectors()      # 小屏无行业面板
        self._safe_after(30000, self._index_loop)

    def _update_sectors(self):
        def fn():
            return fetch_top_sectors()

        def done(result, err):
            if getattr(self, "compact", False):
                return              # 小屏无行业面板
            top3, bot3 = result or ([], [])
            st = self.sector_txt
            st.config(state="normal")
            st.delete("1.0", "end")
            if not top3 and not bot3:
                st.insert("end", "  暂无数据")
            else:
                st.insert("end", "  涨幅前三\n")
                for name, pct in top3:
                    tag = "g" if pct >= 0 else "r"
                    st.insert("end", f"    {name:<8}", (tag,))
                    st.insert("end", f" {pct:+.2f}%\n", (tag,))
                st.insert("end", "\n  跌幅前三\n")
                for name, pct in bot3:
                    tag = "g" if pct >= 0 else "r"
                    st.insert("end", f"    {name:<8}", (tag,))
                    st.insert("end", f" {pct:+.2f}%\n", (tag,))
            st.tag_config("g", foreground=UP)
            st.tag_config("r", foreground=DOWN)
            st.config(state="disabled")
        self._run_bg(fn, done)

    def copy_report(self):
        if not self.res:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self._report_text())
        self.progress_var.set("报告已复制到剪贴板")

    def export_report(self):
        if not self.res:
            return
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(desk):
            desk = os.path.expanduser("~")
        fn = filedialog.asksaveasfilename(
            initialdir=desk,
            initialfile=f"预测_{self.res['full_code']}_{time.strftime('%Y%m%d')}.txt",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt")])
        if not fn:
            return
        with open(fn, "w", encoding="utf-8") as f:
            f.write(self._report_text())
            f.write("\n\n-- 相似样本明细 --\n")
            for s in self.res["samples"]:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
            f.write(f"\n作者：{AUTHOR}  邮箱：{AUTHOR_EMAIL}  "
                    f"QQ：{AUTHOR_QQ}\n{DISCLAIMER}\n")
        self.progress_var.set(f"已导出: {fn}")

    def mail_report(self):
        """把当前预测报告邮件发送到配置的收件箱。"""
        if not self.res:
            messagebox.showinfo("提示", "请先【分析预测】一只股票")
            return
        self.progress_var.set("正在发送邮件...")
        code = self.res["full_code"]

        def fn():
            body = self._report_text() + "\n\n-- 相似样本明细 --\n"
            for s in self.res["samples"]:
                body += json.dumps(s, ensure_ascii=False) + "\n"
            body += (f"\n作者：{AUTHOR}  邮箱：{AUTHOR_EMAIL}  "
                     f"QQ：{AUTHOR_QQ}\n{DISCLAIMER}\n")
            return send_email_report(
                f"股票预测报告 {code} {time.strftime('%Y-%m-%d %H:%M')}",
                body)

        def done(ok, err):
            if err:
                self.progress_var.set(f"邮件发送失败: {err}")
                messagebox.showerror("邮件发送失败", str(err))
            else:
                self.progress_var.set(
                    f"报告已发送: {code} -> {', '.join(ok)}")
        self._run_bg(fn, done)

    def show_samples(self):
        if not self.res:
            return
        win = tk.Toplevel(self.root)
        win.title(f"相似样本明细 - {self.res['full_code']}")
        txt = tk.Text(win, width=110, font=("Consolas", 9))
        txt.pack(fill="both", expand=True)
        hdr = (f"{'T日':<11}{'T+1日':<11}{'T+2日':<11}{'缺口%':>7}{'高/开%':>8}"
               f"{'低/开%':>8}{'收/开%':>8}{'T2高/开%':>9}{'T2低/开%':>9}{'T2收/开%':>9}\n")
        txt.insert("end", hdr)
        for s in self.res["samples"]:
            txt.insert("end",
                       f"{s['t_date']:<11}{s.get('n1_date') or '-':<11}"
                       f"{s.get('n2_date') or '-':<11}")
            for key in ("gap", "n1_hi", "n1_lo", "n1_cl",
                        "n2_hi", "n2_lo", "n2_cl"):
                v = s.get(key)
                txt.insert("end",
                           f"{v*100:>9.2f}" if v is not None else "        -")
            txt.insert("end", "\n")

    # ---------- AI 分析（DeepSeek） ----------

    def _ai_prompt(self):
        res, tp, pred = self.res, self.res["t_pred"], self.res["pred"]
        q = res["quote"]
        ind = res["ind"]
        last = lambda a: next((v for v in reversed(a) if v is not None), None)
        f3 = lambda v: f"{v:.3f}" if isinstance(v, (int, float)) else "-"
        f1 = lambda v: f"{v:.1f}" if isinstance(v, (int, float)) else "-"
        bars = res["disp_rows"][-10:]
        kline_txt = "\n".join(
            f"{b['date']} 开{b['open']:.2f} 高{b['high']:.2f} "
            f"低{b['low']:.2f} 收{b['close']:.2f} 量{b.get('vol') or 0:.0f}"
            for b in bars)
        sig_txt = "; ".join(f"{d}[{t}]{x}" for _, d, t, x in res["signals"][-6:]) or "无"
        sm = res["samples"]
        vals = [s["n1_cl"] for s in sm if s.get("n1_cl") is not None]
        avg_t1 = (sum(vals) / len(vals) * 100) if vals else 0.0
        idx_txt = (f"{res['idx_chg_today']:+.2f}%"
                   if res["idx_chg_today"] is not None else "数据不足")
        vr_txt = (f"量比 {res['vr_now']:.2f}（{res['cur_regime']}）"
                  if res["vr_now"] is not None else "数据不足")
        sec_txt = (f"{res['sector_name']}今日 {res['sector_chg_today']:+.2f}%"
                   if res["sector_name"] and res["sector_chg_today"] is not None
                   else "板块数据不足")
        txt = (
            f"你是专业A股分析师。请基于以下数据给出简短分析（300字内），"
            f"包含：1)技术面与量价配合解读(MA/MACD/KDJ/RSI/量能) "
            f"2)结合大盘、板块环境、统计预测、筹码分布的短线(1-3日)操作建议，"
            f"须给出明确的买点/卖点参考价位（参考下方支撑位/压力位） 3)风险提示。"
            f"用中文，直接给结论。\n\n"
            f"股票：{q['name']}({res['full_code']}) 快照{q['time']}\n"
            f"昨收{res['prev_close']:.2f} 今开{q['open']:.2f}"
            f"(缺口{res['gap_today']:+.2f}%) 现价{q['price']:.2f}\n"
            f"大盘：上证指数今日 {idx_txt}；本股量能：{vr_txt}；"
            f"板块：{sec_txt}\n\n"
            f"近10日行情：\n{kline_txt}\n\n"
            f"指标最新值：MA5={f3(last(ind['ma'][5]))} MA10={f3(last(ind['ma'][10]))} "
            f"MA20={f3(last(ind['ma'][20]))} MA60={f3(last(ind['ma'][60]))}\n"
            f"DIF={f3(last(ind['dif']))} DEA={f3(last(ind['dea']))} "
            f"MACD柱={f3(last(ind['mhist']))}\n"
            f"K={f1(last(ind['k']))} D={f1(last(ind['d']))} J={f1(last(ind['j']))}\n"
            f"RSI6={f1(last(ind['rsi6']))} RSI12={f1(last(ind['rsi12']))}\n"
            f"布林带(20,2): 上轨={f3(last(ind['boll_up']))} "
            f"中轨={f3(last(ind['boll_mid']))} 下轨={f3(last(ind['boll_low']))}"
            f" 现价位于{'上轨上方' if c > (last(ind['boll_up']) or 1e18) else ('下轨下方' if c < (last(ind['boll_low']) or -1) else '带内')}\n"
            f"ADX/DMI(14): ADX={f1(last(ind['adx']))} "
            f"+DI={f1(last(ind['pdi']))} -DI={f1(last(ind['mdi']))}"
            "（ADX≥25强趋势 / <20震荡，DI方向即趋势方向）\n\n"
             f"历史形态统计预测(锚定{res.get('anchor', '今开')})：\n"
            f"今日收盘 P50={tp['cl'][50]:.2f}(P10 {tp['cl'][10]:.2f}/"
            f"P90 {tp['cl'][90]:.2f}) 上行概率{tp['up_prob']*100:.0f}%\n"
            f"{res.get('next_label', '次日(T+1)')}预测 开{pred['open']:.2f} 收{pred['close']:.2f} "
            f"高{pred['high']:.2f} 低{pred['low']:.2f}\n"
            f"相似样本{len(sm)}个, 样本次日平均涨跌{avg_t1:+.2f}%\n"
            f"近期买卖信号：{sig_txt}\n"
        )
        cp_ = res.get("chips")
        if cp_:
            txt += (
                f"筹码分布：平均成本{cp_['avg_cost']:.2f} "
                f"获利盘{cp_['profit']*100:.0f}% "
                f"90%区间{cp_['p5']:.2f}~{cp_['p95']:.2f}"
                + (f" 支撑位{cp_['sup']:.2f}" if cp_["sup"] else "")
                + (f" 压力位{cp_['res']:.2f}" if cp_["res"] else "") + "\n"
            )
        act = res.get("action")
        if act:
            det = "; ".join(f"{lab}{sc:+d}({note})"
                            for lab, sc, note in act["items"])
            txt += (f"多维综合评估：合计{act['score']:+d}，{act['verdict']}"
                    f" [{det}]\n")
        return txt

    def open_tools(self):
        if not self.res:
            messagebox.showinfo("提示", "请先【分析预测】一只股票")
            return
        win = tk.Toplevel(self.root)
        win.title("工具")
        win.configure(bg=DARK_BG)
        win.geometry("620x520")
        win.transient(self.root)
        win.grab_set()

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        # ── 胜率计算 ──
        f_bt = ttk.Frame(nb, padding=10)
        nb.add(f_bt, text=" 信号胜率 ")

        bt_result = tk.Text(f_bt, height=18, bg=PANEL_BG, fg=FG_MAIN,
                            font=("Microsoft YaHei", 10), relief="flat",
                            wrap="word", state="disabled")
        bt_scroll = ttk.Scrollbar(f_bt, command=bt_result.yview)
        bt_result.configure(yscrollcommand=bt_scroll.set)
        bt_scroll.pack(side="right", fill="y")
        bt_result.pack(fill="both", expand=True)

        def run_bt():
            if not self.res:
                return
            bt = backtest_signals(self.res["disp_rows"], self.res["signals"])
            bt_result.config(state="normal")
            bt_result.delete("1.0", "end")
            if not bt or bt.get("winrate") is None:
                bt_result.insert("end", "信号不足，无法计算胜率")
            else:
                wr = f"{bt['winrate']*100:.0f}%"
                bt_result.insert("end",
                    f"  近120日信号回测（无手续费）\n"
                    f"  ─────────────────────────\n"
                    f"  交易 {bt['trades']} 次（已平仓 {bt['closed']}）\n"
                    f"  胜率 {wr}（{bt['wins']}/{bt['closed']}）\n"
                    f"  区间收益 {bt['total']*100:+.1f}%\n"
                    f"  年化收益 {bt['ann']*100:+.1f}%\n"
                    f"  最大回撤 {bt['mdd']*100:.1f}%\n")
                if bt["floating"] is not None:
                    bt_result.insert("end",
                        f"  未平仓浮盈 {bt['floating']*100:+.1f}%\n")
                bt_result.insert("end",
                    f"\n  提示：信号基于多维打分+方向切换触发，\n"
                    f"  每日最多一个B/S标记，仅供参考。\n")
            bt_result.config(state="disabled")

        btn_bt = tk.Button(f_bt, text="计算胜率", command=run_bt,
                           bg=BTN_BG, fg=BTN_FG,
                           activebackground=BTN_HOVER, activeforeground=BTN_FG,
                           relief="flat", cursor="hand2",
                           font=("Microsoft YaHei", 10, "bold"))
        btn_bt.pack(pady=6, ipadx=16, ipady=4)
        tk.Button(f_bt, text="重选策略(消融回测)", command=self._rerun_strategy,
                  bg=BTN_BG, fg=BTN_FG,
                  activebackground=BTN_HOVER, activeforeground=BTN_FG,
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 10, "bold")).pack(
                      pady=2, ipadx=10, ipady=4)

        # ── AI 分析（多轮对话，共享同一份数据上下文） ──
        f_ai = ttk.Frame(nb, padding=10)
        nb.add(f_ai, text=" AI 分析 ")

        ai_result = tk.Text(f_ai, height=16, bg=PANEL_BG, fg=FG_MAIN,
                            font=("Microsoft YaHei", 10), relief="flat",
                            wrap="word", state="disabled")
        ai_scroll = ttk.Scrollbar(f_ai, command=ai_result.yview)
        ai_result.configure(yscrollcommand=ai_scroll.set)
        ai_scroll.pack(side="right", fill="y")
        ai_result.pack(fill="both", expand=True)

        def _ensure_key():
            key = self.api_key
            if not key:
                key = simpledialog.askstring(
                    "DeepSeek API Key",
                    "首次使用请输入 DeepSeek API Key\n(仅保存在本地 stock_gui.ini)：",
                    show="*", parent=win)
                if not key:
                    return None
                self.api_key = key.strip()
                self._save_ini()
            return self.api_key

        def _render():
            ai_result.config(state="normal")
            ai_result.delete("1.0", "end")
            if not self._ai_msgs:
                ai_result.insert("end", "点击【开始分析】，可连续追问。")
            else:
                for m in self._ai_msgs:
                    who = "你" if m["role"] == "user" else "AI"
                    ai_result.insert("end", f"── {who} ──\n{m['content']}\n\n")
            ai_result.config(state="disabled")
            ai_result.see("end")

        def _call(msg_content):
            """msg_content 已含完整数据上下文或追问；追加到历史并发送。"""
            key = _ensure_key()
            if not key:
                return
            if self._ai_msgs and self._ai_msgs[0]["role"] == "user":
                # 首条已带完整数据上下文，追问只追加新问题，复用共享数据
                self._ai_msgs.append({"role": "user", "content": msg_content})
            else:
                self._ai_msgs = [{"role": "user", "content": msg_content}]
            _render()
            ai_result.config(state="normal")
            ai_result.insert("end", "\nAI 思考中...\n")
            ai_result.config(state="disabled")

            msgs = list(self._ai_msgs)
            ask_var.set("")
            follow_btn.config(state="disabled")

            def bg():
                return _deepseek_chat(self.api_key, msgs)

            def done(text, err):
                follow_btn.config(state="normal")
                if err:
                    self._ai_msgs = self._ai_msgs[:-1]  # 回滚失败的那条
                    ai_result.config(state="normal")
                    ai_result.insert(
                        "end", f"\n[AI 分析失败：{err}\n请检查 API Key 与网络。]\n")
                    ai_result.config(state="disabled")
                else:
                    self._ai_msgs.append({"role": "assistant", "content": text})
                    self.ai_text = text
                    self._write_side()
                    _render()
                ai_result.see("end")

            self._run_bg(bg, done)

        def run_ai():
            # 首轮：把完整共享数据上下文放进首条 user 消息
            _call(self._ai_prompt())

        def follow_ai():
            q = ask_var.get().strip()
            if not q:
                return
            _call(q)

        bar = ttk.Frame(f_ai)
        bar.pack(fill="x", pady=6)
        tk.Button(bar, text="开始分析", command=run_ai,
                  bg=BTN_BG, fg=BTN_FG,
                  activebackground=BTN_HOVER, activeforeground=BTN_FG,
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 10, "bold")).pack(
                      side="left", padx=(0, 6), ipadx=10, ipady=4)
        ask_var = tk.StringVar()
        ask = ttk.Entry(bar, textvariable=ask_var)
        ask.pack(side="left", fill="x", expand=True, ipady=3)
        ask.bind("<Return>", lambda e: follow_ai())
        follow_btn = tk.Button(bar, text="追问", command=follow_ai,
                               bg=BTN_BG, fg=BTN_FG,
                               activebackground=BTN_HOVER, activeforeground=BTN_FG,
                               relief="flat", cursor="hand2",
                               font=("Microsoft YaHei", 10, "bold"))
        follow_btn.pack(side="left", padx=(6, 0), ipadx=10, ipady=4)
        _render()

    # ---------- 设置 ----------

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.configure(bg=DARK_BG)
        win.transient(self.root)
        win.resizable(False, False)
        if getattr(self, "compact", False):
            # 小屏：设置窗口自动全屏，保证控件不超出屏幕
            win.geometry(f"{self.root.winfo_screenwidth()}x"
                         f"{self.root.winfo_screenheight()}+0+0")
            win.attributes("-fullscreen", True)
            win.bind("<Escape>",
                     lambda e: win.attributes("-fullscreen", False))
        # 可滚动容器：设置内容多，小屏限高+滚轮滚动，按钮永远可达
        maxh = int(self.root.winfo_screenheight() * 0.88)
        cv = tk.Canvas(win, bg=DARK_BG, highlightthickness=0)
        sb = ttk.Scrollbar(win, orient="vertical", command=cv.yview)
        frm = ttk.Frame(cv, padding=14)
        cv.create_window((0, 0), window=frm, anchor="nw", tags="frm")
        cv.configure(yscrollcommand=sb.set)

        def _fs_fit(_e=None):
            cv.configure(scrollregion=cv.bbox("all"))
            h = min(frm.winfo_reqheight() + 20, maxh)
            try:
                cv.configure(height=max(200, h))
                wwidth = max(580, min(frm.winfo_reqwidth() + 40,
                                      self.root.winfo_screenwidth() - 20))
                win.geometry(f"{wwidth}x{h + 6}")
            except Exception:
                pass
        frm.bind("<Configure>", _fs_fit)

        def _wheel(e):
            try:
                cv.yview_scroll(-1 * (e.delta // 120), "units")
            except Exception:
                pass
        cv.bind("<MouseWheel>", _wheel)
        cv.bind("<Button-4>", lambda e: cv.yview_scroll(-1, "units"))
        cv.bind("<Button-5>", lambda e: cv.yview_scroll(1, "units"))
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        ttk.Label(frm, text="界面主题").grid(row=0, column=0, sticky="w", pady=4)
        theme_var = tk.StringVar(value=self.settings["theme"])
        ttk.Radiobutton(frm, text="暗色", variable=theme_var,
                        value="dark").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(frm, text="亮色", variable=theme_var,
                        value="light").grid(row=0, column=2, sticky="w")

        ttk.Label(frm, text="涨跌配色").grid(row=1, column=0, sticky="w", pady=4)
        ud_var = tk.StringVar(value=self.settings["updown"])
        ttk.Radiobutton(frm, text="红涨绿跌", variable=ud_var,
                        value="red_up").grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(frm, text="绿涨红跌", variable=ud_var,
                        value="green_up").grid(row=1, column=2, sticky="w")

        ttk.Label(frm, text="DeepSeek Key").grid(row=2, column=0, sticky="w",
                                                 pady=(8, 4))
        key_var = tk.StringVar(value=self.api_key)
        ent = ttk.Entry(frm, textvariable=key_var, width=42, show="*")
        ent.grid(row=2, column=1, columnspan=2, sticky="we", pady=(8, 4))

        ttk.Label(frm, text="AI 分析模型").grid(row=3, column=0, sticky="w",
                                                pady=4)
        model_var = tk.StringVar(value=AI_MODEL)
        cmb_model = ttk.Combobox(frm, textvariable=model_var, width=20,
                                 values=["deepseek-v4-pro", "deepseek-chat",
                                         "deepseek-reasoner"])
        cmb_model.grid(row=3, column=1, columnspan=2, sticky="w", pady=4)

        ttk.Label(frm, text="网络代理").grid(row=4, column=0, sticky="w",
                                            pady=4)
        proxy_var = tk.StringVar(value=getattr(self, "proxy_url", ""))
        ent_px = ttk.Entry(frm, textvariable=proxy_var, width=42)
        ent_px.grid(row=4, column=1, columnspan=2, sticky="we", pady=4)

        # ---- 预测参数（存 ini [predict]，下次启动生效）----
        ttk.Separator(frm, orient="horizontal").grid(
            row=5, column=0, columnspan=3, sticky="we", pady=10)
        ttk.Label(frm, text="— 预测参数（改动后需重新分析生效）—").grid(
            row=6, column=0, columnspan=3, sticky="w")

        # 风险偏好（三级：止损宽度/移动止盈/买入阈值/冷却）
        ttk.Label(frm, text="风险偏好").grid(row=7, column=0, sticky="w",
                                             pady=2)
        risk_var = tk.StringVar(value=CFG.RISK_MODE)
        cmb_risk = ttk.Combobox(frm, textvariable=risk_var, width=8,
                                state="readonly",
                                values=list(CFG.RISK_PARAMS.keys()))
        cmb_risk.grid(row=7, column=1, sticky="w", pady=2)
        ttk.Label(frm, text="保守=宽止损少交易 激进=紧止损多交易").grid(
            row=7, column=2, sticky="w")

        def _int_var(attr, lo, hi):
            try:
                v = int(max(lo, min(hi, float(getattr(CFG, attr)))))
            except (ValueError, TypeError):
                v = getattr(CFG, attr)
            return tk.StringVar(value=str(v))

        def _float_var(attr):
            return tk.StringVar(value=str(getattr(CFG, attr)))

        prows = [
            ("形态匹配窗口(日)", _int_var("W_WINDOW", 5, 30), "5-30"),
            ("最终样本数", _int_var("TOPK", 3, 30), "3-30"),
            ("候选样本数", _int_var("CANDIDATE_TOPK", 10, 100), "10-100"),
            ("时间衰减起始(天)", _int_var("TIME_DECAY_DAYS", 0, 1095),
             "0-1095"),
            ("动态权重强度(0-1)", _float_var("DYN_LV_STRENGTH"),
             "0=固定 1=全动态"),
            ("周线回看(周)", _int_var("WEEKLY_N", 2, 8), "2-8"),
            ("K线结构权重", _float_var("STRUCT_W"), "0-3"),
            ("波动率权重", _float_var("VOLA_W"), "0-3"),
            ("RSI权重", _float_var("RSI_W"), "0-3"),
            ("量变权重", _float_var("VOLCHG_W"), "0-3"),
            ("周线环境权重", _float_var("WEEKLY_W"), "0-3"),
        ]
        pvars = []
        for i, (lab, var, hint) in enumerate(prows):
            r = 8 + i
            ttk.Label(frm, text=lab).grid(row=r, column=0, sticky="w",
                                          pady=2)
            e = ttk.Entry(frm, textvariable=var, width=10)
            e.grid(row=r, column=1, sticky="w", pady=2)
            ttk.Label(frm, text=hint).grid(row=r, column=2, sticky="w")
            pvars.append((lab, var))

        # ---- 使用注意（基于n=4500+回测的诚实边界）----
        notes = (
            "【适合】\n"
            "· 看 T+1/T+5 价格区间（已校准，覆盖率达名义值）\n"
            "· 强信号过滤：预测收益>1%才值得关注（阈值↑胜率↑）\n"
            "· ETF：参考 T+5 区间（正向T+5胜率55%，z≈3.2）；\n"
            "  预测偏空时均值信息反向，勿直接当卖出信号\n"
            "· 传统行业个股：L1+L2(同行业+ETF) 全开\n"
            "【不适合】\n"
            "· 上行概率>50% 当买卖信号——方向预测无统计alpha\n"
            "· 题材股(科技/医药)跨股参考无效，仅L1\n"
            "· ETF 不按 T+1 信号交易（劣于买入持有）\n"
            "· 本工具不做仓位/风控，据此操作盈亏自负")
        ttk.Label(frm, text=notes, justify="left", foreground=AXIS_TXT,
                  font=("Microsoft YaHei", 8)).grid(
            row=20, column=0, columnspan=3, sticky="w", pady=(12, 0))

        def save():
            self.settings["theme"] = theme_var.get()
            self.settings["updown"] = ud_var.get()
            new_key = key_var.get().strip()
            key_changed = new_key != self.api_key
            self.api_key = new_key
            self.proxy_url = proxy_var.get().strip()
            if CACHE_OK:
                set_proxy(self.proxy_url)
                set_ai_model(model_var.get().strip())
                self._save_ini()
            apply_theme(self.settings["theme"], self.settings["updown"])
            # 应用并持久化预测参数（非法输入自动回退默认）
            pnotes = []
            try:
                cp = configparser.ConfigParser()
                cp.read(INI_PATH, encoding="utf-8")
                if not cp.has_section("predict"):
                    cp.add_section("predict")

                def apply_i(attr, var, lo, hi, dflt):
                    try:
                        v = int(float(var.get()))
                    except (ValueError, TypeError):
                        return
                    v = max(lo, min(hi, v))
                    setattr(CFG, attr, v)
                    cp.set("predict", attr.lower(), str(v))
                    pnotes.append(f"{attr}={v}")

                def apply_f(attr, var, lo, hi):
                    try:
                        v = float(var.get())
                    except (ValueError, TypeError):
                        return
                    v = max(lo, min(hi, v))
                    setattr(CFG, attr, v)
                    cp.set("predict", attr.lower(), str(v))
                    pnotes.append(f"{attr}={v:.2f}")

                apply_i("W_WINDOW", pvars[0][1], 5, 30, 10)
                apply_i("TOPK", pvars[1][1], 3, 30, 10)
                apply_i("CANDIDATE_TOPK", pvars[2][1], 10, 100, 50)
                apply_i("TIME_DECAY_DAYS", pvars[3][1], 0, 1095, 90)
                apply_f("DYN_LV_STRENGTH", pvars[4][1], 0.0, 1.0)
                apply_i("WEEKLY_N", pvars[5][1], 2, 8, 4)
                apply_f("STRUCT_W", pvars[6][1], 0.0, 3.0)
                apply_f("VOLA_W", pvars[7][1], 0.0, 3.0)
                apply_f("RSI_W", pvars[8][1], 0.0, 3.0)
                apply_f("VOLCHG_W", pvars[9][1], 0.0, 3.0)
                apply_f("WEEKLY_W", pvars[10][1], 0.0, 3.0)
                if risk_var.get() in CFG.RISK_PARAMS:
                    CFG.RISK_MODE = risk_var.get()
                    cp.set("predict", "risk_mode", CFG.RISK_MODE)
                    pnotes.append(f"风险={CFG.RISK_MODE}")
                # 同步模块级别名（W_WINDOW/TOPK 被算法直接引用）
                globals()["W_WINDOW"] = CFG.W_WINDOW
                globals()["TOPK"] = CFG.TOPK
                with open(INI_PATH, "w", encoding="utf-8") as f:
                    cp.write(f)
            except Exception:
                log.exception("保存预测参数失败(忽略)")
            self._rebuild_ui()
            win.destroy()
            self.progress_var.set(
                "设置已保存 · AI模型 " + AI_MODEL
                + (f"，代理 {self.proxy_url}" if self.proxy_url
                   else "（未用代理）")
                + (f" · 预测参数x{len(pnotes)}" if pnotes else ""))

        def clear_cache():
            if not messagebox.askyesno(
                    "清除缓存",
                    "确定清空本地缓存数据库？\n\n"
                    "将删除：日K历史 / 全市场代码表 / 样本池 / 失败记录\n"
                    "下次分析时会自动重新回填（约1-2分钟）"):
                return
            try:
                with db_conn(commit=True) as conn:
                    for t in ("daily_bars", "failed", "stocks", "meta"):
                        conn.execute(f"DELETE FROM {t}")
                with _STATE_LOCK:           # 同步失效内存缓存
                    _TIER_POOL_CACHE.clear()
                    _TIER_POOL_TS.clear()
                messagebox.showinfo("清除缓存",
                                    "已清空 stock_cache.db\n"
                                    "下次分析将重新回填样本池")
            except Exception as e:
                messagebox.showerror("清除缓存", str(e))

        btns = ttk.Frame(frm)
        btns.grid(row=19, column=0, columnspan=3, pady=(12, 0))
        ttk.Button(btns, text="保存并应用", command=save).pack(
            side="left", padx=4)
        ttk.Button(btns, text="清除缓存", command=clear_cache).pack(
            side="left", padx=4)
        ttk.Button(btns, text="全市场回填",
                   command=self.run_backfill).pack(side="left", padx=4)
        ttk.Button(btns, text="数据清洗",
                   command=self.run_clean).pack(side="left", padx=4)
        if getattr(self, "compact", False):
            ttk.Button(btns, text="关机", command=self._shutdown_confirm).pack(
                side="left", padx=4)

    def _shutdown_confirm(self):
        """小屏设备专用：确认后关机（需 sudoers 免密授权 shutdown）。"""
        if not messagebox.askyesno(
                "关机", "确定要关机吗？\n\n关机后需重新上电启动。"):
            return
        self.progress_var.set("正在关机...")
        import subprocess
        for cmd in (["sudo", "-n", "shutdown", "-h", "now"],
                    ["sudo", "-n", "poweroff"]):
            try:
                if subprocess.run(cmd, timeout=10).returncode == 0:
                    return
            except Exception:
                continue
        messagebox.showerror("关机失败",
                             "需要免密权限：请在终端执行\n"
                             "sudo sh -c \"echo '%s ALL=(ALL) NOPASSWD: "
                             "/usr/sbin/shutdown, /sbin/shutdown, "
                             "/usr/sbin/poweroff' > /etc/sudoers.d/stock-shutdown\""
                             % os.environ.get("USER", "lan"))

        # ---- 关于 / 免责声明 ----
        sep = ttk.Separator(frm, orient="horizontal")
        sep.grid(row=6, column=0, columnspan=3, sticky="we", pady=(14, 8))
        about = tk.Text(frm, width=40 if self.compact else 52,
                        height=6 if self.compact else 11, relief="flat",
                        bg=PANEL_BG, fg=FG_MAIN, font=("Microsoft YaHei", 9),
                        wrap="word", highlightthickness=0)
        about.grid(row=7, column=0, columnspan=3, sticky="we")
        about.insert("end", "作者：獨白\n")
        about.insert("end", "邮箱：kingrux106@gmail.com\n")
        about.insert("end", "QQ：2180287399\n")
        about.insert("end", "\n【免责声明】\n")
        about.insert(
            "end",
            "本程序所有内容（包括但不限于K线、指标、形态相似度统计预测、"
            "AI分析）仅为历史数据的技术统计与个人学习研究用途，"
            "不构成任何投资建议或收益承诺。股票有风险，"
            "据此操作产生的盈亏与后果由使用者自行承担。"
            "请遵守所在地区法律法规，理性投资。")
        about.config(state="disabled")

    def _rebuild_ui(self):
        """销毁重建全部控件（主题切换后刷新配色）。"""
        for w in self.root.winfo_children():
            w.destroy()
        self.scales = {}
        self.view = None
        self._style_ttk()
        self._build_toolbar()
        self._build_body()
        self._render_watchlist()
        if self.res:
            self._rerender()
        self._update_indices()
        self._update_sectors()


def _cleanup():
    """程序退出时清理线程池，避免僵尸线程。"""
    try:
        _SHARED_EX.shutdown(wait=False)
    except Exception:
        pass
    try:
        _BG_EX.shutdown(wait=False)
    except Exception:
        pass


atexit.register(_cleanup)


def main():
    try:
        import sys
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
