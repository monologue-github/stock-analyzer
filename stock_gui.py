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
import configparser
import heapq
import json
import logging
import math
import os
import random
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

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

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
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
    W_WINDOW = 10                       # 形态匹配窗口长度
    TOPK = 10                           # Top-K 相似样本数
    LV_W = {"L1": 0.6, "L2": 0.3, "L3": 0.1}    # 三级样本池权重
    SIGNAL_SCORE_BUY = 2                # 多头信号触发分
    SIGNAL_SCORE_SELL = -2              # 空头信号触发分
    SIGNAL_COOLDOWN = 5                 # 相邻信号最小间隔(交易日)
    WEAK_IDX_TH = -1.5                  # 大盘弱势阈值(%)
    WEAK_SEC_TH = -2.0                  # 板块弱势阈值(%)
    BAND_FIT_MIN = 60.0                 # 波段适合度门槛
    PRED_MAX_DAYS = 10                  # 多日预测天数


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
    """K线数据合法性校验：剔除脏数据（负价/高低颠倒/影线超实体1.5倍）。"""
    o, h, l, c = r["open"], r["high"], r["low"], r["close"]
    if None in (o, h, l, c):
        return False
    if min(o, h, l, c) <= 0:
        return False
    if h < l:
        return False
    body = abs(c - o)
    if body < 1e-9:
        return True          # 十字星允许
    if (h - max(o, c)) > body * 1.5 or (min(o, c) - l) > body * 1.5:
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


