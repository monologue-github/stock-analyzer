#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_strategy_ablation.py - 全 A 股多算法策略自动消融选股（口径同 GUI run_ablation）

对每只股票：
  1. 取近 1000 交易日；
  2. 生成 6 种基础算法信号（MACD/KDJ/RSI/布林带/MA趋势/L1形态）+ 多维评分 × 3 档风险；
  3. 训练集（前 75%）选型，验证集（后 25%）只报告；
  4. 按保守/稳健/激进三档目标分别选出最优策略；
  5. 输出每只股票的结果 + 全市场聚合统计。

v2026-09-12
"""
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

import numpy as np

# 从 GUI/CLI 唯一算法源导入回测口径
import stock_gui as sg
from stock_gui import (
    db_conn, _is_etf,
    _sig_macd, _sig_kdj, _sig_rsi, _sig_boll, _sig_ma_trend, _sig_l1_pattern,
    _composite_signals,
    _bt_events, _bull_bear_score, _regime_map,
    ALGO_LABEL, CFG, get_daily
)

OUTPUT_DIR = "research"
PER_STOCK_FILE = os.path.join(OUTPUT_DIR, "strategy_ablation_per_stock.json")
SUMMARY_FILE = os.path.join(OUTPUT_DIR, "strategy_ablation_summary.json")

# 预加载指数 regime，全市场共享
_IDX_REGIME_CACHE = None


def _load_index_regime():
    """加载上证指数并计算牛熊 regime（收盘 >= MA120 为牛）。"""
    global _IDX_REGIME_CACHE
    if _IDX_REGIME_CACHE is not None:
        return _IDX_REGIME_CACHE
    try:
        idx_rows = get_daily("sh000001")
    except Exception as e:
        print(f"上证指数加载失败: {e}")
        idx_rows = []
    # _regime_map 返回 {date: bool}
    _IDX_REGIME_CACHE = _regime_map(idx_rows, len(idx_rows)) if idx_rows else {}
    return _IDX_REGIME_CACHE


def load_stocks(min_bars=400):
    """加载缓存中所有非 ETF、满足最低 bar 数的股票。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, date, open, high, low, close, vol "
            "FROM daily_bars ORDER BY code, date"
        ).fetchall()
    by = {}
    for c, d, o, h, l, cl, v in rows:
        by.setdefault(c, []).append(
            {"date": d, "open": o, "high": h, "low": l, "close": cl, "vol": v or 0.0}
        )
    return [(c, r) for c, r in by.items() if len(r) >= min_bars and not _is_etf(c)]


def _trim_rows(rows, max_bars=1000):
    """截断到最近 max_bars 根，并过滤无效 close。"""
    rows = [r for r in rows if r.get("close") and r["close"] > 0]
    if len(rows) > max_bars:
        rows = rows[-max_bars:]
    return rows


def _pick_candidates(cands, key):
    """按保守/稳健/激进目标从候选中选出最优。"""
    pool = [c for c in cands if c["train"]["trades"] >= 3]
    if not pool:
        pool = cands
    if not pool:
        return None
    if key == "保守":
        pool.sort(key=lambda c: (c["train"]["mdd"], -c["train"]["winrate"]))
    elif key == "激进":
        pool.sort(key=lambda c: -c["train"]["ann"])
    else:  # 稳健：收益回撤比
        pool.sort(key=lambda c: -(c["train"]["ann"] /
                                   max(abs(c["train"]["mdd"]), 0.05)))
    return dict(pool[0])


