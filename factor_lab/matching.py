# -*- coding: utf-8 -*-
"""形态匹配因子：L1(自身) / L2(同行业) / L3(同市值层)。

与 backtest_levels.match_pool 同构（窗口20 + vr/struct/vola/rsi/volchg/weekly
多维距离），但 numpy 向量化 + 分块精确 Top-K（非近似）。
防前视：只匹配标签日期 ≤ 评估日的样本；时间衰减以评估日计算。
L1 = Top-K 相似样本的次日上行概率；L2/L3 = Top-K 加权平均次日收益。
"""
import datetime as _dt
import math

import numpy as np

from stock_gui import CFG, W_WINDOW
from stock_gui import logret

STRUCT_W = CFG.STRUCT_W
VOLA_W = CFG.VOLA_W
RSI_W = CFG.RSI_W
VOLCHG_W = CFG.VOLCHG_W
WEEKLY_W = CFG.WEEKLY_W

_EPOCH = _dt.date(1990, 1, 1)


def _ord(date_str):
    y, m, d = date_str.split("-")
    return (_dt.date(int(y), int(m), int(d)) - _EPOCH).days


def _decay_weight(age_days):
    """与 stock_gui.decay_w 同口径，但以评估日为基准（无前视）。"""
    if age_days <= CFG.TIME_DECAY_DAYS:
        return 1.0
    return max(CFG.TIME_DECAY_RATE,
               1.0 - (age_days - CFG.TIME_DECAY_DAYS) / 365.0 * 0.5)


def _znorm_rows(m):
    mu = m.mean(axis=1, keepdims=True)
    sd = m.std(axis=1, keepdims=True)
    return (m - mu) / np.maximum(sd, 1e-12)


def _windows(lr, W):
    """滑窗矩阵：行 r 对应窗口结束价格下标 j = W + r（j ≤ n-2）。"""
    if len(lr) + 1 <= W + 1:
        return np.empty((0, W)), np.empty(0, dtype=int)
    w = np.lib.stride_tricks.sliding_window_view(lr, W)[:-1]
    j = np.arange(W, len(lr))
    return _znorm_rows(w), j


def _roll_sum(x, w):
    """out[i] = sum(x[i-w+1:i+1])，不足为 NaN。"""
    c = np.concatenate([[0.0], np.cumsum(np.nan_to_num(x))])
    n = len(x)
    out = np.full(n, np.nan)
    if n >= w:
        out[w - 1:] = c[w:] - c[:-w]
    return out


def _roll_mean(x, w):
    with np.errstate(invalid="ignore"):
        return _roll_sum(x, w) / w


