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
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

QT_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
INI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_gui.ini")
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

# ---- 可切换主题 ----
THEMES = {
    "dark": dict(
        UP="#ff5252", DOWN="#26c281", PRED_C="#4da3ff",
        BG="#14181e", GRID_C="#232b34", GUIDE_C="#39434e",
        AXIS_TXT="#8fa0ad", TITLE_TXT="#aebccb", CROSS_C="#9fb3c8",
        DARK_BG="#101418", PANEL_BG="#171c22", FIELD_BG="#1c232b",
        FG_MAIN="#d7dee6",
        BTN_BG="#222a33", BTN_FG="#d7dee6", BTN_HOVER="#2b3540",
        BTN_BORDER="#333e4a",
    ),
    "light": dict(
        UP="#e03131", DOWN="#0ca678", PRED_C="#1971c2",
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

def http_get(url, retries=3):
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("gbk", errors="ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (a + 1))
    raise RuntimeError(f"网络请求失败: {last}")


def normalize_code(code):
    code = code.strip().lower()
    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            return p + code[2:]
    d = "".join(ch for ch in code if ch.isdigit())
    if len(d) != 6:
        raise ValueError(f"代码格式不对: {code}")
    if d[0] in "69" or d[:2] in ("51", "56", "58"):      # 沪股/沪ETF
        return "sh" + d
    if d[0] in "03" or d[:2] in ("15", "16", "18"):      # 深股/深ETF/LOF
        return "sz" + d
    if d[0] in "48":
        return "bj" + d
    raise ValueError(f"不支持的代码: {code}")


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


def fetch_sector_context(full):
    """个股所属行业板块指数上下文。

    返回 (板块名, {date: 当日涨跌%}, 板块今日涨跌%)；失败返回 (None, {}, None)。
    """
    UT = "fa5fd1943c7b386f172d6893dbfba10b"
    hdr = {"User-Agent": "Mozilla/5.0",
           "Referer": "https://quote.eastmoney.com/"}

    def get(u, timeout=10):
        last = None
        for a in range(3):
            try:
                req = urllib.request.Request(u, headers=hdr)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read().decode("utf-8")
            except Exception as e:
                last = e
                time.sleep(0.8 * (a + 1))
        raise last

    try:
        code = full[2:]
        mkt = "1" if full.startswith("sh") else "0"
        # 1) 个股行业名
        u = (f"https://push2.eastmoney.com/api/qt/stock/get"
             f"?secid={mkt}.{code}&fields=f127&ut={UT}")
        ind_name = json.loads(get(u))["data"].get("f127")
        if not ind_name:
            return None, {}, None
        # 2) 行业板块列表（分页），按名称匹配 BK 代码
        bk_code = None
        for pn in (1, 2, 3):
            u = (f"https://push2.eastmoney.com/api/qt/clist/get"
                 f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invariant=0"
                 f"&fields=f12,f14&fs=m:90+t:2&ut={UT}")
            diff = json.loads(get(u)).get("data", {}).get("diff") or {}
            items = list(diff.values()) if isinstance(diff, dict) else diff
            for it in items:
                if it.get("f14") == ind_name:
                    bk_code = it.get("f12")
                    break
            if bk_code or len(items) < 100:
                break
        if not bk_code:
            return ind_name, {}, None
        # 3) 板块日K（收盘价）
        u = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
             f"?secid=90.{bk_code}&fields1=f1,f2,f3&fields2=f51,f53"
             f"&klt=101&fqt=0&beg=20240101&end=20500101")
        kl = json.loads(get(u, timeout=15))["data"]["klines"]
        bars = [(s.split(",")[0], float(s.split(",")[1])) for s in kl]
        chg_by_date = {
            b[0]: (b[1] / a[1]) * 100 - 100
            for a, b in zip(bars, bars[1:])
        }
        today_chg = chg_by_date.get(bars[-1][0])
        return ind_name, chg_by_date, today_chg
    except Exception:
        return None, {}, None


def fetch_quote(full):
    f = http_get(QT_URL + full).split("~")
    if len(f) < 35 or not f[3]:
        raise ValueError("未查询到该股票")
    return {"name": f[1], "price": float(f[3]), "prev_close": float(f[4]),
            "open": float(f[5]), "high": float(f[33]), "low": float(f[34]),
            "time": f[30]}


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
    for i in range(n - 1, len(vals)):
        out[i] = sum(vals[i - n + 1:i + 1]) / n
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


def logret(seq):
    return [math.log(seq[i + 1] / seq[i]) for i in range(len(seq) - 1)]


W_WINDOW, TOPK = 10, 10


# ================= 分析 =================

def analyze(full):
    """全量分析，切片交给GUI。"""
    W = W_WINDOW
    q = fetch_quote(full)
    rows = fetch_daily(full)

    # 识别今日盘中bar
    today_str = time.strftime("%Y-%m-%d")
    live = None
    if rows[-1]["date"] == today_str:
        live = rows.pop()          # 形态匹配剔除今日盘中
        live["close"] = q["price"]
        live["high"] = max(live["high"], q["price"])
        live["low"] = min(live["low"], q["price"]) if q["low"] > 0 else live["low"]

    closes_m = [r["close"] for r in rows]
    rets = logret(closes_m)

    # ---- 大盘与量能上下文（纳入样本匹配）----
    try:
        iq = fetch_quote("sh000001")
        idx_chg_today = ((iq["price"] / iq["prev_close"]) * 100 - 100
                         if iq["prev_close"] else 0.0)
    except Exception:
        idx_chg_today = None
    try:
        idx_rows = fetch_daily("sh000001")
        idx_chg_by_date = {
            b["date"]: (b["close"] / a["close"]) * 100 - 100
            for a, b in zip(idx_rows, idx_rows[1:])
        }
    except Exception:
        idx_chg_by_date = {}

    sec_name, sec_chg_by_date, sec_chg_today = fetch_sector_context(full)

    vols_m = [r.get("vol") or 0.0 for r in rows]
    vr_now = vol_ratio_at(vols_m, len(vols_m) - 1)
    cur_regime = vol_regime(vr_now)

    def _dist_vol(i):
        vr_i = vol_ratio_at(vols_m, i)
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
    sims.sort(key=lambda x: x[0])
    top = sims[:TOPK]

    prev_close = q["prev_close"] or closes_m[-1]
    o_today = q["open"] or prev_close
    gap_today = (o_today / prev_close - 1) * 100

    samples = []
    for _, i in top:
        r, n1, n2 = rows[i], rows[i + 1], rows[i + 2]
        ic = idx_chg_by_date.get(r["date"])
        sc = sec_chg_by_date.get(r["date"])
        samples.append({
            "t_date": r["date"], "n1_date": n1["date"], "n2_date": n2["date"],
            "vr": vol_ratio_at(vols_m, i),
            "idx_chg": (ic - idx_chg_today
                        if (ic is not None and idx_chg_today is not None)
                        else None),
            "sec_d": (sc - sec_chg_today
                      if (sc is not None and sec_chg_today is not None)
                      else None),
            "gap": n1["open"] / r["close"] - 1,
            "hi_o": n1["high"] / n1["open"] - 1,
            "lo_o": n1["low"] / n1["open"] - 1,
            "cl_o": n1["close"] / n1["open"] - 1,
            "t2_gap": n2["open"] / n1["close"] - 1,
            "t2_hi": n2["high"] / n2["open"] - 1,
            "t2_lo": n2["low"] / n2["open"] - 1,
            "t2_cl": n2["close"] / n2["open"] - 1,
        })
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

    t_pred = {
        "cl": {p: o_today * (1 + pct([s["cl_o"] for s in src], p)) for p in (10, 25, 50, 75, 90)},
        "hi": {p: o_today * (1 + pct([s["hi_o"] for s in src], p)) for p in (10, 25, 50, 75, 90)},
        "lo": {p: o_today * (1 + pct([s["lo_o"] for s in src], p)) for p in (10, 25, 50, 75, 90)},
        "up_prob": len([s for s in src if s["cl_o"] > 0]) / len(src),
    }
    base = t_pred["cl"][50]
    # 盘中实时修正：预测区间必须包含已实现的最高/最低
    clamped = False
    if live is not None:
        clamped = True
        for pp in (10, 25, 50, 75, 90):
            t_pred["hi"][pp] = round(max(t_pred["hi"][pp], live["high"]), 2)
            t_pred["lo"][pp] = round(min(t_pred["lo"][pp], live["low"]), 2)
    pred = {"date": "T+1预测", "open": base,
            "close": base * (1 + pct([s["t2_cl"] for s in src], 50)),
            "high": base * (1 + pct([s["t2_hi"] for s in src], 75)),
            "low": base * (1 + pct([s["t2_lo"] for s in src], 25)),
            "vol": None}

    # 指标基于 匹配历史(+今日盘中) 计算
    disp_rows = rows + ([live] if live else [])
    closes_i = [r["close"] for r in disp_rows]
    dif, dea, mhist = calc_macd(closes_i)
    k_, d_, j_ = calc_kdj(disp_rows)
    r6, r12 = calc_rsi(closes_i, 6), calc_rsi(closes_i, 12)
    mas = {n: sma_period(closes_i, n) for n in MA_COLORS}

    signals = []
    start = max(1, len(disp_rows) - 120)
    for i in range(start, len(disp_rows)):
        day = disp_rows[i]["date"]
        if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            continue
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            signals.append((i, day, "BUY", f"MACD金叉 DIF={dif[i]:.3f}>DEA={dea[i]:.3f}"))
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            signals.append((i, day, "SELL", f"MACD死叉 DIF={dif[i]:.3f}<DEA={dea[i]:.3f}"))
        if k_[i - 1] <= d_[i - 1] and k_[i] > d_[i] and k_[i] < 45:
            signals.append((i, day, "BUY", f"KDJ低位金叉 K={k_[i]:.1f}>D={d_[i]:.1f}"))
        elif k_[i - 1] >= d_[i - 1] and k_[i] < d_[i] and k_[i] > 65:
            signals.append((i, day, "SELL", f"KDJ高位死叉 K={k_[i]:.1f}<D={d_[i]:.1f}"))
        if r6[i] is not None and r6[i - 1] is not None:
            if r6[i - 1] < 20 and r6[i] >= 20:
                signals.append((i, day, "BUY", f"RSI6超卖回升 {r6[i]:.1f}"))
            elif r6[i - 1] > 80 and r6[i] <= 80:
                signals.append((i, day, "SELL", f"RSI6超买回落 {r6[i]:.1f}"))

    # ---- 量价配合信号：放量站上/跌破 MA20（结合历史量能）----
    vols_d = [r.get("vol") or 0.0 for r in disp_rows]
    for i in range(max(21, len(disp_rows) - 120), len(disp_rows)):
        ma20, ma20p = mas[20][i], mas[20][i - 1]
        if not ma20 or not ma20p or not vols_d[i]:
            continue
        day = disp_rows[i]["date"]
        c, cp = disp_rows[i]["close"], disp_rows[i - 1]["close"]
        v5 = sum(vols_d[max(0, i - 5):i]) / max(1, min(5, i))
        vr_d = vols_d[i] / v5 if v5 > 0 else 0.0
        if cp <= ma20p and c > ma20 and vr_d > 1.2:
            signals.append((i, day, "BUY",
                            f"放量站上MA20 量比{vr_d:.1f}"))
        elif cp >= ma20p and c < ma20 and vr_d > 1.2:
            signals.append((i, day, "SELL",
                            f"放量跌破MA20 量比{vr_d:.1f}"))

    # ---- 历史形态统计信号：当日窗口在全部历史中找最相似样本，看其次日涨跌分布 ----
    stat_days = min(60, len(rows) - W - 2)
    win_cache = {}

    def zwin(i):
        if i not in win_cache:
            win_cache[i] = znorm(rets[i - W:i])
        return win_cache[i]

    stat_signals = []
    for i in range(len(rows) - stat_days, len(rows)):
        base = zwin(i)
        cands = []
        for j in range(W, len(rets) - 1):
            if abs(j - i) <= 5:      # 避免与自身重叠
                continue
            wj = zwin(j)
            d0 = sum((a - b) ** 2 for a, b in zip(base, wj)) ** 0.5
            d_v = _dist_vol(j)
            sc = d0 + (0.6 * min(d_v, 2.5) if d_v is not None else 0.30)
            cands.append((sc, j))
        cands.sort(key=lambda x: x[0])
        topN = cands[:6]
        ups = tot = 0
        sret = 0.0
        for _, j in topN:
            nxt = rows[j + 1]["close"] / rows[j]["close"] - 1
            tot += 1
            sret += nxt
            if nxt > 0:
                ups += 1
        if tot == 0:
            continue
        ratio, avg = ups / tot, sret / tot * 100
        day = rows[i]["date"]
        if ratio >= 0.67 and avg > 0.2:
            stat_signals.append((i, day, "BUY",
                                 f"历史相似形态偏多 {ups}/{tot} 平均{avg:+.1f}%"))
        elif ratio <= 0.33 and avg < -0.2:
            stat_signals.append((i, day, "SELL",
                                 f"历史相似形态偏空 {tot-ups}/{tot} 平均{avg:+.1f}%"))
    signals.extend(stat_signals)
    signals.sort(key=lambda s: s[0])

    # ---- 每日去重：一天只保留一个信号，主信号按优先级选取 ----
    def _prio(txt):
        if txt.startswith("MACD"):
            return 0
        if txt.startswith("放量"):
            return 1
        if txt.startswith("历史相似形态"):
            return 2
        if txt.startswith("KDJ"):
            return 3
        if txt.startswith("RSI"):
            return 4
        return 5

    grouped = {}
    for sig in signals:
        grouped.setdefault(sig[0], []).append(sig)
    merged = []
    for i, lst in grouped.items():
        lst.sort(key=lambda s: (_prio(s[3]),))
        first = lst[0]
        extras = [s[3].split()[0] for s in lst[1:]]
        txt = first[3]
        if extras:
            txt += f"（+{'、'.join(extras)}）"
        merged.append((i, first[1], first[2], txt))
    merged.sort(key=lambda s: s[0])
    signals = merged

    vols = [r["vol"] for r in disp_rows]
    return {
        "quote": q, "full_code": full, "disp_rows": disp_rows,
        "pred": pred, "t_pred": t_pred, "samples": samples, "src_n": len(src),
        "filtered": src is not samples,
        "filter_note": filter_note,
        "idx_chg_today": idx_chg_today, "vr_now": vr_now,
        "cur_regime": cur_regime,
        "sector_name": sec_name, "sector_chg_today": sec_chg_today,
        "ind": {"ma": mas, "dif": dif, "dea": dea, "mhist": mhist,
                "k": k_, "d": d_, "j": j_, "rsi6": r6, "rsi12": r12},
        "vols": vols,
        "signals": signals,
        "gap_today": gap_today, "prev_close": prev_close,
        "has_live": bool(live),
        "live_high": live["high"] if live else None,
        "live_low": live["low"] if live else None,
        "clamped": clamped,
    }


def slice_view(res, show_n):
    n_total = len(res["disp_rows"])
    off = max(0, n_total - show_n)
    view = {
        "bars": res["disp_rows"][off:] + [res["pred"]],
        "dates": [r["date"] for r in res["disp_rows"][off:]] + ["T+1"],
        "off": off,
        "ma": {nn: vals[off:] for nn, vals in res["ind"]["ma"].items()},
        "dif": res["ind"]["dif"][off:], "dea": res["ind"]["dea"][off:],
        "mhist": res["ind"]["mhist"][off:],
        "k": res["ind"]["k"][off:], "d": res["ind"]["d"][off:], "j": res["ind"]["j"][off:],
        "rsi6": res["ind"]["rsi6"][off:], "rsi12": res["ind"]["rsi12"][off:],
        "vols": res["vols"][off:],
        "signals": [(i - off, dt, t, txt) for i, dt, t, txt in res["signals"] if i >= off],
    }
    return view


# ================= GUI =================

class Chart(tk.Canvas):
    def __init__(self, master, height):
        super().__init__(master, height=height, bg=BG, highlightthickness=0)


def deepseek_chat(api_key, prompt, model="deepseek-chat", timeout=90):
    """调用 DeepSeek chat 接口（纯标准库）。"""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system",
             "content": "你是专业A股分析师，回答简洁直接，给出可操作建议并附风险提示。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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

    def __init__(self, root):
        self.root = root
        root.title("股票形态相似度预测工具 · 增强版")
        root.geometry("1520x940")
        self.settings = {"theme": "dark", "updown": "red_up"}
        self.api_key = ""
        self.watchlist = []
        self.ai_text = ""
        self.idx_data = {}
        self._load_config()
        apply_theme(self.settings["theme"], self.settings["updown"])
        root.configure(bg=DARK_BG)
        self._style_ttk()
        self.res = None
        self.view = None
        self.scales = {}
        self.show_n = tk.IntVar(value=60)
        self.ind_name = tk.StringVar(value="MACD")
        self.ma_on = {nn: tk.BooleanVar(value=True) for nn in MA_COLORS}

        self._build_toolbar()
        if getattr(self, "_last_code", ""):
            self.code_var.set(self._last_code)
        self._build_body()
        self._render_watchlist()
        if self.code_var.get():
            self.run()
        self._update_indices()          # 立即刷新指数
        self.root.after(30000, self._index_loop)

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
        style.configure("TEntry", fieldbackground=FIELD_BG, foreground=FG_MAIN)
        style.configure("TCombobox", fieldbackground=FIELD_BG,
                        foreground=FG_MAIN, background=BTN_BG, arrowcolor=FG_MAIN)
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD_BG)],
                  foreground=[("readonly", FG_MAIN)])
        style.configure("TScrollbar", background=BTN_BG,
                        troughcolor=DARK_BG)
        style.configure("Checkbutton", background=DARK_BG, foreground=FG_MAIN)
        style.map("Checkbutton", background=[("active", DARK_BG)])

    # ---------- 布局 ----------

    def _build_toolbar(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="代码:").pack(side="left")
        self.code_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.code_var, width=14)
        ent.pack(side="left", padx=3)
        ent.bind("<Return>", lambda e: self.run())
        self.btn_run = ttk.Button(top, text="分析预测", command=self.run)
        self.btn_run.pack(side="left", padx=3)

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(top, text="周期:").pack(side="left")
        cb = ttk.Combobox(top, textvariable=self.show_n, width=5, state="readonly",
                          values=[30, 60, 90, 120])
        cb.pack(side="left", padx=3)
        cb.bind("<<ComboboxSelected>>", lambda e: self._rerender())
        ttk.Label(top, text="副图指标:").pack(side="left")
        ci = ttk.Combobox(top, textvariable=self.ind_name, width=6, state="readonly",
                          values=["MACD", "KDJ", "RSI"])
        ci.pack(side="left", padx=3)
        ci.bind("<<ComboboxSelected>>", lambda e: self._rerender())

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        for nn in sorted(MA_COLORS):
            tk.Checkbutton(top, text=f"MA{nn}", variable=self.ma_on[nn],
                           command=self._rerender, font=("Consolas", 8),
                           bg=DARK_BG, fg=FG_MAIN, activebackground=DARK_BG,
                           activeforeground=FG_MAIN,
                           selectcolor=FIELD_BG).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="复制报告", command=self.copy_report).pack(side="left", padx=2)
        ttk.Button(top, text="导出报告", command=self.export_report).pack(side="left", padx=2)
        ttk.Button(top, text="样本明细", command=self.show_samples).pack(side="left", padx=2)
        self.btn_ai = ttk.Button(top, text="AI分析", command=self.ai_analyze)
        self.btn_ai.pack(side="left", padx=2)
        ttk.Button(top, text="⚙设置", command=self.open_settings).pack(side="left", padx=2)

        self.info_var = tk.StringVar(value="输入代码如 002241 / 600519，点击【分析预测】")
        ttk.Label(top, textvariable=self.info_var, foreground=TITLE_TXT).pack(
            side="left", padx=10)
        self.hover_var = tk.StringVar(value="")
        hk = ttk.Label(top, textvariable=self.hover_var, foreground="#4da3ff",
                       font=("Consolas", 9))
        hk.pack(side="right")

    def _build_body(self):
        body = tk.Frame(self.root)
        body.pack(fill="both", expand=True)

        # ---- 左侧：自选池 ----
        wf = ttk.LabelFrame(body, text=" 自选池 ", padding=4)
        wf.pack(side="left", fill="y", padx=(8, 2), pady=2)
        self.watch_list = tk.Listbox(wf, width=15, font=("Consolas", 10),
                                     exportselection=False, bg=PANEL_BG,
                                     fg=FG_MAIN, selectbackground="#2b3540",
                                     selectforeground="#ffffff",
                                     relief="flat", highlightthickness=0)
        self.watch_list.pack(fill="both", expand=True)
        self.watch_list.bind("<Double-Button-1>", self._on_pick)
        bf = ttk.Frame(wf)
        bf.pack(fill="x", pady=(4, 0))
        ttk.Button(bf, text="+ 加自选", width=8,
                   command=self.add_watch).pack(side="left", padx=1)
        ttk.Button(bf, text="- 删除", width=7,
                   command=self.del_watch).pack(side="left", padx=1)

        # ---- 中部：图表 + 指数条 ----
        left = tk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.cv_main = Chart(left, self.PANEL_H["main"])
        self.cv_vol = Chart(left, self.PANEL_H["vol"])
        self.cv_ind = Chart(left, self.PANEL_H["ind"])
        for cv in (self.cv_main, self.cv_vol, self.cv_ind):
            cv.pack(fill="x", padx=(2, 2))
        idxbar = ttk.LabelFrame(left, text=" 五大指数 ", padding=(6, 3))
        idxbar.pack(fill="x", padx=(2, 2), pady=(4, 0))
        self.idx_labels = {}
        for col, (code, name) in enumerate(INDEX_CODES):
            idxbar.columnconfigure(col, weight=1, uniform="idx")
            cell = tk.Frame(idxbar, bg=DARK_BG)
            cell.grid(row=0, column=col, sticky="nsew", padx=4)
            tk.Label(cell, text=name, font=("Microsoft YaHei", 9),
                     fg=AXIS_TXT, bg=DARK_BG).grid(row=0, column=0,
                                                   sticky="w")
            lc = tk.Label(cell, text="-", font=("Consolas", 10, "bold"),
                          bg=DARK_BG)
            lc.grid(row=0, column=1, sticky="e", padx=(6, 0))
            lp = tk.Label(cell, text="-", font=("Consolas", 11, "bold"),
                          fg=FG_MAIN, bg=DARK_BG)
            lp.grid(row=1, column=0, columnspan=2, sticky="w")
            self.idx_labels[code] = (lp, lc)
        idxbar.columnconfigure(len(INDEX_CODES), weight=0)

        right = ttk.LabelFrame(body, text=" 预测参考 ", padding=4)
        right.pack(side="right", fill="y", padx=(2, 8), pady=6)
        self.side_txt = tk.Text(right, width=44, font=("Microsoft YaHei", 9),
                                relief="flat", bg=PANEL_BG, fg=FG_MAIN,
                                insertbackground=FG_MAIN,
                                selectbackground="#2b3540")
        self.side_txt.pack(fill="both", expand=True)

        bottom = tk.Frame(self.root, bg=DARK_BG)
        bottom.pack(fill="both", padx=8, pady=(2, 6))
        self.txt = tk.Text(bottom, height=8, font=("Consolas", 9),
                           bg="#12171d", fg="#cfd8e0",
                           insertbackground=FG_MAIN, relief="flat",
                           selectbackground="#2b3540")
        self.txt.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(bottom, command=self.txt.yview)
        sb.pack(side="right", fill="y")
        self.txt.config(yscrollcommand=sb.set)

        # 十字光标事件
        self.chart_keys = [("main", self.cv_main), ("vol", self.cv_vol),
                           ("ind", self.cv_ind)]
        for key, cv in self.chart_keys:
            cv.bind("<Motion>", lambda e, k=key: self._on_motion(e, k))
            cv.bind("<Leave>", lambda e, k=key: self._on_leave(e, k))
            cv.bind("<Configure>", self._on_resize)
            cv.bind("<MouseWheel>", self._on_wheel)
            cv.bind("<Button-4>", self._on_wheel)
            cv.bind("<Button-5>", self._on_wheel)

    # ---------- 运行分析 ----------

    def _run_bg(self, fn, done):
        """后台线程执行 fn，完成后在主线程调用 done(result|None, err|None)。"""
        holder = {}

        def worker():
            try:
                holder["r"] = fn()
            except Exception as e:
                holder["e"] = e

        threading.Thread(target=worker, daemon=True).start()

        def check(tries=0):
            if "r" in holder:
                done(holder["r"], None)
            elif "e" in holder:
                done(None, holder["e"])
            elif tries < 240:
                self.root.after(250, lambda: check(tries + 1))
            else:
                done(None, RuntimeError("后台任务超时"))
        self.root.after(200, check)

    def run(self):
        try:
            full = normalize_code(self.code_var.get())
        except ValueError as e:
            messagebox.showwarning("代码有误", str(e))
            return
        self.btn_run.config(state="disabled")
        self.info_var.set("正在拉取数据并计算...")
        self._run_bg(lambda: analyze(full), self._done_load)

    def _done_load(self, res, err):
        if err:
            self._fail(str(err))
            return
        self._loaded(res)

    def _fail(self, msg):
        self.btn_run.config(state="normal")
        self.info_var.set("失败")
        messagebox.showerror("错误", msg)

    def _loaded(self, res):
        self.btn_run.config(state="normal")
        self.res = res
        self.ai_text = ""
        self._save_ini()
        q = res["quote"]
        chg = (q["price"] / res["prev_close"] - 1) * 100
        self.info_var.set(
            f"{q['name']} ({res['full_code']})  昨收{res['prev_close']:.2f} "
            f"今开{q['open']:.2f}(缺口{res['gap_today']:+.2f}%) "
            f"现价{q['price']:.2f}({chg:+.2f}%)  快照{q['time']}"
            + ("  [含盘中实时bar]" if res["has_live"] else ""))
        self._rerender()

    def _rerender(self):
        if not self.res:
            return
        try:
            n = int(self.show_n.get())
        except Exception:
            n = 60
        n = max(20, min(n, 250))
        self.view = slice_view(self.res, n)
        self._draw_main()
        self._draw_vol()
        name = self.ind_name.get()
        if name == "MACD":
            self._draw_macd()
        elif name == "KDJ":
            self._draw_kdj()
        else:
            self._draw_rsi()
        self._write_side()
        self._write_report()

    def _on_wheel(self, event):
        """滚轮缩放K线：上滚放大（减少根数），下滚缩小。"""
        if not self.res:
            return
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
        self.info_var.set(f"K线根数: {n}")
        self._rerender()

    def _on_resize(self, _event):
        if getattr(self, "_resize_job", None):
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(200, self._rerender)

    # ---------- 绘图工具 ----------

    def _geom(self, cv, n_bars):
        w = max(cv.winfo_width(), 900)
        h = int(cv["height"])
        L, R, T, B = 58, 70, 16, 20
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
        last = None
        for i, v in enumerate(vals):
            if v is None:
                last = None
                continue
            x, y = xs_fn(i), ymap(v)
            if last:
                cv.create_line(last[0], last[1], x, y, fill=color, width=width)
            last = (x, y)

    def _finish_panel(self, cv, g, key, lo, hi, dates, fmt=None):
        """注册缩放信息并创建十字光标元素。"""
        g["lo_v"], g["hi_v"] = lo, hi
        g["dates"] = dates
        g["fmt"] = fmt or (lambda v: f"{v:.2f}")
        self.scales[key] = g
        cv.delete("cross")
        g["vid"] = cv.create_line(0, 0, 0, 0, state="hidden", fill=CROSS_C,
                                  dash=(4, 3), tags="cross")
        g["hid"] = cv.create_line(0, 0, 0, 0, state="hidden", fill=CROSS_C,
                                  dash=(4, 3), tags="cross")
        g["pid"] = cv.create_text(g["w"] - g["R"] + 34, 0, text="", state="hidden",
                                  fill="#fff", font=("Consolas", 8, "bold"),
                                  tags="cross")
        g["pbg"] = cv.create_rectangle(0, 0, 0, 0, state="hidden",
                                       fill="#1971c2", outline="", tags="cross")
        g["did"] = cv.create_text(0, g["h"] - g["B"] // 2 + 2, text="", state="hidden",
                                  fill="#fff", font=("Consolas", 8, "bold"),
                                  tags="cross")
        g["dbgd"] = cv.create_rectangle(0, 0, 0, 0, state="hidden",
                                        fill="#333c46", outline="", tags="cross")
        cv.tag_raise("cross")

    # ---------- 主图 ----------

    def _draw_main(self):
        cv, v = self.cv_main, self.view
        cv.delete("all")
        bars = v["bars"]
        g = self._geom(cv, len(bars))
        los = [b["low"] for b in bars]
        his = [b["high"] for b in bars]
        for nn, on in self.ma_on.items():
            if on.get():
                vals = [x for x in v["ma"][nn] if x is not None]
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

        for i, b in enumerate(bars):
            x = xs(i)
            isp = b["date"] == "T+1预测"
            up = b["close"] >= b["open"]
            color = PRED_C if isp else (UP if up else DOWN)
            dash = (3, 2) if isp else ()
            yo, yc = ymap(b["open"]), ymap(b["close"])
            cv.create_line(x, ymap(b["high"]), x, ymap(b["low"]),
                           fill=color, dash=dash)
            bw2 = max(g["bw"] * 0.62, 2)
            ty, by = min(yo, yc), max(yo, yc)
            if by - ty < 1:
                by = ty + 1
            cv.create_rectangle(x - bw2 / 2, ty, x + bw2 / 2, by,
                                fill="" if isp else color,
                                outline=color, dash=dash)

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
        cv.create_text(xs(len(bars) - 1), g["T"] - 3,
                       text=f"T+1 C:{pb['close']:.2f}", fill=PRED_C,
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
        vols = v["vols"][:-1]      # 最后一个是预测位，无成交量
        n = len(v["bars"])
        g = self._geom(cv, n)
        vmax = max(vols) if vols else 1.0
        lo, hi = 0, vmax * 1.08

        def ymap(val):
            return g["T"] + (hi - val) / (hi - lo) * g["ph"]

        def xs(i):
            return g["L"] + g["bw"] * (i + 0.5)
        self._axes(cv, g, lo, hi, lambda x: f"{x/10000:.0f}万", 2)
        bw2 = max(g["bw"] * 0.62, 2)
        for i, vol in enumerate(vols):
            c = UP if v["bars"][i]["close"] >= v["bars"][i]["open"] else DOWN
            y = ymap(vol)
            cv.create_rectangle(xs(i) - bw2 / 2, y, xs(i) + bw2 / 2, ymap(0),
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

    def _draw_macd(self):
        cv, v = self.cv_ind, self.view
        cv.delete("all")
        dif, dea, mh = v["dif"], v["dea"], v["mhist"]
        n = len(v["bars"])
        g = self._geom(cv, n)
        vals = [x for x in dif + dea + mh if x is not None]
        lo, hi = self._pad_range(min(vals + [0]), max(vals + [0]), 0.12)

        def ymap(val):
            return g["T"] + (hi - val) / (hi - lo) * g["ph"]

        def xs(i):
            return g["L"] + g["bw"] * (i + 0.5)
        zero = ymap(0)
        cv.create_line(g["L"], zero, g["w"] - g["R"], zero, fill=GRID_C)
        self._axes(cv, g, lo, hi, "{:.2f}", 2)
        bw2 = max(g["bw"] * 0.3, 1.5)
        for i, hv in enumerate(mh):
            if hv is None:
                continue
            c = UP if hv >= 0 else DOWN
            y = ymap(hv)
            cv.create_rectangle(xs(i) - bw2, min(y, zero),
                                xs(i) + bw2, max(y, zero), fill=c, outline=c)
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
        g = self._geom(cv, n)
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
        g = self._geom(cv, n)
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
        if key not in self.scales or not self.view:
            return
        g = self.scales[key]
        idx = int((event.x - g["L"]) / g["bw"])
        idx = max(0, min(g["n"] - 1, idx))
        cx = g["L"] + g["bw"] * (idx + 0.5)
        date = g["dates"][idx]

        for k, cv in (("main", self.cv_main), ("vol", self.cv_vol),
                      ("ind", self.cv_ind)):
            sg = self.scales.get(k)
            if not sg:
                continue
            cv.coords(sg["vid"], cx, sg["T"], cx, sg["h"] - sg["B"])
            cv.itemconfigure(sg["vid"], state="normal")
            if k == key:
                y = min(max(event.y, sg["T"]), sg["T"] + sg["ph"])
                cv.coords(sg["hid"], sg["L"], y, sg["w"] - sg["R"], y)
                cv.itemconfigure(sg["hid"], state="normal")
                price = sg["hi_v"] - (y - sg["T"]) / sg["ph"] * (sg["hi_v"] - sg["lo_v"])
                fmt = sg.get("fmt")
                txt = fmt(price) if fmt else f"{price:.2f}"
                px = sg["w"] - sg["R"] + 30
                cv.coords(sg["pid"], px, y)
                cv.itemconfigure(sg["pid"], text=txt, state="normal")
                bb = cv.bbox(sg["pid"])
                if bb:
                    cv.coords(sg["pbg"], bb[0] - 3, bb[1], bb[2] + 3, bb[3])
                    cv.itemconfigure(sg["pbg"], state="normal")
                    cv.tag_raise(sg["pid"])   # 文字必须在色块之上
            dl = date if len(date) <= 8 else date[:10]
            cv.coords(sg["did"], cx, sg["h"] - sg["B"] // 2 + 2)
            cv.itemconfigure(sg["did"], text=dl, state="normal")
            bb = cv.bbox(sg["did"])
            if bb:
                cv.coords(sg["dbgd"], bb[0] - 3, bb[1] - 1, bb[2] + 3, bb[3] + 1)
                cv.itemconfigure(sg["dbgd"], state="normal")
                cv.tag_lower(sg["dbgd"], sg["did"])

        # OHLC 提示
        bars = self.view["bars"]
        if idx < len(bars):
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
                else:
                    r6 = v["rsi6"][idx]
                    r12 = v["rsi12"][idx]
                    ind_txt = ("  RSI6:%.1f RSI12:%s"
                               % (r6, f"{r12:.1f}" if r12 is not None else "-"))
            if b["date"] == "T+1预测":
                self.hover_var.set(
                    f"[预测T+1] 开{b['open']:.2f} 高{b['high']:.2f} "
                    f"低{b['low']:.2f} 收{b['close']:.2f}")
            else:
                if idx > 0:
                    pc = bars[idx - 1]["close"]
                else:
                    off = self.view["off"]
                    pc = (self.res["disp_rows"][off - 1]["close"]
                          if off > 0 else self.res["prev_close"])
                chg = (b["close"] / pc - 1) * 100
                vol_s = f"{b['vol']/10000:.0f}万手" if b.get("vol") else "-"
                self.hover_var.set(
                    f"{b['date']} 开{b['open']:.2f} 高{b['high']:.2f} "
                    f"低{b['low']:.2f} 收{b['close']:.2f} ({chg:+.2f}%) "
                    f"量{vol_s}{ind_txt}")

    def _on_leave(self, _event):
        self.hover_var.set("")
        for _, cv in self.chart_keys:
            cv.itemconfigure("cross", state="hidden")

    # ---------- 文字区 ----------

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
        L.append("-- 今日(T)预测 -- 锚定今开")
        for p in (10, 25, 50, 75, 90):
            L.append(f"P{p}: 收盘{tp['cl'][p]:.2f} 最高{tp['hi'][p]:.2f} "
                     f"最低{tp['lo'][p]:.2f}")
        L.append(f"开盘->收盘 上行概率 {tp['up_prob']*100:.0f}%   "
                 f"有效样本 {res['src_n']}/{len(res['samples'])}"
                 f"（{res['filter_note']}）")
        if res["has_live"] and res["clamped"]:
            L.append(f"[盘中实时修正] 已实现最高 {res['live_high']:.2f} / "
                     f"最低 {res['live_low']:.2f}，已并入预测区间")
        L.append("-- 次日(T+1)预测 --")
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

        p("■ 今日(T)收盘预测  [锚定今开]")
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
        if res["has_live"] and res["clamped"]:
            p(f"  [实时修正] 盘中已实现 高{res['live_high']:.2f} "
              f"低{res['live_low']:.2f}")
        p()
        p("■ 次日(T+1)预测")
        p(f"  开{pred['open']:.2f} 收{pred['close']:.2f}")
        p(f"  高{pred['high']:.2f} 低{pred['low']:.2f}")
        p()
        p("■ 相似历史参考日期")
        p(f"(近{W_WINDOW}日形态匹配 Top{TOPK})")
        p(f"{'T日':<11}{'T+1日':<11}{'次日涨跌':>8}")
        for s in res["samples"]:
            chg1 = s["cl_o"] * 100
            mark = "*" if abs(s["gap"] * 100 - res["gap_today"]) <= 1.0 else ""
            p(f"{s['t_date']:<11}{s['n1_date']:<11}{chg1:>+7.1f}% {mark}")
        p()
        p("* = 开盘缺口与今日接近")
        p("──────────────────────")
        p("提示：前复权价统计推断，")
        p("仅供参考，不构成投资建议")
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
        try:
            with open(INI_PATH, "w", encoding="utf-8") as f:
                cp.write(f)
        except OSError as e:
            print(f"[ini] 保存失败: {e}")

    def _render_watchlist(self):
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
            self.info_var.set(f"{full} 已在自选池")
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
        self.root.after(30000, self._index_loop)

    def copy_report(self):
        if not self.res:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self._report_text())
        self.info_var.set("报告已复制到剪贴板")

    def export_report(self):
        if not self.res:
            return
        fn = filedialog.asksaveasfilename(
            initialdir=r"C:\Users\蓝广勋\Desktop",
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
        self.info_var.set(f"已导出: {fn}")

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
                       f"{s['t_date']:<11}{s['n1_date']:<11}{s['n2_date']:<11}"
                       f"{s['gap']*100:>7.2f}{s['hi_o']*100:>8.2f}"
                       f"{s['lo_o']*100:>8.2f}{s['cl_o']*100:>8.2f}"
                       f"{s['t2_hi']*100:>9.2f}{s['t2_lo']*100:>9.2f}"
                       f"{s['t2_cl']*100:>9.2f}\n")

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
        avg_t1 = sum(s["cl_o"] for s in sm) / len(sm) * 100
        idx_txt = (f"{res['idx_chg_today']:+.2f}%"
                   if res["idx_chg_today"] is not None else "数据不足")
        vr_txt = (f"量比 {res['vr_now']:.2f}（{res['cur_regime']}）"
                  if res["vr_now"] is not None else "数据不足")
        sec_txt = (f"{res['sector_name']}今日 {res['sector_chg_today']:+.2f}%"
                   if res["sector_name"] and res["sector_chg_today"] is not None
                   else "板块数据不足")
        return (
            f"你是专业A股分析师。请基于以下数据给出简短分析（300字内），"
            f"包含：1)技术面与量价配合解读(MA/MACD/KDJ/RSI/量能) "
            f"2)结合大盘、板块环境与统计预测的短线(1-3日)操作建议 3)风险提示。"
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
            f"RSI6={f1(last(ind['rsi6']))} RSI12={f1(last(ind['rsi12']))}\n\n"
             f"历史形态统计预测(锚定今开)：\n"
            f"今日收盘 P50={tp['cl'][50]:.2f}(P10 {tp['cl'][10]:.2f}/"
            f"P90 {tp['cl'][90]:.2f}) 上行概率{tp['up_prob']*100:.0f}%\n"
            f"次日预测 开{pred['open']:.2f} 收{pred['close']:.2f} "
            f"高{pred['high']:.2f} 低{pred['low']:.2f}\n"
            f"相似样本{n_len if (n_len:=len(sm)) else 0}个, 样本次日平均涨跌{avg_t1:+.2f}%\n"
            f"近期信号：{sig_txt}"
        )

    def ai_analyze(self):
        if not self.res:
            messagebox.showinfo("提示", "请先【分析预测】一只股票")
            return
        key = self.api_key
        if not key:
            key = simpledialog.askstring(
                "DeepSeek API Key",
                "首次使用请输入 DeepSeek API Key\n(仅保存在本地 stock_gui.ini)：",
                show="*", parent=self.root)
            if not key:
                return
            self.api_key = key.strip()
            self._save_ini()
        prompt = self._ai_prompt()
        self.btn_ai.config(state="disabled")
        self.info_var.set("DeepSeek 分析中...")
        self._run_bg(lambda: deepseek_chat(self.api_key, prompt),
                     self._ai_done)

    def _ai_done(self, text, err):
        self.btn_ai.config(state="normal")
        if err:
            self.info_var.set("AI分析失败")
            messagebox.showerror(
                "AI 分析失败",
                f"{err}\n\n请检查 API Key 与网络后重试（设置里可修改 Key）。")
            return
        self.ai_text = text
        self.info_var.set("AI分析完成（见右侧预测参考栏）")
        self._write_side()
        self.side_txt.see("end")

    # ---------- 设置 ----------

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.configure(bg=DARK_BG)
        win.transient(self.root)
        win.resizable(False, False)
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

        def save():
            self.settings["theme"] = theme_var.get()
            self.settings["updown"] = ud_var.get()
            new_key = key_var.get().strip()
            key_changed = new_key != self.api_key
            self.api_key = new_key
            self._save_ini()
            apply_theme(self.settings["theme"], self.settings["updown"])
            self._rebuild_ui()
            win.destroy()
            self.info_var.set("设置已保存")

        ttk.Button(frm, text="保存并应用", command=save).grid(
            row=3, column=0, columnspan=3, pady=(12, 0))

        # ---- 关于 / 免责声明 ----
        sep = ttk.Separator(frm, orient="horizontal")
        sep.grid(row=4, column=0, columnspan=3, sticky="we", pady=(14, 8))
        about = tk.Text(frm, width=52, height=11, relief="flat",
                        bg=PANEL_BG, fg=FG_MAIN, font=("Microsoft YaHei", 9),
                        wrap="word", highlightthickness=0)
        about.grid(row=5, column=0, columnspan=3, sticky="we")
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
