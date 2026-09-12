# -*- coding: utf-8 -*-
"""面板构建：全A × 近1000交易日 × 21原子因子。

阶段：
  chip_opt  先在小样本上用训练段优化筹码参数（防泄漏）
  build     逐股计算框架因子/L1/筹码（Pass A）→ 行业/市值层匹配（Pass B）
  panel     拼装 + 逐日截面标准化 + 训练/验证切分
"""
import json
import os
import pickle
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

from . import chips as chip_mod
from . import data as data_mod
from . import factors as fac_mod
from . import matching as mat_mod
from .factors import K

CACHE_DIR = os.path.join("research", "factor_lab")
PANEL_FILE = os.path.join(CACHE_DIR, "panel.npz")
STOCK_CACHE = os.path.join(CACHE_DIR, "stocks_pass_a.pkl")
MATCH_CACHE = os.path.join(CACHE_DIR, "match_pass_b.pkl")
CHIP_PARAMS_FILE = os.path.join(CACHE_DIR, "chip_params.json")

EVAL_DAYS = 1000
PRE = 300
MIN_BARS = 400
TRAIN_FRAC = 0.7
EMBARGO = 1
L2_N = 50
L3_N = 30

_G = {}


def _worker_init(glob):
    _G.clear()
    _G.update(glob)


def global_eval_dates():
    with data_mod.sg.db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_bars ORDER BY date DESC LIMIT ?",
            (EVAL_DAYS,)).fetchall()
    return sorted(r[0] for r in rows)


def ret5_map(series_by_date):
    """{date: 5日累计收益}（按日期升序的 chain）。"""
    dates = sorted(series_by_date.keys())
    out = {}
    for i in range(4, len(dates)):
        c = 1.0
        for k in range(i - 4, i + 1):
            c *= 1.0 + series_by_date[dates[k]]
        out[dates[i]] = c - 1.0
    return out


