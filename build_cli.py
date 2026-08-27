#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_cli.py - 从 stock_gui.py 自动生成独立单文件版 stock_predict.py

stock_gui.py 是唯一算法源。本脚本做三件事：
  1. 把缓存层（原 stock_cache.py 的内容，现已内嵌在 stock_gui.py 里）抽出
  2. 抽出算法主体（QT_URL 起，到 slice_view 前止，不含任何 tkinter 代码）
  3. 拼上 CLI 专属代码（推送/命令行入口），写出 stock_predict.py

改完 stock_gui.py 的算法后运行： python build_cli.py
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_PATH = os.path.join(HERE, "stock_gui.py")
CLI_PATH = os.path.join(HERE, "stock_predict.py")

CLI_HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票形态相似度预测 · 命令行版（独立单文件，不依赖 stock_gui.py）

与 stock_gui.py 共用同一套分析算法（由 build_cli.py 自动生成）：
价格形态 + 量能状态 + 大盘 + 板块 + 同行业 + 同市值层 多级加权匹配。
内建 SQLite 缓存（stock_cache.db），同行业/同市值层样本池只回填一次。
K线源自动切换：腾讯 -> 东财 -> 网易163 -> 新浪；支持代理（stock_gui.ini
的 [proxy] url，如 http://127.0.0.1:7890）。

用法：python stock_predict.py [--push] [--refresh-cache] [股票代码]
  --push           分析完成后把报告推送到 Pi 量化系统收件箱（ai-quant）
  --refresh-cache  刷新全市场代码表/市值分层（约1分钟，7天有效）
"""

'''

CLI_IMPORTS = '''
import heapq
import json
import logging
import math
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CACHE_OK = True      # 缓存层已内嵌，恒可用

'''

CLI_MAIN_MARKER = "# ==================== 以下为 CLI 专属 ===================="


def extract(src, start_marker, end_marker):
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def extract_cache_block(gui_src):
    """缓存层：GUI 内嵌段，从 CACHE_OK = True 后的缓存标记到 数据获取 段前。"""
    start = gui_src.index("# ================= 内嵌缓存层")
    end = gui_src.index("# ================= 数据获取", start)
    return gui_src[start:end]


def extract_algo_block(gui_src):
    """算法主体：QT_URL 起，slice_view 前止。"""
    return extract(gui_src, "QT_URL = ", "def slice_view")


def extract_cli_tail(old_cli_src):
    """CLI 专属：build_payload 起到文件尾（清洗掉旧的外部模块引用）。"""
    tail = old_cli_src[old_cli_src.index("def build_payload"):]
    tail = re.sub(r"\bsca\.", "", tail)
    tail = re.sub(r"\b(sca|sc) is not None", "True", tail)
    return tail


def main():
    gui_src = open(GUI_PATH, encoding="utf-8").read()

    cache_block = extract_cache_block(gui_src)
    algo_block = extract_algo_block(gui_src)

    # 算法块里不应有 tkinter 残留
    assert "tkinter" not in algo_block, "算法块混入了 tkinter 代码"

    old_cli = open(CLI_PATH, encoding="utf-8").read()
    cli_tail = extract_cli_tail(old_cli)

    out = (CLI_HEADER + CLI_IMPORTS + "\n" + cache_block + "\n\n"
           + algo_block.rstrip() + "\n\n"
           + CLI_MAIN_MARKER + "\n\n" + cli_tail)
    with open(CLI_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    print("已生成 %s (%.1f KB, 算法+缓存内嵌, 独立运行)"
          % (CLI_PATH, len(out.encode("utf-8")) / 1024))


if __name__ == "__main__":
    main()
