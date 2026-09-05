#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_full.py - 全A股日K批量回填（目标≥1000交易日）

复用 stock_gui.py 内嵌缓存层（同一 stock_cache.db / 同一套多源HTTP）。
- 主源：东财 push2his（一次请求拉全量前复权日K，lmt=1100）
- 备源：腾讯 ifzq fqkline
- 断点续传：本地已有 ≥950 根且最新日期够新的代码直接跳过
- 并发 8 线程 + 全局限流（_http_get 内置 0.16s 间隔），进度每20只打印
- 代码表缺失时自动刷新全市场代码表（含市值分层，GUI直接受益）

用法：
  python backfill_full.py            # 全量回填（断点续传）
  python backfill_full.py --limit 50 # 只回填50只（测试）
  python backfill_full.py --force    # 忽略断点，全部重拉
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import stock_gui as sg  # noqa: E402  （内嵌缓存层：DB/多源HTTP/代码表刷新）

MIN_BARS = 950           # 断点续传门槛（目标1000根，留余量）
FRESH_DAYS = 6           # 最新bar距今超过6个自然日视为过期（吸收长假）
BAR_COUNT = 1100         # 单次请求根数（东财支持，约4.5年）


def _fresh_date():
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=FRESH_DAYS)
            ).isoformat()


def _local_progress():
    """{code: (bars, last_date)}，一次查询。"""
    with sg.db_conn() as conn:
        rows = conn.execute(
            "SELECT code, COUNT(*), MAX(date) FROM daily_bars "
            "GROUP BY code").fetchall()
    return {r[0]: (r[1], r[2] or "") for r in rows}


# 腾讯三域名（配额按 域名×IP 组合计，轮换可延长连续批量请求寿命）
_TX_HOSTS = [
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
]
_tx_i = [0]


def _tx_fetch(full, end, count):
    """腾讯K线单页：三域名轮换，全部501才抛异常（触发上层全局暂停）。"""
    param = f"?param={full},day,,{end},{count},qfq" if end \
        else f"?param={full},day,,,{count},qfq"
    last = None
    for k in range(len(_TX_HOSTS)):
        u = _TX_HOSTS[(_tx_i[0] + k) % len(_TX_HOSTS)]
        try:
            txt = sg._http_get(u + param, decode="utf-8", retries=1,
                               timeout=8)
            _tx_i[0] = (_tx_i[0] + k + 1) % len(_TX_HOSTS)
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


def _fetch_one(full):
    """单只代码拉取：腾讯翻页为主源（单次上限800根，两页1600根≥目标）。
    返回 rows 或抛异常。"""
    rows1 = _tx_fetch(full, "", 800)
    if not rows1:
        raise RuntimeError("腾讯空数据")
    if len(rows1) >= 790:               # 触顶 → 翻第二页补历史
        import datetime
        d0 = datetime.date.fromisoformat(rows1[0]["date"])
        end = (d0 - datetime.timedelta(days=1)).isoformat()
        try:
            rows2 = _tx_fetch(full, end, 800)
            have = {r["date"] for r in rows1}
            rows1 = [r for r in rows2 if r["date"] not in have] + rows1
        except Exception:
            pass                        # 第二页失败就用第一页（≥800根）
    if len(rows1) >= 300:
        return rows1
    try:
        rows = sg._fetch_eastmoney(full, count=BAR_COUNT)
        if len(rows) >= 300:
            return rows
    except Exception:
        pass
    raise RuntimeError("有效数据不足300根")


