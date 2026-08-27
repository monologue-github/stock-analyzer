# -*- coding: utf-8 -*-
# ============================================================
# stock_e63.py - 股票预测 · Nokia E63 (S60 3rd FP1) · PyS60 1.9.x
# 双模式：
#   1) 直连腾讯行情（btsocket 选接入点，E网可用）
#   2) 失败则走 Pi 转接（家里LAN / 公网映射端口）
# ============================================================
import appuifw
import e32
import sys

# PyS60 1.9+ 必须用 btsocket，且要在 httplib 之前替换 socket
try:
    import btsocket as socket
    sys.modules['socket'] = socket
except ImportError:
    import socket

import httplib
import graphics
import re
import math
import threading
import time

W = 10
TOPK = 10
NBARS = 120

# Pi 转接地址（依次尝试）
RELAYS = [('192.168.3.28', 8010, 8),      # 家里LAN(快超时)
          ('bore.pub', 21982, 60)]        # 公网隧道(EDGE放宽)

UP_C = 0xE03131
DN_C = 0x0CA678
PRED_C = 0x1971C2
GRID_C = 0xECECEC
TXT_C = 0x555555

lock = e32.Ao_lock()
appuifw.app.screen = 'large'
appuifw.app.title = u'股票预测'
LAST = {'report': None, 'bars': None, 'pred': None}
MENU_VIEW = None
AP_OBJ = None
BUSY = False
# 跨线程 UI 调用门闸：worker 线程通过它安全刷新界面
GATED = e32.ao_callgate(lambda f, *a: f(*a))


def wnote(txt, t='info'):
    """worker 线程里发提示。"""
    GATED(note, txt, t)


def quit():
    lock.signal()
appuifw.app.exit_key_handler = quit


def note(txt, t='info'):
    try:
        appuifw.note(unicode(txt), t)
    except Exception:
        pass


def ensure_ap_obj(iapid):
    """worker 线程里启动接入点（阻塞操作不占UI线程）。"""
    global AP_OBJ
    if AP_OBJ is not None:
        return
    ap = socket.access_point(iapid)
    socket.set_default_access_point(ap)
    try:
        ap.start()
    except Exception:
        # -4159 等：首次附着偶发失败，3秒后重试一次
        e32.ao_sleep(3)
        ap.start()
    AP_OBJ = ap


def http_get(host, port, path, timeout=45):
    # 不设超时：塞班socket底层自带超时(-33)，btsocket无timeout API
    c = httplib.HTTPConnection(host, port)
    try:
        c.request('GET', path, headers={'User-Agent': 'Mozilla/5.0'})
        r = c.getresponse()
        return r.read()
    finally:
        c.close()


def normalize_code(raw):
    code = raw.strip().lower()
    for p in ('sh', 'sz'):
        if code.startswith(p):
            return p + code[2:]
    d = ''.join(ch for ch in code if ch.isdigit())
    if len(d) != 6:
        raise ValueError(u'代码格式不对: %s' % raw)
    if d[0] in '69' or d[:2] in ('51', '56', '58'):
        return 'sh' + d
    if d[0] in '03' or d[:2] in ('15', '16', '18'):
        return 'sz' + d
    raise ValueError(u'不支持的代码: %s' % raw)


# ---------------- 直连腾讯 ----------------

def fetch_quote(full):
    data = http_get('qt.gtimg.cn', 80, '/q=' + full).decode('gbk', 'ignore')
    f = data.split('~')
    if len(f) < 35 or not f[3]:
        raise ValueError(u'未查询到该股票')
    return {'name': f[1], 'price': float(f[3]),
            'prev_close': float(f[4]), 'open': float(f[5]),
            'high': float(f[33]), 'low': float(f[34]), 'time': f[30]}


BAR_RE = re.compile(
    r'\["(\d{4}-\d\d-\d\d)","([\d.]+)","([\d.]+)","([\d.]+)","([\d.]+)","(\d+)"')


def fetch_kline(full):
    data = http_get('web.ifzq.gtimg.cn', 80,
                    '/appstock/app/fqkline/get?param=%s,day,,,%d,qfq'
                    % (full, NBARS), timeout=40)
    try:
        data = data.decode('utf-8', 'ignore')
    except AttributeError:
        pass
    bars = []
    for m in BAR_RE.finditer(data):
        o = float(m.group(2)); c = float(m.group(3))
        if c > 0:
            bars.append({'date': m.group(1), 'open': o, 'close': c,
                         'high': float(m.group(4)),
                         'low': float(m.group(5)),
                         'vol': float(m.group(6))})
    if len(bars) < 100:
        raise ValueError(u'上市时间太短，样本不足')
    return bars


