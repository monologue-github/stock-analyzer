#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_rot_t10.py - Rot-T10only 稳健性复核（防选优偏差）

1) 分段窗口：把 408 交易日切成 3 段，各段独立组合模拟，看年化/回撤是否稳定为正
2) 参数敏感性：rot_top / cooldown 邻域取值，看 +15.6% 是否孤峰
只读缓存与 research/v4_preds.pkl，不改任何状态。"""
import importlib.util
import os
import pickle
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location("sp", os.path.join(HERE, "stock_predict.py"))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)


def slice_mats(mats, d0, d1):
    cal, M, codes = mats
    idx = [k for k, d in enumerate(cal) if d0 <= d <= d1]
    if len(idx) < 60:
        return None
    i0, i1 = idx[0], idx[-1]
    Ms = {}
    for k, v in M.items():
        if isinstance(v, np.ndarray) and v.ndim == 2 and v.shape[1] == len(cal):
            Ms[k] = v[:, i0:i1 + 1].copy()
        else:
            Ms[k] = v
    return (cal[i0:i1 + 1], Ms, codes)


def main():
    print("载入 preds 缓存 ...")
    with open(os.path.join(HERE, "research", "v4_preds.pkl"), "rb") as f:
        blob = pickle.load(f)
    preds = blob["preds"]
    # 与 run_v4_research 相同的时间对齐过滤（测试段结束距最新日 ≤45 自然日）
    import datetime as _dt
    last_d = max(r["dates"][-1] for r in preds)
    _ld = _dt.date.fromisoformat(last_d)
    preds = [r for r in preds
             if (_ld - _dt.date.fromisoformat(r["dates"][-1])).days <= 45]
    print("对齐后 %d 只" % len(preds))
    with sp.db_conn() as conn:
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
    print("市场/行业上下文（一遍扫描）...")
    mkt, ind = sp._v4_mkt_ind_ctx(ind_of)
    mats = sp._v4_stack(preds)
    sp._v4_attach_rotation(mats[1], mats[0], mats[2], ind_of, mkt, ind)
    cal = mats[0]
    print("日历：%s ~ %s（%d 交易日）" % (cal[0], cal[-1], len(cal)))

    T = sp._V4_TIERS
    base_off = {"use_logistic": False, "use_adaptive": False,
                "use_lgbm": False, "use_quantile": False,
                "use_dist_exit": False}
    cfgs = {
        "Fullv4(平衡)": (T["平衡"], {"mode": "full", "tier_name": "平衡"}),
        "T10only(平衡)": (T["平衡"], {"mode": "full", "tier_name": "平衡",
                                      "h_only": 10, "cooldown": 5, "min_hold": 5}),
        "RotT10(平衡)": (T["平衡"], {"mode": "full", "tier_name": "平衡",
                                     "h_only": 10, "cooldown": 5, "min_hold": 5,
                                     "rot_top": 0.70}),
        "RotTop30(平衡)": (T["平衡"], {"mode": "full", "tier_name": "平衡",
                                       "rot_top": 0.70}),
        "RotT10(激进档)": (T["激进"], {"mode": "full", "tier_name": "激进",
                                       "h_only": 10, "cooldown": 5, "min_hold": 5,
                                       "rot_top": 0.70}),
        "baseline(平衡)": (T["平衡"], dict(base_off, mode="baseline")),
    }
    windows = [
        ("全窗口", None, None),
        ("段1 %s~" % cal[0], cal[0], "2025-06-30"),
        ("段2 2025-07~", "2025-07-01", "2026-01-31"),
        ("段3 2026-02~", "2026-02-01", cal[-1]),
    ]
    print("\n==== 分段稳健性（年化 / 最大回撤 / Calmar / 胜率 / 交易数）====")
    hdr = "%-18s" % "配置"
    for wn, _, _ in windows:
        hdr += " | %-26s" % wn[:26]
    print(hdr)
    for cn, (tier, rules) in cfgs.items():
        row = "%-18s" % cn
        for wn, d0, d1 in windows:
            m = None
            if d0 is None:
                m = sp._v4_portfolio_sim(mats, tier, rules)
            else:
                ms = slice_mats(mats, d0, d1)
                if ms:
                    m = sp._v4_portfolio_sim(ms, tier, rules)
            if m and m["trades"]:
                row += " | %+6.1f%%/%6.1f%%/%5.2f/%3.0f%%%4d" % (
                    m["ann"] * 100, m["mdd"] * 100, m["calmar"],
                    m["winrate"] * 100, m["trades"])
            else:
                row += " | %-26s" % "无交易"
        print(row)

    print("\n==== 参数敏感性（全窗口，RotT10(平衡) 邻域）====")
    print("%-34s %8s %8s %7s %6s %5s" % ("配置", "年化", "回撤", "Calmar", "胜率", "n"))
    sens = []
    for rt in (0.60, 0.70, 0.80, 0.90):
        sens.append(("rot_top=%.2f" % rt,
                     {"h_only": 10, "cooldown": 5, "min_hold": 5, "rot_top": rt}))
    for cd in (3, 5, 8):
        sens.append(("cooldown=%d" % cd,
                     {"h_only": 10, "cooldown": cd, "min_hold": cd, "rot_top": 0.70}))
    sens.append(("rot_strong(替代rot_top)",
                 {"h_only": 10, "cooldown": 5, "min_hold": 5, "rot_strong": True}))
    sens.append(("无轮动(T10only)", {"h_only": 10, "cooldown": 5, "min_hold": 5}))
    for name, vr in sens:
        rules = dict(vr, mode="full", tier_name="平衡")
        m = sp._v4_portfolio_sim(mats, T["平衡"], rules)
        print("%-34s %+7.1f%% %+7.1f%% %7.2f %5.1f%% %5d" % (
            name, m["ann"] * 100, m["mdd"] * 100, m["calmar"],
            m["winrate"] * 100, m["trades"]))

    print("\n==== 段3 诊断：T+10 信号样本外 IC 分段衰减检查 ====")
    for wn, d0, d1 in windows[1:]:
        ms = slice_mats(mats, d0, d1)
        if not ms:
            continue
        _, Ms, _ = ms
        e = sp._v4_pool_eval(Ms["ml10"], Ms["y10"])
        print("%s: T+10 pooled IC = %+.4f (n=%s)" % (
            wn, e["ic"] if e["ic"] is not None else float("nan"),
            f"{e['n']:,}"))


if __name__ == "__main__":
    main()
