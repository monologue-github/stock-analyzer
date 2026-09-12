# -*- coding: utf-8 -*-
"""过拟合治理与选型：只在训练段选因子/定权重，验证段一次性报告。

输出：
  1) 贪心前向路径（train IC 随因子数变化）+ 验证段同路径（过拟合可视化）
  2) 稳定度选择：按日 bootstrap 重采样贪心路径的因子入选频率
  3) LASSO 选因子（TimeSeriesSplit CV）
  4) 多重检验：验证段 BH-FDR（对训练段 Top-N 组合）
  5) 候选模型训练/验证 IC、RankIC、Top-Decile 多空与纯多组合回测
  6) 逐股时序交叉验证（IC 分布 / 事件胜率）
"""
import json
import os

import numpy as np

from . import panel as P
from .factors import FACTOR_NAMES, K, FRAMEWORK_W

ENUM = os.path.join(P.CACHE_DIR, "enum_stats.npz")
OUT = os.path.join(P.CACHE_DIR, "validate_results.json")


def mask_bits(mask):
    return np.array([(mask >> i) & 1 for i in range(K)], dtype=np.float64)


def ic_series(mask, base_w, S, G, YY, day_idx, min_rows=None):
    w = base_w * mask_bits(mask)
    idx = np.asarray(day_idx)
    ok = YY[idx] > 0
    idx = idx[ok]
    c = S[idx] @ w
    Ww = np.outer(w, w)
    g = np.einsum("dij,ij->d", G[idx], Ww)
    with np.errstate(divide="ignore", invalid="ignore"):
        ic = c / np.sqrt(np.maximum(g * YY[idx], 1e-18))
    ic = ic[np.isfinite(ic)]
    return ic


def ic_stats(ic):
    if len(ic) < 5:
        return {"n": int(len(ic)), "mean": None, "icir": None, "t": None}
    m, s = float(ic.mean()), float(ic.std(ddof=1))
    return {"n": int(len(ic)), "mean": m,
            "icir": m / s if s > 0 else None,
            "t": m / s * np.sqrt(len(ic)) if s > 0 else None}


def greedy_path(base_w, S, G, YY, days, max_k=None):
    sel, path = 0, []
    remaining = set(range(K))
    max_k = max_k or K
    for _ in range(max_k):
        best = (-1e9, None)
        for f in remaining:
            ic = ic_series(sel | (1 << f), base_w, S, G, YY, days).mean()
            if np.isfinite(ic) and ic > best[0]:
                best = (ic, f)
        if best[1] is None:
            break
        sel |= 1 << best[1]
        remaining.discard(best[1])
        path.append({"factor": FACTOR_NAMES[best[1]], "index": best[1],
                     "mask": sel, "train_ic": float(best[0])})
    return path


def stability_selection(base_w, S, G, YY, train_days, b=30, prefix=8,
                        seed=11, frac=0.8):
    rng = np.random.RandomState(seed)
    train_days = np.asarray(train_days)
    counts = np.zeros(K)
    pos_freq = np.zeros((prefix, K))
    for i in range(b):
        pick = rng.choice(train_days, size=int(len(train_days) * frac),
                          replace=False)
        path = greedy_path(base_w, S, G, YY, pick, max_k=prefix)
        for step, node in enumerate(path):
            pos_freq[step, node["index"]] += 1
            counts[node["index"]] += 1
    return counts / b, pos_freq / b


def lasso_select(X, Y1, train_rows, seed=3, alpha_max=1e-1):
    from sklearn.linear_model import LassoCV
    from sklearn.model_selection import TimeSeriesSplit
    rng = np.random.RandomState(seed)
    n = min(300000, train_rows.sum())
    idx = np.nonzero(train_rows)[0]
    idx = rng.choice(idx, size=n, replace=False)
    m = LassoCV(cv=TimeSeriesSplit(5),
                alphas=np.logspace(-4, np.log10(alpha_max), 16),
                max_iter=3000, n_jobs=-1)
    m.fit(X[idx], Y1[idx])
    coef = m.coef_
    return {"alpha": float(m.alpha_),
            "factors": [FACTOR_NAMES[i] for i in range(K)
                        if abs(coef[i]) > 1e-8],
            "coef": {FACTOR_NAMES[i]: float(coef[i]) for i in range(K)}}