def pctile(vals, p):
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo = int(k); hi = min(lo + 1, len(s) - 1)
    f = k - lo
    return s[lo] * (1 - f) + s[hi] * f


def znorm(win):
    m = sum(win) / len(win)
    sd = math.sqrt(sum((x - m) ** 2 for x in win) / len(win)) or 1e-12
    return [(x - m) / sd for x in win]


def vol_ratio_at(vols, i):
    if i < 19:
        return None
    r5 = sum(vols[i - 4:i + 1]) / 5.0
    p15 = sum(vols[i - 19:i - 4]) / 15.0
    return (r5 / p15) if p15 > 0 else None


def analyze(q, rows):
    today = time.strftime('%Y-%m-%d')
    live = None
    if rows[-1]['date'] == today and q['price'] > 0:
        live = rows.pop()
        live['close'] = q['price']
        live['high'] = max(live['high'], q['price'])
        live['low'] = min(live['low'], q['price'])

    closes = [r['close'] for r in rows]
    rets = [math.log(closes[i + 1] / closes[i])
            for i in range(len(closes) - 1)]
    vols = [r.get('vol') or 0.0 for r in rows]
    vr_now = vol_ratio_at(vols, len(vols) - 1)

    cur = znorm(rets[-W:])
    sims = []
    for i in range(W, len(rets) - 1):
        w = znorm(rets[i - W:i])
        d0 = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur, w)))
        vr_i = vol_ratio_at(vols, i)
        if vr_now is not None and vr_i is not None:
            dv = abs(math.log(max(vr_now, 1e-6) / max(vr_i, 1e-6)))
            sc = d0 + 0.6 * min(dv, 2.5)
        else:
            sc = d0 + 0.30
        sims.append((sc, i))
    sims.sort()

    prev_close = q['prev_close'] or closes[-1]
    o_today = q['open'] or prev_close
    gap_today = (o_today / prev_close - 1) * 100

    samples = []
    for _, i in sims[:TOPK]:
        r, n1, n2 = rows[i], rows[i + 1], rows[i + 2]
        samples.append({
            't_date': r['date'], 'n1_date': n1['date'],
            'gap': n1['open'] / r['close'] - 1,
            'hi_o': n1['high'] / n1['open'] - 1,
            'lo_o': n1['low'] / n1['open'] - 1,
            'cl_o': n1['close'] / n1['open'] - 1,
            't2_cl': n2['close'] / n2['open'] - 1})
    sel = [s for s in samples if abs(s['gap'] * 100 - gap_today) <= 1.0]
    src = sel if len(sel) >= 3 else samples

    t_pred = {}
    for p in (10, 50, 90):
        t_pred[p] = {
            'cl': o_today * (1 + pctile([s['cl_o'] for s in src], p)),
            'hi': o_today * (1 + pctile([s['hi_o'] for s in src], p)),
            'lo': o_today * (1 + pctile([s['lo_o'] for s in src], p))}
    up_prob = len([s for s in src if s['cl_o'] > 0]) / float(len(src))
    base = t_pred[50]['cl']
    if live is not None:
        for p in (10, 50, 90):
            t_pred[p]['hi'] = max(t_pred[p]['hi'], live['high'])
            t_pred[p]['lo'] = min(t_pred[p]['lo'], live['low'])
    pred = {'open': base,
            'close': base * (1 + pctile([s['t2_cl'] for s in src], 50))}
    disp = rows + ([live] if live else [])
    return {'quote': q, 'rows': disp, 't_pred': t_pred, 'pred': pred,
            'samples': samples, 'src_n': len(src),
            'filtered': sel is src, 'up_prob': up_prob,
            'has_live': bool(live), 'prev_close': prev_close,
            'o_today': o_today}