def _fetch_tencent(full, count=600):
    """腾讯K线（原接口，IP可能被限流）"""
    txt = _http_get(KLINE_URL + f"?param={full},day,,,{count},qfq",
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
    """多源自动切换 + 熔断调度：腾讯 → 东财 → 网易163 → 新浪。

    运行逻辑：
    1. 按优先级遍历数据源，跳过处于熔断冷却期的源；
    2. 若所有源都在冷却（极端503风暴），退化为「半开探测」：
       选冷却结束最早的源强行试一次，成功即重置熔断；
    3. 单次调用内只对一个源做至多2次限流重试，
       失败立刻切下一源，避免整体请求被单源拖死。"""
    sources = [
        ("腾讯", lambda: _fetch_tencent(full, count)),
        ("东财", lambda: _fetch_eastmoney(full, count)),
        ("网易163", lambda: _fetch_163(full, count)),
        ("新浪", lambda: _fetch_sina(full, count)),
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
            if rows and len(rows) >= 20:
                return rows
            last_err = RuntimeError(f"{name}返回空K线")
        except Exception as e:
            _cb_record(name, False, e)
            last_err = e
            continue
    raise RuntimeError(f"所有数据源均失败: {last_err}")


FAIL_TTL = 3600                 # 拉取失败记忆期1小时
_MIN_INTERVAL = 0.16            # 全局HTTP最小间隔，防腾讯限速
_THROTTLE_LOCK = threading.Lock()
_LAST_REQ = [0.0]


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
            remote = [r for r in _fetch_remote_rows(full)
                      if r["date"] < today and _bar_ok(r)]
            with db_conn(commit=True) as conn2:
                if bad_cache:
                    conn2.execute("DELETE FROM daily_bars WHERE code=?",
                                  (full,))
                if remote:
                    conn2.executemany(
                        "INSERT OR REPLACE INTO daily_bars"
                        "(code,date,open,high,low,close,vol) "
                        "VALUES(?,?,?,?,?,?,?)",
                        [(full, r["date"], r["open"], r["high"], r["low"],
                          r["close"], r["vol"]) for r in remote])
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
        remote = _fetch_remote_rows(full)
    except Exception as e:
        log.warning("get_daily 首次拉取失败 %s: %s", full, e)
        with db_conn(commit=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO failed(code,ts,reason) VALUES(?,?,?)",
                (full, time.time(), str(e)[:120]))
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

    def one(c):
        try:
            get_daily(c)
        except Exception:
            log.debug("prefetch 跳过 %s", c, exc_info=True)
        done[0] += 1
        if progress and done[0] % 10 == 0:
            progress(f"缓存回填 {done[0]}/{total}")

    ex = _SHARED_EX                # 全局共享线程池，不再每次新建
    list(ex.map(one, codes))


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


def tier_sample(full: str, n: int = L3_DEFAULT_N, exclude_industries=()):
    """L3池：同市值层抽样，同一市值层当天共享同一份样本（避免每只股票
    各自随机抽样导致大量重复拉取/缓存膨胀）。
    用 (tier, 日期) 做共享缓存键，当天首次计算后缓存复用。
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
            rnd = random.Random(today + tier)
            rnd.shuffle(rows)
            pool = rows[:n]
            _TIER_POOL_CACHE[key] = pool
            _TIER_POOL_TS[key] = now
    # 排除自身与 L2 已覆盖的行业（industry 名字，非代码）
    exclude = set(exclude_industries or ())
    if exclude:
        pool = [r for r in pool if (r[1] or "") not in exclude]
    out = [c for c, _ in pool if c != full]
    return out[:n], tier


def pool_codes(full, l2_n=L2_DEFAULT_N, l3_n=L3_DEFAULT_N):
    """一次拿到两级样本池。"""
    peers, industry = industry_peers(full, l2_n)
    l3, tier = tier_sample(full, l3_n, exclude_industries=(industry,))
    seen = {full}
    l3 = [c for c in l3 if c not in seen and not seen.add(c)]
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
    # 触摸屏/输入法常见：全角字符折叠为半角，删除所有空白
    code = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
        for ch in code)
    code = "".join(code.split())
    # 兼容 002241.sz / 600519.SH 等后缀形式
    if "." in code:
        parts = code.split(".")
        if len(parts) == 2 and len(parts[0]) == 6 and len(parts[1]) == 2:
            code = parts[1] + parts[0]
    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            return p + code[2:]
    d = "".join(ch for ch in code if ch.isdigit())
    if len(d) != 6:
        # !r 显示原始输入（含隐藏字符），便于远程排查
        raise ValueError(f"代码格式不对: {code!r}")
    if d[0] in "69" or d[:2] in ("51", "56", "58"):      # 沪股/沪ETF
        return "sh" + d
    if d[0] in "03" or d[:2] in ("15", "16", "18"):      # 深股/深ETF/LOF
        return "sz" + d
    if d[0] in "48":
        return "bj" + d
    raise ValueError(f"不支持的代码: {code!r}")


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


def fetch_top_sectors():
    """获取今日行业板块涨跌幅排行（Top3涨/Top3跌）。
    返回 [(name, pct), ...] 的两个列表，带10分钟缓存。
    缓存读写持有 _STATE_LOCK；请求统一走熔断 _http_get。"""
    global _TOP_SECTORS_CACHE, _TOP_SECTORS_TS
    now = time.time()
    with _STATE_LOCK:
        if _TOP_SECTORS_CACHE and now - _TOP_SECTORS_TS < 600:
            return _TOP_SECTORS_CACHE
    UT = "fa5fd1943c7b386f172d6893dbfba10b"
    hdr = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    hosts = ("https://push2.eastmoney.com", "https://push2delay.eastmoney.com")
    try:
        items = []
        vals = []
        for pn in (1, 2, 3):
            u = (f"/api/qt/clist/get"
                 f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invariant=0"
                 f"&fields=f12,f14,f3&fs=m:90+t:2&ut={UT}")
            done = False
            for host in hosts:
                try:
                    data = json.loads(_http_get(
                        host + u, retries=2, timeout=6, headers=dict(hdr),
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
                    log.warning("板块排行第%d页 %s 失败: %s", pn, host, e)
                    continue
            if not done:
                break
            if len(vals) < 100:
                break
        items.sort(key=lambda x: x[1], reverse=True)
        top3 = items[:3]
        bot3 = items[-3:][::-1]
        result = (top3, bot3)
        with _STATE_LOCK:
            _TOP_SECTORS_CACHE = result
            _TOP_SECTORS_TS = now
        return result
    except Exception:
        log.exception("fetch_top_sectors 失败")
        return ([], [])


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


def backtest_signals(rows, signals):
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
                atr_stop = entry - 2.0 * atrs[i] if atrs[i] > 0 else entry * 0.95
                trail_stop = highest * 0.92 if highest > entry * 1.03 else atr_stop
                
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


def _pool_match(pool_rows, cur, vr_now, idx_chg_by_date, idx_chg_today,
                topk=TOPK):
    """在多只股票的日K池里跑与 L1 相同的窗口匹配，返回样本列表。

    pool_rows: [(code, rows)]；评分 = 形态距离 + 量能项 + 大盘环境项。
    """
    W = W_WINDOW
    info = {}
    sims = []
    for code, rows in pool_rows:
        closes = [r["close"] for r in rows]
        rets = logret(closes, is_etf=_is_etf(code))
        if len(rets) < W + 3:
            continue
        vols = [r.get("vol") or 0.0 for r in rows]
        vr_arr = [vol_ratio_at(vols, k) for k in range(len(vols))]
        info[code] = (rows, vr_arr)
        for i in range(W, len(rets) - 1):
            w = znorm(rets[i - W:i])
            d_px = sum((a - b) ** 2 for a, b in zip(cur, w)) ** 0.5
            vr_i = vr_arr[i]
            if vr_now is not None and vr_i is not None:
                d_v = abs(math.log(max(vr_now, 1e-6) / max(vr_i, 1e-6)))
                s_v = 0.6 * min(d_v, 2.5)
            else:
                s_v = 0.30
            ic = idx_chg_by_date.get(rows[i]["date"])
            if ic is not None and idx_chg_today is not None:
                s_i = min(1.5, 0.3 * abs(ic - idx_chg_today))
            else:
                s_i = 0.40
            sims.append((d_px + s_v + s_i, i, code))
    if not info:
        return []
    top = heapq.nsmallest(topk, sims, key=lambda x: x[0])
    out = []
    max_pred_days = 10
    for score, i, code in top:
        rows, vr_arr = info[code]
        r = rows[i]
        ic = idx_chg_by_date.get(r["date"])
        sample = {
            "t_date": r["date"],
            "vr": vr_arr[i],
            "idx_chg": (ic - idx_chg_today
                        if (ic is not None and idx_chg_today is not None)
                        else None),
            "gap": rows[i + 1]["open"] / r["close"] - 1 if i + 1 < len(rows) else None,
            "code": code,
        }
        # 扩展样本：记录T+1到T+max_pred_days的相对前日收盘涨跌幅
        for d in range(1, max_pred_days + 1):
            if i + d < len(rows):
                nd = rows[i + d]
                prev_c = rows[i + d - 1]["close"] if i + d - 1 >= 0 else r["close"]
                sample[f"n{d}_date"] = nd["date"]
                sample[f"n{d}_cl"] = nd["close"] / prev_c - 1
                sample[f"n{d}_hi"] = nd["high"] / prev_c - 1
                sample[f"n{d}_lo"] = nd["low"] / prev_c - 1
            else:
                sample[f"n{d}_date"] = None
                sample[f"n{d}_cl"] = None
                sample[f"n{d}_hi"] = None
                sample[f"n{d}_lo"] = None
        out.append(sample)
    return out


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
                progress=None):
    """取 L2/L3 池K线（走缓存增量）并跑匹配，返回 {"L2":[...], "L3":[...]}。"""
    out = {}
    t0 = time.time()
    for key in ("L2", "L3"):
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
                              idx_chg_today)
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
    order = [("L2", pool_info.get("l2") or []),
             ("L3", pool_info.get("l3") or [])]
    for key, codes in order:
        if not codes:
            continue
        t0 = time.time()
        acc_rows = []       # 本级的累计池K线，逐批变大，匹配随之变准
        for i in range(0, len(codes), batch):
            chunk = codes[i:i + batch]
            try:
                prefetch(chunk, workers=8, progress=progress)
            except Exception:
                log.warning("样本池回填失败 %s(跳过)", chunk, exc_info=True)
            # 批量读缓存
            try:
                with db_conn() as conn:
                    cached = _db_rows_batch(conn, chunk)
            except Exception:
                cached = {}
            acc_rows += [(c, r) for c, r in cached.items() if len(r) >= 130]
            # 用已累计的全部本池K线跑匹配，样本随加载增多而变准
            try:
                smp = _pool_match(acc_rows, ctx["cur"], ctx["vr_now"],
                                  ctx["idx_chg_by_date"], ctx["idx_chg_today"])
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
            if time.time() - t0 > 40:   # 单级超时保护
                break


# ================= 分析 =================

def _fusion_prediction(o_today, pre_open, live, levels):
    """三级加权融合预测：由 levels=[("L1",样本),...] 计算预测区间与预测K线。
    供 analyze 与后台增量加载器复用（增量加载时只重算这段，K线实时更新）。"""
    tot_w = sum(LV_W[k] for k, _ in levels) or 1.0

    def _wfield(field):
        pairs = []
        for k, smp in levels:
            w = LV_W[k] / tot_w / len(smp)
            pairs.extend((s[field], w) for s in smp if s.get(field) is not None)
        return pairs

    PS = (10, 25, 50, 75, 90)
    t_pred = {
        "cl": {p: o_today * (1 + wpct(_wfield("n1_cl"), p)) for p in PS},
        "hi": {p: o_today * (1 + wpct(_wfield("n1_hi"), p)) for p in PS},
        "lo": {p: o_today * (1 + wpct(_wfield("n1_lo"), p)) for p in PS},
        "up_prob": sum(LV_W[k] / tot_w
                       * len([s for s in smp if s.get("n1_cl") is not None and s["n1_cl"] > 0]) / len(smp)
                       for k, smp in levels),
    }
    base = t_pred["cl"][50]
    # 盘中实时修正：预测区间必须包含已实现的最高/最低
    clamped = False
    if live is not None:
        clamped = True
        for pp in PS:
            t_pred["hi"][pp] = round(max(t_pred["hi"][pp], live["high"]), 2)
            t_pred["lo"][pp] = round(min(t_pred["lo"][pp], live["low"]), 2)
    pred = {"date": "T日预测" if pre_open else "T+1预测", "open": base,
            "close": base * (1 + wpct(_wfield("n1_cl"), 50)),
            "high": base * (1 + wpct(_wfield("n1_hi"), 75)),
            "low": base * (1 + wpct(_wfield("n1_lo"), 25)),
            "vol": None}
    tpred_bar = None
    if live is not None:
        tpred_bar = {"date": "T日预测", "open": o_today,
                     "close": t_pred["cl"][50],
                     "high": t_pred["hi"][50], "low": t_pred["lo"][50],
                     "vol": None}
    return t_pred, pred, clamped, tpred_bar


def _multi_day_prediction(o_today, levels, max_days=10):
    """多日预测：基于样本的统计分布，预测T+1到T+max_days的走势。
    带均值回归修正：长期预测向零回归，减少累积误差。"""
    tot_w = sum(LV_W[k] for k, _ in levels) or 1.0
    
    def _wfield(field):
        pairs = []
        for k, smp in levels:
            w = LV_W[k] / tot_w / len(smp)
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
        
        day_pred = {
            "day": d,
            "label": f"T+{d}",
            "cl": {p: wpct(cl_pairs, p) for p in PS},
            "hi": {p: wpct(hi_pairs, p) if hi_pairs else wpct(cl_pairs, p) for p in PS},
            "lo": {p: wpct(lo_pairs, p) if lo_pairs else wpct(cl_pairs, p) for p in PS},
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
    if len(rows) < 100:
        raise ValueError("上市时间太短，样本不足")

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
    sims = []
    for i in range(W, len(rets) - 1):
        w = znorm(rets[i - W:i])
        d_px = sum((a - b) ** 2 for a, b in zip(cur, w)) ** 0.5
        d_v = _dist_vol(i)
        d_i = _dist_idx(i)
        d_s = _dist_sec(i)
        score = (d_px
                 + (0.6 * min(d_v, 2.5) if d_v is not None else 0.30)
                 + (min(1.5, 0.3 * d_i) if d_i is not None else 0.40)
                 + (min(1.2, 0.25 * d_s) if d_s is not None else 0.30))
        sims.append((score, i))
    top = heapq.nsmallest(TOPK, sims, key=lambda x: x[0])

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
    else:
        o_today = q["open"] or prev_close
        anchor = "今开"
    gap_today = (o_today / prev_close - 1) * 100

    # 市场阶段（按行情快照时间）
    phase = market_phase_text(q.get("time"))
    next_label = "今日(T)" if pre_open else "次日(T+1)"

    samples = []
    max_pred_days = 10  # 最多预测10天
    for _, i in top:
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
        }
        # 扩展样本：记录T+1到T+max_pred_days的相对前日收盘涨跌幅
        for d in range(1, max_pred_days + 1):
            if i + d < len(rows):
                nd = rows[i + d]
                prev_c = rows[i + d - 1]["close"] if i + d - 1 >= 0 else r["close"]
                sample[f"n{d}_date"] = nd["date"]
                sample[f"n{d}_cl"] = nd["close"] / prev_c - 1
                sample[f"n{d}_hi"] = nd["high"] / prev_c - 1
                sample[f"n{d}_lo"] = nd["low"] / prev_c - 1
            else:
                sample[f"n{d}_date"] = None
                sample[f"n{d}_cl"] = None
                sample[f"n{d}_hi"] = None
                sample[f"n{d}_lo"] = None
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

    # ---- 二三级样本池：L2 同行业 / L3 同市值层 ----
    level_map = {}
    pool_note = ""
    if CACHE_OK and pool_info:
        try:
            if progress:
                progress("拉取同行/同市值层K线(首次回填较慢)...")
            level_map = _load_pools(pool_info, cur, vr_now,
                                    idx_chg_by_date, idx_chg_today,
                                    progress)
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
    mas = {n: sma_period(closes_i, n) for n in MA_COLORS}

    signals = []
    # ---- 多维打分：每日综合评分，方向切换时生成买卖信号 ----
    # 评分维度：MACD趋势、KDJ状态、RSI超买超卖、量价配合、MA20趋势、
    #          筹码位置、统计偏多/偏空；合计≥2→多头信号，≤-2→空头信号
    _bull_scores = []   # (index, date, score, reasons)
    start = max(1, len(disp_rows) - 120)
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
        # MACD
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            sc += 2
            reasons.append("MACD金叉")
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            sc -= 2
            reasons.append("MACD死叉")
        elif dif[i] > dea[i]:
            sc += 1
            reasons.append("DIF>DEA")
        else:
            sc -= 1
            reasons.append("DIF<DEA")
        # KDJ
        if k_[i - 1] <= d_[i - 1] and k_[i] > d_[i] and k_[i] < 45:
            sc += 2
            reasons.append("KDJ低位金叉")
        elif k_[i - 1] >= d_[i - 1] and k_[i] < d_[i] and k_[i] > 65:
            sc -= 2
            reasons.append("KDJ高位死叉")
        elif k_[i] > d_[i]:
            sc += 1
        else:
            sc -= 1
        # RSI
        if r6[i] is not None and r6[i - 1] is not None:
            if r6[i - 1] < 20 and r6[i] >= 20:
                sc += 2
                reasons.append("RSI超卖回升")
            elif r6[i - 1] > 80 and r6[i] <= 80:
                sc -= 2
                reasons.append("RSI超买回落")
            elif r6[i] < 30:
                sc += 1
            elif r6[i] > 70:
                sc -= 1
        # 量价
        c, cp = disp_rows[i]["close"], disp_rows[i - 1]["close"]
        v5 = sum(vols_d[max(0, i - 5):i]) / max(1, min(5, i))
        vr_d = vols_d[i] / v5 if v5 > 0 else 0.0
        if vr_d > 1.5 and c > cp:
            sc += 1
            reasons.append("放量上涨")
        elif vr_d > 1.5 and c < cp:
            sc -= 1
            reasons.append("放量下跌")
        # MA20趋势
        ma20, ma20p = mas[20][i], mas[20][i - 1]
        if ma20 and ma20p:
            if c > ma20 and ma20 > ma20p:
                sc += 1
            elif c < ma20 and ma20 < ma20p:
                sc -= 1
        # 筹码
        snap = chip_snaps.get(disp_rows[i]["date"])
        if snap:
            sup_i, res_i = snap[0], snap[1]
            if sup_i and c <= sup_i * 1.01:
                sc += 1
                reasons.append("贴近支撑")
            elif res_i and c >= res_i * 0.99:
                sc -= 1
                reasons.append("贴近压力")
        # 布林带：均值回归参考（下轨超卖偏多 / 上轨超买偏空）
        bu_i, bl_i = b_up[i], b_low[i]
        if None not in (bu_i, bl_i):
            if c < bl_i:
                sc += 1
                reasons.append("布林下轨超卖")
            elif c > bu_i:
                sc -= 1
                reasons.append("布林上轨超买")
            elif (disp_rows[i - 1]["close"] <= (b_low[i - 1] or 0)
                    and c > bl_i):
                sc += 1
                reasons.append("布林下轨回升")
            elif (disp_rows[i - 1]["close"] >= (b_up[i - 1] or 1e18)
                    and c < bu_i):
                sc -= 1
                reasons.append("布林上轨回落")
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

        # 弱势行情过滤：当日大盘/板块弱势时，BUY需要更高分，SELL保持阈值
        buy_threshold = (CFG.SIGNAL_SCORE_BUY + 1) if _weak_day(day) \
            else CFG.SIGNAL_SCORE_BUY
        sell_threshold = CFG.SIGNAL_SCORE_SELL

        if sc >= buy_threshold and prev_dir <= 0:
            bull_r = [r for r in reasons if r not in _bear_words]
            reason_str = "多维偏多 " + " ".join(bull_r or reasons)
            if _weak_day(day):
                reason_str += " [弱势谨慎]"
            signals.append((idx_i, day, "BUY", reason_str))
            prev_dir = 1
            cooldown = CFG.SIGNAL_COOLDOWN
        elif sc <= sell_threshold and prev_dir >= 0:
            bear_r = [r for r in reasons if r not in _bull_words]
            signals.append((idx_i, day, "SELL",
                            "多维偏空 " + " ".join(bear_r or reasons)))
            prev_dir = -1
            cooldown = CFG.SIGNAL_COOLDOWN

    # ---- 波段适合度路由：适合波段用现有多维/形态算法；不适合用长周期趋势跟踪 ----
    band_score = _band_fit_score(disp_rows, mas, vr_arr)
    band_fit = band_score >= CFG.BAND_FIT_MIN
    if band_fit:
        band_algo = "波段·多维融合"
    else:
        # 不适合波段：改用长周期 MA20/MA60 趋势跟踪，信号少而稳
        signals = _trend_track_signals(disp_rows, mas,
                                       idx_chg_by_date, idx_chg_today)
        band_algo = "趋势跟踪·MA20/60"
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
        score = sum(s for _, s, _ in items)
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

    # ---- 回测统计：基于全部历史信号计算胜率/盈亏/年化 ----
    bt_stats = None
    if signals and len(signals) >= 2:
        try:
            bt_stats = backtest_signals(disp_rows, signals)
        except Exception:
            log.exception("回测统计失败(bt_stats=None)")
    
    return {
        "quote": q, "full_code": full, "disp_rows": disp_rows,
        "anchor": anchor, "pre_open": pre_open,
        "phase": phase, "next_label": next_label,
        "tpred_bar": tpred_bar,
        "pred": pred, "t_pred": t_pred, "multi_pred": multi_pred,
        "samples": samples, "src_n": len(src),
        "filtered": src is not samples,
        "filter_note": filter_note,
        "levels": [{"key": k, "label": LV_LABEL[k], "n": len(smp),
                    "up_prob": len([s for s in smp if s.get("n1_cl") is not None and s["n1_cl"] > 0])
                    / len(smp)} for k, smp in levels],
        "pool_note": pool_note,
        "idx_chg_today": idx_chg_today, "vr_now": vr_now,
        "cur_regime": cur_regime,
        "sector_name": sec_name, "sector_chg_today": sec_chg_today,
        "ind": {"ma": mas, "dif": dif, "dea": dea, "mhist": mhist,
                "k": k_, "d": d_, "j": j_, "rsi6": r6, "rsi12": r12,
                "boll_mid": b_mid, "boll_up": b_up, "boll_low": b_low},
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
            "vr_now": vr_now, "idx_chg_by_date": idx_chg_by_date,
            "idx_chg_today": idx_chg_today, "gap_today": gap_today,
            "prev_close": prev_close, "pool_info": pool_info,
        },
    }


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
    view = {
        "bars": vis + [res["pred"]],
        "dates": ([r["date"] for r in vis]
                  + ["T+1" if pd.startswith("T+") else "T日"]),
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
        "boll_low": res["ind"]["boll_low"][off:end],
        "vols": res["vols"][off:end] + [None],
        "signals": [(i - off, dt, t, txt) for i, dt, t, txt in res["signals"]
                    if off <= i < end],
    }
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
                              state="readonly", values=["MACD", "KDJ", "RSI", "BOLL"])
            ci.pack(side="left", padx=2)
            ci.bind("<<ComboboxSelected>>", lambda e: self._rerender())
            # 小屏：报告/样本/缓存/指数 全部收进【工具】菜单
            ttk.Button(top2, text="工具", width=4,
                       command=lambda: self._tools_menu(top2)
                       ).pack(side="right", padx=2)
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
                              values=["MACD", "KDJ", "RSI", "BOLL"])
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

        def _fit_info(_e=None):
            try:
                avail = info_row.winfo_width() - 120
                text = self.info_var.get()
                if avail > 10:
                    est = max(6, int(avail / 9))
                    if len(text) > est:
                        info_lbl.config(text=text[:est] + "…")
                    else:
                        info_lbl.config(text=text)
            except Exception:
                pass
        info_row.bind("<Configure>", _fit_info)
        # 变量变化时也重新适配
        self.info_var.trace_add("write", lambda *a: self._fit_info())
        self._fit_info = _fit_info
        self._fit_info()

    def _build_body(self):
        body = tk.Frame(self.root)
        body.pack(fill="both", expand=True)

        # ---- 右侧：预测参考（先pack，防止遮挡图表；小屏省略）----
        if not self.compact:
            right = ttk.LabelFrame(body, text=" 预测参考 ", padding=4)
            right.pack(side="right", fill="y", padx=(2, 8), pady=6)

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

        bottom = tk.Frame(self.root, bg=DARK_BG)
        bottom.pack(fill="both", padx=8, pady=(2, 6))
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
    # ---------- 折叠 ----------

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

    def _done_load(self, res, err):
        if err:
            self._fail(str(err))
            return
        self._loaded(res)
        if res.get("quick"):
            # 启动后后台逐步加载样本池，边加载边更新预测K线（不阻塞界面）
            self._start_progressive(res)

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
        res["levels"] = [
            {"key": k, "label": LV_LABEL[k], "n": len(smp),
             "up_prob": len([s for s in smp if s.get("n1_cl") is not None and s["n1_cl"] > 0]) / len(smp)}
            for k, smp in sorted(level_map.items()) if smp]
        self._rerender()
        self.progress_var.set("样本池加载中，预测已更新: " + pool_note)

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
            live["low"] = min(live["low"], q["price"]) if q["low"] > 0 else live["low"]
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

    def _drag_move(self, event):
        if not self.res or not hasattr(self, "_drag_x"):
            return
        dx = event.x - self._drag_x
        g = self.scales.get("main")
        if not g or g.get("bw", 0) <= 0:
            return
        bars_moved = int(dx / g["bw"])
        new_pan = max(0, min(self._drag_pan + bars_moved,
                             len(self.res["disp_rows"]) - 20))
        if new_pan != self.view_pan:
            self.view_pan = new_pan
            # 拖拽节流：拖动中只保证 ~30ms 一次重绘，小屏触摸更跟手不卡顿
            if not getattr(self, "_drag_job", None):
                self._drag_job = self.root.after(30, self._rerender)

    def _drag_end(self, event):
        self._drag_x = None
        # 拖动结束时立即补一次最终位置重绘
        if getattr(self, "_drag_job", None):
            self.root.after_cancel(self._drag_job)
            self._drag_job = None
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
        for i, b in enumerate(bars):
            x = xs(i)
            isp = b["date"] in ("T+1预测", "T日预测")
            up = b["close"] >= b["open"]
            color = PRED_C if isp else (UP if up else DOWN)
            dash = (3, 2) if isp else ()
            yo, yc = ymap(b["open"]), ymap(b["close"])
            cv.create_line(x, ymap(b["high"]), x, ymap(b["low"]),
                           fill=color, dash=dash)
            bw2 = max(g["bw"] * 0.62, 2)
            ty, by2 = min(yo, yc), max(yo, yc)
            if by2 - ty < 1:
                by2 = ty + 1
            cv.create_rectangle(x - bw2 / 2, ty, x + bw2 / 2, by2,
                                fill="" if isp else color,
                                outline=color, dash=dash)

        # 当日预测：白色虚线幽灵K线叠加在实时bar同一位置
        tpred = v.get("tpred")
        if tpred:
            li = len(v["bars"]) - 2      # 实时bar在视图中的下标
            x = xs(li)
            cv.create_line(x, ymap(tpred["high"]), x, ymap(tpred["low"]),
                           fill=TPRED_C, dash=(3, 2))
            y1 = ymap(max(tpred["open"], tpred["close"]))
            y2 = ymap(min(tpred["open"], tpred["close"]))
            bw3 = max(g["bw"] * 0.62, 2) + 3   # 略宽于实体，形成外框
            cv.create_rectangle(x - bw3 / 2, y1, x + bw3 / 2,
                                max(y2, y1 + 1),
                                fill="", outline=TPRED_C, dash=(3, 2))

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

        pb = bars[-1]
        if len(bars) >= 2 and bars[-2]["date"] == "T日预测":
            tb = bars[-2]
            cv.create_text(xs(len(bars) - 1) - 44, g["T"] - 3,
                           text=f"T日C:{tb['close']:.2f}", fill=TPRED_C,
                           anchor="e",
                           font=("Microsoft YaHei", 8, "bold"))
        cv.create_text(xs(len(bars) - 1), g["T"] - 3,
                       text=f"{v.get('pred_label', 'T+1')} C:{pb['close']:.2f}",
                       fill=PRED_C,
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
            if (None in (up[i], low[i]) or up[i] <= low[i]):
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
        if b["date"] in ("T+1预测", "T日预测"):
            tag = "预测T+1" if b["date"] == "T+1预测" else "预测T日"
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
        lv = res.get("levels") or []
        if len(lv) > 1:
            L.append("分层上行概率: " + " | ".join(
                f"{x['label']} {x['up_prob']*100:.0f}%(n={x['n']})"
                for x in lv))
            w = {"L1": 0.6, "L2": 0.3, "L3": 0.1}
            L.append(f"融合权重: " +
                     " ".join(f"{x['label']}{w[x['key']]}" for x in lv))
        if res.get("pool_note"):
            L.append(res["pool_note"])
        if res["has_live"] and res["clamped"]:
            L.append(f"[盘中实时修正] 已实现最高 {res['live_high']:.2f} / "
                     f"最低 {res['live_low']:.2f}，已并入预测区间")
        L.append(f"-- {res.get('next_label', '次日(T+1)')}预测 --")
        L.append(f"开 {pred['open']:.2f} | 收 {pred['close']:.2f} | "
                 f"高 {pred['high']:.2f} | 低 {pred['low']:.2f}")
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

        p(f"■ 今日(T)收盘预测  [锚定{res.get('anchor', '今开')}]")
        for pp in (10, 50, 90):
            p(f"  P{pp}: 收{tp['cl'][pp]:.2f} 高{tp['hi'][pp]:.2f} 低{tp['lo'][pp]:.2f}")
        p(f"  上行概率 {tp['up_prob']*100:.0f}%  "
          f"样本{res['src_n']}/{len(res['samples'])}")
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
            pos = ("上轨上方·超买" if q["price"] > bl_up
                   else "下轨下方·超卖" if q["price"] < bl_low
                   else "带内")
            p(f"  现价 {q['price']:.2f} 位于{pos}")
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
        p(f"(近{W_WINDOW}日形态匹配 Top{TOPK})")
        p(f"{'T日':<11}{'T+1日':<11}{'次日涨跌':>8}")
        for s in res["samples"]:
            n1_cl = s.get("n1_cl")
            chg1 = n1_cl * 100 if n1_cl is not None else 0
            n1_date = s.get("n1_date", "N/A")
            mark = "*" if abs((s.get("gap") or 0) * 100 - res["gap_today"]) <= 1.0 else ""
            p(f"{s['t_date']:<11}{n1_date:<11}{chg1:>+7.1f}% {mark}")
        p()
        p("* = 开盘缺口与今日接近")
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
            f" 现价位于{'上轨上方' if c > (last(ind['boll_up']) or 1e18) else ('下轨下方' if c < (last(ind['boll_low']) or -1) else '带内')}\n\n"
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
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)

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
            self._rebuild_ui()
            win.destroy()
            self.progress_var.set(
                "设置已保存 · AI模型 " + AI_MODEL
                + (f"，代理 {self.proxy_url}" if self.proxy_url
                   else "（未用代理）"))

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
        btns.grid(row=5, column=0, columnspan=3, pady=(12, 0))
        ttk.Button(btns, text="保存并应用", command=save).pack(
            side="left", padx=4)
        ttk.Button(btns, text="清除缓存", command=clear_cache).pack(
            side="left", padx=4)
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
