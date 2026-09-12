#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_clean.py - 股票日K缓存数据清洗/复权口径迁移（独立脚本，直接改库）

背景（2026-09 全库体检结论）：
  东财/腾讯的"前复权"使用除权公式，现金分红做减法。长期高分红股
  历史前复权价会趋近 0 甚至为负（如潞安环能 2020-02 收盘 0.088 元），
  导致全库 961 只股票出现 3791 处假跳变（|单日涨跌|>21%，个别 +241%/+300%）。
  收益率、形态匹配、标签全部被污染。

修复口径：
  库内统一存【后复权 hfq】（乘法、恒正、收益率正确）；
  另外维护 adjust(code->K) 表：K=最新不复权价/后复权末价，
  读取层用 hfq*K 还原为"乘法前复权"用于展示（收益率不变）。

用法：
  python data_clean.py                  # 只扫描报告（不改库）
  python data_clean.py --fix            # 扫描+修复异常代码（hfq 口径）
  python data_clean.py --all-adj        # 全库复权口径迁移（hfq+adjust，断点续传）
  python data_clean.py --all-adj --workers 3 --limit 500
  python data_clean.py --db x.db
"""
import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import date, datetime

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "stock_cache.db")
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "清洗报告_" + time.strftime("%Y%m%d") + ".md")
EM_HOSTS = ("push2his.eastmoney.com", "92.push2his.eastmoney.com",
            "93.push2his.eastmoney.com", "97.push2his.eastmoney.com")
TX_HOSTS = ("https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
            "http://ifzq.gtimg.cn/appstock/app/fqkline/get")

_rate_lock = threading.Lock()
_last_req = [0.0]
MIN_INTERVAL = 0.25


def _throttle():
    with _rate_lock:
        dt = time.time() - _last_req[0]
        if dt < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - dt)
        _last_req[0] = time.time()


def _get(url, decode="utf-8", retries=3, timeout=20, headers=None):
    last = None
    for i in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(
                url, headers=headers or {
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://quote.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(decode, "ignore")
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def _limit_pct(code, name, d):
    """该股当日涨跌幅限制(%)，None=不限（与 stock_gui._limit_pct 同规则）。"""
    if d < "1996-12-16":
        return None
    if code.startswith("bj"):
        return 30.0
    board = code[2:4] if len(code) >= 4 else ""
    if board == "68":
        return 20.0
    if board == "30":
        return 20.0 if d >= "2020-08-24" else 10.0
    if name and "ST" in name.upper() and d < "2026-07-06":
        return 5.0
    return 10.0


def _is_etf(code):
    pre = code[2:4] if len(code) >= 4 else ""
    return pre in ("51", "56", "58", "15", "16", "18")


def _secid(full):
    return ("1." if full.startswith("sh") else "0.") + full[2:]


def _parse_em(kd):
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


def _fetch_em_kline(full, fqt=2, count=8000):
    """东财日K。fqt=2 后复权（迁移主源），fqt=0 不复权，fqt=1 勿用。"""
    for host in EM_HOSTS:
        try:
            txt = _get(
                f"https://{host}/api/qt/stock/kline/get"
                f"?secid={_secid(full)}&fields1=f1,f2,f3"
                f"&fields2=f51,f52,f53,f54,f55,f56"
                f"&klt=101&fqt={fqt}&beg=0&end=20500101&lmt={count}")
            out = _parse_em(json.loads(txt))
            if out:
                return out
        except Exception:
            continue
    return []


_TX_RAW = {}


def _parse_qt(qt):
    """腾讯快照 qt → 最新原始价（不复权）。"""
    if not isinstance(qt, dict):
        return None
    for v in qt.values():
        if isinstance(v, (list, tuple)) and len(v) > 3:
            try:
                p = float(v[3])
                if p > 0:
                    return p
            except (TypeError, ValueError):
                continue
    return None


def _fetch_tx_kline(full, fq="hfq", pages=3, page=800):
    """腾讯日K（后复权，翻页补历史），fq='' 为不复权。"""
    out, end, have = [], "", set()
    key = {"hfq": "hfqday", "qfq": "qfqday"}.get(fq, "day")
    for _ in range(pages):
        param = (f"?param={full},day,,{end},{page},{fq}" if (end and fq)
                 else f"?param={full},day,,,{page},{fq}" if fq
                 else f"?param={full},day,,,{page}")
        got = None
        for host in TX_HOSTS:
            try:
                txt = _get(host + param, retries=1, timeout=10)
                d = (json.loads(txt).get("data") or {}).get(full) or {}
                p = _parse_qt(d.get("qt"))
                if p:
                    _TX_RAW[full] = p
                bars = d.get(key) or d.get("day") or []
                if bars:
                    got = bars
                    break
            except Exception:
                continue
        if not got:
            break
        rows = []
        for b in got:
            try:
                if float(b[2]) <= 0:
                    continue
                rows.append((b[0], float(b[1]), float(b[3]), float(b[4]),
                             float(b[2]), float(b[5])))
            except (ValueError, IndexError):
                continue
        add = [r for r in rows if r[0] not in have]
        if not add:
            break
        out = add + out
        have.update(r[0] for r in add)
        if len(got) < page - 10:
            break
        try:
            d0 = datetime.strptime(out[0][0], "%Y-%m-%d").date()
            end = (d0 - __import__("datetime").timedelta(days=1)).isoformat()
        except Exception:
            break
    return out


def fetch_raw_last(full):
    """最新不复权价：腾讯快照(qt) → 东财 fqt=0 → 腾讯不复权K线。"""
    if _TX_RAW.get(full):
        return _TX_RAW[full]
    try:
        rows = _fetch_em_kline(full, fqt=0, count=5)
        if rows:
            return rows[-1][4]
    except Exception:
        pass
    try:
        rows = _fetch_tx_kline(full, fq="", pages=1, page=5)
        if rows:
            return rows[-1][4]
    except Exception:
        pass
    try:                    # hfq 请求响应里也带 qt 快照
        _fetch_tx_kline(full, fq="hfq", pages=1, page=5)
    except Exception:
        pass
    return _TX_RAW.get(full)


def _bar_valid(b):
    o, h, l, cl = b[1], b[2], b[3], b[4]
    if None in (o, h, l, cl) or min(o, h, l, cl) <= 0:
        return False
    if h < l or h < max(o, cl) or l > min(o, cl):
        return False
    return True


def _validate(rows, code, name):
    """hfq 序列健康检查：结构合法 + 除前5根新股外无涨跌停越界。"""
    if len(rows) < 100:
        return False, len(rows), "历史不足100根"
    viol = 0
    for i in range(max(5, 1), len(rows)):
        prev = rows[i - 1]
        cur = rows[i]
        lim = _limit_pct(code, name, cur[0])
        if lim is None or not prev[4] or not cur[4]:
            continue
        if not _bar_valid(cur):
            viol += 1
            continue
        if abs(cur[4] / prev[4] - 1) * 100 > lim + 3.0:
            viol += 1
    return viol <= 3, viol, ""


def ensure_tables(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS adjust("
                 "code TEXT PRIMARY KEY, k REAL, ts REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS adj_done("
                 "code TEXT PRIMARY KEY, ts REAL, bars INTEGER, last_date TEXT)")


def set_adjust(conn, code, k):
    if k and k > 0:
        conn.execute("INSERT OR REPLACE INTO adjust VALUES(?,?,?)",
                     (code, float(k), time.time()))


def migrate_one(conn, full, name, log=None, force=False, min_bars=100):
    """把单只代码迁移为后复权(hfq)存储 + 写入显示缩放 K。返回状态串。"""
    row = conn.execute("SELECT ts FROM adj_done WHERE code=?",
                       (full,)).fetchone()
    if row and not force:
        return "skip"
    rows = _fetch_em_kline(full, fqt=2)
    src = "em"
    if len(rows) < min_bars:
        tx = _fetch_tx_kline(full, fq="hfq", pages=6)
        if len(tx) > len(rows):
            rows, src = tx, "tx"
    if len(rows) < min_bars:
        return "nodata"
    ok, viol, why = _validate(rows, full, name)
    if not ok:
        return f"reject({why or 'viol=%d' % viol})"
    raw = fetch_raw_last(full)
    k = (raw / rows[-1][4]) if (raw and rows[-1][4] > 0) else None
    today = time.strftime("%Y-%m-%d")
    bars = [r for r in rows if r[0] < today]
    if not bars:
        return "nodata"
    conn.execute("DELETE FROM daily_bars WHERE code=?", (full,))
    conn.executemany(
        "INSERT OR REPLACE INTO daily_bars"
        "(code,date,open,high,low,close,vol) VALUES(?,?,?,?,?,?,?)",
        [(full, r[0], r[1], r[2], r[3], r[4], r[5]) for r in bars])
    if k:
        set_adjust(conn, full, k)
    conn.execute("INSERT OR REPLACE INTO adj_done VALUES(?,?,?,?)",
                 (full, time.time(), len(bars), bars[-1][0]))
    conn.commit()
    if log:
        ks = f"K={k:.4f}" if k else "K=?"
        log(f"  [{src}] {full} {len(bars)}根 "
            f"{bars[0][0]}~{bars[-1][0]} {ks}")
    return "ok"


def migrate_all(db, workers=2, limit=None, force=False, log=print,
                min_bars=100):
    """全库迁移（可由多进程/多机分片：--shard i/n）。"""
    conn = sqlite3.connect(db, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_tables(conn)
    names = {r[0]: (r[1] or "") for r in
             conn.execute("SELECT code, name FROM stocks").fetchall()}
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM daily_bars GROUP BY code HAVING COUNT(*)>=? "
        "ORDER BY code", (min_bars,)).fetchall()
        if not r[0].startswith("bj")]
    if limit:
        codes = codes[:limit]
    todo = [c for c in codes
            if force or not conn.execute(
                "SELECT 1 FROM adj_done WHERE code=?", (c,)).fetchone()]
    log(f"待迁移 {len(todo)}/{len(codes)} 只（workers={workers}）")
    lock = threading.Lock()
    done = [0]

    def one(c):
        lconn = sqlite3.connect(db, timeout=60)
        lconn.execute("PRAGMA journal_mode=WAL")
        try:
            st = migrate_one(lconn, c, names.get(c, ""), force=force,
                             min_bars=min_bars)
        except Exception as e:
            st = f"err({str(e)[:60]})"
        finally:
            lconn.close()
        with lock:
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == len(todo):
                log(f"  迁移 {done[0]}/{len(todo)} ({done[0]*100//max(1,len(todo))}%)")
        if st.startswith(("err", "reject")):
            with lock:
                bad.append((c, st))

    bad = []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))
    log(f"迁移完成：成功{len(todo)-len(bad)} 异常{len(bad)}")
    for c, st in bad[:40]:
        log(f"  ! {c} {st}")
    conn.close()
    return bad


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
             "zero_vol": 0, "stale": 0, "low_price": 0,
             "neg_price": 0, "stale_vs_market": 0}
    market_last = max((b[-1][0] for b in by.values()), default="")

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
            stats["neg_price"] += sum(
                1 for b in bad if min(b[1], b[2], b[3], b[4]) < 0)
        # ---- 2) 涨跌幅越界（除权公式前复权残留/坏数据）----
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
        elif viol:
            consec = any(b and a for a, b in zip(flags, flags[1:]))
            if consec:
                add(c, "refetch", "ETF连续越界跳变")
                stats["refetch"] += 1
        # ---- 2b) 价格失真（前复权做减法导致末价过低）----
        if bars[-1][4] is not None and bars[-1][4] < 0.5:
            add(c, "low_price", f"末价 {bars[-1][4]:.3f} 失真")
            stats["low_price"] += 1
        # ---- 3) 停牌缺口 / 零成交 ----
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
        d1 = datetime.strptime(bars[-1][0], "%Y-%m-%d").date()
        age = (today - d1).days
        if age > 180:
            add(c, "delisted", f"最后bar {bars[-1][0]} (距今{age}天)")
            stats["delisted"] += 1
        # ---- 4b) 相对市场最新日陈旧（回填中断/漏拉）----
        elif market_last and bars[-1][0] < market_last:
            dl = (datetime.strptime(market_last, "%Y-%m-%d").date()
                  - d1).days
            if dl > 10:
                add(c, "stale_vs_market",
                    f"最后bar {bars[-1][0]}，落后市场 {dl} 天")
                stats["stale_vs_market"] += 1
        # ---- 5) 价格粘性（连续≥20日收盘不变）----
        run = 1
        for (a, b) in zip(bars, bars[1:]):
            run = run + 1 if a[4] == b[4] and a[4] else 1
            if run >= 20:
                add(c, "stale", f"连续{run}日收盘不变(至 {b[0]})")
                stats["stale"] += 1
                break
    return issues, stats, names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="执行修复(默认只报告)")
    ap.add_argument("--all-adj", action="store_true",
                    help="全库复权口径迁移(存hfq+adjust, 断点续传)")
    ap.add_argument("--workers", type=int, default=2,
                    help="--all-adj 并发数（免费源限流，建议2~3）")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    lines = [f"# 数据清洗报告 {time.strftime('%Y-%m-%d %H:%M')}",
             f"库: `{args.db}`  模式: "
             f"**{'迁移' if args.all_adj else ('修复' if args.fix else '只扫描')}**",
             ""]

    def log(s):
        print(s, flush=True)
        lines.append(s)

    if args.all_adj:
        bad = migrate_all(args.db, workers=args.workers, limit=args.limit,
                          force=args.force, log=log)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\n报告已保存: {REPORT_PATH}")
        return

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    t0 = time.time()
    issues, stats, names = scan(conn)
    log(f"扫描完成({time.time() - t0:.0f}s)：{stats['codes']}只代码 "
        f"{stats['bars']}根日K")
    for k, label in (("bad_bars", "结构异常bar"), ("refetch",
                     "涨跌幅越界(疑似前复权失真,需迁移)"),
                     ("low_price", "末价<0.5元"), ("neg_price", "负价bar"),
                     ("suspicious", "长停牌缺口"), ("zero_vol", "零成交bar"),
                     ("delisted", "疑似退市(>180天)"),
                     ("stale_vs_market", "落后市场最新日>10天"),
                     ("stale", "价格粘性")):
        log(f"- {label}: {stats[k]}")
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
        log("## 修复动作（整只按 hfq 迁移）")
        conn.execute("CREATE TABLE IF NOT EXISTS delisted("
                     "code TEXT PRIMARY KEY, last_date TEXT, ts REAL)")
        ensure_tables(conn)
        fixed = 0
        for c, items in issues.items():
            kinds = {k for k, _ in items}
            if kinds & {"bad_bars", "refetch", "low_price", "stale",
                        "stale_vs_market"} and not c.startswith("bj"):
                st = migrate_one(conn, c, names.get(c, ""), log=log)
                if st == "ok":
                    fixed += 1
            dl = next((d.split(" ")[0] for k, d in items
                       if k == "delisted"), None)
            if dl:
                conn.execute("INSERT OR REPLACE INTO delisted VALUES(?,?,?)",
                             (c, dl, time.time()))
        conn.commit()
        log(f"修复完成：成功 {fixed} 只")
    conn.close()
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n报告已保存: {REPORT_PATH}")


if __name__ == "__main__":
    main()