def build_report(res):
    q, tp = res['quote'], res['t_pred']
    L = []
    L.append(u'%s (%s)' % (q['name'], q.get('code', '')))
    L.append(u'快照 %s%s' % (q['time'],
                             u' [盘中]' if res['has_live'] else u''))
    L.append(u'昨收 %.2f 今开 %.2f 现价 %.2f'
             % (res['prev_close'], res['o_today'], q['price']))
    L.append(u'')
    L.append(u'== 今日(T)预测 锚定今开 ==')
    for p in (10, 50, 90):
        L.append(u'P%d 收%.2f 高%.2f 低%.2f'
                 % (p, tp[p]['cl'], tp[p]['hi'], tp[p]['lo']))
    L.append(u'上行概率 %d%% 样本%d/%d%s'
             % (int(res['up_prob'] * 100), res['src_n'],
                len(res['samples']),
                u'(缺口筛选)' if res['filtered'] else u''))
    L.append(u'')
    pr = res['pred']
    L.append(u'== T+1预测 ==')
    L.append(u'开%.2f 收%.2f' % (pr['open'], pr['close']))
    L.append(u'')
    L.append(u'== 相似历史日期 ==')
    for s in res['samples']:
        mk = u'*' if abs(s['gap'] * 100 -
                         (res['o_today'] / res['prev_close'] - 1) * 100) \
             <= 1.0 else u''
        L.append(u'%s->%s %+5.1f%%%s'
                 % (s['t_date'][5:], s['n1_date'][5:],
                    s['cl_o'] * 100, mk))
    L.append(u'')
    L.append(u'* 缺口接近 统计参考不构成建议')
    return u'\n'.join(L)


def direct_analyze(full):
    """直连腾讯 + 本地计算。返回 (report, bars, pred)。"""
    q = fetch_quote(full)
    q['code'] = full
    rows = fetch_kline(full)
    res = analyze(q, rows)
    bars = [{'date': r['date'], 'open': r['open'], 'close': r['close'],
             'high': r['high'], 'low': r['low']} for r in res['rows'][-45:]]
    bars.append({'date': u'T+1', 'open': res['pred']['open'],
                 'close': res['pred']['close'],
                 'high': max(res['pred']['open'], res['pred']['close']),
                 'low': min(res['pred']['open'], res['pred']['close'])})
    return build_report(res), bars, res['pred']


# ---------------- Pi 转接 ----------------

def fetch_relay(full):
    last_err = None
    for host, port, tmo in RELAYS:
        try:
            c = httplib.HTTPConnection(host, port)
            try:
                c.request('GET', '/e63?code=%s&bars=1' % full,
                          headers={'User-Agent': 'E63'})
                r = c.getresponse()
                data = r.read().decode('utf-8', 'ignore')
            finally:
                c.close()
            if data.startswith('ERR'):
                raise ValueError(data[3:])
            report, _, datasec = data.partition('@@')
            bars = []
            pred = None
            for line in datasec.strip().split('\n'):
                seg = line.strip().split('|')
                if seg[0] == 'PRED' and len(seg) >= 3:
                    pred = {'open': float(seg[1]), 'close': float(seg[2])}
                elif len(seg) >= 6:
                    bars.append({'date': seg[0], 'open': float(seg[1]),
                                 'close': float(seg[2]),
                                 'high': float(seg[3]),
                                 'low': float(seg[4])})
            if pred:
                bars.append({'date': u'T+1', 'open': pred['open'],
                             'close': pred['close'],
                             'high': max(pred['open'], pred['close']),
                             'low': min(pred['open'], pred['close'])})
            return report + u'\n[Pi转接 %s]' % host, bars, pred
        except Exception:
            last_err = sys.exc_info()[1]
    raise last_err or ValueError(u'转接全部失败')


# ---------------- 视图 ----------------

def show_report(text):
    txt = appuifw.Text()
    txt.color = 0x222222
    txt.font = ('', 9)
    txt.set(unicode(text))
    appuifw.app.body = txt

    def back():
        main_menu()
    appuifw.app.menu = [(u'K线图', lambda: show_chart()),
                        (u'返回主菜单', back),
                        (u'退出', quit)]
    appuifw.app.exit_key_handler = back