def combo_score(Xo, mask, w):
    v = mask_bits(mask) * w
    return Xo @ v


def daily_ic_x(score, y, rd, day_idx):
    ics = []
    for d in day_idx:
        m = rd == d
        if m.sum() < 30:
            continue
        x, yy = score[m], y[m]
        if x.std() <= 0 or yy.std() <= 0:
            continue
        ics.append(np.corrcoef(x, yy)[0, 1])
    ics = np.array([v for v in ics if np.isfinite(v)])
    return ics


def rank_ic_x(score, y, rd, day_idx):
    from scipy.stats import spearmanr
    ics = []
    for d in day_idx:
        m = rd == d
        if m.sum() < 30:
            continue
        x, yy = score[m], y[m]
        if x.std() <= 0 or yy.std() <= 0:
            continue
        ics.append(spearmanr(x, yy).statistic)
    ics = np.array([v for v in ics if np.isfinite(v)])
    return ics


def portfolio(score, y, rd, day_idx, q=0.1, cost=0.001, mode="long"):
    """按日分位组合：long / longshort。cost=单边滑点，按换手计。"""
    days = sorted(set(day_idx.tolist()))
    prev_top, prev_bot = set(), set()
    rets, turn = [], []
    for d in days:
        m = rd == d
        n = int(m.sum())
        if n < 50:
            continue
        idx = np.nonzero(m)[0]
        s = score[idx]
        k = max(1, int(n * q))
        order = np.argsort(-s)
        top = set(idx[order[:k]].tolist())
        bot = set(idx[order[-k:]].tolist())
        t = (len(top - prev_top) + len(bot - prev_bot)) / (2 * k)
        turn.append(t)
        if mode == "long":
            r = y[list(top)].mean() - cost * t
        else:
            r = (y[list(top)].mean() - y[list(bot)].mean()) - cost * t
        rets.append(r)
        prev_top, prev_bot = top, bot
    rets = np.array(rets)
    if len(rets) < 20:
        return None
    nav = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(nav)
    mdd = float((nav / peak - 1).min())
    ann = float(nav[-1] ** (250 / len(rets)) - 1)
    vol = float(rets.std(ddof=1) * np.sqrt(250))
    sharpe = float(rets.mean() / rets.std(ddof=1) * np.sqrt(250)) \
        if rets.std(ddof=1) > 0 else None
    return {"n_days": len(rets), "ann": ann, "mdd": mdd, "sharpe": sharpe,
            "vol": vol, "win": float((rets > 0).mean()),
            "avg_turnover": float(np.mean(turn))}


def eval_candidate(name, mask, w, Xo, Y1, rd, tr_days, va_days):
    score = combo_score(Xo, mask, w)
    tr_ic = daily_ic_x(score, Y1, rd, tr_days)
    va_ic = daily_ic_x(score, Y1, rd, va_days)
    tr_rk = rank_ic_x(score, Y1, rd, tr_days)
    va_rk = rank_ic_x(score, Y1, rd, va_days)
    return {
        "name": name, "k": int(mask_bits(mask).sum()),
        "factors": [FACTOR_NAMES[i] for i in range(K)
                    if (mask >> i) & 1],
        "train_ic": ic_stats(tr_ic), "val_ic": ic_stats(va_ic),
        "train_rank_ic": ic_stats(tr_rk), "val_rank_ic": ic_stats(va_rk),
        "train_long": portfolio(score, Y1, rd, tr_days, mode="long"),
        "val_long": portfolio(score, Y1, rd, va_days, mode="long"),
        "train_ls": portfolio(score, Y1, rd, tr_days, mode="longshort"),
        "val_ls": portfolio(score, Y1, rd, va_days, mode="longshort"),
        "train_long_gross": portfolio(score, Y1, rd, tr_days, mode="long",
                                      cost=0.0),
        "val_long_gross": portfolio(score, Y1, rd, va_days, mode="long",
                                    cost=0.0),
    }