def industry_context():
    """返回 ({industry: {date: r5}}, {date: (med, frozenset(lead))})。"""
    ind_ret = data_mod.industry_ret_maps()
    dates = sorted(ind_ret.keys())
    ind_daily = {}
    for d, m in ind_ret.items():
        for ind, r in m.items():
            ind_daily.setdefault(ind, {})[d] = r
    ind5 = {ind: ret5_map(series) for ind, series in ind_daily.items()}
    med_lead = {}
    for i in range(4, len(dates)):
        d = dates[i]
        r5 = {}
        for k in range(i - 4, i + 1):
            for ind, r in ind_ret[dates[k]].items():
                r5[ind] = r5.get(ind, 0.0) + r
        vals = sorted(r5.values())
        med = vals[len(vals) // 2] if vals else 0.0
        srt = sorted(r5.items(), key=lambda kv: -kv[1])
        lead = frozenset(x for x, _ in srt[:max(1, len(srt) // 5)])
        med_lead[d] = (med, lead)
    return ind5, med_lead


def mkt5_map():
    rows = data_mod.load_index()
    closes = {r["date"]: r["close"] for r in rows}
    dates = sorted(closes)
    out = {}
    for i in range(4, len(dates)):
        c0, c1 = closes[dates[i - 4]], closes[dates[i]]
        if c0 and c1 and c0 > 0:
            out[dates[i]] = c1 / c0 - 1.0
    return out


def _ind_ctx_for(industry, ind5, med_lead):
    ctx = {}
    series = ind5.get(industry) or {}
    for d, (med, lead) in med_lead.items():
        r5 = series.get(d)
        ctx[d] = (r5, med, industry in lead)
    return ctx


def _pass_a_one(code):
    try:
        bars = data_mod.load_stock(code, tail=EVAL_DAYS + PRE)
        if len(bars) < MIN_BARS:
            return code, None
        ind = _G["industry_of"].get(code, "")
        ind_ctx = _ind_ctx_for(ind, _G["ind5"], _G["med_lead"])
        dates, Fm, y1, y5, y10 = fac_mod.compute_stock_factors(
            bars, ind_ctx5=ind_ctx, mkt5=_G["mkt5"])
        sd = mat_mod.build_stock_data(bars, with_ctx=False)
        Fm[:, fac_mod.F["L1形态"]] = mat_mod.l1_factor_series(sd)
        start = max(0, len(bars) - EVAL_DAYS)
        cf, _ = chip_mod.chip_features_stock(bars, start, _G.get("chip_params"))
        Fm[:, fac_mod.F["筹码支撑"]] = cf[:, 0]
        Fm[:, fac_mod.F["筹码压力"]] = cf[:, 1]
        Fm[:, fac_mod.F["筹码获利"]] = cf[:, 2]
        return code, {"dates": dates, "F": Fm.astype(np.float32),
                      "y1": y1.astype(np.float32),
                      "y5": y5.astype(np.float32),
                      "y10": y10.astype(np.float32)}
    except Exception as e:
        import traceback
        return code, {"error": f"{e}\n{traceback.format_exc()}"}


def stage_chip_opt(sample_n=300, seed=7):
    from .chips import optimize as chip_optimize
    codes = data_mod.list_codes(min_bars=MIN_BARS)
    meta = data_mod.load_meta()
    rng = np.random.RandomState(seed)
    by_ind = {}
    for c in codes:
        by_ind.setdefault(meta.get(c, {}).get("industry", ""), []).append(c)
    sample = []
    per_ind = max(1, sample_n // max(1, len(by_ind)))
    for ind, cs in by_ind.items():
        pick = rng.choice(cs, size=min(per_ind, len(cs)), replace=False)
        sample.extend(pick.tolist())
    sample = sample[:sample_n]
    print(f"筹码优化样本: {len(sample)} 只")
    eval_dates = global_eval_dates()
    train_dates = set(eval_dates[:int(len(eval_dates) * TRAIN_FRAC)])
    bars_list = []
    for c in sample:
        bars = data_mod.load_stock(c, tail=EVAL_DAYS + PRE)
        if len(bars) < MIN_BARS:
            continue
        cl = np.array([b["close"] for b in bars], float)
        y1 = np.full(len(bars), np.nan)
        y1[:-1] = cl[1:] / np.maximum(cl[:-1], 1e-12) - 1.0
        bars_list.append((bars, [b["date"] for b in bars], y1))
    best, results = chip_optimize(bars_list, train_dates)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CHIP_PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump({"best": best,
                   "top": [{"score": r[0], "cfg": r[1],
                            "ic": [None if not np.isfinite(x) else float(x)
                                   for x in r[2]]}
                           for r in results[:10]]},
                  f, ensure_ascii=False, indent=1)
    print(f"筹码参数写入 {CHIP_PARAMS_FILE}: {best}")
    return best


def _load_chip_params():
    if os.path.exists(CHIP_PARAMS_FILE):
        with open(CHIP_PARAMS_FILE, encoding="utf-8") as f:
            return json.load(f)["best"]
    return None


def _build_stock_datas(codes):
    datas = {}
    for c in codes:
        b = data_mod.load_stock(c, tail=EVAL_DAYS + PRE)
        if len(b) >= MIN_BARS:
            datas[c] = mat_mod.build_stock_data(b)
    return datas


def _l2_worker(group):
    datas = _build_stock_datas(group)
    cap = _G["mktcap"]
    out = {}
    for c, d in datas.items():
        peers = [p for p in datas if p != c]
        peers.sort(key=lambda p: abs(
            np.log(max(cap.get(p, 1e8), 1e8))
            - np.log(max(cap.get(c, 1e8), 1e8))))
        arr = mat_mod.pool_match_series(d, [datas[p] for p in peers[:L2_N]])
        out[c] = arr.astype(np.float32)
    return out


def _l3_worker(group):
    datas = _build_stock_datas(group)
    cap, ind_of = _G["mktcap"], _G["industry_of"]
    out = {}
    for c, d in datas.items():
        peers = [p for p in datas
                 if p != c and ind_of.get(p, "") != ind_of.get(c, "")]
        peers.sort(key=lambda p: abs(
            np.log(max(cap.get(p, 1e8), 1e8))
            - np.log(max(cap.get(c, 1e8), 1e8))))
        if len(peers) < 3:
            continue
        arr = mat_mod.pool_match_series(d, [datas[p] for p in peers[:L3_N]])
        out[c] = arr.astype(np.float32)
    return out


def stage_build(limit=None, workers=None):
    t0 = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)
    meta = data_mod.load_meta()
    codes = data_mod.list_codes(min_bars=MIN_BARS)
    if limit:
        codes = codes[:limit]
    print(f"股票池 {len(codes)} 只")
    ind5, med_lead = industry_context()
    glob = {"industry_of": {c: meta.get(c, {}).get("industry", "")
                            for c in codes},
            "tier_of": {c: (meta.get(c, {}).get("tier") or "")
                        for c in codes},
            "mktcap": {c: (meta.get(c, {}).get("mktcap") or 0.0)
                       for c in codes},
            "ind5": ind5, "med_lead": med_lead, "mkt5": mkt5_map(),
            "chip_params": _load_chip_params()}
    print(f"筹码参数: {glob['chip_params']}")

    workers = workers or max(1, min(os.cpu_count() or 4, 8))
    res = {}
    errs = 0
    with ProcessPoolExecutor(max_workers=workers,
                             initializer=_worker_init,
                             initargs=(glob,)) as exe:
        futs = {exe.submit(_pass_a_one, c): c for c in codes}
        done = 0
        for fut in as_completed(futs):
            code, out = fut.result()
            done += 1
            if out is None:
                continue
            if "error" in out:
                errs += 1
                if errs <= 3:
                    print(f"  [{code}] {out['error'][:300]}")
                continue
            res[code] = out
            if done % 500 == 0:
                print(f"  Pass A {done}/{len(codes)} 有效{len(res)} "
                      f"{time.time()-t0:.0f}s")
    print(f"Pass A 完成：有效 {len(res)}/{len(codes)} 异常{errs} "
          f"{time.time()-t0:.0f}s")
    with open(STOCK_CACHE, "wb") as f:
        pickle.dump(res, f, protocol=4)

    groups = {}
    for c in res:
        groups.setdefault(glob["industry_of"].get(c, ""), []).append(c)
    tasks = [g for g in groups.values() if len(g) >= 2]
    L2 = {}
    with ProcessPoolExecutor(max_workers=workers,
                             initializer=_worker_init,
                             initargs=(glob,)) as exe:
        futs = {exe.submit(_l2_worker, g): g for g in tasks}
        done = 0
        for fut in as_completed(futs):
            L2.update(fut.result())
            done += 1
            if done % 40 == 0:
                print(f"  L2 组 {done}/{len(tasks)} {time.time()-t0:.0f}s")
    print(f"L2 完成：{len(L2)} 只 {time.time()-t0:.0f}s")

    groups3 = {}
    for c in res:
        groups3.setdefault(glob["tier_of"].get(c, ""), []).append(c)
    tasks3 = [g for g in groups3.values() if len(g) >= 2]
    L3 = {}
    if not tasks3:
        print("L3: 无有效分组，跳过")
    with ProcessPoolExecutor(max_workers=max(1, min(workers, len(tasks3))),
                             initializer=_worker_init,
                             initargs=(glob,)) as exe:
        futs = {exe.submit(_l3_worker, g): g for g in tasks3}
        done = 0
        for fut in as_completed(futs):
            L3.update(fut.result())
            done += 1
            print(f"  L3 组 {done}/{len(tasks3)} {time.time()-t0:.0f}s")
    print(f"L3 完成：{len(L3)} 只 {time.time()-t0:.0f}s")
    with open(MATCH_CACHE, "wb") as f:
        pickle.dump({"L2": L2, "L3": L3}, f, protocol=4)
    print(f"匹配缓存写入 {MATCH_CACHE}")
    return res, L2, L3


def assemble_panel():
    with open(STOCK_CACHE, "rb") as f:
        res = pickle.load(f)
    with open(MATCH_CACHE, "rb") as f:
        mm = pickle.load(f)
    L2, L3 = mm["L2"], mm["L3"]

    eval_dates = global_eval_dates()
    eval_set = set(eval_dates)
    date_idx = {d: i for i, d in enumerate(eval_dates)}
    codes = sorted(res.keys())
    code_idx = {c: i for i, c in enumerate(codes)}

    rd, rc, X, Y1 = [], [], [], []
    have = 0
    for c in codes:
        d = res[c]
        dates = d["dates"]
        Fm = d["F"].copy()
        Fm[:, fac_mod.F["L2同行业"]] = L2.get(c, np.nan)
        Fm[:, fac_mod.F["L3同市值"]] = L3.get(c, np.nan)
        keep = [i for i, dt in enumerate(dates) if dt in eval_set]
        if len(keep) < 60:
            continue
        Fm = Fm[keep]
        y1 = d["y1"][keep]
        ok = np.isfinite(Fm).all(axis=1) & np.isfinite(y1)
        if ok.sum() < 60:
            continue
        i = len(ok) - 1
        while i >= 0 and not ok[i]:
            i -= 1
        end = i
        while i >= 0 and ok[i]:
            i -= 1
        start = i + 1
        if end - start + 1 < 60:
            continue
        sel = slice(start, end + 1)
        di = np.array([date_idx[dates[k]] for k in np.array(keep)[sel]],
                      dtype=np.int32)
        rd.append(di)
        rc.append(np.full(len(di), code_idx[c], dtype=np.int32))
        X.append(Fm[sel].astype(np.float32))
        Y1.append(y1[sel].astype(np.float32))
        have += 1
    X = np.vstack(X)
    Y1 = np.concatenate(Y1)
    rd = np.concatenate(rd)
    rc = np.concatenate(rc)
    print(f"面板: {have} 只 × 有效行 {len(Y1)}, K={K}")

    order = np.argsort(rd, kind="stable")
    X, Y1, rd, rc = X[order], Y1[order], rd[order], rc[order]
    starts = np.searchsorted(rd, np.arange(len(eval_dates)))
    ends = np.searchsorted(rd, np.arange(len(eval_dates)) + 1)
    big = (ends - starts) >= 30
    keep_row = big[rd]
    X, Y1, rd, rc = X[keep_row], Y1[keep_row], rd[keep_row], rc[keep_row]
    starts = np.searchsorted(rd, np.arange(len(eval_dates)))
    ends = np.searchsorted(rd, np.arange(len(eval_dates)) + 1)
    for k in range(K):
        col = X[:, k]
        for d in range(len(eval_dates)):
            a, b = starts[d], ends[d]
            if b - a < 30:
                continue
            seg = col[a:b].astype(np.float64)
            sd = seg.std()
            if sd <= 1e-12:
                col[a:b] = 0.0
                continue
            z = (seg - seg.mean()) / sd
            np.clip(z, -3.5, 3.5, out=z)
            col[a:b] = z
    X = np.nan_to_num(X, nan=0.0)

    train_n = int(len(eval_dates) * TRAIN_FRAC)
    is_train = rd < train_n
    is_val = rd >= train_n + EMBARGO
    os.makedirs(CACHE_DIR, exist_ok=True)
    np.savez(PANEL_FILE, X=X, Y1=Y1, date=rd, code=rc,
             is_train=is_train, is_val=is_val,
             eval_dates=np.array(eval_dates), codes=np.array(codes),
             train_n=np.array(train_n), embargo=np.array(EMBARGO))
    print(f"面板写入 {PANEL_FILE} "
          f"({os.path.getsize(PANEL_FILE)/1e6:.0f}MB)")
    return {"X": X, "Y1": Y1, "date": rd, "code": rc,
            "is_train": is_train, "is_val": is_val,
            "eval_dates": eval_dates, "codes": codes, "train_n": train_n}


def load_panel():
    z = np.load(PANEL_FILE, allow_pickle=True)
    is_train = z["is_train"]
    if "train_n" in z.files:
        train_n = int(z["train_n"])
    else:
        tr = np.nonzero(is_train)[0]
        train_n = int(z["date"][tr].max()) + 1 if len(tr) else 0
    return {"X": z["X"], "Y1": z["Y1"], "date": z["date"],
            "code": z["code"], "is_train": is_train,
            "is_val": z["is_val"],
            "eval_dates": [str(x) for x in z["eval_dates"]],
            "codes": [str(x) for x in z["codes"]],
            "train_n": train_n}
