#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_ablation.py - 匹配特征消融回测（纯本地缓存，无网络）

协议（与主程序 L1 匹配完全同构）：
  - 在历史每个评估日 t（walk-forward），只用 t 及以前的数据：
    cur = znorm(rets[t+1-W : t+1])，样本窗口 j ∈ [W, t+1-W)（与当前窗口不重叠），
    标签 = close[j+1]/close[j]-1（j+1 ≤ t，无前视），
    预测 = 加权分位P50，实际 = close[t+1]/close[t]-1。
  - 每个配置只改一组开关/权重，其余不动 → 纯净消融。

指标：方向命中率 / Spearman IC / MAE / 样本数。

v2026-09-11: 全缓存 + 步长1 版本，numpy 向量化加速。
"""
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

import stock_gui as sg
from stock_gui import (znorm, logret, wpct, rsi_at, vola_at, candle_feats,
                       volchg_at, weekly_ctx, vol_ratio_at, _is_etf, CFG)

W = CFG.W_WINDOW
TOPK = CFG.TOPK
MIN_GAP = max(3, W // 2)
WEEKLY_N = CFG.WEEKLY_N

# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_stocks(min_bars=400, top_n=None):
    """加载缓存中所有非 ETF 股票。top_n=None 表示全量。"""
    with sg.db_conn() as conn:
        rows = conn.execute(
            "SELECT code, date, open, high, low, close, vol "
            "FROM daily_bars ORDER BY code, date").fetchall()
    by = {}
    for c, d, o, h, l, cl, v in rows:
        by.setdefault(c, []).append(
            {"date": d, "open": o, "high": h, "low": l, "close": cl,
             "vol": v or 0.0})
    cands = [(c, r) for c, r in by.items() if len(r) >= min_bars
             and not _is_etf(c)]
    cands.sort(key=lambda cr: -len(cr[1]))
    if top_n:
        cands = cands[:top_n]
    return cands


# ---------------------------------------------------------------------------
# numpy 预计算
# ---------------------------------------------------------------------------

def _znorm_seq(rets, w):
    """返回 (n+1, w) 的 znorm 窗口矩阵，第 i 行是 rets[i-w:i] 的 znorm。
    不足 w 的行用 NaN 填充。"""
    n = len(rets)
    out = np.full((n + 1, w), np.nan, dtype=np.float64)
    for i in range(w, n + 1):
        win = np.array(rets[i - w:i], dtype=np.float64)
        m = win.mean()
        sd = win.std() or 1e-12
        out[i] = (win - m) / sd
    return out


def precompute(rows):
    """对单只股票预计算所有向量化特征。"""
    n = len(rows)
    closes = np.array([r["close"] for r in rows], dtype=np.float64)
    vols = np.array([r["vol"] for r in rows], dtype=np.float64)

    rets = np.array(logret(closes.tolist(), is_etf=False), dtype=np.float64)
    # znorm 窗口矩阵
    zmat = _znorm_seq(rets, W)

    # 扩展特征
    struct = np.full((n, 4), np.nan, dtype=np.float64)
    vola = np.full(n, np.nan, dtype=np.float64)
    rsi = np.full(n, np.nan, dtype=np.float64)
    volchg = np.full(n, np.nan, dtype=np.float64)
    weekly = np.full((n, WEEKLY_N), np.nan, dtype=np.float64)
    vr = np.full(n, np.nan, dtype=np.float64)

    for i in range(n):
        cs = candle_feats(rows, i)
        if cs is not None:
            struct[i] = cs
        v = vola_at(rets.tolist(), i)
        if v is not None:
            vola[i] = v
        r = rsi_at(closes.tolist(), i)
        if r is not None:
            rsi[i] = r
        vc = volchg_at(vols.tolist(), i)
        if vc is not None:
            volchg[i] = vc
        wc = weekly_ctx(rows, i, WEEKLY_N)
        if wc is not None:
            weekly[i] = wc
        vr[i] = vol_ratio_at(vols.tolist(), i) or 0.0

    # 标签：close[j+1]/close[j]-1，长度 n-1
    labels = closes[1:] / closes[:-1] - 1.0

    return {
        "n": n,
        "closes": closes,
        "rets": rets,
        "zmat": zmat,
        "struct": struct,
        "vola": vola,
        "rsi": rsi,
        "volchg": volchg,
        "weekly": weekly,
        "vr": vr,
        "labels": labels,
    }


# ---------------------------------------------------------------------------
# 向量化预测
# ---------------------------------------------------------------------------

def predict_all(f, wts, use_decay=True, use_simw=True):
    """对单只股票所有有效 t 一次性批量出预测。
    返回 (preds, ups, acts, mask)，mask 表示哪些 t 有效。
    """
    n = f["n"]
    zmat = f["zmat"]          # (n+1, W)
    struct = f["struct"]      # (n, 4)
    vola = f["vola"]          # (n,)
    rsi = f["rsi"]            # (n,)
    volchg = f["volchg"]      # (n,)
    weekly = f["weekly"]      # (n, WEEKLY_N)
    vr = f["vr"]              # (n,)
    labels = f["labels"]      # (n-1,)
    closes = f["closes"]      # (n,)

    # 有效 t 范围：[2W+2, n-2]，因为需要 t+1 ≤ n-1 才能计算 act
    t_min = max(2 * W + 2, W)
    t_max = n - 2
    if t_max < t_min:
        return np.array([]), np.array([]), np.array([]), np.array([], dtype=bool)

    ts = np.arange(t_min, t_max + 1)
    nt = len(ts)

    # 基础 mask：j 范围 [W, t+1-W) 非空
    max_j = ts + 1 - W  # 不包含
    n_valid = np.maximum(0, max_j - W)

    preds = np.full(nt, np.nan, dtype=np.float64)
    ups = np.full(nt, np.nan, dtype=np.float64)

    for idx, t in enumerate(ts):
        if n_valid[idx] < TOPK:
            continue
        js = np.arange(W, t + 1 - W)

        # 形态距离
        cur = zmat[t + 1]  # (W,)
        hist = zmat[js]    # (nj, W)
        shape_dist = np.sqrt(np.sum((hist - cur) ** 2, axis=1))

        # 量能距离
        vr_cur = vr[t]
        vr_hist = vr[js]
        vol_dist = np.full(len(js), 0.3)
        if not np.isnan(vr_cur) and vr_cur is not None:
            mask = ~np.isnan(vr_hist) & (vr_hist > 0)
            if mask.any():
                ratios = np.abs(np.log(np.maximum(vr_cur, 1e-6) / np.maximum(vr_hist, 1e-6)))
                vol_dist[mask] = 0.6 * np.minimum(ratios[mask], 2.5)

        # 扩展特征距离：与原版一致，存在+w*dist，缺失-0.5*w
        extra = np.zeros(len(js))

        # struct
        if wts["struct"]:
            cs = struct[t]
            hs = struct[js]
            if ~np.isnan(cs).any():
                valid = ~np.isnan(hs).any(axis=1)
                diff = hs[valid] - cs  # (k, 4)
                extra[valid] += wts["struct"] * (np.sqrt(np.sum(diff ** 2, axis=1)) / 4.0 ** 0.5)
                extra[~valid] += -0.5 * wts["struct"]
            else:
                extra += -0.5 * wts["struct"]

        # vola
        if wts["vola"]:
            cv = vola[t]
            hv = vola[js]
            if not np.isnan(cv) and cv > 0:
                valid = ~np.isnan(hv) & (hv > 0)
                ratios = np.abs(np.log(cv / hv[valid]))
                extra[valid] += wts["vola"] * np.minimum(ratios, 1.5)
                extra[~valid] += -0.5 * wts["vola"]
            else:
                extra += -0.5 * wts["vola"]

        # rsi
        if wts["rsi"]:
            cr = rsi[t]
            hr = rsi[js]
            if not np.isnan(cr):
                valid = ~np.isnan(hr)
                diff = np.abs(cr - hr[valid]) / 100.0
                extra[valid] += wts["rsi"] * diff
                extra[~valid] += -0.5 * wts["rsi"]
            else:
                extra += -0.5 * wts["rsi"]

        # volchg
        if wts["volchg"]:
            cg = volchg[t]
            hg = volchg[js]
            if not np.isnan(cg):
                valid = ~np.isnan(hg)
                diff = np.minimum(np.abs(cg - hg[valid]), 1.5)
                extra[valid] += wts["volchg"] * diff
                extra[~valid] += -0.5 * wts["volchg"]
            else:
                extra += -0.5 * wts["volchg"]

        # weekly
        if wts["weekly"]:
            cw = weekly[t]
            hw = weekly[js]
            if ~np.isnan(cw).any():
                valid = ~np.isnan(hw).any(axis=1)
                diff = hw[valid] - cw  # (k, WEEKLY_N)
                dist = np.sqrt(np.sum(diff ** 2, axis=1)) / WEEKLY_N ** 0.5
                dist = np.minimum(dist * 5, 1.5)
                extra[valid] += wts["weekly"] * dist
                extra[~valid] += -0.5 * wts["weekly"]
            else:
                extra += -0.5 * wts["weekly"]

        dist = shape_dist + vol_dist + extra

        # 选 topk 带 MIN_GAP
        order = np.argsort(dist)
        picked_idx = []
        last_j = -10 ** 9
        for oi in order:
            j = js[oi]
            if abs(j - last_j) < MIN_GAP:
                continue
            picked_idx.append(oi)
            last_j = j
            if len(picked_idx) >= TOPK:
                break

        if len(picked_idx) < 3:
            continue

        picked_idx = np.array(picked_idx)
        picked_j = js[picked_idx]
        picked_dist = dist[picked_idx]
        # 与 legacy 一致：样本窗口 znorm(rets[j-W:j]) 对应的标签是 close[j+1]/close[j]-1
        picked_labels = labels[picked_j]

        # 相似度加权权重
        best = picked_dist[0]
        if use_simw:
            weights = np.exp(-np.minimum(np.maximum(picked_dist - best, 0.0), 6.0) / 0.9)
        else:
            weights = np.ones_like(picked_dist)

        # 时间衰减
        if use_decay:
            age = t - picked_j
            mask_decay = age > CFG.TIME_DECAY_DAYS
            if mask_decay.any():
                decay = np.maximum(CFG.TIME_DECAY_RATE,
                                   1.0 - (age[mask_decay] - CFG.TIME_DECAY_DAYS) / 365.0 * 0.5)
                weights[mask_decay] *= decay

        # 加权分位数 P50
        pairs = list(zip(picked_labels.tolist(), weights.tolist()))
        preds[idx] = wpct(pairs, 50)
        ups[idx] = np.sum(weights[picked_labels > 0]) / np.sum(weights)

    # 实际收益
    acts = closes[ts + 1] / closes[ts] - 1.0
    mask = ~np.isnan(preds)
    return preds, ups, acts, mask


# ---------------------------------------------------------------------------
# 评估配置
# ---------------------------------------------------------------------------

def eval_one_stock(args):
    """多进程 worker：对单只股票跑一个配置（与 legacy 完全同口径）。"""
    f, wts, use_decay, use_simw, step, tail = args
    n = f["n"]
    preds_all, ups_all, acts_all, mask_all = predict_all(f, wts, use_decay, use_simw)
    if len(preds_all) == 0:
        return None

    # 与 legacy 同口径：t0 = max(2W+2, n-tail)，ts = [t0, n-2]，step
    t_min = max(2 * W + 2, W)
    t0 = max(2 * W + 2, n - tail) if tail and tail > 0 else t_min
    ts = np.arange(t0, n - 1, step)
    if len(ts) == 0:
        return None
    idx = ts - t_min
    preds = preds_all[idx]
    ups = ups_all[idx]
    acts = acts_all[idx]
    mask = mask_all[idx]

    preds = preds[mask]
    ups = ups[mask]
    acts = acts[mask]
    if len(preds) == 0:
        return None

    hit = np.mean((preds > 0) == (acts > 0))
    up_hit = np.mean((ups > 0.5) == (acts > 0)) if len(ups) else 0.0
    mae = np.mean(np.abs(preds - acts))

    # Spearman IC
    rp = np.argsort(np.argsort(preds)).astype(np.float64)
    ra = np.argsort(np.argsort(acts)).astype(np.float64)
    mp, ma = rp.mean(), ra.mean()
    cov = np.sum((rp - mp) * (ra - ma))
    sp = math.sqrt(np.sum((rp - mp) ** 2) * np.sum((ra - ma) ** 2)) or 1.0
    ic = cov / sp

    return {"n": len(preds), "dir_hit": hit, "up_hit": up_hit, "ic": ic, "mae": mae}


def eval_config(feats, wts, use_decay=True, use_simw=True, step=1, tail=None,
                max_workers=None):
    """对所有股票跑一个配置，返回聚合指标。"""
    args_list = [(f, wts, use_decay, use_simw, step, tail) for f in feats]

    results = []
    if max_workers is None:
        max_workers = max(1, min(8, len(args_list)))

    with ProcessPoolExecutor(max_workers=max_workers) as exe:
        for r in exe.map(eval_one_stock, args_list):
            if r is not None:
                results.append(r)

    if not results:
        return None

    total_n = sum(r["n"] for r in results)
    hit = sum(r["dir_hit"] * r["n"] for r in results) / total_n
    up_hit = sum(r["up_hit"] * r["n"] for r in results) / total_n
    mae = sum(r["mae"] * r["n"] for r in results) / total_n
    ic = sum(r["ic"] * r["n"] for r in results) / total_n
    return {"n": total_n, "dir_hit": hit, "up_hit": up_hit, "ic": ic, "mae": mae}


# ---------------------------------------------------------------------------
# 兼容旧版的单配置接口（保留，便于小样本验证）
# ---------------------------------------------------------------------------

def _is_valid(v):
    """判断一个值是否有效（非 None 且非 NaN）。"""
    if v is None:
        return False
    try:
        if np.isnan(v).any():
            return False
    except (TypeError, ValueError):
        pass
    return True


def predict_at_legacy(f, t, wts, use_decay=True, use_simw=True):
    """原 Python 循环实现，仅用于对比验证。接受 numpy 预计算结构。"""
    n = f["n"]
    if t + 1 >= n or t + 1 - W < W:
        return None
    cur_win = f["zmat"][t + 1]
    if cur_win is None or np.isnan(cur_win).any():
        return None
    cur = {"struct": f["struct"][t], "vola": f["vola"][t],
           "rsi": f["rsi"][t], "volchg": f["volchg"][t],
           "weekly": f["weekly"][t]}
    vr_now = f["vr"][t]
    sims = []
    for j in range(W, t + 1 - W):
        w = f["zmat"][j]
        if np.isnan(w).any():
            continue
        d = sum((a - b) ** 2 for a, b in zip(cur_win, w)) ** 0.5
        vr_j = f["vr"][j]
        if _is_valid(vr_now) and _is_valid(vr_j):
            d += 0.6 * min(abs(math.log(max(float(vr_now), 1e-6)
                                        / max(float(vr_j), 1e-6))), 2.5)
        else:
            d += 0.30
        sx = f["struct"][j]
        if wts["struct"] and _is_valid(cur["struct"]) and _is_valid(sx):
            d += wts["struct"] * (
                sum((float(a) - float(b)) ** 2 for a, b in zip(cur["struct"], sx))
                / len(sx)) ** 0.5
        elif wts["struct"]:
            d += wts["struct"] * 0.5
        va = f["vola"][j]
        if wts["vola"] and _is_valid(cur["vola"]) and _is_valid(va):
            d += wts["vola"] * min(abs(math.log(float(cur["vola"]) / float(va))), 1.5)
        elif wts["vola"]:
            d += wts["vola"] * 0.5
        rj = f["rsi"][j]
        if wts["rsi"] and _is_valid(cur["rsi"]) and _is_valid(rj):
            d += wts["rsi"] * abs(float(cur["rsi"]) - float(rj)) / 100.0
        elif wts["rsi"]:
            d += wts["rsi"] * 0.5
        gj = f["volchg"][j]
        if wts["volchg"] and _is_valid(cur["volchg"]) and _is_valid(gj):
            d += wts["volchg"] * min(abs(float(cur["volchg"]) - float(gj)), 1.5)
        elif wts["volchg"]:
            d += wts["volchg"] * 0.5
        wj = f["weekly"][j]
        if wts["weekly"] and _is_valid(cur["weekly"]) and _is_valid(wj):
            d += wts["weekly"] * min(
                (sum((float(a) - float(b)) ** 2 for a, b in zip(cur["weekly"], wj))
                 / len(wj)) ** 0.5 * 5, 1.5)
        elif wts["weekly"]:
            d += wts["weekly"] * 0.5
        sims.append((d, j))
    if len(sims) < TOPK:
        return None
    sims.sort(key=lambda x: x[0])
    best = sims[0][0]
    picked, last_j = [], -10 ** 9
    for d, j in sims:
        if abs(j - last_j) < MIN_GAP:
            continue
        picked.append((d, j))
        last_j = j
        if len(picked) >= TOPK:
            break
    if len(picked) < 3:
        return None
    pairs = []
    for d, j in picked:
        weight = math.exp(-min(max(0.0, d - best), 6.0) / 0.9) \
            if use_simw else 1.0
        if use_decay:
            age = t - j
            if age > CFG.TIME_DECAY_DAYS:
                weight *= max(CFG.TIME_DECAY_RATE,
                              1.0 - (age - CFG.TIME_DECAY_DAYS) / 365.0 * 0.5)
        label = float(f["closes"][j + 1]) / float(f["closes"][j]) - 1
        pairs.append((label, weight))
    return wpct(pairs, 50), sum(w for v, w in pairs if v > 0) / sum(
        w for _, w in pairs)


def eval_config_legacy(stocks_feats, wts, use_decay=True, use_simw=True,
                       step=6, tail=220):
    preds, acts, ups = [], [], []
    for f in stocks_feats:
        n = len(f["closes"])
        t0 = max(2 * W + 2, n - tail)
        for t in range(t0, n - 1, step):
            r = predict_at_legacy(f, t, wts, use_decay, use_simw)
            if r is None:
                continue
            a = f["closes"][t + 1] / f["closes"][t] - 1
            preds.append(r[0])
            acts.append(a)
            ups.append(r[1])
    if not preds:
        return None
    hit = sum(1 for p, a in zip(preds, acts)
              if (p > 0) == (a > 0)) / len(preds)
    up_hit = sum(1 for u, a in zip(ups, acts)
                 if (u > 0.5) == (a > 0)) / len(preds)
    mae = sum(abs(p - a) for p, a in zip(preds, acts)) / len(preds)

    def rank(x):
        idx = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        for rr, i in enumerate(idx):
            r[i] = rr
        return r

    rp, ra = rank(preds), rank(acts)
    mp, ma = sum(rp) / len(rp), sum(ra) / len(ra)
    cov = sum((a - mp) * (b - ma) for a, b in zip(rp, ra))
    sp = math.sqrt(sum((a - mp) ** 2 for a in rp)
                   * sum((b - ma) ** 2 for b in ra)) or 1.0
    return {"n": len(preds), "dir_hit": hit, "up_hit": up_hit,
            "ic": cov / sp, "mae": mae}


BASE_WTS = {"struct": CFG.STRUCT_W, "vola": CFG.VOLA_W, "rsi": CFG.RSI_W,
            "volchg": CFG.VOLCHG_W, "weekly": CFG.WEEKLY_W}


def zero(w, *keys):
    d = dict(w)
    for k in keys:
        d[k] = 0.0
    return d


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    t0 = time.time()

    configs = [
        ("base(全部特征)",       dict(wts=BASE_WTS, use_decay=True,  use_simw=True)),
        ("-struct(去K线结构)",    dict(wts=zero(BASE_WTS, "struct"), use_decay=True,  use_simw=True)),
        ("-vola(去波动率)",      dict(wts=zero(BASE_WTS, "vola"),   use_decay=True,  use_simw=True)),
        ("-rsi(去RSI)",         dict(wts=zero(BASE_WTS, "rsi"),    use_decay=True,  use_simw=True)),
        ("-volchg(去量变)",      dict(wts=zero(BASE_WTS, "volchg"), use_decay=True,  use_simw=True)),
        ("-weekly(去周线)",      dict(wts=zero(BASE_WTS, "weekly"), use_decay=True,  use_simw=True)),
        ("px_only(纯形态=旧版)",  dict(wts=zero(BASE_WTS, "struct", "vola", "rsi", "volchg", "weekly"),
                                     use_decay=True,  use_simw=True)),
        ("-decay(去时间衰减)",    dict(wts=BASE_WTS, use_decay=False, use_simw=True)),
        ("-simw(去相似度加权)",   dict(wts=BASE_WTS, use_decay=True,  use_simw=False)),
    ]

    # 小样本验证：先用前 10 只股票对比新旧实现
    print("\n小样本验证新旧实现一致性...")
    print("加载 10 只样本股票...")
    test_stocks = load_stocks(min_bars=400, top_n=10)
    test_feats = [precompute(r) for _, r in test_stocks]
    ok = True
    for name, kw in configs[:2]:
        r_old = eval_config_legacy(test_feats, **kw, step=6, tail=220)
        r_new = eval_config(test_feats, **kw, step=6, tail=220, max_workers=1)
        if r_old is None or r_new is None:
            continue
        # 方向命中/MAE 应完全一致；IC 对排序敏感，允许浮点差异
        diff_hit = abs(r_old["dir_hit"] - r_new["dir_hit"])
        diff_mae = abs(r_old["mae"] - r_new["mae"])
        diff_ic = abs(r_old["ic"] - r_new["ic"])
        if diff_hit > 1e-6 or diff_mae > 1e-6 or diff_ic > 0.05:
            print(f"  {name}: 不一致 hit={diff_hit:.6f} mae={diff_mae:.6f} ic={diff_ic:.4f}")
            print(f"    old={r_old}")
            print(f"    new={r_new}")
            ok = False
        else:
            print(f"  {name}: OK (IC={r_new['ic']:+.4f}, n={r_new['n']})")
    if not ok:
        print("验证失败，退出")
        sys.exit(1)

    # 全量加载
    print("\n加载全缓存股票...")
    stocks = load_stocks(min_bars=400, top_n=None)
    print(f"股票数 {len(stocks)}，总K线 {sum(len(r) for _, r in stocks):,}")

    print("预计算特征（numpy）...")
    t1 = time.time()
    feats = [precompute(r) for _, r in stocks]
    print(f"预计算完成 {time.time()-t1:.1f}s")

    print("\n全缓存 + 步长1 消融回测...")
    results = {}
    for name, kw in configs:
        t1 = time.time()
        r = eval_config(feats, **kw, step=1, tail=None)
        elapsed = time.time() - t1
        results[name] = r
        if r:
            print(f"{name:<22} n={r['n']:<8} 方向命中={r['dir_hit']*100:5.1f}%  "
                  f"上行命中={r['up_hit']*100:5.1f}%  IC={r['ic']:+.4f}  "
                  f"MAE={r['mae']*100:.3f}%  ({elapsed:.1f}s)")
        else:
            print(f"{name:<22} 无有效样本")

    with open("ablation_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    print(f"\n总完成 {time.time()-t0:.0f}s，明细已存 ablation_results.json")


if __name__ == "__main__":
    main()