def walk_forward(base_w, S, G, YY, D, train_n, min_train=350, step=60,
                 max_k=8):
    """扩张窗滚动再选：每 step 日用截至前一日的数据重选因子，
    下一段只做样本外评估（严格无前视）。"""
    folds = []
    for t0 in range(train_n, D - 5, step):
        t1 = min(D, t0 + step)
        past = np.arange(0, max(min_train, t0 - 1))
        path = greedy_path(base_w, S, G, YY, past, max_k=max_k)
        mask = path[-1]["mask"]
        ic = ic_series(mask, base_w, S, G, YY, np.arange(t0, t1))
        names = [node["factor"] for node in path]
        folds.append({
            "start_day": int(t0), "end_day": int(t1 - 1),
            "factors": names, "k": len(names),
            "oos_ic": float(ic.mean()) if len(ic) else None,
            "oos_icir": (float(ic.mean() / ic.std(ddof=1))
                         if len(ic) > 1 and ic.std(ddof=1) > 0 else None),
            "n_days": int(len(ic)),
        })
    ics = [f["oos_ic"] for f in folds if f["oos_ic"] is not None]
    agg = {"n_folds": len(folds),
           "mean_ic": float(np.mean(ics)) if ics else None,
           "pos_folds": int(sum(1 for v in ics if v > 0)),
           "folds": folds}
    return agg


def bh_fdr(pvals, q=0.05):
    p = np.asarray(pvals)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order]
    thresh = q * (np.arange(1, n + 1)) / n
    passed = ranked <= thresh
    if not passed.any():
        return 0, 0.0
    kmax = np.nonzero(passed)[0].max()
    return int(kmax + 1), float(ranked[kmax])


def per_stock_check(Xo, Y1, rd, code, masks, train_days, val_days):
    """逐股时序 IC / 事件收益（旧口径交叉验证）。"""
    out = {}
    D = int(rd.max()) + 1
    tr_map = np.zeros(D, dtype=bool)
    va_map = np.zeros(D, dtype=bool)
    tr_map[np.asarray(train_days)] = True
    va_map[np.asarray(val_days)] = True
    codes = sorted(set(code.tolist()))
    for name, (mask, w) in masks.items():
        score = combo_score(Xo, mask, w)
        ics, hits, ev_rets, ev_win = [], [], [], []
        for c in codes:
            m = code == c
            if m.sum() < 80:
                continue
            sd = rd[m]
            s = score[m]
            y = Y1[m]
            vr = va_map[sd]
            if vr.sum() < 30:
                continue
            xs, ys = s[vr], y[vr]
            if xs.std() > 0 and ys.std() > 0:
                ics.append(np.corrcoef(xs, ys)[0, 1])
            tr = tr_map[sd]
            if tr.sum() >= 50 and s[tr].std() > 0:
                thr = np.quantile(s[tr], 0.8)
                ev = vr & (s >= thr)
                if ev.sum() >= 5:
                    ev_rets.append(float(y[ev].mean()))
                    ev_win.append(float((y[ev] > 0).mean()))
                    hits.append(float(((s[vr] > 0) == (y[vr] > 0)).mean()))
        ics = np.array([v for v in ics if np.isfinite(v)])
        out[name] = {
            "stocks": int(len(ics)),
            "ic_median": float(np.median(ics)) if len(ics) else None,
            "ic_pos_pct": float((ics > 0).mean()) if len(ics) else None,
            "event_ret_median": float(np.median(ev_rets)) if ev_rets else None,
            "event_win_median": float(np.median(ev_win)) if ev_win else None,
            "hit_median": float(np.median(hits)) if hits else None,
        }
    return out