def show_chart():
    if not LAST.get('bars'):
        note(u'无K线数据', 'error')
        return
    B = list(LAST['bars'])
    cv = appuifw.Canvas()
    appuifw.app.body = cv
    w, h = cv.size
    Lm, Rm, Tm, Bm = 34, 6, 12, 14
    pw, ph = w - Lm - Rm, h - Tm - Bm
    bw = pw / float(len(B))
    lo = min(b['low'] for b in B)
    hi = max(b['high'] for b in B)
    rng = (hi - lo) or 1
    pad = rng * 0.06
    lo -= pad
    hi += pad

    def Y(val):
        return int(Tm + (hi - val) / (hi - lo) * ph)

    def X(i):
        return int(Lm + bw * (i + 0.5))

    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        cv.line((Lm, y, w - Rm, y), outline=GRID_C)
        cv.text((2, y + 3), u'%.2f' % v, fill=TXT_C, font=('small', 7))
    for i, b in enumerate(B):
        isp = b['date'] == u'T+1'
        up = b['close'] >= b['open']
        col = PRED_C if isp else (UP_C if up else DN_C)
        x = X(i)
        cv.line((x, Y(b['high']), x, Y(b['low'])), outline=col)
        y1 = Y(max(b['open'], b['close']))
        y2 = Y(min(b['open'], b['close']))
        bw2 = max(int(bw * 0.62), 1)
        if y2 - y1 < 1:
            y2 = y1 + 1
        if isp:
            yy = y1
            while yy <= y2:
                cv.rectangle((x - bw2, yy, x + bw2, min(yy + 2, y2)),
                             outline=col)
                yy += 4
        else:
            cv.rectangle((x - bw2, y1, x + bw2, y2), fill=col)
    step = max(1, len(B) // 6)
    for i in range(0, len(B), step):
        cv.text((X(i) - 14, h - 4), unicode(B[i]['date'])[5:],
                fill=TXT_C, font=('small', 7))

    def back():
        if LAST.get('report'):
            show_report(LAST['report'])
        else:
            main_menu()
    appuifw.app.menu = [(u'返回', back), (u'退出', quit)]
    appuifw.app.exit_key_handler = back


def _pick_ap_order():
    """UI线程：选首选接入点，返回全部iapid（首选在前）。"""
    aps = socket.access_points()
    if not aps:
        raise ValueError(u'无可用接入点')
    names = [ap['name'] for ap in aps]
    idx = appuifw.selection_list(names)
    if idx is None or idx < 0:
        raise ValueError(u'未选择接入点')
    order = [aps[idx]['iapid']]
    order += [a['iapid'] for i, a in enumerate(aps) if i != idx]
    return order


def _analysis_done(report, bars, pred, err):
    """UI线程：展示结果。"""
    global BUSY
    BUSY = False
    if err:
        note(u'失败[%s]: %s' % (err.__class__.__name__, err), 'error')
        return
    LAST['report'] = report
    LAST['bars'] = bars
    LAST['pred'] = pred
    show_report(report)


def _worker(full, ap_order):
    """后台线程：联网+取数+计算，全程不碰UI。"""
    global AP_OBJ
    try:
        if AP_OBJ is None:
            last = None
            for n_i, iapid in enumerate(ap_order):
                try:
                    ap = socket.access_point(iapid)
                    socket.set_default_access_point(ap)
                    ap.start()
                    AP_OBJ = ap
                    break
                except Exception:
                    last = sys.exc_info()[1]
                    if n_i < len(ap_order) - 1:
                        GATED(note, u'该接入点失败(%s) 自动换下一个...'
                              % last, 'info')
                        e32.ao_sleep(3)
            if AP_OBJ is None:
                raise last or ValueError(u'接入点全部失败')
        GATED(note, u'直连腾讯...', 'info')
        try:
            report, bars, pred = direct_analyze(full)
        except Exception:
            GATED(note, u'直连失败 转Pi...', 'info')
            report, bars, pred = fetch_relay(full)
        GATED(_analysis_done, report, bars, pred, None)
    except Exception:
        e = sys.exc_info()[1]
        GATED(_analysis_done, None, None, None, e)


def do_analyze():
    global BUSY
    if BUSY:
        note(u'正在分析中...', 'info')
        return
    raw = appuifw.query(u'输入代码 如 002241', 'text', u'002241')
    if not raw:
        return
    try:
        full = normalize_code(unicode(raw))
    except ValueError:
        note(unicode(sys.exc_info()[1]), 'error')
        return
    try:
        iapid = AP_OBJ is None and _pick_ap() or None
    except ValueError:
        note(unicode(sys.exc_info()[1]), 'error')
        return
    except Exception:
        note(u'接入点列表失败', 'error')
        return
    BUSY = True
    note(u'连接网络中(EDGE较慢)...', 'info')

    def watchdog():
        if BUSY:
            GATED(note, u'已150秒无进展，可能卡住：退出重进再试', 'error')
    e32.ao_sleep(150, watchdog)
    threading.Thread(target=_worker, args=(full, iapid)).start()


def main_menu():
    entries = [u'>> 分析预测', u'>> 关于/帮助']
    body = appuifw.Listbox([(e, u'') for e in entries], _menu_pick)
    appuifw.app.body = body
    appuifw.app.menu = [(u'退出', quit)]
    appuifw.app.exit_key_handler = quit
    global MENU_VIEW
    MENU_VIEW = body


def _menu_pick():
    idx = MENU_VIEW.current()
    if idx == 0:
        do_analyze()
    elif idx == 1:
        note(u'E63双模式·直连腾讯优先\n失败走Pi转接\n统计参考 不构成建议', 'info')


if __name__ == '__main__':
    main_menu()
    lock.wait()
