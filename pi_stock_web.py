#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票形态相似度预测器 · 网页版 v2（纯标准库）

架构：Pi 仅作行情数据代理（转发腾讯接口原始K线），
     指标计算/形态匹配/预测全部在浏览器端 JS 完成，Pi 负载极低。

  python3 stock_web.py [--port 8010]
"""
import argparse
import math
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

QT_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
STATUS_HTML = "/var/www/status/index.html"
W_WINDOW, TOPK = 10, 10
DEFAULT_CODE = "000725"


def http_get(url, retries=3, timeout=15):
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("gbk", errors="ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (a + 1))
    raise RuntimeError(f"网络请求失败: {last}")


def normalize_code(code):
    code = code.strip().lower()
    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            return p + code[2:]
    d = "".join(ch for ch in code if ch.isdigit())
    if len(d) != 6:
        raise ValueError(f"代码格式不对: {code}")
    if d[0] in "69" or d[:2] in ("51", "56", "58"):      # 沪股/沪ETF
        return "sh" + d
    if d[0] in "03" or d[:2] in ("15", "16", "18"):      # 深股/深ETF/LOF
        return "sz" + d
    if d[0] in "48":
        return "bj" + d
    raise ValueError(f"不支持的代码: {code}")


def api_data(full):
    """只做数据中转：实时快照 + 原始日K。"""
    f = http_get(QT_URL + full).split("~")
    if len(f) < 35 or not f[3]:
        raise ValueError("未查询到该股票")
    quote = {"name": f[1], "price": float(f[3]), "prev_close": float(f[4]),
             "open": float(f[5]), "high": float(f[33]), "low": float(f[34]),
             "time": f[30]}
    kd = json.loads(http_get(KLINE_URL + f"?param={full},day,,,500,qfq"))
    d = kd.get("data", {}).get(full)
    if not d:
        raise ValueError("K线数据获取失败")
    bars = d.get("qfqday") or d.get("day")
    rows = [{"dt": b[0], "o": float(b[1]), "c": float(b[2]),
             "h": float(b[3]), "l": float(b[4]), "v": float(b[5])}
            for b in bars if float(b[2]) > 0]
    if len(rows) < 130:
        raise ValueError("上市时间太短，样本不足")
    return {
        "name": quote["name"], "code": full,
        "price": quote["price"], "prev_close": quote["prev_close"],
        "open": quote["open"], "high": quote["high"], "low": quote["low"],
        "time": quote["time"], "today": time.strftime("%Y-%m-%d"),
        "bars": rows,
    }


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>股票形态预测</title>
<style>
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:#101418;color:#ddd;font-family:"Microsoft YaHei",Helvetica,Arial,sans-serif}
.bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:10px 14px;background:#171c22;border-bottom:1px solid #232a33;position:sticky;top:0;z-index:5}
input,select,button{background:#222a33;border:1px solid #333e4a;color:#eee;border-radius:6px;padding:7px 10px;font-size:14px}
button{cursor:pointer}button:hover{background:#2b3540}
#hover{color:#4da3ff;font-size:13px;margin-left:auto;white-space:nowrap}
.wrap{padding:10px 12px;display:flex;gap:10px;flex-wrap:wrap}
.charts{flex:1;min-width:0}
.cwrap{position:relative;margin-bottom:8px;overflow:hidden}
.cwrap>canvas{display:block;width:100%;height:100%;background:#fff;border-radius:8px;cursor:grab;touch-action:pan-y}
.ovl{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;background:transparent!important}
/* 纵向高度锁定：容器定高，canvas 只填充，永不撑开 */
#cwMain{height:380px}
#cwVol{height:110px}
#cwInd{height:170px}
.side{width:330px;background:#171c22;border-radius:8px;padding:10px 12px;font-size:13px;line-height:1.55}
.side h3{margin:6px 0 4px;font-size:14px;color:#ffd34d;border-bottom:1px solid #2a323c;padding-bottom:3px}
table{border-collapse:collapse;width:100%;font-size:12px}
td,th{padding:2px 3px;text-align:right}
th{color:#889;font-weight:normal}
.r{text-align:right}.l{text-align:left;color:#aab}
.up{color:#ff6b6b}.dn{color:#51cf66}
.note{font-size:11px;color:#667;padding:8px 14px}
@media(max-width:760px){
  #cwMain{height:240px}
  #cwVol{height:64px}
  #cwInd{height:96px}
  .side{width:100%;padding:8px;font-size:12px}   /* 手机端预测参考显示在指标下方 */
  .bar{padding:6px}
  input,select,button{padding:9px 10px;font-size:14px}
  #code{width:34vw}
  #info{font-size:12px}
  #hover{display:none}
  canvas{border-radius:6px}
  .wrap{padding:6px;gap:6px}
  .cwrap{margin-bottom:6px}
  .side{padding:8px;font-size:12px}
  body{-webkit-text-size-adjust:100%}
}
</style>
</head>
<body>
<div class="bar">
  <input id="code" placeholder="代码 如002241/600519" size="16" value="000725">
  <button onclick="go()">分析</button>
  <select id="ind" onchange="drawAll()">
    <option>MACD</option><option>KDJ</option><option>RSI</option>
  </select>
  <button onclick="goLatest()" title="回到最新K线">⟨最新</button>
  <span id="info"></span><span id="hover"></span>
</div>
<div class="wrap">
 <div class="charts">
  <div class="cwrap" id="cwMain"><canvas id="cvMain"></canvas><canvas id="ovMain" class="ovl"></canvas></div>
  <div class="cwrap" id="cwVol"><canvas id="cvVol"></canvas><canvas id="ovVol" class="ovl"></canvas></div>
  <div class="cwrap" id="cwInd"><canvas id="cvInd"></canvas><canvas id="ovInd" class="ovl"></canvas></div>
 </div>
 <div class="side" id="side">输入代码后点【分析】<br>分析计算均在浏览器本地完成</div>
</div>
<div class="note">前复权价 · 历史统计推断仅供参考，不构成投资建议 · 滚轮缩放K线 · 模拟盘状态页见 <a style="color:#4da3ff" href="/quant/">/quant/</a> · 开源地址 <a style="color:#4da3ff" href="https://github.com/monologue-github/stock-analyzer" target="_blank" rel="noopener">GitHub stock-analyzer</a> · 作者 獨白 · <a style="color:#4da3ff" href="mailto:kingrux106@gmail.com">kingrux106@gmail.com</a> · QQ 2180287399</div>
<script>
"use strict";
let D=null,A=null; // D=原始数据 A=分析结果
const DEFAULT_CODE="000725";
var UP="#e03131",DN="#0ca678",PC="#1971c2",CROSS_C="#9fb3c8";
var MAC={5:"#f08c00",10:"#1971c2",20:"#9c36b5",30:"#2f9e44",60:"#86421f"};
const WN=10,TOPK=10;

// ================= 指标计算（浏览器端） =================
function smaP(v,n){const o=new Array(v.length).fill(null);
  let s=0;for(let i=0;i<v.length;i++){s+=v[i];if(i>=n)s-=v[i-n];if(i>=n-1)o[i]=s/n;}return o;}
function emaA(v,n){const o=[],k=2/(n+1);let e=null;
  for(const x of v){e=e==null?x:x*k+e*(1-k);o.push(e);}return o;}
function calcMACD(c){const dif=[],dea=[];
  const e12=emaA(c,12),e26=emaA(c,26);
  for(let i=0;i<c.length;i++)dif.push(e12[i]-e26[i]);
  const dr=emaA(dif,9);
  for(let i=0;i<c.length;i++)dea.push(i<8?null:dr[i]);
  const mh=dif.map((x,i)=>dea[i]==null?null:2*(x-dea[i]));
  return [dif,dea,mh];}
function calcKDJ(rows,n){const ks=[],ds=[];let k=50,d=50;
  rows.forEach((r,i)=>{const s=rows.slice(Math.max(0,i-n+1),i+1);
    let lo=Infinity,hi=-Infinity;s.forEach(x=>{lo=Math.min(lo,x.l);hi=Math.max(hi,x.h);});
    const rsv=hi>lo?(r.c-lo)/(hi-lo)*100:50;
    k=k*2/3+rsv/3;d=d*2/3+k/3;ks.push(k);ds.push(d);});
  const j=ks.map((x,i)=>3*x-2*ds[i]);return [ks,ds,j];}
function calcRSI(c,n){const out=new Array(c.length).fill(null);
  if(c.length<=n)return out;
  let ag=0,al=0;
  for(let i=1;i<=n;i++){const ch=c[i]-c[i-1];ag+=Math.max(ch,0);al+=Math.max(-ch,0);}
  ag/=n;al/=n;
  out[n]=al===0?100:100-100/(1+ag/al);
  for(let i=n+1;i<c.length;i++){const ch=c[i]-c[i-1];
    ag=(ag*(n-1)+Math.max(ch,0))/n;al=(al*(n-1)+Math.max(-ch,0))/n;
    out[i]=al===0?100:100-100/(1+ag/al);}
  return out;}
function pctile(vals,p){const s=[...vals].sort((a,b)=>a-b);
  const k=(s.length-1)*p/100,lo=Math.floor(k),hi=Math.min(lo+1,s.length-1),f=k-lo;
  return s[lo]*(1-f)+s[hi]*f;}
function znorm(w){const m=w.reduce((a,b)=>a+b,0)/w.length;
  const sd=Math.sqrt(w.reduce((a,b)=>a+(b-m)*(b-m),0)/w.length)||1e-12;
  return w.map(x=>(x-m)/sd);}

// ================= 形态匹配与预测 =================
function analyze(){
  const today=D.today,q=D;
  const all=D.bars.map(b=>({date:b.dt,open:b.o,close:b.c,high:b.h,low:b.l,vol:b.v}));
  let live=null,hist=all;
  if(all.length&&all[all.length-1].date===today){
    live={...all[all.length-1]};
    live.close=q.price;
    live.high=Math.max(live.high,q.price);
    if(q.low>0)live.low=Math.min(live.low,q.price);
    hist=all.slice(0,-1);
  }
  const rets=[];for(let i=1;i<hist.length;i++)rets.push(Math.log(hist[i].close/hist[i-1].close));
  const cur=znorm(rets.slice(-WN));
  const sims=[];
  for(let i=WN;i<rets.length-1;i++){
    const w=znorm(rets.slice(i-WN,i));
    let dsum=0;for(let j=0;j<WN;j++){const dd=cur[j]-w[j];dsum+=dd*dd;}
    sims.push([Math.sqrt(dsum),i]);
  }
  sims.sort((a,b)=>a[0]-b[0]);
  const top=sims.slice(0,TOPK);

  const prevClose=q.prev_close||hist[hist.length-1].close;
  const oToday=q.open||prevClose;
  const gapToday=(oToday/prevClose-1)*100;

  const samples=top.map(([,i])=>{
    const r=hist[i],n1=hist[i+1],n2=hist[i+2];
    return {t_date:r.date,t_close:r.close,n1_date:n1.date,n2_date:n2.date,
      gap:n1.open/r.close-1, hi_o:n1.high/n1.open-1, lo_o:n1.low/n1.open-1,
      cl_o:n1.close/n1.open-1, t2_gap:n2.open/n1.close-1,
      t2_hi:n2.high/n2.open-1, t2_lo:n2.low/n2.open-1, t2_cl:n2.close/n2.open-1};
  });
  const sel=samples.filter(s=>Math.abs(s.gap*100-gapToday)<=1);
  const src=sel.length>=3?sel:samples;
  const P=p=>pctile(src.map(s=>s.cl_o),p),
        PH=p=>pctile(src.map(s=>s.hi_o),p),
        PL=p=>pctile(src.map(s=>s.lo_o),p);
  const tp={};
  [10,25,50,75,90].forEach(p=>{tp[p]={cl:oToday*(1+P(p)),hi:oToday*(1+PH(p)),lo:oToday*(1+PL(p))};});
  const upProb=src.filter(s=>s.cl_o>0).length/src.length;
  const base=tp[50].cl;
  const pred={date:"T+1预测",open:base,
    close:base*(1+pctile(src.map(s=>s.t2_cl),50)),
    high:base*(1+pctile(src.map(s=>s.t2_hi),75)),
    low:base*(1+pctile(src.map(s=>s.t2_lo),25))};

  // 指标基于 匹配历史(+今日盘中)
  const disp=hist.concat(live?[live]:[]);
  const closes=disp.map(r=>r.close);
  const [dif,dea,mh]=calcMACD(closes);
  const [kk,dd,jj]=calcKDJ(disp,9);
  const r6=calcRSI(closes,6),r12=calcRSI(closes,12);
  const ma={};[5,10,20,30,60].forEach(n=>ma[n]=smaP(closes,n));

  // 买卖信号
  const signals=[];
  const start=Math.max(1,disp.length-120);
  for(let i=start;i<disp.length;i++){
    const day=disp[i].date;
    if(dif[i]!=null&&dea[i]!=null&&dif[i-1]!=null&&dea[i-1]!=null){
      if(dif[i-1]<=dea[i-1]&&dif[i]>dea[i])
        signals.push({i,date:day,type:"B",txt:`MACD金叉 DIF=${dif[i].toFixed(3)}>DEA=${dea[i].toFixed(3)}`});
      else if(dif[i-1]>=dea[i-1]&&dif[i]<dea[i])
        signals.push({i,date:day,type:"S",txt:`MACD死叉 DIF=${dif[i].toFixed(3)}<DEA=${dea[i].toFixed(3)}`});
    }
    if(kk[i-1]<=dd[i-1]&&kk[i]>dd[i]&&kk[i]<45)
      signals.push({i,date:day,type:"B",txt:`KDJ低位金叉 K=${kk[i].toFixed(1)}`});
    else if(kk[i-1]>=dd[i-1]&&kk[i]<dd[i]&&kk[i]>65)
      signals.push({i,date:day,type:"S",txt:`KDJ高位死叉 K=${kk[i].toFixed(1)}`});
    if(r6[i]!=null&&r6[i-1]!=null){
      if(r6[i-1]<20&&r6[i]>=20)signals.push({i,date:day,type:"B",txt:`RSI6超卖回升 ${r6[i].toFixed(1)}`});
      else if(r6[i-1]>80&&r6[i]<=80)signals.push({i,date:day,type:"S",txt:`RSI6超买回落 ${r6[i].toFixed(1)}`});
    }
  }
  // ---- 历史相似形态统计信号（候选窗口仅取当日之前 j<i-5，无前视偏差）----
  const volsH=hist.map(r=>r.vol);
  function vrAt(vl,i){if(i<19)return null;
    let s5=0,s15=0;
    for(let k=i-4;k<=i;k++)s5+=vl[k];
    for(let k=i-19;k<i-4;k++)s15+=vl[k];
    return s15>0?(s5/5)/(s15/15):null;}
  const vrNow=vrAt(volsH,volsH.length-1);
  const dVol=j=>{const vj=vrAt(volsH,j);
    return (vrNow!=null&&vj!=null)?Math.abs(Math.log(Math.max(vrNow,1e-6)/Math.max(vj,1e-6))):null;};
  const zc={};
  const zwin=i=>zc[i]||(zc[i]=znorm(rets.slice(i-WN,i)));
  const statDays=Math.min(60,hist.length-WN-2);
  for(let i=hist.length-statDays;i<hist.length;i++){
    const base=zwin(i),cands=[];
    for(let j=WN;j<i-5;j++){
      const wj=zwin(j);
      let d0=0;for(let k2=0;k2<WN;k2++){const dd=base[k2]-wj[k2];d0+=dd*dd;}
      const dv=dVol(j);
      cands.push([Math.sqrt(d0)+(dv!=null?0.6*Math.min(dv,2.5):0.30),j]);
    }
    if(!cands.length)continue;
    cands.sort((a,b)=>a[0]-b[0]);
    let ups=0,avg=0;
    const tn=Math.min(6,cands.length);
    for(let t2=0;t2<tn;t2++){
      const j=cands[t2][1],nx=hist[j+1].close/hist[j].close-1;
      avg+=nx;if(nx>0)ups++;
    }
    avg=avg/tn*100;
    if(ups/tn>=0.67&&avg>0.2)
      signals.push({i,date:hist[i].date,type:"B",txt:`历史相似形态偏多 ${ups}/${tn} 平均${avg>=0?"+":""}${avg.toFixed(1)}%`});
    else if(ups/tn<=0.33&&avg<-0.2)
      signals.push({i,date:hist[i].date,type:"S",txt:`历史相似形态偏空 ${tn-ups}/${tn} 平均${avg>=0?"+":""}${avg.toFixed(1)}%`});
  }
  A={name:q.name,code:q.code,price:q.price,prevClose:prevClose,open:oToday,
     gapToday:gapToday,time:q.time,hasLive:!!live,
     bars:disp,pred,tp,samples,srcN:src.length,filtered:sel.length>=3,upProb,
     ind:{ma,dif,dea,mh,k:kk,d:dd,j:jj,r6,r12},signals,vols:disp.map(r=>r.vol)};
}

// ================= 绘图 =================
function prep(cv){
  const w=cv.clientWidth,dpr=window.devicePixelRatio||1;
  // 高度锁定在容器(.cwrap)上，canvas 只填充，永不改变布局
  const H=cv.parentElement.clientHeight||380;
  cv.width=Math.round(w*dpr);
  cv.height=Math.round(H*dpr);
  const ctx=cv.getContext("2d");ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.fillStyle="#fff";ctx.fillRect(0,0,w,H);
  return [ctx,w,H];
}
function syncOverlays(){
  ["cvMain","cvVol","cvInd"].forEach(id=>{
    const b=document.getElementById(id),o=document.getElementById(OV[id]);
    if(o.width!==b.width||o.height!==b.height){o.width=b.width;o.height=b.height;}
  });
}
function stepN(n){return Math.max(1,Math.ceil(n/9));}
const L=52,R=64;
const IS_MOBILE = window.matchMedia
    && matchMedia("(max-width:760px)").matches;
let viewN = IS_MOBILE ? 30 : 60;
let viewOff=0;      // 可视窗口左端在全量数据中的下标
const MINB = IS_MOBILE ? 12 : 20, MAXB=500;
let lastWheel=0;
let drag=null,tStart=null,tMode=null,lastPinchD=0,pendingDraw=false;
function scheduleCharts(){
  if(pendingDraw)return;
  pendingDraw=true;
  requestAnimationFrame(()=>{pendingDraw=false;if(A)drawCharts();});
}
let lastCross=null;
let V=null;   // 当前视图切片
function clampOff(){const n=A?A.bars.length:0;
  viewN=Math.max(MINB,Math.min(n||MINB,viewN));
  viewOff=Math.max(0,Math.min(n-viewN,viewOff));}
function buildView(){
  const n=A.bars.length,vn=Math.max(MINB,Math.min(viewN,n));
  const off=Math.max(0,Math.min(viewOff,n-vn));
  viewN=vn;viewOff=off;
  const sl=a=>a.slice(off,off+vn);
  V={off,bars:A.bars.slice(off,off+vn),
    includePred:off+vn>=n,
    ma:Object.fromEntries(Object.keys(A.ind.ma).map(k=>[k,A.ind.ma[k].slice(off,off+vn)])),
    dif:sl(A.ind.dif),dea:sl(A.ind.dea),mh:sl(A.ind.mh),
    k:sl(A.ind.k),d:sl(A.ind.d),j:sl(A.ind.j),r6:sl(A.ind.r6),r12:sl(A.ind.r12),
    vols:sl(A.vols),
    signals:A.signals.filter(s=>s.i>=off&&s.i<off+vn).map(s=>({...s,i:s.i-off}))};
}
function drawCharts(){
  if(!A)return;
  buildView();syncOverlays();mainChart();volChart();indChart();
}
function drawAll(){if(!A)return;drawCharts();renderSide();}
function mainChart(){
  const cv=document.getElementById("cvMain");
  const [ctx,w,H]=prep(cv);
  const B=V.bars.concat(V.includePred?[A.pred]:[]),n=B.length,T=14,Bm=20;
  const pw=w-L-R,ph=H-T-Bm,bw=pw/n;
  let lo=1e18,hi=-1e18;
  B.forEach(b=>{lo=Math.min(lo,b.low);hi=Math.max(hi,b.high);});
  Object.keys(MAC).forEach(nn=>V.ma[nn].forEach(v=>{
    if(v!=null){lo=Math.min(lo,v);hi=Math.max(hi,v);}}));
  const rng=(hi-lo)||1,pad=rng*.06;lo-=pad;hi+=pad;
  const Y=v=>T+(hi-v)/(hi-lo)*ph,X=i=>L+bw*(i+.5);
  ctx.strokeStyle="#ececec";ctx.fillStyle="#888";ctx.font="10px Consolas";ctx.textAlign="right";
  for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4,y=Y(v);
    ctx.beginPath();ctx.moveTo(L,y);ctx.lineTo(w-R,y);ctx.stroke();
    ctx.fillText(v.toFixed(2),L-4,y+3);}
  ctx.textAlign="left";
  Object.keys(MAC).forEach(nn=>{
    ctx.strokeStyle=MAC[nn];ctx.beginPath();let st=false;
    V.ma[nn].forEach((v,i)=>{if(v==null){st=false;return;}
      st?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v));st=true;});
    ctx.stroke();
    ctx.fillStyle=MAC[nn];ctx.fillText("MA"+nn,L+2+[5,10,20,30,60].indexOf(+nn)*36,10);});
  ctx.textAlign="center";
  B.forEach((b,i)=>{
    const up=b.close>=b.open,col=b.date==="T+1预测"?PC:(up?UP:DN);
    ctx.setLineDash(b.date==="T+1预测"?[3,2]:[]);
    ctx.strokeStyle=col;ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(X(i),Y(b.high));ctx.lineTo(X(i),Y(b.low));ctx.stroke();
    const bw2=Math.max(bw*.62,2),y1=Y(Math.max(b.open,b.close)),y2=Y(Math.min(b.open,b.close));
    if(b.date==="T+1预测")ctx.strokeRect(X(i)-bw2/2,y1,bw2,Math.max(y2-y1,1));
    else{ctx.fillStyle=col;ctx.fillRect(X(i)-bw2/2,y1,bw2,Math.max(y2-y1,1));}
    ctx.setLineDash([]);
  });
  V.signals.forEach(s=>{if(s.i<0||s.i>=n-1)return;
    const b=B[s.i],x=X(s.i);ctx.fillStyle=s.type==="B"?UP:DN;
    if(s.type==="B"){const y=Y(b.low)+5;
      ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x-5,y+8);ctx.lineTo(x+5,y+8);ctx.fill();}
    else{const y=Y(b.high)-5;
      ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x-5,y-8);ctx.lineTo(x+5,y-8);ctx.fill();}});
  ctx.fillStyle=PC;ctx.textAlign="center";
  ctx.fillText(`T+1 C:${A.pred.close.toFixed(2)}`,X(n-1)-40,T-2);
  ctx.fillStyle="#888";ctx.textAlign="left";ctx.font="10px Consolas";
  B.forEach((b,i)=>{if(i%stepN(n)===0)ctx.fillText(b.date.slice(5),X(i)-12,H-6);});
  panelScale.cvMain={lo,hi,T,ph};
}
function volChart(){
  const cv=document.getElementById("cvVol");
  const [ctx,w,H]=prep(cv);
  const n=V.bars.length+(V.includePred?1:0),T=10,Bm=18;
  const pw=w-L-R,ph=H-T-Bm,bw=pw/n;
  const vs=V.vols.filter(v=>v!=null);
  const vmax=Math.max(...vs,1)*1.08;
  const Y=v=>T+(1-v/vmax)*ph,X=i=>L+bw*(i+.5);
  const fv=v=>v>=1e8?(v/1e8).toFixed(1)+"亿":v>=1e4?(v/1e4).toFixed(0)+"万":""+Math.round(v);
  ctx.strokeStyle="#ececec";ctx.fillStyle="#888";ctx.font="10px Consolas";ctx.textAlign="right";
  [0,vmax/2,vmax].forEach(v=>{ctx.fillText(fv(v),L-4,Y(v)+3);
    ctx.beginPath();ctx.moveTo(L,Y(v));ctx.lineTo(w-R,Y(v));ctx.stroke();});
  const bw2=Math.max(bw*.62,2);
  V.bars.forEach((b,i)=>{if(b.vol==null)return;
    ctx.fillStyle=b.close>=b.open?UP:DN;
    ctx.fillRect(X(i)-bw2/2,Y(b.vol),bw2,Y(0)-Y(b.vol));});
  if(vs.length>=5){const mv=vs.slice(-5).reduce((a,b)=>a+b,0)/5;
    ctx.strokeStyle="#e8890c";ctx.setLineDash([5,3]);
    ctx.beginPath();ctx.moveTo(L,Y(mv));ctx.lineTo(w-R,Y(mv));ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle="#e8890c";ctx.textAlign="left";
    ctx.fillText("5日均量 "+fv(mv)+"手",L+2,Y(mv)-4);}
  ctx.textAlign="left";ctx.fillStyle="#555";ctx.fillText("VOL",L+2,T-1);
  panelScale.cvVol={lo:0,hi:vmax,T,ph};
}
function indChart(){
  const cv=document.getElementById("cvInd");
  const [ctx,w,H]=prep(cv);
  const mode=document.getElementById("ind").value;
  const n=V.bars.length+(V.includePred?1:0),T=14,Bm=18;
  const pw=w-L-R,ph=H-T-Bm,bw=pw/n;
  const X=i=>L+bw*(i+.5);
  let vals,guides=[],lines=[],histArr=null,label=mode;
  if(mode==="MACD"){
    vals=[0];[V.dif,V.dea,V.mh].forEach(a=>a.forEach(v=>{if(v!=null)vals.push(v);}));
    lines=[[V.dif,"#e8890c"],[V.dea,"#1971c2"]];histArr=V.mh;
  }else if(mode==="KDJ"){
    vals=[...V.k,...V.d,...V.j];guides=[20,50,80];
    lines=[[V.k,"#e8890c"],[V.d,"#1971c2"],[V.j,"#9c36b5"]];
  }else{
    vals=[0,100];guides=[30,50,70];
    lines=[[V.r6,"#e8890c"],[V.r12,"#1971c2"]];label="RSI6/RSI12";
  }
  let lo=Math.min(...vals),hi=Math.max(...vals);
  if(mode==="MACD"){const r=(hi-lo)||1;lo=Math.min(lo,-r*.05);hi=Math.max(hi,r*.05);}
  const rr=(hi-lo)||1,pad=rr*.08;lo-=pad;hi+=pad;
  const Y=v=>T+(hi-v)/(hi-lo)*ph;
  ctx.strokeStyle="#ececec";ctx.fillStyle="#888";ctx.font="10px Consolas";ctx.textAlign="right";
  guides.forEach(gv=>{const y=Y(gv);
    ctx.beginPath();ctx.moveTo(L,y);ctx.lineTo(w-R,y);ctx.stroke();
    ctx.fillText(String(gv),L-4,y+3);});
  if(histArr){const zero=Y(0),bw2=Math.max(pw/n*.3,1.5);
    histArr.forEach((hv,i)=>{if(hv==null)return;
      ctx.fillStyle=hv>=0?UP:DN;
      ctx.fillRect(X(i)-bw2,Math.min(zero,Y(hv)),bw2*2,Math.abs(Y(hv)-zero)||1);});}
  lines.forEach(([arr,c])=>{ctx.strokeStyle=c;ctx.beginPath();let st=false;
    arr.forEach((v,i)=>{if(v==null){st=false;return;}
      st?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v));st=true;});
    ctx.stroke();});
  ctx.fillStyle="#555";ctx.textAlign="left";
  const last=a=>{for(let i=a.length-1;i>=0;i--)if(a[i]!=null)return a[i];return null;};
  let info=label+" ";
  if(mode==="MACD")info+=`DIF:${last(A.ind.dif)?.toFixed(3)??"-"} DEA:${last(A.ind.dea)?.toFixed(3)??"-"} MACD:${last(A.ind.mh)?.toFixed(3)??"-"}`;
  else if(mode==="KDJ")info+=`K:${last(A.ind.k)?.toFixed(1)} D:${last(A.ind.d)?.toFixed(1)} J:${last(A.ind.j)?.toFixed(1)}`;
  else info+=`RSI6:${last(A.ind.r6)?.toFixed(1)??"-"} RSI12:${last(A.ind.r12)?.toFixed(1)??"-"}`;
  ctx.fillText(info,L+2,T-2);
  panelScale.cvInd={lo,hi,T,ph};
}

// ================= 十字光标（独立覆盖层，不重绘底图，避免抖动） =================
const OV = {cvMain:"ovMain", cvVol:"ovVol", cvInd:"ovInd"};
const panelScale = {};   // id -> {lo,hi,T,ph}
function clearOv(id){
  const o=document.getElementById(OV[id]),ctx=o.getContext("2d");
  ctx.setTransform(1,0,0,1,0,0);
  ctx.clearRect(0,0,o.width,o.height);
}
function cross(cv,mx,my){
  if(!A||!V)return;
  const n=V.bars.length+(V.includePred?1:0),w=cv.clientWidth;
  let idx=Math.floor((mx-L)/(w-L-R)*n);
  idx=Math.max(0,Math.min(n-1,idx));
  const b=(idx===n-1)?A.pred:V.bars[idx];
  let hv=(idx===n-1)?"[预测T+1] ":((b.date)+" ");
  hv+=`开${b.open.toFixed(2)} 高${b.high.toFixed(2)} 低${b.low.toFixed(2)} 收`;
  if(idx<n-1){
    const pc=(idx>0)?V.bars[idx-1].close:
      ((V.off>0)?A.bars[V.off-1].close:A.prevClose);
    const chg=((b.close/pc-1)*100).toFixed(2);
    hv+=`<b class="${b.close>=pc?'up':'dn'}">${b.close.toFixed(2)}(${chg>=0?"+":""}${chg}%)</b>`;
    if(b.vol!=null)hv+=` 量${fmtV(b.vol)}手`;
    const mode=document.getElementById("ind").value;
    const ai=V.off+idx;
    if(mode==="MACD"&&A.ind.dif[ai]!=null&&A.ind.dea[ai]!=null)
      hv+=` | DIF:${A.ind.dif[ai].toFixed(3)} DEA:${A.ind.dea[ai].toFixed(3)} MACD:${(A.ind.mh[ai]!=null)?A.ind.mh[ai].toFixed(3):"-"}`;
    else if(mode==="KDJ")
      hv+=` | K:${A.ind.k[ai].toFixed(1)} D:${A.ind.d[ai].toFixed(1)} J:${A.ind.j[ai].toFixed(1)}`;
    else if(mode==="RSI")
      hv+=` | RSI6:${(A.ind.r6[ai]!=null)?A.ind.r6[ai].toFixed(1):"-"} RSI12:${(A.ind.r12[ai]!=null)?A.ind.r12[ai].toFixed(1):"-"}`;
  }else hv+=` 开${b.open.toFixed(2)} 高${b.high.toFixed(2)} 低${b.low.toFixed(2)} 收<b>${b.close.toFixed(2)}</b>`;
  document.getElementById("hover").innerHTML=hv;
}
function fmtV(v){return v>=1e8?(v/1e8).toFixed(1)+"亿":v>=1e4?(v/1e4).toFixed(0)+"万":""+Math.round(v);}

// ================= 右侧栏 =================
function renderSide(){
  const t=A.tp,pr=A.pred;
  let h=`<h3>今日(T)预测 [锚定今开]</h3><table>`;
  [10,50,90].forEach(pp=>{
    h+=`<tr><td class="l">P${pp}</td><td class="r">收 ${t[pp].cl.toFixed(2)}</td><td class="r">高 ${t[pp].hi.toFixed(2)}</td><td class="r">低 ${t[pp].lo.toFixed(2)}</td></tr>`;});
  h+=`</table><div style="margin-top:4px">上行概率 <b>${(A.upProb*100).toFixed(0)}%</b> · 样本${A.srcN}/${A.samples.length}${A.filtered?"(缺口筛选)":""}</div>`;
  h+=`<h3>次日(T+1)预测</h3><table>
   <tr><td class="l">开</td><td class="r">${pr.open.toFixed(2)}</td><td class="l" style="padding-left:14px">收</td><td class="r"><b>${pr.close.toFixed(2)}</b></td></tr>
   <tr><td class="l">高</td><td class="r">${pr.high.toFixed(2)}</td><td class="l" style="padding-left:14px">低</td><td class="r">${pr.low.toFixed(2)}</td></tr></table>`;
  h+=`<h3>相似历史参考日期</h3><table><tr><th class="l">T日</th><th class="l">T+1日</th><th>T+1涨跌</th><th>T+2涨跌</th></tr>`;
  A.samples.forEach(s=>{
    const mark=Math.abs(s.gap*100-A.gapToday)<=1?"*":"";
    h+=`<tr><td class="l">${s.t_date}</td><td class="l">${s.n1_date}${mark}</td>
    <td class="r ${s.cl_o>=0?'up':'dn'}">${(s.cl_o*100).toFixed(1)}%</td>
    <td class="r ${s.t2_cl>=0?'up':'dn'}">${(s.t2_cl*100).toFixed(1)}%</td></tr>`;});
  h+=`</table><div style="font-size:11px;color:#667;margin-top:6px">* 开盘缺口与今日接近<br>最近信号：</div>`;
  A.signals.slice(-6).reverse().forEach(s=>{
    h+=`<div style="font-size:11px">${s.date} [<span class="${s.type==='B'?'up':'dn'}">${s.type==="B"?"买":"卖"}</span>] ${s.txt}</div>`;});
  document.getElementById("side").innerHTML=h;
}

// ================= 加载 =================
function go(){
  const code=document.getElementById("code").value.trim();
  if(!code)return;
  document.getElementById("info").textContent="加载中...";
  fetch("/api?code="+encodeURIComponent(code)).then(r=>r.json()).then(j=>{
    if(j.error){document.getElementById("info").textContent=j.error;return;}
    D=j;
    const t0=performance.now();
    analyze();
    viewN = IS_MOBILE ? 30 : 60;
    viewOff=Math.max(0,A.bars.length-viewN);
    drawAll();
    document.getElementById("info").innerHTML=
      `<b>${A.name}</b>(${A.code}) 昨收${A.prevClose.toFixed(2)} 今开${A.open.toFixed(2)}(缺口${A.gapToday>=0?"+":""}${A.gapToday.toFixed(2)}%) 现价<b class="${A.price>=A.prevClose?'up':'dn'}">${A.price.toFixed(2)}(${((A.price/A.prevClose-1)*100)>=0?"+":""}${((A.price/A.prevClose-1)*100).toFixed(2)}%)</b> ${A.time}${A.hasLive?" [盘中]":""}`;
  }).catch(e=>document.getElementById("info").textContent="加载失败:"+e);
}
function goLatest(){
  if(!A)return;
  viewOff=Math.max(0,A.bars.length-viewN);
  drawCharts();
}
document.getElementById("code").addEventListener("keydown",e=>{if(e.key==="Enter")go();});
go();   // 默认加载京东方A
window.addEventListener("resize",()=>drawCharts());
["cvMain","cvVol","cvInd"].forEach(id=>{
  const cv=document.getElementById(id);
  cv.addEventListener("mousemove",e=>{const r=cv.getBoundingClientRect();cross(cv,e.clientX-r.left,e.clientY-r.top);});
  cv.addEventListener("mouseleave",()=>{
    document.getElementById("hover").textContent="";
    clearOv(id);lastCross=null;
  });
  cv.addEventListener("wheel",e=>{
    e.preventDefault();               // 图表上滚轮永不滚动页面
    if(!A)return;
    const now=performance.now();
    if(now-lastWheel<150)return;      // 限流：防触摸板惯性连发
    lastWheel=now;
    const f=e.deltaY<0?0.8:1/0.8;     // 上滚放大(减少根数) 下滚缩小
    const center=viewOff+viewN/2;
    viewN=Math.round(viewN*f);
    clampOff();
    viewOff=Math.max(0,Math.min(A.bars.length-viewN,
                                Math.round(center-viewN/2)));
    drawCharts();                     // 只重绘图表，纵向自适应可见区间
  },{passive:false});
  // 拖动平移（鼠标）
  cv.addEventListener("mousedown",e=>{
    if(!A)return;
    drag={x:e.clientX,off:viewOff};
    e.preventDefault();
  });
  // 触屏手势：方向锁(横滑平移/竖滑滚页面) + 双指捏合缩放
  cv.addEventListener("touchstart",e=>{
    if(!A)return;
    if(e.touches.length===2){
      tMode="pinch";
      lastPinchD=Math.hypot(
        e.touches[0].clientX-e.touches[1].clientX,
        e.touches[0].clientY-e.touches[1].clientY);
      e.preventDefault();
    }else if(e.touches.length===1){
      tStart={x:e.touches[0].clientX,y:e.touches[0].clientY,off:viewOff};
      tMode=null;
    }
  },{passive:false});
  cv.addEventListener("touchmove",e=>{
    if(!A||!tStart)return;
    const n=A.bars.length;
    if(e.touches.length===2||tMode==="pinch"){
      if(tMode!=="pinch")return;
      e.preventDefault();
      const d=Math.hypot(
        e.touches[0].clientX-e.touches[1].clientX,
        e.touches[0].clientY-e.touches[1].clientY);
      if(lastPinchD>0){
        viewN=Math.round(viewN*lastPinchD/Math.max(d,1));
        clampOff();scheduleCharts();
      }
      lastPinchD=d;
    }else if(e.touches.length===1){
      const dx=e.touches[0].clientX-tStart.x,
            dy=e.touches[0].clientY-tStart.y;
      if(!tMode){
        if(Math.abs(dx)<12&&Math.abs(dy)<12)return;
        tMode=Math.abs(dx)>Math.abs(dy)?"pan":"vscroll";
      }
      if(tMode==="pan"){
        e.preventDefault();
        const bw=(cv.clientWidth-L-R)/(viewN+1);
        viewOff=Math.max(0,Math.min(n-viewN,
          Math.round(tStart.off-dx/bw)));
        scheduleCharts();
      }
      /* vscroll：不拦截，页面自然竖向滚动 */
    }
  },{passive:false});
  cv.addEventListener("touchend",()=>{
    if(touchesLeft(e)===0){tStart=null;tMode=null;}
  });
  function touchesLeft(e){return e.touches.length;}
});
document.addEventListener("mousemove",e=>{
  if(!drag||!A)return;
  const cvMain=document.getElementById("cvMain");
  const bw=(cvMain.clientWidth-L-R)/(viewN+1);
  const shift=Math.round((drag.x-e.clientX)/bw);
  const n=A.bars.length;
  const no=Math.max(0,Math.min(n-viewN,drag.off+shift));
  if(no!==viewOff){viewOff=no;drawCharts();}
});
document.addEventListener("mouseup",()=>{drag=null;});
</script>
</body>
</html>"""


