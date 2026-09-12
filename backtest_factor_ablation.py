#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_factor_ablation.py - 全A因子级消融（重构版）

把打分体系彻底拆成 21 个原子因子（含 L2 同行业、L3 同市值层、优化后筹码峰），
在 全A × 近1000交易日 面板上做 2^21 全组合穷举，训练段选型、验证段报告，
并做稳定度选择/LASSO/多重检验/逐股时序交叉验证等过拟合治理。

用法：
  python backtest_factor_ablation.py --stage chip      # 仅筹码参数优化
  python backtest_factor_ablation.py --stage build     # Pass A/B 因子构建
  python backtest_factor_ablation.py --stage enum      # 2^21 穷举
  python backtest_factor_ablation.py --stage validate  # 过拟合治理+报告
  python backtest_factor_ablation.py --stage all       # 全流程
  python backtest_factor_ablation.py --stage all --limit 200 --workers 4
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=("chip", "build", "panel", "enum", "validate",
                             "all"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sample", type=int, default=None,
                    help="按行业分层随机抽样 N 只")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--weights", default="both",
                    choices=("equal", "framework", "both"))
    ap.add_argument("--chip-sample", type=int, default=300)
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    from factor_lab import enumerate as enum_mod
    from factor_lab import panel as panel_mod
    from factor_lab import validate as val_mod
    from factor_lab.factors import K, FRAMEWORK_W

    t0 = time.time()
    if args.stage in ("chip", "all"):
        print("=" * 70)
        print("Stage 1/4 筹码峰参数优化（训练段）")
        print("=" * 70)
        panel_mod.stage_chip_opt(sample_n=args.chip_sample)

    if args.stage in ("build", "all"):
        print("=" * 70)
        print("Stage 2/4 因子面板构建（Pass A + Pass B）")
        print("=" * 70)
        panel_mod.stage_build(limit=args.limit, workers=args.workers,
                              sample=args.sample, seed=args.seed)

    if args.stage in ("panel",):
        panel_mod.assemble_panel()

    if args.stage in ("enum", "all"):
        print("=" * 70)
        print(f"Stage 3/4 2^{K} 全组合穷举")
        print("=" * 70)
        tags = ("equal", "framework") if args.weights == "both" else (
            args.weights,)
        for tag in tags:
            bw = np.ones(K) if tag == "equal" else FRAMEWORK_W
            enum_mod.run(base_w=bw, tag=tag)
            if tag != tags[-1]:
                import shutil
                from factor_lab.panel import CACHE_DIR
                shutil.move(
                    os.path.join(CACHE_DIR, "enum_stats.npz"),
                    os.path.join(CACHE_DIR, f"enum_stats_{tag}.npz"))
                shutil.move(
                    os.path.join(CACHE_DIR, "enum_top.json"),
                    os.path.join(CACHE_DIR, f"enum_top_{tag}.json"))

    if args.stage in ("validate", "all") and not args.no_verify:
        print("=" * 70)
        print("Stage 4/4 过拟合治理 + 逐股交叉验证")
        print("=" * 70)
        val_mod.run()

    print(f"\n全部完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