def _store(full, rows):
    today = time.strftime("%Y-%m-%d")
    data = [r for r in rows if r["date"] < today and sg._bar_ok(r)]
    if not data:
        raise RuntimeError("过滤后无有效数据")
    with sg.db_conn(commit=True) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_bars"
            "(code,date,open,high,low,close,vol) VALUES(?,?,?,?,?,?,?)",
            [(full, r["date"], r["open"], r["high"], r["low"],
              r["close"], r["vol"]) for r in data])
    return len(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前N只(测试)")
    ap.add_argument("--force", action="store_true", help="忽略断点全部重拉")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--throttle", type=float, default=0.45,
                    help="全局请求最小间隔秒(防501限流)")
    args = ap.parse_args()

    sg._MIN_INTERVAL = args.throttle     # 批量模式放缓全局节流
    # 探测可用腾讯域，排到轮换队列最前（主域可能被批量限流501）
    for k, u in enumerate(list(_TX_HOSTS)):
        if sg._probe_kline_url(u):
            if k:
                _TX_HOSTS[0], _TX_HOSTS[k] = u, _TX_HOSTS[0]
            print(f"腾讯源就绪: {u.split('/')[2]}")
            break
    else:
        print("警告: 所有腾讯域探测失败，仍尝试轮换")

    fresh = _fresh_date()
    # 1) 代码表
    with sg.db_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    if n < 1000:
        print(f"代码表仅{n}只，先刷新全市场代码表...")
        sg.refresh_all_codes(progress=lambda s: print("  " + s, flush=True))
    with sg.db_conn() as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM stocks WHERE code NOT LIKE 'bj%' "
            "ORDER BY code").fetchall()]
    if args.limit:
        codes = codes[:args.limit]
    print(f"代码表 {len(codes)} 只，目标每只≥{MIN_BARS}根日K")

    # 2) 断点续传过滤
    have = {} if args.force else _local_progress()
    todo = [c for c in codes
            if not (have.get(c, (0, ""))[0] >= MIN_BARS
                    and have.get(c, (0, ""))[1] >= fresh)]
    print(f"本地已达标 {len(codes) - len(todo)} 只，待回填 {len(todo)} 只")
    if not todo:
        print("全部已缓存，无需回填")
        return

    # 3) 并发回填（501/429限流 → 全局暂停，时长递增：2分钟→5分钟）
    stat = {"ok": 0, "skip": 0, "fail": 0}
    t0 = time.time()
    failed = []
    pause_until = [0.0]
    pause_level = [0]

    def work(c):
        if time.time() < pause_until[0]:
            time.sleep(pause_until[0] - time.time())
        try:
            n = _store(c, _fetch_one(c))
            pause_level[0] = 0
            stat["ok"] += 1
            return ("ok", c, n)
        except Exception as e:
            msg = str(e)
            if "501" in msg or "429" in msg or "503" in msg:
                pause_level[0] = min(pause_level[0] + 1, 4)
                wait = 120 if pause_level[0] < 3 else 300
                pause_until[0] = max(pause_until[0], time.time() + wait)
                print(f"  [限流] 全局暂停{wait}s (第{pause_level[0]}次)",
                      flush=True)
            stat["fail"] += 1
            return ("fail", c, msg[:80])

    done_n = [0]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for kind, c, info in ex.map(work, todo):
            done_n[0] += 1
            if kind == "fail":
                failed.append((c, info))
                print(f"  [失败] {c}: {info}", flush=True)
            if done_n[0] % 20 == 0 or done_n[0] == len(todo):
                el = time.time() - t0
                eta = el / done_n[0] * (len(todo) - done_n[0])
                print(f"回填 {done_n[0]}/{len(todo)} "
                      f"({done_n[0] * 100 // len(todo)}%) "
                      f"成功{stat['ok']} 失败{stat['fail']} "
                      f"耗时{el:.0f}s ETA {eta:.0f}s", flush=True)

    # 4) 失败重试一轮（换源概率）
    if failed:
        print(f"重试 {len(failed)} 只失败的代码...")
        retry = [c for c, _ in failed]
        stat["fail"] = 0
        for c in retry:
            try:
                _store(c, _fetch_one(c))
                stat["ok"] += 1
            except Exception as e:
                stat["fail"] += 1
                print(f"  [仍失败] {c}: {str(e)[:80]}", flush=True)

    # 5) 汇总
    have = _local_progress()
    total_bars = sum(v[0] for v in have.values())
    deep = sum(1 for v in have.values() if v[0] >= MIN_BARS)
    print("=" * 48)
    print(f"完成：本次成功{stat['ok']} 失败{stat['fail']}")
    print(f"库内：{len(have)}只代码 共{total_bars}根日K "
          f"(≥{MIN_BARS}根的 {deep} 只)")
    print(f"总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
