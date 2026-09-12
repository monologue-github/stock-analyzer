# -*- coding: utf-8 -*-
"""数据加载：全A股票池、指数、行业/市值元数据。

只用 SQLite 缓存，不联网。与 backtest_strategy_ablation.py 一致：
非 ETF、剔北交所、每只股票最少 min_bars 根K线。
"""
import sqlite3

import stock_gui as sg
from stock_gui import DB_PATH, _is_etf


def load_meta():
    """{code: {"name":..,"industry":..,"mktcap":..,"tier":..}}"""
    with sg.db_conn() as conn:
        rows = conn.execute(
            "SELECT code, name, industry, mktcap, tier FROM stocks").fetchall()
    meta = {}
    for code, name, ind, cap, tier in rows:
        meta[code] = {"name": name or "", "industry": ind or "",
                      "mktcap": cap or 0.0, "tier": tier or ""}
    return meta


def list_codes(min_bars=400, max_bars=None, require_meta=True):
    """满足最少K线数的非ETF个股代码（按代码排序，稳定可复现）。

    require_meta=True 时只保留 stocks 表内且有行业字段的个股，
    排除指数（sh000001 等）、无行业元数据的代码。
    """
    with sg.db_conn() as conn:
        if max_bars:
            rows = conn.execute(
                "SELECT code, COUNT(*) c FROM daily_bars GROUP BY code "
                "HAVING c >= ? AND c <= ?", (min_bars, max_bars)).fetchall()
        else:
            rows = conn.execute(
                "SELECT code, COUNT(*) c FROM daily_bars GROUP BY code "
                "HAVING c >= ?", (min_bars,)).fetchall()
    meta = load_meta() if require_meta else {}
    codes = sorted(
        r[0] for r in rows
        if not _is_etf(r[0]) and not r[0].startswith("bj")
        and (not require_meta or (r[0] in meta and meta[r[0]]["industry"])))
    return codes


def load_stock(code, tail=None):
    """读取单只股票日K（可按尾部截断），返回按日期升序的 dict 列表。"""
    with sg.db_conn() as conn:
        rows = conn.execute(
            "SELECT date, open, high, low, close, vol FROM daily_bars "
            "WHERE code=? ORDER BY date", (code,)).fetchall()
    bars = [{"date": d, "open": o, "high": h, "low": l, "close": c,
             "vol": v or 0.0} for d, o, h, l, c, v in rows]
    bars = [b for b in bars if b["close"] and b["close"] > 0]
    if tail:
        bars = bars[-tail:]
    return bars


def load_all_bars(tail=None):
    """一次性载入全部股票（内存敏感，仅小样本/测试用）。"""
    by = {}
    with sg.db_conn() as conn:
        cur = conn.execute(
            "SELECT code, date, open, high, low, close, vol "
            "FROM daily_bars ORDER BY code, date")
        for code, d, o, h, l, c, v in cur:
            by.setdefault(code, []).append(
                {"date": d, "open": o, "high": h, "low": l, "close": c,
                 "vol": v or 0.0})
    if tail:
        by = {c: r[-tail:] for c, r in by.items()}
    return by


def load_index():
    """上证指数日K（大盘因子）。"""
    try:
        rows = sg.get_daily("sh000001")
    except Exception:
        rows = []
    return [{"date": r["date"], "close": r["close"]} for r in rows
            if r.get("close")]


def index_ret_map():
    """{date: 指数日收益}，用于大盘5日动量。"""
    rows = load_index()
    out = {}
    for i in range(1, len(rows)):
        c0, c1 = rows[i - 1]["close"], rows[i]["close"]
        if c0 and c1 and c0 > 0:
            out[rows[i]["date"]] = c1 / c0 - 1.0
    return out


def industry_ret_maps():
    """全市场行业等权日收益：{date: {industry: ret}}（一遍扫描全库）。"""
    ind_of = {}
    with sg.db_conn() as conn:
        for code, ind in conn.execute("SELECT code, industry FROM stocks"):
            ind_of[code] = ind or ""
    acc, prev = {}, {}
    with sg.db_conn() as conn:
        cur = conn.execute(
            "SELECT code, date, close FROM daily_bars ORDER BY date")
        for code, d, c in cur:
            pv = prev.get(code)
            if c and c > 0 and pv and pv > 0:
                r = c / pv - 1.0
                b = acc.setdefault(d, {}).setdefault(ind_of.get(code, ""),
                                                     [0.0, 0])
                b[0] += r
                b[1] += 1
            if c:
                prev[code] = c
    return {d: {k: s / n for k, (s, n) in v.items() if n}
            for d, v in acc.items()}
