#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_clean.py - 股票日K缓存数据清洗（独立脚本，直接操作 stock_cache.db）

清洗范围：
  1. 数据异常   OHLC缺失/非正价/高低颠倒/影线越界 → 删除
  2. 除权残留   相邻日涨跌幅超出涨跌停允许范围（前复权序列不该出现）
                → 整只代码从前复权源重新下载全量替换（ETF孤立跳变放行）
  3. 停牌       上市区间内超长日历缺口 / 零成交 → 报告（缺数据是事实，不造数）
  4. 退市       最后bar距今超180天 → 记入 delisted 表并报告
  5. 价格粘性   连续≥20日收盘价完全不变（坏源冻结数据）→ 视同异常重拉

用法：
  python data_clean.py           # 只扫描+输出报告（不改库）
  python data_clean.py --fix     # 扫描+执行修复
  python data_clean.py --db x.db # 指定库路径
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import date, datetime

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "stock_cache.db")
# 涨跌停限制(%)：(板块前缀, 创业板2020-08-24注册制后20%)，北交所30
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "清洗报告_" + time.strftime("%Y%m%d") + ".md")


def _limit_pct(code, name, d):
    """该股当日涨跌幅限制(%)，None=不限（与 stock_gui._limit_pct 同规则）。"""
    if d < "1996-12-16":
        return None
    if code.startswith("bj"):
        return 30.0
    board = code[2:4] if len(code) >= 4 else ""
    if board in ("68",):
        return 20.0
    if board == "30":
        return 20.0 if d >= "2020-08-24" else 10.0
    if name and "ST" in name.upper() and d < "2026-07-06":
        return 5.0
    return 10.0


def _is_etf(code):
    pre = code[2:4] if len(code) >= 4 else ""
    return pre in ("51", "56", "58", "15", "16", "18")


def _fetch_em_qfq(full, count=1200):
    """东财前复权日K（与主程序同接口，用于整只替换）。"""
    secid = ("1." if full.startswith("sh") else "0.") + full[2:]
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
           f"&klt=101&fqt=1&beg=0&end=20500101&lmt={count}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0",
                      "Referer": "https://quote.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        kd = json.loads(r.read().decode("utf-8", "ignore"))
    out = []
    for line in (kd.get("data") or {}).get("klines") or []:
        p = line.split(",")
        if len(p) < 6:
            continue
        try:
            if float(p[2]) <= 0:
                continue
            out.append((p[0], float(p[1]), float(p[3]), float(p[4]),
                        float(p[2]), float(p[5])))
        except ValueError:
            continue
    return out


def _bar_valid(b):
    """单根bar结构校验：OHLC非空非负、high≥low、high≥max(o,c)、low≤min(o,c)。"""
    o, h, l, cl = b[1], b[2], b[3], b[4]
    if None in (o, h, l, cl):
        return False
    if min(o, h, l, cl) <= 0:
        return False
    if h < l:
        return False
    if h < max(o, cl) or l > min(o, cl):
        return False
    return True


def scan(conn):
    """全库扫描，返回 {code: {...问题列表}} 与全局统计。"""
    names = {r[0]: (r[1] or "") for r in
             conn.execute("SELECT code, name FROM stocks").fetchall()}
    rows = conn.execute(
        "SELECT code,date,open,high,low,close,vol FROM daily_bars "
        "ORDER BY code,date").fetchall()
    by = {}
    for c, d, o, h, l, cl, v in rows:
        by.setdefault(c, []).append((d, o, h, l, cl, v or 0.0))

    today = date.today()
    issues = {}
    stats = {"codes": len(by), "bars": len(rows), "bad_bars": 0,
             "refetch": 0, "suspicious": 0, "delisted": 0,
             "zero_vol": 0, "stale": 0}

    def add(c, kind, detail):
        issues.setdefault(c, []).append((kind, detail))

    for c, bars in by.items():
        name = names.get(c, "")
        n = len(bars)
        # ---- 1) 结构异常 ----
        bad = [b for b in bars if not _bar_valid(b)]
        if bad:
            add(c, "bad_bars", f"{len(bad)}根结构异常(如 {bad[0][0]})")
            stats["bad_bars"] += len(bad)
        # ---- 2) 涨跌幅越界（疑似除权残留/坏数据）----
        flags = []
        for prev, cur in zip(bars, bars[1:]):
            pc, cl = prev[4], cur[4]
            lim = _limit_pct(c, name, cur[0])
            if not pc or not cl or lim is None:
                flags.append(False)
                continue
            flags.append(abs(cl / pc - 1) * 100 > lim + 3.0)
        viol = [i for i, f in enumerate(flags) if f]
        if viol and not _is_etf(c):
            add(c, "refetch",
                f"{len(viol)}处涨跌幅越界(首处 {bars[viol[0] + 1][0]})")
            stats["refetch"] += 1
        elif viol:      # ETF：连续两处才判坏
            consec = any(b and a for a, b in zip(flags, flags[1:]))
            if consec:
                add(c, "refetch", "ETF连续越界跳变")
                stats["refetch"] += 1
        # ---- 3) 停牌缺口 / 零成交 ----
        d0 = datetime.strptime(bars[0][0], "%Y-%m-%d").date()
        d1 = datetime.strptime(bars[-1][0], "%Y-%m-%d").date()
        gaps = []
        for a, b in zip(bars, bars[1:]):
            da = datetime.strptime(a[0], "%Y-%m-%d").date()
            db_ = datetime.strptime(b[0], "%Y-%m-%d").date()
            if (db_ - da).days > 20:
                gaps.append(f"{a[0]}~{b[0]}")
        if gaps:
            add(c, "suspend", f"长缺口{len(gaps)}处: {gaps[:3]}")
            stats["suspicious"] += 1
        zv = sum(1 for b in bars if b[5] <= 0)
        if zv:
            stats["zero_vol"] += zv
        # ---- 4) 退市 ----
        age = (today - d1).days
        if age > 180:
            add(c, "delisted", f"最后bar {bars[-1][0]} (距今{age}天)")
            stats["delisted"] += 1
        # ---- 5) 价格粘性（连续≥20日收盘不变）----
        run = 1
        for (a, b) in zip(bars, bars[1:]):
            run = run + 1 if a[4] == b[4] and a[4] else 1
            if run >= 20:
                add(c, "stale", f"连续{run}日收盘不变(至 {b[0]})")
                stats["stale"] += 1
                break
    return issues, stats, names