# ---------------- 服务端分析（文字版用） ----------------

def _sma(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = sum(vals[i - n + 1:i + 1]) / n
    return out


def _ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def _macd(c):
    dif = [a - b for a, b in zip(_ema(c, 12), _ema(c, 26))]
    dr = _ema(dif, 9)
    dea = [None] * 8 + dr[8:]
    mh = [None if dd is None else 2 * (a - dd) for a, dd in zip(dif, dea)]
    return dif, dea, mh


def _kdj(rows, n=9):
    ks, ds, k, d = [], [], 50.0, 50.0
    for i in range(len(rows)):
        seg = rows[max(0, i - n + 1):i + 1]
        lo = min(r["low"] for r in seg)
        hi = max(r["high"] for r in seg)
        rsv = ((rows[i]["close"] - lo) / (hi - lo) * 100
               if hi > lo else 50.0)
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        ds.append(d)
    return ks, ds


def _rsi(c, n):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    gains = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    losses = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def _fetch_quote_one(full):
    f = http_get(QT_URL + full).split("~")
    if len(f) < 35 or not f[3]:
        raise ValueError("未查询到该股票")
    return {"name": f[1], "price": float(f[3]), "prev_close": float(f[4]),
            "open": float(f[5]), "high": float(f[33]), "low": float(f[34]),
            "time": f[30]}


def _fetch_kline_rows(full):
    kd = json.loads(http_get(
        KLINE_URL + "?param=" + full + ",day,,,500,qfq", timeout=20))
    d = kd.get("data", {}).get(full)
    if not d:
        raise ValueError("K线数据获取失败")
    bars = d.get("qfqday") or d.get("day")
    return [{"date": b[0], "open": float(b[1]), "close": float(b[2]),
             "high": float(b[3]), "low": float(b[4]), "vol": float(b[5])}
            for b in bars if float(b[2]) > 0]


def _logret(seq):
    return [math.log(seq[i + 1] / seq[i]) for i in range(len(seq) - 1)]


def _znorm(w):
    m = sum(w) / len(w)
    sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5 or 1e-12
    return [(x - m) / sd for x in w]


def _pctile(vals, p):
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    f = k - lo
    return s[lo] * (1 - f) + s[hi] * f


def analyze_server(full):
    W = W_WINDOW
    q = _fetch_quote_one(full)
    rows = _fetch_kline_rows(full)
    today = time.strftime("%Y-%m-%d")
    live = None
    if rows[-1]["date"] == today:
        live = rows.pop()
        live["close"] = q["price"]
        live["high"] = max(live["high"], q["price"])
        if q["low"] > 0:
            live["low"] = min(live["low"], q["price"])
    closes = [r["close"] for r in rows]
    rets = _logret(closes)
    cur = _znorm(rets[-W:])
    sims = []
    for i in range(W, len(rets) - 1):
        w = _znorm(rets[i - W:i])
        sims.append((sum((a - b) ** 2 for a, b in zip(cur, w)) ** 0.5, i))
    sims.sort(key=lambda x: x[0])
    prev_close = q["prev_close"] or closes[-1]
    o_today = q["open"] or prev_close
    gap = (o_today / prev_close - 1) * 100
    samples = []
    for _, i in sims[:TOPK]:
        r, n1, n2 = rows[i], rows[i + 1], rows[i + 2]
        samples.append({
            "t_date": r["date"], "n1_date": n1["date"],
            "cl_o": n1["close"] / n1["open"] - 1,
            "hi_o": n1["high"] / n1["open"] - 1,
            "lo_o": n1["low"] / n1["open"] - 1,
            "t2_cl": n2["close"] / n2["open"] - 1,
            "t2_hi": n2["high"] / n2["open"] - 1,
            "t2_lo": n2["low"] / n2["open"] - 1,
            "gap": n1["open"] / r["close"] - 1,
        })
    sel = [s for s in samples if abs(s["gap"] * 100 - gap) <= 1.0]
    src = sel if len(sel) >= 3 else samples

    def P(key, p):
        return _pctile([s[key] for s in src], p)

    t_pred = {p: {"cl": o_today * (1 + P("cl_o", p)),
                  "hi": o_today * (1 + P("hi_o", p)),
                  "lo": o_today * (1 + P("lo_o", p))}
              for p in (10, 25, 50, 75, 90)}
    up_prob = len([s for s in src if s["cl_o"] > 0]) / len(src)
    base = t_pred[50]["cl"]
    pred = {"open": base,
            "close": base * (1 + _pctile([s["t2_cl"] for s in src], 50)),
            "high": base * (1 + _pctile([s["t2_hi"] for s in src], 75)),
            "low": base * (1 + _pctile([s["t2_lo"] for s in src], 25))}

    disp = rows + ([live] if live else [])
    ci = [r["close"] for r in disp]
    dif, dea, mh = _macd(ci)
    ks, ds = _kdj(disp)
    js_ = [3 * a - 2 * b for a, b in zip(ks, ds)]
    r6 = _rsi(ci, 6)

    def lv(a):
        return next((v for v in reversed(a) if v is not None), None)

    grouped = {}
    start = max(1, len(disp) - 60)
    for i in range(start, len(disp)):
        day = disp[i]["date"]
        txts = []
        if None not in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
                txts.append((0, "B", "MACD金叉"))
            elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
                txts.append((0, "S", "MACD死叉"))
        if ks[i - 1] <= ds[i - 1] and ks[i] > ds[i] and ks[i] < 45:
            txts.append((1, "B", f"KDJ低位金叉 K={ks[i]:.1f}"))
        elif ks[i - 1] >= ds[i - 1] and ks[i] < ds[i] and ks[i] > 65:
            txts.append((1, "S", f"KDJ高位死叉 K={ks[i]:.1f}"))
        if r6[i] is not None and r6[i - 1] is not None:
            if r6[i - 1] < 20 and r6[i] >= 20:
                txts.append((2, "B", f"RSI6超卖回升 {r6[i]:.1f}"))
            elif r6[i - 1] > 80 and r6[i] <= 80:
                txts.append((2, "S", f"RSI6超买回落 {r6[i]:.1f}"))
        if txts:
            txts.sort()
            grouped[i] = (i, day, txts[0][1], txts[0][2])
    signals = sorted(grouped.values(), key=lambda s: s[0])

    # 视图切片（供 SVG 服务端绘图）
    off_s = max(0, len(disp) - 60)

    def _sl(a):
        return a[off_s:]

    series = {
        "bars": disp[off_s:],
        "dates": [r["date"] for r in disp[off_s:]],
        "ma": {n: _sma(ci, n)[off_s:] for n in (5, 10, 20, 30, 60)},
        "dif": _sl(dif), "dea": _sl(dea), "mh": _sl(mh),
        "vols": _sl([r["vol"] for r in disp]),
        "signals": [(i - off_s, d, t, x) for (i, d, t, x) in signals
                    if i >= off_s],
        "includePred": True,
    }
    idx_rows = _fetch_kline_rows("sh000001")
    idx_chg = ((idx_rows[-1]["close"] / idx_rows[-2]["close"]) * 100 - 100
               if len(idx_rows) >= 2 else None)

    lines = [
        f"{q['name']} ({full})  快照 {q['time']}",
        f"昨收 {prev_close:.2f} | 今开 {o_today:.2f} (缺口 {gap:+.2f}%) "
        f"| 现价 {q['price']:.2f}",
        f"大盘(上证)今日: {(idx_chg or 0):+.2f}%",
        "",
        "== 今日(T)收盘预测 [锚定今开] ==",
    ]
    for pp in (10, 25, 50, 75, 90):
        lines.append(f"P{pp}: 收{t_pred[pp]['cl']:.2f} "
                     f"高{t_pred[pp]['hi']:.2f} 低{t_pred[pp]['lo']:.2f}")
    lines.append(f"上行概率 {up_prob*100:.0f}%  样本 {len(src)}/{len(samples)}"
                 f"{'(缺口筛选)' if len(sel) >= 3 else ''}")
    lines.append("")
    lines.append("== 次日(T+1)预测 ==")
    lines.append(f"开{pred['open']:.2f} 收{pred['close']:.2f} "
                 f"高{pred['high']:.2f} 低{pred['low']:.2f}")
    lines.append("")
    lines.append(f"DIF {lv(dif):.3f} | DEA {lv(dea):.3f} | "
                 f"K {lv(ks):.1f} | D {lv(ds):.1f} | J {lv(js_):.1f} | "
                 f"RSI6 {lv(r6):.1f}")
    lines.append("")
    lines.append("== 相似历史参考日期 ==")
    for s in samples:
        mark = "*" if abs(s["gap"] * 100 - gap) <= 1.0 else ""
        lines.append(f"  {s['t_date']} -> {s['n1_date']}  "
                     f"{s['cl_o']*100:+.2f}% {mark}")
    lines.append("")
    lines.append("== 近期信号 ==")
    for i, day, t, txt in signals[-8:]:
        lines.append(f"  {day} [{'买' if t == 'B' else '卖'}] {txt}")
    lines.append("")
    lines.append("免责声明：仅为历史数据统计研究用途，不构成投资建议，盈亏自负。"
                 "作者 獨白 kingrux106@gmail.com QQ:2180287399")
    return {"text": "\n".join(lines), "series": series,
            "pred": pred}





PAGE_LITE = '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>股票预测 精简版</title>\n<style>\nbody{margin:0;background:#fff;color:#222;font-family:Arial,sans-serif;font-size:14px}\n.hd{padding:6px;background:#eef1f4;border-bottom:1px solid #ddd}\ninput{font-size:16px;width:38%;padding:5px}\nbutton{font-size:15px;padding:6px 9px;margin:2px 0}\nselect{font-size:14px;padding:4px}\ncanvas{width:100%;display:block;border:1px solid #ccc;margin-top:6px;background:#fff}\n#info{padding:4px 8px;color:#555;min-height:18px}\n.side{padding:4px 10px 10px;font-size:13px;line-height:1.55}\na{color:#06c}\n</style>\n</head>\n<body>\n<div class="hd">\n<input id="code" value="000725">\n<button id="btnGo" type="button">分析</button>\n<select id="ind">\n<option>MACD</option><option>KDJ</option><option>RSI</option>\n</select>\n<button id="btnZi" type="button">+</button>\n<button id="btnZo" type="button">-</button>\n<button id="btnLat" type="button">最新</button>\n</div>\n<div id="info">精简版(兼容老设备) 输入代码点【分析】</div>\n<canvas id="cv1" height="250"></canvas>\n<canvas id="cv2" height="80"></canvas>\n<div class="side" id="side"></div>\n<p style="font-size:12px;padding:4px 10px">\n文字版(无JS) <a href="/text">/text</a> |\n完整版 <a href="/">/</a> |\n仅供参考，不构成投资建议\n</p>\n<script>\nvar D=null,A=null,viewN=30,viewOff=0,L=46,R=46;\nfunction el(id){return document.getElementById(id);}\nfunction isMob(){return screen.width<760;}\nfunction httpGet(url,cb){\n  var x=new XMLHttpRequest();\n  x.open("GET",url,true);\n  x.onreadystatechange=function(){\n    if(x.readyState===4){\n      if(x.status===200)cb(x.responseText);else cb(null);\n    }\n  };\n  x.send();\n}\n/* ---- 指标计算 (ES5) ---- */\nfunction smaP(v,n){var o=[],i,s=0;\n  for(i=0;i<v.length;i++){s+=v[i];if(i>=n)s-=v[i-n];o.push(i>=n-1?s/n:null);}\n  return o;}\nfunction emaA(v,n){var o=[],k=2/(n+1),e=null,i;\n  for(i=0;i<v.length;i++){e=(e==null)?v[i]:v[i]*k+e*(1-k);o.push(e);}\n  return o;}\nfunction calcMACD(c){var dif=[],dea=[],mh=[],i;\n  var e12=emaA(c,12),e26=emaA(c,26);\n  for(i=0;i<c.length;i++)dif.push(e12[i]-e26[i]);\n  var dr=emaA(dif,9);\n  for(i=0;i<c.length;i++){\n    var dd=(i<8)?null:dr[i];\n    dea.push(dd);\n    mh.push((dd==null)?null:2*(dif[i]-dd));\n  }\n  return [dif,dea,mh];}\nfunction calcKDJ(rows,n){var ks=[],ds=[],k=50,d=50,i,j;\n  for(i=0;i<rows.length;i++){\n    var lo=1e18,hi=-1e18;\n    for(j=Math.max(0,i-n+1);j<=i;j++){\n      if(rows[j].low<lo)lo=rows[j].low;\n      if(rows[j].high>hi)hi=rows[j].high;\n    }\n    var rsv=(hi>lo)?((rows[i].close-lo)/(hi-lo)*100):50;\n    k=k*2/3+rsv/3;d=d*2/3+k/3;\n    ks.push(k);ds.push(d);\n  }\n  return [ks,ds];}\nfunction calcRSI(c,n){var out=[],i,ch;\n  for(i=0;i<c.length;i++)out.push(null);\n  if(c.length<=n)return out;\n  var ag=0,al=0;\n  for(i=1;i<=n;i++){ch=c[i]-c[i-1];if(ch>0)ag+=ch;else al-=ch;}\n  ag/=n;al/=n;\n  out[n]=(al===0)?100:100-100/(1+ag/al);\n  for(i=n+1;i<c.length;i++){\n    ch=c[i]-c[i-1];\n    ag=(ag*(n-1)+Math.max(ch,0))/n;\n    al=(al*(n-1)+Math.max(-ch,0))/n;\n    out[i]=(al===0)?100:100-100/(1+ag/al);\n  }\n  return out;}\nfunction pctile(v,p){var s=v.slice().sort(function(a,b){return a-b;});\n  var k=(s.length-1)*p/100,lo=Math.floor(k),hi=Math.min(lo+1,s.length-1),f=k-lo;\n  return s[lo]*(1-f)+s[hi]*f;}\nfunction znorm(w){var m=0,i,sd,o=[];\n  for(i=0;i<w.length;i++)m+=w[i];m/=w.length;\n  sd=0;for(i=0;i<w.length;i++)sd+=(w[i]-m)*(w[i]-m);\n  sd=Math.sqrt(sd/w.length)||1e-12;\n  for(i=0;i<w.length;i++)o.push((w[i]-m)/sd);return o;}\n/* ---- 分析 ---- */\nfunction analyzeAll(){\n  var today=D.today,Wn=10,K=10,i,j,kk,si,pi;\n  var all=D.bars,bars2=[];\n  for(i=0;i<all.length;i++)\n    bars2.push({date:all[i].dt,open:all[i].o,close:all[i].c,\n                high:all[i].h,low:all[i].l,vol:all[i].v});\n  var live=null,hist=bars2,last=null;\n  if(bars2.length&&bars2[bars2.length-1].date===today){\n    last=bars2[bars2.length-1];\n    live={date:last.date,open:last.open,close:D.price,\n          high:(D.price>last.high)?D.price:last.high,\n          low:last.low,vol:last.vol};\n    if(D.low>0&&D.price<live.low)live.low=D.price;\n    hist=bars2.slice(0,-1);\n  }\n  var rets=[];\n  for(i=1;i<hist.length;i++)\n    rets.push(Math.log(hist[i].close/hist[i-1].close));\n  var cur=znorm(rets.slice(-Wn));\n  var sims=[];\n  for(i=Wn;i<rets.length-1;i++){\n    var w=znorm(rets.slice(i-Wn,i)),dsq=0;\n    for(j=0;j<Wn;j++){var df=cur[j]-w[j];dsq+=df*df;}\n    sims.push([Math.sqrt(dsq),i]);\n  }\n  sims.sort(function(a,b){return a[0]-b[0];});\n  var top=sims.slice(0,K);\n  var prevClose=D.prev_close||hist[hist.length-1].close;\n  var oT=D.open||prevClose,gapT=(oT/prevClose-1)*100;\n  var samples=[];\n  for(si=0;si<top.length;si++){\n    var idx=top[si][1],r=hist[idx],n1=hist[idx+1],n2=hist[idx+2];\n    samples.push({t_date:r.date,n1_date:n1.date,\n      cl_o:n1.close/n1.open-1,t2_cl:n2.close/n2.open-1,\n      gap:n1.open/r.close-1});\n  }\n  var sel=[];\n  for(si=0;si<samples.length;si++)\n    if(Math.abs(samples[si].gap*100-gapT)<=1)sel.push(samples[si]);\n  var src=(sel.length>=3)?sel:samples;\n  function P(key,p){var a=[],q2;for(q2=0;q2<src.length;q2++)a.push(src[q2][key]);return pctile(a,p);}\n  var tp={},ps=[10,25,50,75,90],pi;\n  for(pi=0;pi<ps.length;pi++){\n    var pp=ps[pi];\n    tp[pp]={cl:oT*(1+P("cl_o",pp)),hi:oT*(1+P("hi_o",pp)),lo:oT*(1+P("lo_o",pp))};\n  }\n  var ups=0;\n  for(si=0;si<src.length;si++)if(src[si].cl_o>0)ups++;\n  var upProb=ups/src.length;\n  var base=tp[50].cl;\n  var pred={date:"T+1预测",open:base,\n    close:base*(1+P("t2_cl",50)),\n    high:base*(1+P("t2_hi",75)),\n    low:base*(1+P("t2_lo",25))};\n  var disp=hist.concat(live?[live]:[]);\n  var closes=[];\n  for(i=0;i<disp.length;i++)closes.push(disp[i].close);\n  var md=calcMACD(closes);\n  var kd=calcKDJ(disp,9);\n  var r6=calcRSI(closes,6);\n  var ma={},ns=[5,10,20,30,60];\n  for(i=0;i<ns.length;i++)ma[ns[i]]=smaP(closes,ns[i]);\n  /* 信号：每日一个，优先级 MACD>KDJ>RSI */\n  var signals=[],seen={},start=Math.max(1,disp.length-60);\n  for(i=start;i<disp.length;i++){\n    var day=disp[i].date,items=[];\n    if(md[0][i]!=null&&md[1][i]!=null&&md[0][i-1]!=null&&md[1][i-1]!=null){\n      if(md[0][i-1]<=md[1][i-1]&&md[0][i]>md[1][i])\n        items.push([0,"B","MACD金叉"]);\n      else if(md[0][i-1]>=md[1][i-1]&&md[0][i]<md[1][i])\n        items.push([0,"S","MACD死叉"]);\n    }\n    if(kd[0][i-1]<=kd[1][i-1]&&kd[0][i]>kd[1][i]&&kd[0][i]<45)\n      items.push([1,"B","KDJ低位金叉"]);\n    else if(kd[0][i-1]>=kd[1][i-1]&&kd[0][i]<kd[1][i]&&kd[0][i]>65)\n      items.push([1,"S","KDJ高位死叉"]);\n    if(r6[i]!=null&&r6[i-1]!=null){\n      if(r6[i-1]<20&&r6[i]>=20)items.push([2,"B","RSI6超卖回升 "+r6[i].toFixed(1)]);\n      else if(r6[i-1]>80&&r6[i]<=80)items.push([2,"S","RSI6超买回落 "+r6[i].toFixed(1)]);\n    }\n    if(items.length){\n      items.sort(function(a,b){return a[0]-b[0];});\n      seen[i]={i:i,date:day,type:items[0][1],txt:items[0][2]+\n        ((items.length>1)?"(+"+(items.length-1)+")":"")};\n    }\n  }\n  var sigList=[];\n  for(var key in seen)sigList.push(seen[key]);\n  sigList.sort(function(a,b){return a.i-b.i;});\n  var vols=[];\n  for(i=0;i<disp.length;i++)vols.push(disp[i].vol);\n  A={name:D.name,code:D.code,price:D.price,prevClose:prevClose,\n     open:oT,gapToday:gapT,time:D.time,hasLive:(live!=null),\n     bars:disp,pred:pred,tp:tp,samples:samples,srcN:src.length,\n     filtered:(sel.length>=3),upProb:upProb,\n     ind:{ma:ma,dif:md[0],dea:md[1],mh:md[2],k:kd[0],d:kd[1],r6:r6},\n     signals:sigList,vols:vols};\n}\n/* ---- 绘图 ---- */\nfunction prep(cv,hDef){\n  var w=cv.clientWidth||320,dpr=(window.devicePixelRatio||1);\n  var hEl=hDef;\n  cv.style.height=hEl+"px";\n  cv.width=Math.round(w*dpr);cv.height=Math.round(hEl*dpr);\n  var ctx=cv.getContext("2d");ctx.setTransform(dpr,0,0,dpr,0,0);\n  ctx.fillStyle="#fff";ctx.fillRect(0,0,w,hEl);\n  return [ctx,w,hEl];\n}\nfunction viewSlice(){\n  var n=A.bars.length,vn=viewN;\n  if(vn>n)vn=n;if(vn<12)vn=12;viewN=vn;\n  if(viewOff>n-vn)viewOff=n-vn;if(viewOff<0)viewOff=0;\n  var off=viewOff;\n  function sl(a){return a.slice(off,off+vn);}\n  var ma={},ns=[5,10,20,30,60];\n  for(var i=0;i<ns.length;i++)ma[ns[i]]=A.ind.ma[ns[i]].slice(off,off+vn);\n  var sigs=[];\n  for(var j=0;j<A.signals.length;j++){var s=A.signals[j];\n    if(s.i>=off&&s.i<off+vn){var c={};for(var k in s)c[k]=s[k];c.i=s.i-off;sigs.push(c);}}\n  return {off:off,bars:A.bars.slice(off,off+vn),\n    includePred:(off+vn>=n),\n    ma:ma,dif:A.ind.dif.slice(off,off+vn),dea:A.ind.dea.slice(off,off+vn),\n    mh:A.ind.mh.slice(off,off+vn),k:A.ind.k.slice(off,off+vn),\n    d:A.ind.d.slice(off,off+vn),r6:A.ind.r6.slice(off,off+vn),\n    signals:sigs};\n}\nvar V=null;\nvar UP="#e03131",DN="#0ca678",PC="#1971c2",CROSS_C="#9fb3c8";\nvar MAC={5:"#f08c00",10:"#1971c2",20:"#9c36b5",30:"#2f9e44",60:"#86421f"};\nfunction drawMain(){\n  var cv=el("cv1"),res=prep(cv,250),ctx=res[0],w=res[1],H=res[2];\n  V=viewSlice();\n  var B=V.bars.slice();\n  if(V.includePred)B.push(A.pred);\n  var n=B.length,T=12,Bm=16,pw=w-L-R,ph=H-T-Bm,bw=pw/n,i,nn,b;\n  var lo=1e18,hi=-1e18;\n  for(i=0;i<n;i++){if(B[i].low<lo)lo=B[i].low;if(B[i].high>hi)hi=B[i].high;}\n  for(nn in MAC){var arr=V.ma[nn];\n    for(i=0;i<arr.length;i++)if(arr[i]!=null){\n      if(arr[i]<lo)lo=arr[i];if(arr[i]>hi)hi=arr[i];}}\n  var rng=(hi-lo)||1,pad=rng*0.06;lo-=pad;hi+=pad;\n  function Y(v){return T+(hi-v)/(hi-lo)*ph;}\n  function X(i2){return L+bw*(i2+0.5);}\n  ctx.strokeStyle="#dddddd";ctx.fillStyle="#666666";ctx.font="10px Arial";ctx.textAlign="right";\n  for(var g2=0;g2<=3;g2++){var gv=lo+(hi-lo)*g2/3,y=Y(gv);\n    ctx.beginPath();ctx.moveTo(L,y);ctx.lineTo(w-R,y);ctx.stroke();\n    ctx.fillText(gv.toFixed(2),L-4,y+3);}\n  ctx.textAlign="left";\n  for(nn in MAC){ctx.strokeStyle=MAC[nn];ctx.beginPath();var st=false;\n    var arr=V.ma[nn];\n    for(i=0;i<arr.length;i++){var v=arr[i];\n      if(v==null){st=false;continue;}\n      if(st)ctx.lineTo(X(i),Y(v));else ctx.moveTo(X(i),Y(v));st=true;}\n    ctx.stroke();\n    ctx.fillStyle=MAC[nn];ctx.fillText("MA"+nn,L+2+[5,10,20,30,60].indexOf(+nn)*34,10);}\n  ctx.textAlign="center";\n  for(i=0;i<n;i++){b=B[i];\n    var up=b.close>=b.open,col=(b.date=="T+1预测")?PC:(up?UP:DN);\n    if(b.date=="T+1预测")ctx.setLineDash([3,2]);\n    ctx.strokeStyle=col;ctx.beginPath();\n    ctx.moveTo(X(i),Y(b.high));ctx.lineTo(X(i),Y(b.low));ctx.stroke();\n    var bw2=Math.max(bw*0.62,2);\n    var y1=Y(Math.max(b.open,b.close)),y2=Y(Math.min(b.open,b.close));\n    if(y2-y1<1)y2=y1+1;\n    if(b.date=="T+1预测"){ctx.strokeRect(X(i)-bw2/2,y1,bw2,y2-y1);}\n    else{ctx.fillStyle=col;ctx.fillRect(X(i)-bw2/2,y1,bw2,y2-y1);}\n    ctx.setLineDash([]);\n  }\n  for(i=0;i<V.signals.length;i++){var s=V.signals[i];\n    if(s.i<0||s.i>=n-1)continue;\n    b=B[s.i];var x=X(s.i);ctx.fillStyle=(s.type=="B")?UP:DN;\n    if(s.type=="B"){var yy=Y(b.low)+5;\n      ctx.beginPath();ctx.moveTo(x,yy);ctx.lineTo(x-4,yy+8);ctx.lineTo(x+4,yy+8);ctx.fill();}\n    else{yy=Y(b.high)-5;\n      ctx.beginPath();ctx.moveTo(x,yy);ctx.lineTo(x-4,yy-8);ctx.lineTo(x+4,yy-8);ctx.fill();}\n  }\n  ctx.fillStyle="#888888";ctx.textAlign="left";\n  var stepN=Math.max(1,Math.ceil(n/8));\n  for(i=0;i<n;i+=stepN)ctx.fillText(B[i].date.slice(5),X(i)-14,H-4);\n}\nfunction indChart(){\n  var cv=el("cv2"),res=prep(cv,90),ctx=res[0],w=res[1],H=res[2];\n  var mode=el("ind").value;\n  var n=V.bars.length+(V.includePred?1:0),T=10,Bm=14;\n  var pw=w-L-R,ph=H-T-Bm,bw=pw/n,i,j2,v;\n  function X(i2){return L+bw*(i2+.5);}\n  var vals=[0],guides=[],lines=[],histArr=null,label=mode;\n  if(mode==="MACD"){\n    var srcs=[V.dif,V.dea,V.mh];\n    for(i=0;i<srcs.length;i++)for(j2=0;j2<srcs[i].length;j2++)\n      if(srcs[i][j2]!=null)vals.push(srcs[i][j2]);\n    lines=[[V.dif,"#e8890c"],[V.dea,"#1971c2"]];histArr=V.mh;\n  }else if(mode==="KDJ"){\n    vals=V.k.concat(V.d).concat(V.j);guides=[20,50,80];\n    lines=[[V.k,"#e8890c"],[V.d,"#1971c2"]];label="KDJ K/D";\n  }else{\n    vals=[0,100];guides=[30,50,70];\n    lines=[[V.r6,"#e8890c"]];label="RSI6";\n  }\n  var lo=Math.min.apply(null,vals),hi=Math.max.apply(null,vals);\n  var rr=(hi-lo)||1;lo-=rr*0.08;hi+=rr*0.08;\n  function Y(v){return T+(hi-v)/(hi-lo)*ph;}\n  guides.forEach(function(gv){var y=Y(gv);\n    ctx.strokeStyle="#eeeeee";ctx.beginPath();\n    ctx.moveTo(L,y);ctx.lineTo(w-R,y);ctx.stroke();\n    ctx.fillStyle="#888888";ctx.font="10px Arial";ctx.textAlign="right";\n    ctx.fillText(String(gv),L-4,y+3);});\n  if(histArr){var zero=Y(0);\n    for(i=0;i<histArr.length;i++){var hv=histArr[i];\n      if(hv==null)continue;\n      ctx.fillStyle=hv>=0?UP:DN;\n      ctx.fillRect(X(i)-1,Math.min(zero,Y(hv)),2,Math.abs(Y(hv)-zero)||1);}}\n  for(i=0;i<lines.length;i++){\n    ctx.strokeStyle=lines[i][1];ctx.beginPath();var st=false;\n    var arr=lines[i][0];\n    for(j2=0;j2<arr.length;j2++){v=arr[j2];\n      if(v==null){st=false;continue;}\n      if(st)ctx.lineTo(X(j2),Y(v));else ctx.moveTo(X(j2),Y(v));st=true;}\n    ctx.stroke();}\n  ctx.fillStyle="#555555";ctx.textAlign="left";ctx.fillText(label,L+2,T-2);\n}\nfunction draw(){if(!A)return;drawMain();indChart();renderSide();}\nfunction zoom(f){\n  if(!A)return;\n  viewN=Math.round(viewN*f);\n  var mn=isMob()?12:20;\n  if(viewN<mn)viewN=mn;\n  if(viewN>A.bars.length)viewN=A.bars.length;\n  clampView();draw();\n}\nfunction latest(){if(!A)return;viewOff=A.bars.length-viewN;if(viewOff<0)viewOff=0;draw();}\nfunction clampView(){if(viewOff>A.bars.length-viewN)viewOff=A.bars.length-viewN;if(viewOff<0)viewOff=0;}\nfunction renderSide(){\n  var t=A.tp,pr=A.pred,h="<b>今日(T)预测</b>(锚定今开)<br>",i;\n  var ps=[10,50,90];\n  for(i=0;i<ps.length;i++){var pp=ps[i];\n    h+="P"+pp+": 收"+t[pp].cl.toFixed(2)+" 高"+t[pp].hi.toFixed(2)+" 低"+t[pp].lo.toFixed(2)+"<br>";}\n  h+="上行概率 "+Math.round(A.upProb*100)+"% (样本"+A.srcN+"/"+A.samples.length+")<br>";\n  h+="<b>次日(T+1)</b>: 开"+pr.open.toFixed(2)+" 收"+pr.close.toFixed(2)+\n     " 高"+pr.high.toFixed(2)+" 低"+pr.low.toFixed(2)+"<br>";\n  h+="<b>相似历史日期</b><br>";\n  for(i=0;i<A.samples.length;i++){var s=A.samples[i];\n    var mk=(Math.abs(s.gap*100-A.gapToday)<=1)?"*":"";\n    h+=s.t_date+"→"+s.n1_date+" "+(s.cl_o*100).toFixed(1)+"%"+mk+"<br>";}\n  h+="<span style=\'color:#888\'>仅供参考，不构成投资建议</span>";\n  el("side").innerHTML=h;\n}\n/* 触屏平移（单指横向） */\nvar tX=null,tOff=0;\n(function(){var cv=el("cv1");\n  cv.addEventListener("touchstart",function(e){\n    if(!A)return;tX=e.touches[0].clientX;tOff=viewOff;},{passive:true});\n  cv.addEventListener("touchmove",function(e){\n    if(!A||tX==null)return;\n    e.preventDefault();\n    var bw=(cv.clientWidth-L-R)/(viewN+1);\n    var shift=Math.round((tX-e.touches[0].clientX)/bw);\n    var n=A.bars.length;\n    viewOff=Math.max(0,Math.min(n-viewN,tOff+shift));\n    draw();\n  },{passive:false});\n  cv.addEventListener("touchend",function(){tX=null;});\n})();\nwindow.addEventListener("resize",function(){draw();});\n/* 老设备兼容绑定：click+touchend 双通道、防连击、keyCode 回车 */\nfunction bindTap(id,fn){\n  var b=el(id);\n  if(!b)return;\n  var last=0;\n  function handler(){\n    var now=new Date().getTime();\n    if(now-last<350)return;\n    last=now;\n    fn();\n  }\n  if(b.addEventListener){\n    b.addEventListener("click",handler,false);\n    b.addEventListener("touchend",function(){handler();},false);\n  }else if(b.attachEvent){\n    b.attachEvent("onclick",function(){handler();});\n  }\n}\nbindTap("btnGo",go);\nbindTap("btnZi",function(){zoom(0.8);});\nbindTap("btnZo",function(){zoom(1.25);});\nbindTap("btnLat",latest);\nel("ind").onchange=function(){draw();};\nel("code").onkeydown=function(e){\n  e=e||window.event;\n  var k=e.keyCode||e.which||0;\n  if(k===13)go();\n};\ngo();\n</script>\n</body>\n</html>\n'




def build_svg(res):
    """服务端渲染 K 线 SVG（老设备兼容，<img> 直接显示）。"""
    S = res["series"]
    pred = res["pred"]
    bars = S["bars"]
    B = bars + [dict(open=pred["open"], close=pred["close"],
                     high=pred["high"], low=pred["low"], date="T+1预测")]
    n = len(B)
    Wd, Ht = 720, 440
    L, R = 46, 50
    pT, pH = 14, 240
    vT, vH = 262, 56
    mT, mH = 326, 96
    pw = Wd - L - R
    bw = pw / n

    lo, hi = 1e18, -1e18
    for b in B:
        lo = min(lo, b["low"]); hi = max(hi, b["high"])
    for nn in S["ma"]:
        for v in S["ma"][nn]:
            if v is not None:
                lo = min(lo, v); hi = max(hi, v)
    rng = (hi - lo) or 1
    lo -= rng * .06; hi += rng * .06

    def Y(v):
        return round(pT + (hi - v) / (hi - lo) * pH, 1)

    def X(i):
        return round(L + bw * (i + .5), 1)

    e = [f'<rect width="{Wd}" height="{Ht}" fill="#ffffff"/>']
    for k in range(4):
        gv = lo + (hi - lo) * k / 3
        y = Y(gv)
        e.append(f'<line x1="{L}" y1="{y:.1f}" x2="{Wd-R}" y2="{y:.1f}" '
                 f'stroke="#ececec"/>')
        e.append(f'<text x="{L-4}" y="{y+3:.1f}" text-anchor="end" '
                 f'font-size="9" fill="#888888">{gv:.2f}</text>')
    colors = {5: "#f08c00", 10: "#1971c2", 20: "#9c36b5",
              30: "#2f9e44", 60: "#86421f"}
    for nn, col in colors.items():
        pts = []
        arr = S["ma"][nn]
        for i, v in enumerate(arr):
            if v is not None:
                pts.append(str(X(i)) + "," + str(Y(v)))
        if len(pts) > 1:
            e.append('<polyline points="' + " ".join(pts) +
                     '" fill="none" stroke="' + col + '" stroke-width="1"/>')
        xi = L + 2 + [5, 10, 20, 30, 60].index(nn) * 34
        e.append(f'<text x="{xi}" y="10" font-size="9" fill="{col}">'
                 f'MA{nn}</text>')
    for i, b in enumerate(B):
        isp = (b["date"] == "T+1预测")
        up = b["close"] >= b["open"]
        col = "#1971c2" if isp else ("#e03131" if up else "#0ca678")
        dash = ' stroke-dasharray="3,2"' if isp else ""
        x = X(i)
        e.append(f'<line x1="{x}" y1="{Y(b["high"]):.1f}" x2="{x}" '
                 f'y2="{Y(b["low"]):.1f}" stroke="{col}"{dash}/>')
        bw2 = max(bw * .62, 1.5)
        y1 = Y(max(b["open"], b["close"]))
        y2 = Y(min(b["open"], b["close"]))
        hgt = max(y2 - y1, 1)
        if isp:
            e.append(f'<rect x="{x-bw2/2:.1f}" y="{y1:.1f}" width="{bw2:.1f}"'
                     f' height="{hgt:.1f}" fill="none" stroke="{col}"{dash}/>')
        else:
            e.append(f'<rect x="{x-bw2/2:.1f}" y="{y1:.1f}" width="{bw2:.1f}"'
                     f' height="{hgt:.1f}" fill="{col}"/>')
    for (si_, d_, t_, txt_) in S["signals"]:
        if si_ < 0 or si_ >= n - 1:
            continue
        b = B[si_]
        x = X(si_)
        col = "#e03131" if t_ == "B" else "#0ca678"
        if t_ == "B":
            y = Y(b["low"]) + 5
            e.append(f'<polygon points="{x},{y} {x-4},{y+7} {x+4},{y+7}" '
                     f'fill="{col}"/>')
        else:
            y = Y(b["high"]) - 5
            e.append(f'<polygon points="{x},{y} {x-4},{y-7} {x+4},{y-7}" '
                     f'fill="{col}"/>')
    e.append(f'<text x="{X(n-1)-44:.1f}" y="{pT-2}" font-size="10" '
             f'fill="#1971c2">T+1 C:{pred["close"]:.2f}</text>')
    stepn = max(1, n // 8)
    for i in range(0, n, stepn):
        e.append(f'<text x="{X(i)-12:.1f}" y="{pT+pH+12}" font-size="8" '
                 f'fill="#888888">{B[i]["date"][5:]}</text>')

    vols = S["vols"]
    vmax = max([v for v in vols if v], default=1) * 1.08

    def VY(v):
        return round(vT + (1 - v / vmax) * vH, 1)

    bw2 = max(bw * .62, 1.5)
    for i, b in enumerate(B[:-1]):
        v = vols[i] if i < len(vols) else None
        if not v:
            continue
        col = "#e03131" if b["close"] >= b["open"] else "#0ca678"
        e.append(f'<rect x="{X(i)-bw2/2:.1f}" y="{VY(v):.1f}" '
                 f'width="{bw2:.1f}" height="{vT+vH-VY(v):.1f}" '
                 f'fill="{col}" opacity="0.85"/>')
    e.append(f'<text x="{L+2}" y="{vT-2}" font-size="9" fill="#555555">'
             f'VOL</text>')

    dif, dea, mh = S["dif"], S["dea"], S["mh"]
    mvals = [0]
    for a in (dif, dea, mh):
        for v in a:
            if v is not None:
                mvals.append(v)
    mlo, mhi = min(mvals), max(mvals)
    mrr = (mhi - mlo) or 1
    mlo -= mrr * .08; mhi += mrr * .08

    def MY(v):
        return round(mT + (mhi - v) / (mhi - mlo) * mH, 1)

    zero = MY(0)
    e.append(f'<line x1="{L}" y1="{zero}" x2="{Wd-R}" y2="{zero}" '
             f'stroke="#dddddd"/>')
    for k in range(3):
        vv = mlo + (mhi - mlo) * k / 2
        e.append(f'<text x="{L-4}" y="{MY(vv)+3:.1f}" text-anchor="end" '
                 f'font-size="9" fill="#888888">{vv:.2f}</text>')
    mbw = max(pw / n * .3, 1)
    for i, hv in enumerate(mh):
        if hv is None:
            continue
        col = "#e03131" if hv >= 0 else "#0ca678"
        y = MY(hv)
        e.append(f'<rect x="{X(i)-mbw:.1f}" y="{min(y,zero):.1f}" '
                 f'width="{mbw*2:.1f}" height="{max(abs(y-zero),1):.1f}" '
                 f'fill="{col}"/>')
    for arr, col in ((dif, "#e8890c"), (dea, "#1971c2")):
        pts = [str(X(i)) + "," + str(MY(v)) for i, v in enumerate(arr)
               if v is not None]
        if len(pts) > 1:
            e.append('<polyline points="' + " ".join(pts) +
                     '" fill="none" stroke="' + col + '" stroke-width="1"/>')
    e.append(f'<text x="{L+2}" y="{mT-2}" font-size="9" fill="#555555">'
             f'MACD</text>')

    return ('<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {Wd} {Ht}" width="{Wd}" height="{Ht}">'
            + "".join(e) + "</svg>")




class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(time.strftime("%H:%M:%S"), self.address_string(), fmt % args)

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 强制不缓存：任何设备刷新即得最新版
        self.send_header("Cache-Control",
                         "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE)
        elif parsed.path == "/api":
            qs = parse_qs(parsed.query)
            try:
                full = normalize_code(qs.get("code", [""])[0])
                res = api_data(full)
                self._send(200, json.dumps(res, ensure_ascii=False),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif parsed.path == "/quant/" or parsed.path == "/quant":
            try:
                with open(STATUS_HTML, "r", encoding="utf-8") as f:
                    self._send(200, f.read())
            except OSError:
                self._send(404, "status page not found", "text/plain")
        elif parsed.path == "/text" or parsed.path == "/t":
            qs = parse_qs(parsed.query)
            code = qs.get("code", [DEFAULT_CODE])[0] or DEFAULT_CODE
            try:
                res = analyze_server(normalize_code(code))
                img_tag = ('<img src="/svg?code=' + code +
                           '" style="max-width:100%" alt="K线图">')
                body = ("<html><head><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width,"
                        "initial-scale=1'><title>预测文字版</title></head>"
                        "<body style='font-family:Arial;font-size:15px;'>"
                        "<div>" + img_tag + "</div>"
                        "<pre style='white-space:pre-wrap;font-size:15px;"
                        "line-height:1.6'>" + res +
                        "</pre><p><a href='/lite?code=" + code + "'>图形版</a>"
                        " | <a href='/'>完整版</a> | "
                        "<a href='/text?code=000725'>京东方A</a></p></body>"
                        "</html>")
                self._send(200, body)
            except Exception as e:
                self._send(200, "加载失败: " + str(e), "text/plain")
        elif parsed.path == "/lite" or parsed.path == "/l":
            self._send(200, PAGE_LITE)
        elif parsed.path == "/svg":
            qs = parse_qs(parsed.query)
            code = qs.get("code", [DEFAULT_CODE])[0] or DEFAULT_CODE
            try:
                res = analyze_server(normalize_code(code))
                self._send(200, build_svg(res), "image/svg+xml")
            except Exception as e:
                self._send(404, str(e), "text/plain")
        elif parsed.path == "/e63":
            # E63/PyS60 转接端点：纯文本报告（+可选紧凑K线数据）
            qs = parse_qs(parsed.query)
            code = qs.get("code", [DEFAULT_CODE])[0] or DEFAULT_CODE
            want_bars = qs.get("bars", ["0"])[0] == "1"
            try:
                res = analyze_server(normalize_code(code))
                body = res["text"]
                if want_bars:
                    bl = ["BARS"]
                    for b in res["series"]["bars"][-45:]:
                        bl.append("%s|%.4f|%.4f|%.4f|%.4f|%.0f" % (
                            b["date"], b["open"], b["close"],
                            b["high"], b["low"], b.get("vol") or 0))
                    pd = res["pred"]
                    bl.append("PRED|%.4f|%.4f" % (pd["open"], pd["close"]))
                    body += "\n@@" + "\n".join(bl)
                self._send(200, body, "text/plain; charset=utf-8")
            except Exception as e:
                self._send(200, "ERR " + str(e), "text/plain; charset=utf-8")
        elif parsed.path == "/healthz":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"stock_web v2 listening on 127.0.0.1:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