def run_ablation_for_stock(args):
    """对单只股票跑完整消融。多进程 worker。"""
    code, rows, regime = args
    rows = _trim_rows(rows)
    n = len(rows)
    if n < 200:
        return None
    val_n = max(200, n // 4)
    split = n - val_n

    # 各基础算法信号发生器
    gens = {
        "macd": lambda: _sig_macd(rows),
        "kdj": lambda: _sig_kdj(rows),
        "rsi": lambda: _sig_rsi(rows),
        "boll": lambda: _sig_boll(rows),
        "ma_trend": lambda: _sig_ma_trend(rows),
        "l1_pattern": lambda: _sig_l1_pattern(rows),
    }

    cands = []
    for algo, gen in gens.items():
        try:
            sigs = gen()
        except Exception:
            continue
        if not sigs:
            continue
        for mode, rp in CFG.RISK_PARAMS.items():
            tr = _bt_events(rows, sigs, rp, 0, split)
            va = _bt_events(rows, sigs, rp, split, n)
            if not tr:
                continue
            bull, bear = _bull_bear_score(rows, tr["curve"], tr["i0"], regime)
            cands.append({
                "algo": algo,
                "mode": mode,
                "params": dict(rp),
                "label": f"{ALGO_LABEL.get(algo, algo)}·{mode}",
                "train": {k: v for k, v in tr.items() if k != "curve"},
                "val": ({k: v for k, v in va.items() if k != "curve"} if va else None),
                "bull": bull,
                "bear": bear,
            })

    # 多维评分 × 3 档风险
    for mode, rp in CFG.RISK_PARAMS.items():
        try:
            sigs = _composite_signals(rows, rp, idx_chg_by_date=None)
        except Exception:
            continue
        if not sigs:
            continue
        tr = _bt_events(rows, sigs, rp, 0, split)
        va = _bt_events(rows, sigs, rp, split, n)
        if not tr:
            continue
        bull, bear = _bull_bear_score(rows, tr["curve"], tr["i0"], regime)
        cands.append({
            "algo": "composite",
            "mode": mode,
            "params": dict(rp),
            "label": f"多维评分·{mode}",
            "train": {k: v for k, v in tr.items() if k != "curve"},
            "val": ({k: v for k, v in va.items() if k != "curve"} if va else None),
            "bull": bull,
            "bear": bear,
        })

    if not cands:
        return None

    out = {
        "code": code,
        "bars": n,
        "train_n": split,
        "val_n": n - split,
        "mode_candidates": {
            "保守": _pick_candidates(cands, "保守"),
            "稳健": _pick_candidates(cands, "稳健"),
            "激进": _pick_candidates(cands, "激进"),
        },
        "all_candidates": cands,
    }
    return out


def _median_or_none(vals):
    vals = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return float(np.median(vals)) if vals else None


def _mean_or_none(vals):
    vals = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return float(np.mean(vals)) if vals else None


def _pct_or_none(count, total):
    return round(count / total * 100, 2) if total else None


def build_summary(per_stock):
    """由每只股票结果构建聚合统计。"""
    total = len(per_stock)
    if total == 0:
        return {}

    modes = ["保守", "稳健", "激进"]
    summary = {
        "total_stocks": total,
        "timestamp": datetime.now().isoformat(),
        "modes": {},
    }

    for mode in modes:
        selections = [s["mode_candidates"][mode] for s in per_stock
                      if s and s["mode_candidates"].get(mode)]
        n_sel = len(selections)
        if n_sel == 0:
            summary["modes"][mode] = {"count": 0}
            continue

        # 算法分布
        algo_counts = {}
        for c in selections:
            algo_counts[c["algo"]] = algo_counts.get(c["algo"], 0) + 1

        train_ann = [c["train"]["ann"] for c in selections]
        train_mdd = [c["train"]["mdd"] for c in selections]
        train_win = [c["train"]["winrate"] for c in selections]
        train_trades = [c["train"]["trades"] for c in selections]
        val_ann = [c["val"]["ann"] for c in selections if c.get("val")]
        val_mdd = [c["val"]["mdd"] for c in selections if c.get("val")]
        val_win = [c["val"]["winrate"] for c in selections if c.get("val")]
        val_trades = [c["val"]["trades"] for c in selections if c.get("val")]
        bull = [c["bull"] for c in selections if c.get("bull") is not None]
        bear = [c["bear"] for c in selections if c.get("bear") is not None]

        summary["modes"][mode] = {
            "count": n_sel,
            "algo_distribution": {k: {"count": v, "pct": round(v / n_sel * 100, 2)}
                                  for k, v in algo_counts.items()},
            "train": {
                "ann_median": _median_or_none(train_ann),
                "ann_mean": _mean_or_none(train_ann),
                "mdd_median": _median_or_none(train_mdd),
                "winrate_median": _median_or_none(train_win),
                "trades_median": _median_or_none(train_trades),
            },
            "val": {
                "ann_median": _median_or_none(val_ann),
                "ann_mean": _mean_or_none(val_ann),
                "mdd_median": _median_or_none(val_mdd),
                "winrate_median": _median_or_none(val_win),
                "trades_median": _median_or_none(val_trades),
            },
            "regime": {
                "bull_ann_median": _median_or_none(bull),
                "bear_ann_median": _median_or_none(bear),
            },
        }
    return summary


def main(limit=None, max_workers=None):
    t0 = time.time()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("加载上证指数 regime...")
    regime = _load_index_regime()
    print(f"  regime 日期数: {len(regime)}")

    print("加载全缓存股票...")
    stocks = load_stocks(min_bars=400)
    if limit:
        stocks = stocks[:limit]
    print(f"  股票数: {len(stocks)}")

    if max_workers is None:
        max_workers = max(1, min(os.cpu_count() or 4, 8))

    print(f"开始消融（workers={max_workers}），每只股票自动产生三档策略...")
    per_stock = []
    done = 0
    skipped = 0

    args_list = [(code, rows, regime) for code, rows in stocks]
    with ProcessPoolExecutor(max_workers=max_workers) as exe:
        futures = {exe.submit(run_ablation_for_stock, a): a[0] for a in args_list}
        for fut in as_completed(futures):
            code = futures[fut]
            done += 1
            try:
                res = fut.result()
            except Exception as e:
                print(f"[{done}/{len(stocks)}] {code} 异常: {e}")
                skipped += 1
                continue
            if res is None:
                skipped += 1
                if done % 500 == 0:
                    print(f"[{done}/{len(stocks)}] {code} 无有效候选...")
                continue
            per_stock.append(res)
            if done % 500 == 0:
                print(f"[{done}/{len(stocks)}] {code} 完成，累计有效 {len(per_stock)}")

    print(f"\n消融完成: 总股票 {done}, 有效 {len(per_stock)}, 跳过 {skipped}, 耗时 {time.time()-t0:.0f}s")

    # 第一层输出：每只股票
    print(f"写入 {PER_STOCK_FILE} ...")
    with open(PER_STOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(per_stock, f, ensure_ascii=False, indent=1)

    # 第二层输出：聚合统计
    print(f"构建并写入 {SUMMARY_FILE} ...")
    summary = build_summary(per_stock)
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    print("\n=== 聚合摘要 ===")
    for mode, data in summary.get("modes", {}).items():
        print(f"\n【{mode}】选中 {data['count']} 只")
        print(f"  算法分布: {data.get('algo_distribution', {})}")
        t = data.get("train", {})
        v = data.get("val", {})
        print(f"  训练集: 年化中位={t.get('ann_median'):.1%} 回撤中位={t.get('mdd_median'):.1%} 胜率中位={t.get('winrate_median'):.1%} 交易中位={t.get('trades_median')}")
        print(f"  验证集: 年化中位={v.get('ann_median'):.1%} 回撤中位={v.get('mdd_median'):.1%} 胜率中位={v.get('winrate_median'):.1%} 交易中位={v.get('trades_median')}")

    print(f"\n全部完成，总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 只股票（测试用）")
    parser.add_argument("--workers", type=int, default=None, help="并行进程数")
    args = parser.parse_args()
    main(limit=args.limit, max_workers=args.workers)