def fix(conn, issues, log):
    """执行修复：删结构异常bar；越界/粘性代码整只重拉替换；退市入表。"""
    conn.execute("CREATE TABLE IF NOT EXISTS delisted("
                 "code TEXT PRIMARY KEY, last_date TEXT, ts REAL)")
    today = time.strftime("%Y-%m-%d")
    for c, items in issues.items():
        kinds = {k for k, _ in items}
        # 1) 删结构异常
        if "bad_bars" in kinds:
            bars = conn.execute(
                "SELECT date,open,high,low,close FROM daily_bars "
                "WHERE code=? ORDER BY date", (c,)).fetchall()
            bad = [(c, b[0]) for b in bars if not _bar_valid(b)]
            if bad:
                conn.executemany(
                    "DELETE FROM daily_bars WHERE code=? AND date=?", bad)
                log(f"  [删] {c} 结构异常{len(bad)}根")
        # 2) 整只重拉替换（除权残留/粘性）
        if ("refetch" in kinds or "stale" in kinds) and not c.startswith("bj"):
            try:
                fresh = _fetch_em_qfq(c)
                if len(fresh) >= 200:
                    conn.execute("DELETE FROM daily_bars WHERE code=?", (c,))
                    conn.executemany(
                        "INSERT OR REPLACE INTO daily_bars"
                        "(code,date,open,high,low,close,vol) "
                        "VALUES(?,?,?,?,?,?,?)",
                        [(c, *r) for r in fresh if r[0] < today])
                    log(f"  [换] {c} 重拉{len(fresh)}根(前复权全量替换)")
                else:
                    log(f"  [跳] {c} 重拉仅{len(fresh)}根，保守起见不动")
            except Exception as e:
                log(f"  [败] {c} 重拉失败: {e}")
        # 3) 退市登记
        dl = next((d for k, d in items if k == "delisted"), None)
        if dl:
            last = dl.split(" ")[0]
            conn.execute("INSERT OR REPLACE INTO delisted VALUES(?,?,?)",
                         (c, last, time.time()))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="执行修复(默认只报告)")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    lines = [f"# 数据清洗报告 {time.strftime('%Y-%m-%d %H:%M')}",
             f"库: `{args.db}`  模式: **{'修复' if args.fix else '只扫描'}**",
             ""]

    def log(s):
        print(s, flush=True)
        lines.append(s)

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    t0 = time.time()
    issues, stats, names = scan(conn)
    log(f"扫描完成({time.time() - t0:.0f}s)：{stats['codes']}只代码 "
        f"{stats['bars']}根日K")
    log(f"- 结构异常bar: {stats['bad_bars']}")
    log(f"- 涨跌幅越界(疑似除权残留,整只重拉): {stats['refetch']}只")
    log(f"- 长停牌缺口: {stats['suspicious']}只 | 零成交bar: {stats['zero_vol']}")
    log(f"- 疑似退市(>180天无数据): {stats['delisted']}只")
    log(f"- 价格粘性(冻结数据): {stats['stale']}只")
    log("")
    if issues:
        log("## 问题明细（按代码）")
        log("")
        log("| 代码 | 名称 | 问题 |")
        log("|---|---|---|")
        for c in sorted(issues):
            nm = names.get(c, "")
            for kind, detail in issues[c]:
                log(f"| {c} | {nm} | {kind}: {detail} |")
    else:
        log("未发现问题数据。")
    if args.fix:
        log("")
        log("## 修复动作")
        fix(conn, issues, log)
        log("修复完成。")
    conn.close()
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n报告已保存: {REPORT_PATH}")


if __name__ == "__main__":
    main()