def context_series(bars, lr=None):
    """逐日匹配扩展特征（与 stock_gui 同名函数同口径，全向量化）。"""
    n = len(bars)
    close = np.array([b["close"] for b in bars], float)
    high = np.array([b["high"] or np.nan for b in bars], float)
    low = np.array([b["low"] or np.nan for b in bars], float)
    op = np.array([b["open"] or np.nan for b in bars], float)
    vol = np.array([b.get("vol") or 0.0 for b in bars], float)
    if lr is None:
        lr = np.full(n, np.nan)
        lr[1:] = np.log(np.maximum(close[1:], 1e-12)
                        / np.maximum(close[:-1], 1e-12))
    r5 = _roll_mean(vol, 5)
    s15 = _roll_sum(vol, 15)
    p15 = np.full(n, np.nan)
    if n >= 20:
        p15[19:] = s15[14:n - 5] / 15.0
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = (r5 / p15).astype(np.float32)
    vola = np.full(n, np.nan)
    if n >= 10:
        m = _roll_mean(lr, 10)
        m2 = _roll_mean(lr * lr, 10)
        with np.errstate(invalid="ignore"):
            vola[9:] = np.sqrt(np.maximum(m2[9:] - m[9:] ** 2, 0.0))
        vola[9] = np.nan if not np.isfinite(lr[0]) else vola[9]
    dclose = np.diff(close, prepend=close[:1])
    gains = np.where(dclose > 0, dclose, 0.0)
    losses = np.where(dclose < 0, -dclose, 0.0)
    rsi = np.full(n, np.nan)
    if n >= 15:
        g = _roll_sum(gains, 14)[14:]
        l = _roll_sum(losses, 14)[14:]
        with np.errstate(divide="ignore", invalid="ignore"):
            r = 100.0 - 100.0 / (1.0 + g / np.maximum(l, 1e-12))
        r[(l <= 0) & (g > 0)] = 100.0
        r[(l <= 0) & (g <= 0)] = np.nan
        rsi[14:] = r
    vc_full = np.full(n, np.nan)
    if n > 9:
        m5 = _roll_mean(vol, 5)
        with np.errstate(divide="ignore", invalid="ignore"):
            vc_full[9:] = np.log(m5[9:] / m5[4:n - 5])
    rng = high - low
    struct = np.full((n, 4), np.nan, dtype=np.float32)
    ok = np.isfinite(op) & np.isfinite(high) & np.isfinite(low) & (op > 0)
    good = ok & (rng > 1e-9)
    with np.errstate(divide="ignore", invalid="ignore"):
        struct[good, 0] = ((close - op) / rng)[good]
        struct[good, 1] = ((high - np.maximum(op, close)) / rng)[good]
        struct[good, 2] = ((np.minimum(op, close) - low) / rng)[good]
        struct[good, 3] = ((close - low) / rng)[good]
    struct[ok & (rng <= 1e-9)] = (0.0, 0.0, 0.0, 0.5)
    nw = CFG.WEEKLY_N
    weekly = np.full((n, nw), np.nan, dtype=np.float32)
    start = 5 * nw - 1
    if n > start:
        for k in range(nw):
            a = close[start - 5 * k:n - 5 * k]
            b = close[start - 5 * k - 4:n - 5 * k - 4]
            with np.errstate(divide="ignore", invalid="ignore"):
                weekly[start:, nw - 1 - k] = a / b - 1.0
    return {"struct": struct, "vola": vola.astype(np.float32),
            "rsi": rsi.astype(np.float32), "volchg": vc_full.astype(np.float32),
            "weekly": weekly, "vr": vr}


def build_stock_data(bars, W=W_WINDOW, with_ctx=True):
    """预计算单只股票的匹配数据（窗口/标签/上下文切片），供多次复用。"""
    close = np.array([b["close"] for b in bars], float)
    n = len(close)
    lr = np.asarray(logret(list(close), is_etf=False), dtype=np.float64)
    lr_full = np.concatenate([[np.nan], lr])
    Wm, j = _windows(lr, W)
    data = {"n": n, "close": close,
            "dates": [b["date"] for b in bars],
            "date_ord": np.array([_ord(b["date"]) for b in bars]),
            "W": W, "windows": Wm, "j": j}
    if not Wm.shape[0]:
        data["labels"] = np.empty(0)
        data["label_date_ord"] = np.empty(0, dtype=int)
        data["ctx"] = {}
        return data
    if with_ctx:
        ctx = context_series(bars, lr=lr_full)
        ctxs = {k: v[j] for k, v in ctx.items()}
        valid = np.ones(len(j), dtype=bool)
        for v in ctxs.values():
            valid &= (np.isfinite(v).all(axis=1) if v.ndim > 1
                      else np.isfinite(v))
        j = j[valid]
        Wm = Wm[valid]
        data["ctx"] = {k: v[valid] for k, v in ctxs.items()}
    else:
        data["ctx"] = {}
    lab = np.full(len(j), np.nan)
    ok = (j + 1) < n
    lab[ok] = close[j[ok] + 1] / close[j[ok]] - 1.0
    data.update({"windows": Wm, "j": j, "labels": lab,
                 "label_date_ord": data["date_ord"][j + 1]})
    return data


