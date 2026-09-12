# -*- coding: utf-8 -*-
"""筹码分布引擎（可调参）+ 筹码峰因子 + 训练段参数优化。

原口径（stock_gui.calc_chips / chip_snapshots）:
  - 固定价格网格（全历史低-高），成交量在[low,high]区间均匀摊分；
  - 每日衰减 t = clip(0.02 * vol / median_vol, 0.002, 0.20)。
本模块把 decay/cap/nbin/prominence 参数化，并输出三类连续因子：
  筹码支撑 = -(close - 支撑峰)/close      （越接近支撑越高）
  筹码压力 = -(压力峰 - close)/close      （越接近压力越高）
  筹码获利 = 现价下方筹码占比
参数优化只在训练段日期上做，避免验证段泄漏。
"""
import numpy as np

DEFAULT_PARAMS = dict(nbin=200, decay_a=0.02, cap=0.20, floor=0.002,
                      prom=0.0)

GRID = dict(nbin=(100, 200), decay_a=(0.01, 0.02, 0.03),
            cap=(0.10, 0.20, 0.30), prom=(0.0, 0.10, 0.20))


def chip_features_stock(bars, start_idx, params=None):
    """单只股票逐日筹码因子（start_idx 之前只演化不输出）。

    返回 (feature[n×3], valid[n])，列为 支撑/压力/获利；不足 30 根返回空。
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    nbin = int(p["nbin"])
    n_all = len(bars)
    keep = [i for i, b in enumerate(bars)
            if b.get("vol") and b.get("low") and b["low"] > 0
            and b.get("high") and b["high"] >= b["low"]]
    bars = [bars[i] for i in keep]
    n = len(bars)
    out = np.full((n_all, 3), np.nan)
    valid = np.zeros(n_all, dtype=bool)
    if n < 30:
        return out, valid
    lo = min(b["low"] for b in bars)
    hi = max(b["high"] for b in bars)
    if hi <= lo:
        return out, valid
    step = (hi - lo) / nbin
    mids = lo + step * (np.arange(nbin + 1) + 0.5)
    chips = np.zeros(nbin + 1)
    med_vol = sorted(b["vol"] for b in bars)[n // 2] or 1.0
    a, cap, floor = p["decay_a"], p["cap"], p["floor"]
    prom = p["prom"]

    for k, b in enumerate(bars):
        i = keep[k]
        t = min(cap, max(floor, a * (b["vol"] / med_vol)))
        chips *= (1.0 - t)
        b_lo = max(0, int((b["low"] - lo) / step))
        b_hi = min(nbin, int((b["high"] - lo) / step))
        if b_hi <= b_lo:
            chips[b_hi] += b["vol"]
        else:
            chips[b_lo:b_hi + 1] += b["vol"] / (b_hi - b_lo + 1)
        if i < start_idx:
            continue
        tot = chips.sum()
        if tot <= 0:
            continue
        c = b["close"]
        valid[i] = True
        # 局部峰（可用相对高度过滤噪声尖刺）
        thr = prom * chips.max()
        inner = chips[1:-1]
        pk = (inner > chips[:-2]) & (inner >= chips[2:]) & (inner > thr)
        idx = np.nonzero(pk)[0] + 1
        pk_mass, pk_mid = (chips[idx], mids[idx]) if idx.size else (
            np.empty(0), np.empty(0))

        def strongest(below):
            m = pk_mid < c if below else pk_mid >= c
            if m.any():
                return pk_mid[m][np.argmax(pk_mass[m])]
            # 无局部峰退化为最密集单bin（同 calc_chips）
            b = mids < c if below else mids >= c
            if b.any():
                return mids[b][np.argmax(chips[b])]
            return np.nan

        sup = strongest(True)
        res = strongest(False)
        profit = chips[mids <= c].sum() / tot
        out[i, 0] = -(c - sup) / c if np.isfinite(sup) else np.nan
        out[i, 1] = -(res - c) / c if np.isfinite(res) else np.nan
        out[i, 2] = profit
    return out, valid


def _daily_ic(feats, y1, dates):
    """特征按日截面 Pearson IC 均值（feats/y1 长度一致）。"""
    ic = np.zeros(3)
    cnt = np.zeros(3)
    by_day = {}
    for t, d in enumerate(dates):
        if np.isfinite(y1[t]):
            by_day.setdefault(d, []).append(t)
    for d, ts in by_day.items():
        if len(ts) < 30:
            continue
        yy = y1[ts]
        if yy.std() <= 0:
            continue
        for j in range(3):
            xx = feats[ts, j]
            m = np.isfinite(xx)
            if m.sum() < 30 or xx[m].std() <= 0:
                continue
            ic[j] += np.corrcoef(xx[m], yy[m])[0, 1]
            cnt[j] += 1
    return np.where(cnt > 0, ic / np.maximum(cnt, 1), np.nan), cnt


def evaluate_config(sample, params, train_dates):
    """sample=[(bars, dates, y1)], 返回按训练日截面IC统计的配置得分。"""
    feats_all, y_all, d_all = [], [], []
    for bars, dates, y1 in sample:
        f, _ = chip_features_stock(bars, max(0, len(bars) - 1000), params)
        feats_all.append(f)
        y_all.append(y1)
        d_all.append(dates)
    feats = np.vstack(feats_all) if feats_all else np.empty((0, 3))
    y1 = np.concatenate(y_all) if y_all else np.empty(0)
    dates = np.concatenate(d_all) if d_all else np.empty(0, dtype=object)
    tr = np.array([d in train_dates for d in dates])
    ic, cnt = _daily_ic(feats[tr], y1[tr], dates[tr])
    score = np.nanmean(np.abs(ic))
    return ic, cnt, score


def train_val_dates(dates_all, train_frac=0.7, embargo=1):
    """全局日期排序切分：训练段在前 70%，验证段后 30%，embargo 日隔断。"""
    ds = sorted(set(dates_all))
    split = int(len(ds) * train_frac)
    train = set(ds[:split])
    val = set(ds[split + embargo:])
    return train, val, ds


def optimize(sample, train_dates, grid=None, verbose=True):
    """网格搜索最优筹码参数（目标=训练段三因子 |IC| 均值）。"""
    grid = grid or GRID
    results = []
    keys = list(grid.keys())
    import itertools
    for vals in itertools.product(*(grid[k] for k in keys)):
        cfg = dict(zip(keys, vals))
        ic, cnt, score = evaluate_config(sample, cfg, train_dates)
        results.append((score, cfg, ic, cnt))
        if verbose:
            print(f"  chip cfg {cfg} -> score={score:.4f} "
                  f"ic={np.round(ic, 4).tolist()}")
    results.sort(key=lambda x: -x[0])
    best = results[0]
    if verbose:
        print(f"筹码最优参数: {best[1]} (score={best[0]:.4f})")
    return best[1], results
