#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票形态相似度预测 · 命令行版

与 stock_gui.py 共用同一套分析算法（直接导入），保证两者结果完全一致：
价格形态 + 量能状态 + 大盘 + 板块指数 四维加权匹配。

用法：python stock_predict.py [股票代码]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from stock_gui import analyze, normalize_code, AUTHOR, AUTHOR_EMAIL, AUTHOR_QQ, DISCLAIMER
except ImportError:
    print("错误：需要与本文件同目录放置 stock_gui.py（共用算法）")
    sys.exit(1)


def main():
    if len(sys.argv) > 1:
        code_in = " ".join(sys.argv[1:])
    else:
        try:
            code_in = input("请输入股票代码（如 002241 / 600519）：")
        except EOFError:
            return
    try:
        full = normalize_code(code_in)
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    print("\n拉取数据并计算中...")
    res = analyze(full)
    q, tp, pred = res["quote"], res["t_pred"], res["pred"]

    print("=" * 68)
    print(f"{q['name']} ({res['full_code']})  快照 {q['time']}")
    print(f"昨收 {res['prev_close']:.2f} | 今开 {q['open']:.2f} "
          f"(缺口 {res['gap_today']:+.2f}%) | 现价 {q['price']:.2f} "
          f"({(q['price']/res['prev_close']-1)*100:+.2f}%)"
          + ("  [含盘中实时bar]" if res["has_live"] else ""))
    idx_txt = (f"{res['idx_chg_today']:+.2f}%"
               if res["idx_chg_today"] is not None else "未知")
    vr_txt = (f"量比 {res['vr_now']:.2f}（{res['cur_regime']}）"
              if res["vr_now"] is not None else "数据不足")
    sec_txt = (f"{res['sector_name']} {res['sector_chg_today']:+.2f}%"
               if res["sector_name"] and res["sector_chg_today"] is not None
               else "未知")
    print(f"大盘(上证) {idx_txt} | 板块 {sec_txt} | 本股量能 {vr_txt}")

    print("-" * 68)
    print("今日(T)预测 [锚定今开]")
    print(f"{'分位':<6}{'收盘':>10}{'最高':>10}{'最低':>10}")
    for p in (10, 25, 50, 75, 90):
        print(f"P{p:<5}{tp['cl'][p]:>10.2f}{tp['hi'][p]:>10.2f}{tp['lo'][p]:>10.2f}")
    print(f"开盘->收盘 上行概率 {tp['up_prob']*100:.0f}%   "
          f"有效样本 {res['src_n']}/{len(res['samples'])}"
          f"（{res['filter_note']}）")
    if res["has_live"] and res["clamped"]:
        print(f"[盘中实时修正] 已实现最高 {res['live_high']:.2f} / "
              f"最低 {res['live_low']:.2f}，已并入预测区间")

    print("-" * 68)
    print(f"次日(T+1)预测: 开{pred['open']:.2f} 收{pred['close']:.2f} "
          f"高{pred['high']:.2f} 低{pred['low']:.2f}")

    print("-" * 68)
    print("相似历史参考日期（含 量能/大盘 匹配）")
    print(f"{'T日':<12}{'T+1日':<12}{'次日收/开':>9} {'T+2收/开':>9}  标记")
    for s in res["samples"]:
        mark = ""
        if s.get("regime") == res.get("cur_regime"):
            mark += "[量]"
        if s.get("idx_chg") is not None and abs(s["idx_chg"]) <= 0.8:
            mark += "[盘]"
        print(f"{s['t_date']:<12}{s['n1_date']:<12}"
              f"{s['cl_o']*100:>+8.2f}% {s['t2_cl']*100:>+8.2f}%  {mark}")

    print("-" * 68)
    sigs = res["signals"]
    print(f"近期买卖信号（每日一个，按优先级合并）")
    for i, day, typ, txt in sigs[-12:]:
        tag = "买" if typ == "BUY" else "卖"
        print(f"  {day} [{tag}] {txt}")
    if not sigs:
        print("  近期无")

    print("=" * 68)
    print(f"作者：{AUTHOR}  邮箱：{AUTHOR_EMAIL}  QQ：{AUTHOR_QQ}")
    print(DISCLAIMER)


if __name__ == "__main__":
    main()
