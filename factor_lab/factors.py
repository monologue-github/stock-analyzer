# -*- coding: utf-8 -*-
"""21 原子因子（框架口径）:

 0 MACD      1 KDJ       2 RSI       3 量价      4 MA20
 5 MA趋势    6 爆发力     7 量能      8 板块      9 布林带
10 ADX      11 L1形态   12 L2同行业 13 L3同市值 14 筹码支撑
15 筹码压力 16 筹码获利 17 大盘5日  18 BIAS20  19 BIAS60
20 VOLA20

逐日打分与 stock_gui.daily_pick_score 完全同构（v4.0.1 口径），
L1/L2/L3/筹码 分别由 matching.py / chips.py 填充。
所有因子只用 T 日及以前数据。
"""
import math

import numpy as np

from stock_gui import (calc_adx, calc_boll, calc_kdj, calc_macd, calc_rsi,
                       sma_period, W_WINDOW)

FACTOR_NAMES = (
    "MACD", "KDJ", "RSI", "量价", "MA20", "MA趋势", "爆发力", "量能",
    "板块", "布林带", "ADX", "L1形态", "L2同行业", "L3同市值",
    "筹码支撑", "筹码压力", "筹码获利", "大盘5日", "BIAS20", "BIAS60",
    "VOLA20",
)
K = len(FACTOR_NAMES)
F = {n: i for i, n in enumerate(FACTOR_NAMES)}

# 框架 IND_W 权重（新因子默认 1.0；筹码沿用 0.8/3 拆分为三等份）
FRAMEWORK_W = np.array([
    1.1, 0.9, 0.9, 1.0, 1.0, 1.2, 1.0, 0.9, 1.0, 0.8, 0.8,
    1.2, 1.0, 1.0, 0.8, 0.8, 0.8, 1.0, 1.0, 1.0, 1.0,
], dtype=np.float64)


def _arr(seq):
    return np.array([np.nan if v is None else v for v in seq], dtype=np.float64)


