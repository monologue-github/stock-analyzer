#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票形态相似度预测 · 命令行版（独立单文件，不依赖 stock_gui.py）

与 stock_gui.py 共用同一套分析算法（由 build_cli.py 自动生成）：
价格形态 + 量能状态 + 大盘 + 板块 + 同行业 + 同市值层 多级加权匹配。
内建 SQLite 缓存（stock_cache.db），同行业/同市值层样本池只回填一次。
K线源自动切换：腾讯 -> 东财 -> 网易163 -> 新浪；支持代理（stock_gui.ini
的 [proxy] url，如 http://127.0.0.1:7890）。

用法：python stock_predict.py [--push] [--refresh-cache] [股票代码]
  --push           分析完成后把报告推送到 Pi 量化系统收件箱（ai-quant）
  --refresh-cache  刷新全市场代码表/市值分层（约1分钟，7天有效）
"""


import heapq
import json
import logging
import math
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CACHE_OK = True      # 缓存层已内嵌，恒可用


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
                opener = _PROXY_OPENER
                if opener is not None:
                    with opener.open(req, timeout=timeout) as r:
                        txt = r.read().decode(decode, errors="ignore")
                else:
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
        # 板块接口统一走熔断 _http_get（东财板块源上报）
        return _http_get(u, retries=1, timeout=timeout, headers=dict(hdr),
                         src_name="东财板块")

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
        # 3) 板块日K（收盘价）
        u = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
             f"?secid=90.{bk_code}&fields1=f1,f2,f3&fields2=f51,f53"
             f"&klt=101&fqt=0&beg=20240101&end=20500101")
        kl = json.loads(get(u, timeout=8))["data"]["klines"]
        bars = [(s.split(",")[0], float(s.split(",")[1])) for s in kl]
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
        # 统计样本维度不放历史打分：今日匹配样本不能用于标注过去（防前视）
        # 最新一天的样本倾向已由综合评估中的"统计预测"维度体现
        _bull_scores.append((i, disp_rows[i]["date"], sc, reasons))

    # 方向切换触发：多头得分≥2且前一次信号为空头→BUY；空头得分≤-2且前一次为多头→SELL
    prev_dir = 0   # 0=无信号, 1=多头, -1=空头
    cooldown = 0
    _bear_words = {"DIF<DEA", "放量下跌", "贴近压力"}
    _bull_words = {"DIF>DEA", "放量上涨", "贴近支撑"}

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
                "k": k_, "d": d_, "j": j_, "rsi6": r6, "rsi12": r12},
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

# ==================== 以下为 CLI 专属 ====================

def build_payload(full, res):
    q, tp, pred = res["quote"], res["t_pred"], res["pred"]
    return {
        "source": "stock_predict_cli",
        "code": full,
        "name": q["name"],
        "snapshot_time": q["time"],
        "price": q["price"],
        "prev_close": res["prev_close"],
        "gap_today_pct": round(res["gap_today"], 2),
        "market_pct": round(res["idx_chg_today"], 2)
        if res["idx_chg_today"] is not None else None,
        "volume_regime": res["cur_regime"],
        "sector": res["sector_name"],
        "sector_pct": round(res["sector_chg_today"], 2)
        if res["sector_chg_today"] is not None else None,
        "t_pred": {
            "close_p50": tp["cl"][50], "close_p10": tp["cl"][10],
            "close_p90": tp["cl"][90],
            "up_prob": round(tp["up_prob"] * 100, 1),
        },
        "next_day_pred": pred,
        "signals": [
            {"date": d, "type": t, "reason": x}
            for _, d, t, x in res["signals"][-6:]
        ],
        "filter_note": res["filter_note"],
        "disclaimer": DISCLAIMER,
    }


def push_report(full, res):
    """把分析报告 JSON 推到 Pi 的 ai-quant 收件箱（SSH 免密需已配置）。

    PUSH_HOST / PUSH_USER 可经环境变量覆盖，默认推到 Orangepi ai-quant。"""
    import os as _os
    push_user = _os.environ.get("PUSH_USER", "orangepi")
    push_host = _os.environ.get("PUSH_HOST", "192.168.3.28")
    inbox_dir = _os.environ.get(
        "INBOX_DIR", "~/ai-quant/memory/inbox")
    target = f"{push_user}@{push_host}"
    payload = build_payload(full, res)
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        fname = ("stock_%s_%s.json"
                 % (full, time.strftime("%Y%m%d")))
        subprocess.run(["ssh", target, f"mkdir -p {inbox_dir}"],
                       check=True, timeout=20, capture_output=True)
        r = subprocess.run(
            ["scp", tmp, f"{target}:{inbox_dir}/{fname}"],
            timeout=30, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"[push] 已推送 -> {target}:{inbox_dir}/{fname}")
        else:
            print(f"[push] 推送失败: {r.stderr.strip()}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def main():
    argv = sys.argv[1:]
    do_push = "--push" in argv
    argv = [a for a in argv if a != "--push"]
    do_refresh = "--refresh-cache" in argv
    argv = [a for a in argv if a != "--refresh-cache"]
    if do_refresh and True:
        print("刷新缓存数据库（全市场代码表/分层）...")
        refresh_all_codes(print)
        print("完成。")
        if not argv:
            return
    if argv:
        code_in = " ".join(argv)
    else:
        try:
            code_in = input("请输入股票代码（如 002241 / 600519）：")
        except EOFError:
            return
    try:
        full = normalize_code(code_in)
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    print("\n拉取数据并计算中...")
    res = analyze(full, progress=print)
    q, tp, pred = res["quote"], res["t_pred"], res["pred"]

    print("=" * 68)
    print(f"{q['name']} ({res['full_code']})  快照 {q['time']}")
    if res.get("pre_open"):
        print(f"昨收 {res['prev_close']:.2f} | [未开盘·T日预测锚定昨收] "
              f"| 现价 {q['price']:.2f} "
              f"({(q['price']/res['prev_close']-1)*100:+.2f}%)")
    else:
        print(f"昨收 {res['prev_close']:.2f} | 今开 {q['open']:.2f} "
              f"(缺口 {res['gap_today']:+.2f}%) | 现价 {q['price']:.2f} "
              f"({(q['price']/res['prev_close']-1)*100:+.2f}%)"
              + ("  [含盘中实时bar]" if res["has_live"] else ""))
    idx_txt = (f"{res['idx_chg_today']:+.2f}%"
               if res["idx_chg_today"] is not None else "未知")
    vr_txt = (f"量比 {res['vr_now']:.2f}（{res['cur_regime']}）"
              if res["vr_now"] is not None else "数据不足")
    sec_txt = (f"{res['sector_name']} {res['sector_chg_today']:+.2f}%"
               if res["sector_name"] and res["sector_chg_today"] is not None
               else "未知")
    print(f"大盘(上证) {idx_txt} | 板块 {sec_txt} | 本股量能 {vr_txt}")
    print(f"市场状态: {res.get('phase', '未知')}")

    print("-" * 68)
    print(f"今日(T)预测 [锚定{res.get('anchor', '今开')}]")
    print(f"{'分位':<6}{'收盘':>10}{'最高':>10}{'最低':>10}")
    for p in (10, 25, 50, 75, 90):
        print(f"P{p:<5}{tp['cl'][p]:>10.2f}{tp['hi'][p]:>10.2f}{tp['lo'][p]:>10.2f}")
    print(f"开盘->收盘 上行概率 {tp['up_prob']*100:.0f}%   "
          f"有效样本 {res['src_n']}/{len(res['samples'])}"
          f"（{res['filter_note']}）")
    lv = res.get("levels") or []
    if len(lv) > 1:
        print("分层上行概率: " + " | ".join(
            f"{x['label']} {x['up_prob']*100:.0f}%(n={x['n']})" for x in lv)
             + "  [融合权重 L1 0.6 L2 0.3 L3 0.1]")
    if res.get("pool_note"):
        print(res["pool_note"])
    if res["has_live"] and res["clamped"]:
        print(f"[盘中实时修正] 已实现最高 {res['live_high']:.2f} / "
              f"最低 {res['live_low']:.2f}，已并入预测区间")

    print("-" * 68)
    print("-" * 68)
    print(f"{res.get('next_label', '次日(T+1)')}预测: 开{pred['open']:.2f} 收{pred['close']:.2f} "
          f"高{pred['high']:.2f} 低{pred['low']:.2f}")
    
    # 多日预测
    multi = res.get("multi_pred")
    if multi:
        print("-" * 68)
        print("多日预测趋势（基于相似样本统计分布）")
        print(f"{'周期':<8}{'收盘预测':>10}{'最高预测':>10}{'最低预测':>10}{'上行概率':>10}{'累计涨跌':>10}")
        for mp in multi:
            print(f"{mp['label']:<8}"
                  f"{mp['price_cl']:>10.2f}"
                  f"{mp['price_hi']:>10.2f}"
                  f"{mp['price_lo']:>10.2f}"
                  f"{mp['up_prob']*100:>9.0f}%"
                  f"{mp['cum_cl']*100:>+9.1f}%")

    cp_ = res.get("chips")
    if cp_:
        print("-" * 68)
        print("筹码参考")
        print(f"  平均成本 {cp_['avg_cost']:.2f} | 现价 {cp_['cur']:.2f} "
              f"| 获利盘 {cp_['profit']*100:.0f}%")
        if cp_["p5"] and cp_["p95"]:
            print(f"  90%筹码区间 {cp_['p5']:.2f} ~ {cp_['p95']:.2f}")
        lv_txt = []
        if cp_["sup"]:
            lv_txt.append(f"支撑位 {cp_['sup']:.2f}")
        if cp_["res"]:
            lv_txt.append(f"压力位 {cp_['res']:.2f}")
        if lv_txt:
            print("  " + " | ".join(lv_txt))

    act = res.get("action")
    if act:
        print("-" * 68)
        print("综合评估(买卖点)")
        if act.get("band_note"):
            print("  ◆ " + act["band_note"])
        for lab, sc, note in act["items"]:
            mark = "+" if sc > 0 else ("-" if sc < 0 else "·")
            print(f"  [{mark}] {lab}  {note}")
        print(f"  合计 {act['score']:+d} → {act['verdict']}")

    print("-" * 68)
    print("相似历史参考日期（含 量能/大盘 匹配）")
    print(f"{'T日':<12}{'T+1日':<12}{'次日涨跌':>10}  标记")
    for s in res["samples"]:
        mark = ""
        if s.get("regime") == res.get("cur_regime"):
            mark += "[量]"
        if s.get("idx_chg") is not None and abs(s["idx_chg"]) <= 0.8:
            mark += "[盘]"
        n1_cl = s.get("n1_cl")
        n1_cl_str = f"{n1_cl*100:>+8.2f}%" if n1_cl is not None else "N/A"
        print(f"{s['t_date']:<12}{s.get('n1_date', 'N/A'):<12}"
              f"{n1_cl_str:>10}  {mark}")

    print("-" * 68)
    sigs = res["signals"]
    print(f"近期买卖信号（每日一个，按优先级合并）")
    for i, day, typ, txt in sigs[-12:]:
        tag = "买" if typ == "BUY" else "卖"
        print(f"  {day} [{tag}] {txt}")
    if not sigs:
        print("  近期无")
    
    # 回测统计
    bt = res.get("bt_stats")
    if bt:
        print("-" * 68)
        print("回测统计（样本内全部信号）")
        print(f"  总交易 {bt['trades']} 笔 | 已平仓 {bt['closed']} 笔 | 盈利 {bt['wins']} 笔")
        if bt.get("winrate") is not None:
            print(f"  胜率 {bt['winrate']*100:.1f}% | 区间收益 {bt['total']*100:+.1f}%"
                  f" | 年化收益 {bt['ann']*100:+.1f}%"
                  f" | 最大回撤 {bt['mdd']*100:.1f}%")
        if bt.get("floating") is not None:
            print(f"  未平仓浮盈 {bt['floating']*100:+.1f}%")

    print("=" * 68)
    print(f"作者：{AUTHOR}  邮箱：{AUTHOR_EMAIL}  QQ：{AUTHOR_QQ}")
    print(DISCLAIMER)

    if do_push:
        print("-" * 68)
        push_report(full, res)


if __name__ == "__main__":
    main()
