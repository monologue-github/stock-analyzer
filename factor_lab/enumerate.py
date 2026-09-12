# -*- coding: utf-8 -*-
"""2^K 全组合穷举（K=21 ≈ 210万组合）。

方法（精确、无近似）：
  逐日截面充分统计量 s_d=X'y、G_d=X'X、yy_d=y'y；对任意组合权重 w：
      IC_d(w) = w's_d / sqrt(w'G_d w · yy_d)
  因此可在 O(2^K·K²·D) 内精确算出每个组合的训练/验证 IC 序列，
  再用 IC 均值/ICIR 排序。不需要对每个组合重扫面板。

防过拟合：
  - 因子方向只用训练段 IC 决定；
  - 训练/验证按时间切分 + 1日 embargo；
  - 全组合统计落盘，验证段只用于报告/多重检验。
"""
import json
import os
import time

import numpy as np

from .factors import FACTOR_NAMES, K, FRAMEWORK_W
from .panel import CACHE_DIR

NPZ = os.path.join(CACHE_DIR, "enum_stats.npz")
JSON = os.path.join(CACHE_DIR, "enum_top.json")
CHUNK = 1 << 18
MIN_DAYS = 30


def _mask_chunk(start, size, k):
    n = np.arange(start, start + size, dtype=np.uint64)
    return ((n[:, None] >> np.arange(k, dtype=np.uint64)) & 1).astype(
        np.float32)


def _flip_by_train(X, Y1, rd, train_n):
    """按训练段单因子 IC 符号翻转因子方向（只用训练段）。"""
    signs = np.ones(K)
    for k in range(K):
        num = den = 0.0
        for d in range(train_n):
            m = rd == d
            if m.sum() < 30:
                continue
            x = X[m, k]
            y = Y1[m]
            if x.std() <= 0 or y.std() <= 0:
                continue
            num += np.corrcoef(x, y)[0, 1]
            den += 1
        if den >= 20 and num < 0:
            signs[k] = -1.0
    return signs


def sufficiency(X, Y1, rd, D):
    """逐日充分统计量：S[D,K], G[D,K,K], YY[D]。"""
    S = np.zeros((D, K), dtype=np.float64)
    G = np.zeros((D, K, K), dtype=np.float64)
    YY = np.zeros(D, dtype=np.float64)
    for d in range(D):
        m = rd == d
        if m.sum() < 30:
            continue
        Xd = X[m].astype(np.float64)
        Xd = Xd - Xd.mean(axis=0)       # 精确中心化（clip 后均值非严格0）
        yd = Y1[m].astype(np.float64)
        yd = yd - yd.mean()
        S[d] = Xd.T @ yd
        G[d] = Xd.T @ Xd
        YY[d] = float(yd @ yd)
    return S, G, YY