def _roll_mean(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(np.nan_to_num(x), 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _roll_mean_prev(x, n):
    """前 n 日（不含当日）均值：out[i] = mean(x[i-n:i])。"""
    return np.concatenate((np.full(1, np.nan), _roll_mean(x, n)[:-1]))


def _roll_std(x, n, ddof=0):
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        seg = x[i - n + 1:i + 1]
        out[i] = seg.std(ddof=ddof)
    return out


def compute_stock_factors(bars, ind_ctx5=None, mkt5=None):
    """返回 (dates, F[n×K], y1, y5, y10)；NaN 表示不可用。

    ind_ctx5: {date: (r5, med, lead_flag)} 行业板块上下文（可 None）
    mkt5:     {date: 指数近5日累计收益}
    """
    n = len(bars)
    out = np.full((n, K), np.nan)
    dates = [b["date"] for b in bars]
    closes = np.array([b["close"] for b in bars], dtype=np.float64)
    highs = np.array([b["high"] or np.nan for b in bars], dtype=np.float64)
    lows = np.array([b["low"] or np.nan for b in bars], dtype=np.float64)
    vols = np.array([b.get("vol") or 0.0 for b in bars], dtype=np.float64)

    dif, dea, _ = calc_macd(list(closes))
    k_, d_, _ = calc_kdj(bars)
    r6 = calc_rsi(list(closes), 6)
    _, b_up, b_low = calc_boll(list(closes))
    pdi, mdi, adx = calc_adx(bars)
    ma20 = _arr(sma_period(list(closes), 20))
    ma60 = _arr(sma_period(list(closes), 60))
    dif, dea = _arr(dif), _arr(dea)
    k_, d_ = _arr(k_), _arr(d_)
    r6 = _arr(r6)
    b_up, b_low = _arr(b_up), _arr(b_low)
    pdi, mdi, adx = _arr(pdi), _arr(mdi), _arr(adx)

    i = np.arange(n)
    prev = np.maximum(i - 1, 0)
    c, cp = closes, closes[prev]

    # 0 MACD
    cu = (dif[prev] <= dea[prev]) & (dif > dea)
    cd = (dif[prev] >= dea[prev]) & (dif < dea)
    v = np.where(cu, 2.0, np.where((~cu) & (dif > dea), 1.0,
                                   np.where(cd, -2.0, -1.0)))
    v[~np.isfinite(dif) | ~np.isfinite(dea)] = np.nan
    out[:, F["MACD"]] = v

    # 1 KDJ
    cu = (k_[prev] <= d_[prev]) & (k_ > d_) & (k_ < 45)
    cd = (k_[prev] >= d_[prev]) & (k_ < d_) & (k_ > 65)
    v = np.where(cu, 2.0, np.where((~cu) & (k_ > d_), 1.0,
                                   np.where(cd, -2.0, -1.0)))
    v[~np.isfinite(k_) | ~np.isfinite(d_)] = np.nan
    out[:, F["KDJ"]] = v

    # 2 RSI（v4.0.1 动量口径）
    v = np.zeros(n)
    v[r6 < 30] = -1.0
    v[r6 > 70] = 1.0
    v[~np.isfinite(r6)] = np.nan
    out[:, F["RSI"]] = v

    # 3 量价（今日量 / 前5日均量）
    v5 = _roll_mean_prev(vols, 5)
    vr = np.where(v5 > 0, vols / np.maximum(v5, 1e-12), 0.0)
    v = np.where((vr > 1.5) & (c > cp), 1.0,
                 np.where((vr > 1.5) & (c < cp), -1.0, 0.0))
    v[~np.isfinite(v5)] = np.nan
    out[:, F["量价"]] = v

    # 4 MA20 / 5 MA趋势
    m20, m20p = ma20, ma20[prev]
    m60 = ma60
    v = np.where((c > m20) & (m20 > m20p), 1.0,
                 np.where((c < m20) & (m20 < m20p), -1.0, 0.0))
    v[~np.isfinite(m20) | ~np.isfinite(m20p)] = np.nan
    out[:, F["MA20"]] = v
    v = np.where((c > m20) & (m20 > m60) & (m20 > m20p), 2.0,
                 np.where((c < m20) & (m20 < m60) & (m20 < m20p), -2.0, 0.0))
    v[~np.isfinite(m20) | ~np.isfinite(m60) | ~np.isfinite(m20p)] = np.nan
    out[:, F["MA趋势"]] = v

    # 6 爆发力（20日动量）
    ret20 = np.full(n, np.nan)
    ret20[20:] = c[20:] / np.maximum(c[:-20], 1e-12) - 1.0
    v = np.zeros(n)
    v[(ret20 >= 0.10) & (ret20 < 0.35)] = 2.0
    v[(ret20 >= 0.05) & (ret20 < 0.10)] = 1.0
    v[ret20 >= 0.35] = -2.0
    v[ret20 <= -0.15] = -1.0
    v[~np.isfinite(ret20)] = np.nan
    out[:, F["爆发力"]] = v

    # 7 量能（5日均量 / 20日均量）
    v20m = _roll_mean(vols, 20)
    v5m = _roll_mean(vols, 5)
    ratio = np.where(v20m > 0, v5m / np.maximum(v20m, 1e-12), 0.0)
    v = np.where((ratio > 1.5) & (c > cp), 1.0, 0.0)
    v[~np.isfinite(v20m) | ~np.isfinite(v5m)] = np.nan
    out[:, F["量能"]] = v

    # 9 布林带
    v = np.where(c < b_low, 1.0, np.where(c > b_up, -1.0, 0.0))
    v[~np.isfinite(b_up) | ~np.isfinite(b_low)] = np.nan
    out[:, F["布林带"]] = v

    # 10 ADX
    v = np.where((adx >= 20) & (pdi > mdi), 1.0,
                 np.where((adx >= 20) & (mdi > pdi), -1.0, 0.0))
    v[~np.isfinite(adx) | ~np.isfinite(pdi) | ~np.isfinite(mdi)] = np.nan
    out[:, F["ADX"]] = v

    # 17 大盘5日（指数动量）
    if mkt5:
        out[:, F["大盘5日"]] = np.array(
            [mkt5.get(d, np.nan) for d in dates], dtype=np.float64)

    # 18/19/20 BIAS / VOLA
    out[:, F["BIAS20"]] = np.where(np.isfinite(ma20) & (ma20 > 0),
                                   c / np.maximum(ma20, 1e-12) - 1.0, np.nan)
    out[:, F["BIAS60"]] = np.where(np.isfinite(ma60) & (ma60 > 0),
                                   c / np.maximum(ma60, 1e-12) - 1.0, np.nan)
    lr = np.full(n, np.nan)
    lr[1:] = np.log(np.maximum(c[1:], 1e-12) / np.maximum(c[:-1], 1e-12))
    out[:, F["VOLA20"]] = _roll_std(lr, 20, ddof=0)

    # 8 板块（行业5日强势 + 前20%领先）
    if ind_ctx5:
        col = np.zeros(n)
        ok = np.zeros(n, dtype=bool)
        for t, d in enumerate(dates):
            ctx = ind_ctx5.get(d)
            if ctx is None:
                continue
            r5, med, lead = ctx
            if r5 is None:
                continue
            ok[t] = True
            if r5 > med:
                col[t] += 1.0
            if lead:
                col[t] += 1.0
        col[~ok] = np.nan
        out[:, F["板块"]] = col

    # 目标：未来1/5/10日收益
    y1 = np.full(n, np.nan)
    y1[:-1] = c[1:] / np.maximum(c[:-1], 1e-12) - 1.0
    y5 = np.full(n, np.nan)
    if n > 5:
        y5[:-5] = c[5:] / np.maximum(c[:-5], 1e-12) - 1.0
    y10 = np.full(n, np.nan)
    if n > 10:
        y10[:-10] = c[10:] / np.maximum(c[:-10], 1e-12) - 1.0
    return dates, out, y1, y5, y10