def l1_factor_series(data, topk=None):
    """L1 形态因子：Top-K 自身历史（非重叠）相似窗口的次日上行概率。"""
    topk = topk or CFG.TOPK
    n = data["n"]
    out = np.full(n, np.nan)
    W, j, lab = data["W"], data["j"], data["labels"]
    Wm = data["windows"]
    if Wm.shape[0] < 6:
        return out
    d2 = np.sqrt(np.maximum(2.0 * W - 2.0 * Wm @ Wm.T, 0.0))
    for t in range(2 * W, n - 1):
        row = np.searchsorted(j, t)
        if row >= len(j) or j[row] != t:
            continue
        cnt = np.searchsorted(j, t - W, side="right")   # 非重叠 j <= t-W
        if cnt < 6:
            continue
        seg = d2[row, :cnt]
        k = min(topk, cnt)
        idx = np.argpartition(seg, k - 1)[:k] if k < cnt else np.arange(cnt)
        labs = lab[idx]
        m = np.isfinite(labs)
        if m.sum() >= 3:
            out[t] = float((labs[m] > 0).mean())
    return out


def _gram_dist(a, b, dim):
    """sqrt(mean((a_i-b_j)^2))，float32，in-place 少临时量。"""
    na = np.einsum("ij,ij->i", a, a)
    nb = np.einsum("ij,ij->i", b, b)
    dd = a @ b.T
    dd *= -2.0
    dd += na[:, None]
    dd += nb[None, :]
    np.maximum(dd, 0.0, out=dd)
    np.sqrt(dd, out=dd)
    dd /= np.float32(dim)
    return dd


def _extra_dist(tt, pp):
    """扩展特征距离（与 match_pool 的 dsc 同构），tt/pp 为切片后的上下文。

    形状项 + vr + struct + vola + rsi + volchg + weekly。
    NaN 自然传播（预热期行在面板组装时已被剔除）。
    """
    nt, npp = tt["struct"].shape[0], pp["struct"].shape[0]
    # 形状项（z-normalized 窗口）：sqrt(sum(diff^2)) = sqrt(2W - 2*cos)
    d = np.sqrt(np.maximum(
        2.0 * W_WINDOW - 2.0 * tt["_win"] @ pp["_win"].T,
        0.0)).astype(np.float32)
    # vr：0.6*min(|log(vr_t/vr_j)|, 2.5)，缺失按 0.30
    vt = np.log(np.maximum(tt["vr"], 1e-6)).astype(np.float32)
    vj = np.log(np.maximum(pp["vr"], 1e-6)).astype(np.float32)
    vd = np.abs(vt[:, None] - vj[None, :])
    np.minimum(vd, 2.5, out=vd)
    vd *= np.float32(0.6)
    d += vd
    # struct（缺失任一侧按 0.5*STRUCT_W）
    vd = _gram_dist(tt["struct"], pp["struct"], 4)
    vd *= np.float32(STRUCT_W)
    d += vd
    # vola
    lt = np.log(np.maximum(tt["vola"], 1e-12)).astype(np.float32)
    lj = np.log(np.maximum(pp["vola"], 1e-12)).astype(np.float32)
    vd = np.abs(lt[:, None] - lj[None, :])
    np.minimum(vd, 1.5, out=vd)
    vd *= np.float32(VOLA_W)
    d += vd
    # rsi
    vd = np.abs(tt["rsi"][:, None] - pp["rsi"][None, :])
    vd /= np.float32(100.0)
    vd *= np.float32(RSI_W)
    d += vd
    # volchg
    vd = np.abs(tt["volchg"][:, None] - pp["volchg"][None, :])
    np.minimum(vd, 1.5, out=vd)
    vd *= np.float32(VOLCHG_W)
    d += vd
    # weekly
    vd = _gram_dist(tt["weekly"], pp["weekly"], tt["weekly"].shape[1])
    vd *= np.float32(5.0)
    np.minimum(vd, 1.5, out=vd)
    vd *= np.float32(WEEKLY_W)
    d += vd
    return d