def enumerate_all(S, G, YY, base_w, is_train, is_val, verbose=True):
    """全组合逐日 IC 统计，返回 dict of arrays (2^K,)。"""
    total = 1 << K
    tr = np.zeros(total, dtype=np.float64)
    tr2 = np.zeros(total, dtype=np.float64)
    tr_n = np.zeros(total, dtype=np.float64)
    va = np.zeros(total, dtype=np.float64)
    va2 = np.zeros(total, dtype=np.float64)
    va_n = np.zeros(total, dtype=np.float64)
    D = S.shape[0]
    t0 = time.time()
    for start in range(0, total, CHUNK):
        size = min(CHUNK, total - start)
        M = _mask_chunk(start, size, K)
        for d in range(D):
            yy = YY[d]
            if yy <= 0:
                continue
            sw = base_w * S[d]
            Gw = G[d] * np.outer(base_w, base_w)
            c = M @ sw
            g = ((M @ Gw) * M).sum(axis=1)
            with np.errstate(divide="ignore", invalid="ignore"):
                ic = c / np.sqrt(g * yy)
            bad = ~np.isfinite(ic)
            if bad.any():
                ic[bad] = 0.0
            if is_train[d]:
                tr[start:start + size] += ic
                tr2[start:start + size] += ic * ic
                tr_n[start:start + size] += 1
            elif is_val[d]:
                va[start:start + size] += ic
                va2[start:start + size] += ic * ic
                va_n[start:start + size] += 1
        if verbose and (start // CHUNK) % 2 == 1:
            print(f"  enum {start+size}/{total} {time.time()-t0:.0f}s")
    out = {}
    for tag, s, s2, n in (("train", tr, tr2, tr_n),
                          ("val", va, va2, va_n)):
        nz = np.maximum(n, 1)
        mean = s / nz
        var = np.maximum(s2 / nz - mean ** 2, 0.0)
        sd = np.sqrt(var)
        out[tag + "_mean"] = mean.astype(np.float32)
        out[tag + "_sd"] = sd.astype(np.float32)
        out[tag + "_n"] = n.astype(np.float32)
        out[tag + "_icir"] = np.where(n >= MIN_DAYS,
                                      mean / np.maximum(sd, 1e-9), np.nan)
    out["k"] = np.array([bin(i).count("1") for i in range(total)],
                        dtype=np.int8)
    return out


def _combo_names(mask):
    return [FACTOR_NAMES[i] for i in range(K) if (mask >> i) & 1]


def top_rows(stats, key, n=50, min_k=1, max_k=K, min_days=MIN_DAYS):
    mean = stats[key + "_mean"]
    icir = stats[key + "_icir"]
    n_d = stats[key + "_n"]
    kk = stats["k"]
    score = mean if key == "train" else mean
    valid = (n_d >= min_days) & (kk >= min_k) & (kk <= max_k) & np.isfinite(score)
    idx = np.nonzero(valid)[0]
    order = idx[np.argsort(-score[idx])][:n]
    out = []
    for i in order:
        row = {"mask": int(i), "k": int(kk[i]),
               "factors": _combo_names(int(i)),
               "train_mean": float(stats["train_mean"][i]),
               "train_icir": (None if not np.isfinite(stats["train_icir"][i])
                              else float(stats["train_icir"][i])),
               "val_mean": float(stats["val_mean"][i]),
               "val_icir": (None if not np.isfinite(stats["val_icir"][i])
                            else float(stats["val_icir"][i])),
               "train_days": int(stats["train_n"][i]),
               "val_days": int(stats["val_n"][i])}
        out.append(row)
    return out


def run(base_w=None, tag="equal"):
    from .panel import load_panel
    p = load_panel()
    X, Y1, rd = p["X"], p["Y1"], p["date"]
    D = len(p["eval_dates"])
    train_n = p["train_n"]
    print(f"面板 {len(Y1)} 行 × {K} 因子，日期 {D}")
    signs = _flip_by_train(X, Y1, rd, train_n)
    print("因子方向(训练段IC符号):")
    for i, s in enumerate(signs):
        print(f"  {FACTOR_NAMES[i]:<10} {s:+.0f}")
    Xo = X * signs[None, :]
    S, G, YY = sufficiency(Xo, Y1, rd, D)
    # 面板存的是逐行掩码，这里折算为逐日掩码
    is_train = np.bincount(rd[p["is_train"]].astype(int),
                           minlength=D) > 0
    is_val = np.bincount(rd[p["is_val"]].astype(int), minlength=D) > 0
    if base_w is None:
        base_w = np.ones(K)
    print(f"穷举 2^{K}={1<<K} 组合，权重方案={tag}"
          f"（训练日{is_train.sum()} 验证日{is_val.sum()}）")
    stats = enumerate_all(S, G, YY, base_w, is_train, is_val)
    stats["signs"] = signs
    stats["base_w"] = base_w
    stats["S"] = S.astype(np.float32)
    stats["G"] = G.astype(np.float32)
    stats["YY"] = YY.astype(np.float32)
    stats["is_train"] = is_train
    stats["is_val"] = is_val
    np.savez(NPZ, **stats)
    print(f"统计写入 {NPZ}")

    # 结果报告
    singles = []
    for i in range(K):
        mask = 1 << i
        singles.append({
            "factor": FACTOR_NAMES[i], "sign": float(signs[i]),
            "train_mean": float(stats["train_mean"][mask]),
            "train_icir": (None if not np.isfinite(stats["train_icir"][mask])
                           else float(stats["train_icir"][mask])),
            "val_mean": float(stats["val_mean"][mask]),
            "val_icir": (None if not np.isfinite(stats["val_icir"][mask])
                         else float(stats["val_icir"][mask])),
            "train_days": int(stats["train_n"][mask]),
            "val_days": int(stats["val_n"][mask])})
    singles.sort(key=lambda r: -abs(r["train_mean"]))
    top_tr = top_rows(stats, "train", 200)

    def _top_by(field, n):
        arr = stats[field]
        nd = stats["train_n" if field.startswith("train") else "val_n"]
        valid = np.isfinite(arr) & (nd >= 100)
        idx = np.nonzero(valid)[0]
        order = idx[np.argsort(-arr[idx])][:n]
        return order.tolist()

    icir_rank = _top_by("train_icir", 200)
    top_icir = []
    for i in icir_rank:
        top_icir.append({
            "mask": int(i), "k": int(stats["k"][i]),
            "factors": _combo_names(int(i)),
            "train_mean": float(stats["train_mean"][i]),
            "train_icir": float(stats["train_icir"][i]),
            "val_mean": float(stats["val_mean"][i]),
            "val_icir": (None if not np.isfinite(stats["val_icir"][i])
                         else float(stats["val_icir"][i])),
            "train_days": int(stats["train_n"][i]),
            "val_days": int(stats["val_n"][i])})
    top_va = top_rows(stats, "val", 50, min_days=1)
    # 验证段总体分布（选择偏差诊断）
    both = ((stats["val_n"] >= MIN_DAYS) & (stats["train_n"] >= MIN_DAYS))
    vm = stats["val_mean"][both]
    tm = stats["train_mean"][both]
    corr_tv = float(np.corrcoef(tm, vm)[0, 1]) if len(tm) > 10 else None
    report = {
        "tag": tag, "signs": signs.tolist(), "base_w": base_w.tolist(),
        "singles": singles,
        "top_train_ic": top_tr,
        "top_train_icir": top_icir,
        "top_val_oracle": top_va[:50],
        "all_train_mean_std": float(tm.std()),
        "all_val_mean_std": float(vm.std()),
        "train_val_corr": corr_tv,
        "total_combos": int(1 << K),
    }
    with open(JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"结果写入 {JSON}")
    print("\n单因子（按训练|IC|）:")
    print(f"{'因子':<10}{'sign':>5}{'训练IC':>10}{'训练ICIR':>10}"
          f"{'验证IC':>10}{'验证ICIR':>10}")
    for r in singles:
        print(f"{r['factor']:<10}{r['sign']:>+5.0f}{r['train_mean']:>+10.4f}"
              f"{(r['train_icir'] or 0):>+10.3f}{r['val_mean']:>+10.4f}"
              f"{(r['val_icir'] or 0):>+10.3f}")
    print(f"\n全组合训练/验证IC相关={corr_tv}")
    print("Top10 训练IC组合:")
    for r in top_tr[:10]:
        print(f"  k={r['k']} tr={r['train_mean']:+.4f} val={r['val_mean']:+.4f} "
              f"{'+'.join(r['factors'])}")
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", choices=("equal", "framework"),
                    default="equal")
    a = ap.parse_args()
    bw = np.ones(K) if a.weights == "equal" else FRAMEWORK_W
    run(base_w=bw, tag=a.weights)