def run():
    from .panel import load_panel
    p = load_panel()
    z = np.load(ENUM)
    signs, base_w = z["signs"], z["base_w"]
    S, G, YY = z["S"], z["G"], z["YY"]
    is_train, is_val = z["is_train"], z["is_val"]
    tr_days = np.nonzero(is_train)[0]
    va_days = np.nonzero(is_val)[0]
    Xo = p["X"] * signs[None, :]
    rd = p["date"]
    D = len(p["eval_dates"])

    print("=== 贪心前向选择（训练段目标）===")
    path = greedy_path(base_w, S, G, YY, tr_days)
    print("step factor           train_ic   val_ic")
    path_rows = []
    for step, node in enumerate(path, 1):
        ic_tr = float(ic_series(node["mask"], base_w, S, G, YY,
                                tr_days).mean())
        ic_va = float(ic_series(node["mask"], base_w, S, G, YY,
                                va_days).mean())
        path_rows.append({"step": step, "factor": node["factor"],
                          "train_ic": ic_tr, "val_ic": ic_va})
        print(f"{step:>4} {node['factor']:<12} {ic_tr:+.5f} {ic_va:+.5f}")

    print("\n=== 稳定度选择（30× 80% 训练日 bootstrap）===")
    freq, pos_freq = stability_selection(base_w, S, G, YY, tr_days)
    for i in range(K):
        if freq[i] > 0:
            print(f"  {FACTOR_NAMES[i]:<10} 入选频率 {freq[i]:.2f}")
    single_ic = np.array([z["train_mean"][1 << i] for i in range(K)])
    # 稳定 + 有实际信息量（单因子训练IC非零）才入选
    stable_idx = [i for i in range(K)
                  if freq[i] >= 0.6 and abs(single_ic[i]) > 1e-4]
    if len(stable_idx) < 2:
        top = np.argsort(-freq)[:5]
        stable_idx = [int(i) for i in top
                      if freq[i] > 0.3 and abs(single_ic[i]) > 1e-4]
    stable_mask = 0
    for i in stable_idx:
        stable_mask |= 1 << i
    print(f"稳定集（freq≥0.6 且单因子IC≠0）: "
          f"{[FACTOR_NAMES[i] for i in stable_idx]}")

    print("\n=== 扩张窗滚动再选（walk-forward, 每60日重选Top8贪心）===")
    wf = walk_forward(base_w, S, G, YY, D, p["train_n"])
    print(f"  folds={wf['n_folds']} 平均OOS IC={wf['mean_ic']} "
          f"正收益折数={wf['pos_folds']}/{wf['n_folds']}")
    for f in wf["folds"]:
        if f["oos_ic"] is not None:
            print(f"    [{f['start_day']:>4}-{f['end_day']:>4}] "
                  f"IC={f['oos_ic']:+.5f} k={f['k']} "
                  f"{'+'.join(f['factors'][:5])}...")

    print("\n=== LASSO 选因子（训练段, TimeSeriesSplit）===")
    las = lasso_select(Xo, p["Y1"], np.isin(rd, tr_days))
    lasso_mask = 0
    for i in range(K):
        if FACTOR_NAMES[i] in las["factors"]:
            lasso_mask |= 1 << i
    print(f"alpha={las['alpha']:.4f} 选中 "
          f"{[FACTOR_NAMES[i] for i in range(K) if (lasso_mask >> i) & 1]}")

    # 基准：框架12维（按框架权重）
    fw_mask = 0
    for i, n in enumerate(FACTOR_NAMES):
        if n in ("MACD", "KDJ", "RSI", "量价", "MA20", "MA趋势", "爆发力",
                 "量能", "板块", "布林带", "ADX", "L1形态"):
            fw_mask |= 1 << i
    fw_w = np.array([FRAMEWORK_W[i] if (fw_mask >> i) & 1 else 0.0
                     for i in range(K)])

    # 稳定集权重：等权 / NNLS 拟合
    from scipy.optimize import nnls
    tr_m = np.isin(rd, tr_days)
    idx = np.nonzero(tr_m)[0]
    rng = np.random.RandomState(5)
    sub = rng.choice(idx, size=min(200000, len(idx)), replace=False)
    Xs = Xo[sub][:, [i for i in stable_idx]]
    ys = p["Y1"][sub]
    coef, _ = nnls(Xs, ys)
    w_stable_fit = np.zeros(K)
    for j, i in enumerate(stable_idx):
        w_stable_fit[i] = coef[j]
    if w_stable_fit.sum() > 0:
        w_stable_fit = w_stable_fit / w_stable_fit.sum() * len(stable_idx)

    # Top-train 组合（从 enum JSON 读）
    with open(os.path.join(P.CACHE_DIR, "enum_top.json"),
              encoding="utf-8") as f:
        enum_rep = json.load(f)
    top_tr = enum_rep["top_train_ic"][:20]

    candidates = {
        "框架12维(权重)": (fw_mask, fw_w),
        "稳定集等权": (stable_mask, np.ones(K)),
        "稳定集NNLS": (stable_mask, w_stable_fit),
        "LASSO集等权": (lasso_mask, np.ones(K)) if lasso_mask else None,
        "全部21等权": ((1 << K) - 1, np.ones(K)),
    }
    # 贪心路径上的最优规模（按训练IC）
    best_node = max(path_rows, key=lambda r: r["train_ic"])
    candidates["贪心最优(train)"] = (path[best_node["step"] - 1]["mask"],
                                    np.ones(K))
    for j, row in enumerate(top_tr[:5], 1):
        candidates[f"枚举Top{j}(train)"] = (row["mask"], np.ones(K))

    print("\n=== 候选模型：训练 vs 验证 ===")
    print(f"{'模型':<18}{'k':>3}{'训练IC':>10}{'训练ICIR':>10}"
          f"{'验证IC':>10}{'验证ICIR':>10}{'验证RankIC':>11}")
    results = {}
    for name, cand in candidates.items():
        if cand is None:
            continue
        mask, w = cand
        r = eval_candidate(name, mask, w, Xo, p["Y1"], rd, tr_days, va_days)
        results[name] = r
        ti = r["train_ic"]["mean"] or 0
        tii = r["train_ic"]["icir"] or 0
        vi = r["val_ic"]["mean"] or 0
        vii = r["val_ic"]["icir"] or 0
        vrk = r["val_rank_ic"]["mean"] or 0
        print(f"{name:<18}{r['k']:>3}{ti:>+10.5f}{tii:>+10.2f}"
              f"{vi:>+10.5f}{vii:>+10.2f}{vrk:>+11.5f}")
    print("\n纯多 Top-Decile（验证段, 含0.1%单边成本）: 年化 / 回撤 / Sharpe")
    for name, r in results.items():
        v = r["val_long"]
        t = r["train_long"]
        if v:
            print(f"  {name:<18} 验证 {v['ann']*100:+7.1f}% / "
                  f"{v['mdd']*100:+6.1f}% / {v['sharpe'] or 0:+.2f}   "
                  f"训练 {t['ann']*100:+7.1f}%")

    # 多重检验：对训练Top 10k组合做验证段 BH-FDR
    n_top = 10000
    order = np.argsort(-z["train_mean"])[:n_top]
    vm = z["val_mean"][order]
    vs = z["val_sd"][order]
    vn = z["val_n"][order].astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        tvals = vm / np.maximum(vs, 1e-12) * np.sqrt(vn)
    from scipy.stats import norm
    pvals = 2 * (1 - norm.cdf(np.abs(tvals)))
    pvals = np.nan_to_num(pvals, nan=1.0)
    n_pass, thr = bh_fdr(pvals, 0.05)
    print(f"\n多重检验：训练Top{n_top}组合在验证段 BH-FDR q<0.05 通过 "
          f"{n_pass} 个（阈值 p≤{thr:.2e}）")
    tm = z["train_mean"][z["train_n"] >= 30]
    vmv = z["val_mean"][z["val_n"] >= 30]
    corr_tv = float(np.corrcoef(tm, vmv)[0, 1])

    print("\n=== 逐股时序交叉验证（验证段）===")
    psc = per_stock_check(Xo, p["Y1"], rd, p["code"],
                          {"稳定集等权": candidates["稳定集等权"],
                           "框架12维(权重)": candidates["框架12维(权重)"]},
                          tr_days, va_days)
    for name, r in psc.items():
        print(f"  {name:<14} 股票{r['stocks']} IC中位="
              f"{(r['ic_median'] or 0):+.4f} 正IC占比="
              f"{(r['ic_pos_pct'] or 0)*100:.1f}% "
              f"事件次日均值={(r['event_ret_median'] or 0)*100:+.3f}% "
              f"事件胜率中位={(r['event_win_median'] or 0)*100:.1f}%")

    report = {
        "greedy_path": path_rows,
        "stability_freq": {FACTOR_NAMES[i]: float(freq[i])
                           for i in range(K)},
        "stable_factors": [FACTOR_NAMES[i] for i in stable_idx],
        "walk_forward": wf,
        "lasso": las,
        "candidates": results,
        "bh": {"n_top": n_top, "pass": n_pass, "thresh": thr,
               "train_val_corr": corr_tv},
        "per_stock": psc,
        "signs": {FACTOR_NAMES[i]: float(signs[i]) for i in range(K)},
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n结果写入 {OUT}")
    return report


if __name__ == "__main__":
    run()