def _merge_topk(best_d, best_lab, best_ldate, d_blk, lab_blk, ldate_blk, topk):
    nt = d_blk.shape[0]
    lab_blk = np.broadcast_to(np.asarray(lab_blk, dtype=np.float32),
                              (nt, len(lab_blk)))
    ldate_blk = np.broadcast_to(np.asarray(ldate_blk),
                                (nt, len(ldate_blk)))
    comb_d = np.concatenate([best_d, d_blk], axis=1)
    comb_l = np.concatenate([best_lab, lab_blk], axis=1)
    comb_t = np.concatenate([best_ldate, ldate_blk], axis=1)
    if comb_d.shape[1] > topk:
        idx = np.argpartition(comb_d, topk - 1, axis=1)[:, :topk]
        vals = np.take_along_axis(comb_d, idx, axis=1)
        order = np.argsort(vals, axis=1)
        idx = np.take_along_axis(idx, order, axis=1)
        comb_d = np.take_along_axis(comb_d, idx, axis=1)
        comb_l = np.take_along_axis(comb_l, idx, axis=1)
        comb_t = np.take_along_axis(comb_t, idx, axis=1)
    return comb_d, comb_l, comb_t


def pool_match_series(data, peers, topk=None, block=8, min_samples=3,
                      apply_decay=True):
    """L2/L3 因子：目标股 vs 多只同伴历史窗口，Top-K 加权平均次日收益。

    data: build_stock_data(target)；peers: [build_stock_data(peer), ...]。
    返回与目标股 bars 对齐的因子数组（只在窗口结束日 j 上有值）。
    """
    topk = topk or CFG.TOPK
    W, jt, nt = data["W"], data["j"], data["windows"].shape[0]
    n = data["n"]
    out = np.full(n, np.nan)
    if nt == 0 or not peers:
        return out
    tdate = data["date_ord"][jt]
    tctx = data["ctx"]

    best_d = np.full((nt, 0), np.inf, dtype=np.float32)
    best_l = np.full((nt, 0), np.nan, dtype=np.float32)
    best_t = np.full((nt, 0), 0, dtype=np.int64)
    keys = ("vr", "struct", "vola", "rsi", "volchg", "weekly")
    for s in range(0, len(peers), block):
        grp = [p for p in peers[s:s + block] if p["windows"].shape[0]]
        if not grp:
            continue
        Wp = np.vstack([p["windows"] for p in grp])
        lab = np.concatenate([p["labels"] for p in grp]).astype(np.float32)
        ldate = np.concatenate([p["label_date_ord"] for p in grp])
        ctx_b = {"_win": Wp}
        for k in keys:
            parts = [p["ctx"][k] for p in grp]
            ctx_b[k] = (np.vstack(parts) if parts[0].ndim > 1
                        else np.concatenate(parts))
        tc = {"_win": data["windows"]}
        for k in keys:
            tc[k] = tctx[k]
        d_blk = _extra_dist(tc, ctx_b)
        d_blk = np.where(np.isfinite(lab)[None, :], d_blk, np.inf)
        d_blk = np.where(ldate[None, :] <= tdate[:, None], d_blk, np.inf)
        lab_b = np.where(np.isfinite(lab), lab, 0.0)
        best_d, best_l, best_t = _merge_topk(
            best_d, best_l, best_t, d_blk.astype(np.float32),
            lab_b.astype(np.float32), ldate, topk)

    # 向量化加权平均
    finite = np.isfinite(best_d) & (best_t > 0)
    age = np.where(finite, tdate[:, None] - best_t, np.inf)
    if apply_decay:
        w = np.where(age <= CFG.TIME_DECAY_DAYS, 1.0,
                     np.maximum(CFG.TIME_DECAY_RATE,
                                1.0 - (age - CFG.TIME_DECAY_DAYS)
                                / 365.0 * 0.5))
    else:
        w = np.ones_like(best_d, dtype=np.float64)
    w = np.where(finite, w, 0.0)
    num = np.sum(w * np.where(finite, best_l, 0.0), axis=1)
    den = np.sum(w, axis=1)
    cnt = finite.sum(axis=1)
    good = (den > 0) & (cnt >= min_samples)
    vals = np.full(nt, np.nan)
    vals[good] = num[good] / den[good]
    out[jt] = vals
    return out
