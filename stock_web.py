#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票形态相似度预测器 · 网页版 v2（纯标准库）

架构：Pi 仅作行情数据代理（转发腾讯接口原始K线），
     指标计算/形态匹配/预测全部在浏览器端 JS 完成，Pi 负载极低。

  python3 stock_web.py [--port 8010]
"""
import argparse
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

QT_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
STATUS_HTML = "/var/www/status/index.html"


def http_get(url, retries=3):
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
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
<div class="note">前复权价 · 历史统计推断仅供参考，不构成投资建议 · 滚轮缩放K线 · 模拟盘状态页见 <a style="color:#4da3ff" href="/quant/">/quant/</a> · 开源地址 <a style="color:#4da3ff" href="https://github.com/monologue-github/stock-analyzer" target="_blank" rel="noopener">GitHub stock-analyzer</a></div>
<script>
"use strict";
let D=null,A=null; // D=原始数据 A=分析结果
const DEFAULT_CODE="000725";
const UP="#e03131",DN="#0ca678",PC="#1971c2";
const MAC={5:"#f08c00",10:"#1971c2",20:"#9c36b5",30:"#2f9e44",60:"#86421f"};
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
  // 重绘后恢复十字光标，避免光标"消失"
  if(lastCross){
    const cv2=document.getElementById(lastCross.id);
    if(cv2){cross(cv2,lastCross.mx,lastCross.my);return;}
  }
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
  lastCross={id:cv.id,mx,my};
  const n=V.bars.length+(V.includePred?1:0),w=cv.clientWidth;
  let idx=Math.floor((mx-L)/(w-L-R)*n);
  idx=Math.max(0,Math.min(n-1,idx));
  const x=L+(w-L-R)*(idx+.5)/n;
  for(const id of ["cvMain","cvVol","cvInd"]){
    clearOv(id);
    const o=document.getElementById(OV[id]),ctx=o.getContext("2d");
    const k=o.width/o.clientWidth||1;ctx.setTransform(k,0,0,k,0,0);
    const h=o.clientHeight;
    ctx.save();ctx.strokeStyle=CROSS_C;ctx.setLineDash([4,3]);ctx.lineWidth=1;
    if(id===cv.id){                     // 悬停列高亮
      const bw2=(w-L-R)/n;
      ctx.setLineDash([]);
      ctx.fillStyle="rgba(25,113,194,0.10)";
      ctx.fillRect(x-bw2/2,12,bw2,h-30);
      ctx.setLineDash([4,3]);
    }
    ctx.beginPath();ctx.moveTo(x,14);ctx.lineTo(x,h-18);ctx.stroke();
    if(id===cv.id){
      ctx.beginPath();ctx.moveTo(L,my);ctx.lineTo(w-R,my);ctx.stroke();ctx.restore();
      // 价格标签
      const sc=panelScale[id]||{lo:0,hi:1,T:14};
      let pv=sc.hi-(my-sc.T)/sc.ph*(sc.hi-sc.lo);
      ctx.setLineDash([]);ctx.font="bold 11px Consolas";
      const txt=Math.abs(pv)>=100?pv.toFixed(1):pv.toFixed(2);
      const tw=ctx.measureText(txt).width;
      ctx.fillStyle="#1971c2";ctx.fillRect(w-R-2,my-9,tw+12,18);
      ctx.fillStyle="#fff";ctx.textAlign="left";ctx.fillText(txt,w-R+4,my+4);
    }else ctx.restore();
    ctx.fillStyle="#555";ctx.font="bold 11px Consolas";ctx.textAlign="center";
    ctx.fillText((idx===n-1?"T+1预测":V.bars[idx].date),x,h-5);
    ctx.setTransform(1,0,0,1,0,0);
  }
  const b=idx===n-1?A.pred:V.bars[idx];
  let hv=idx===n-1?"[预测T+1]":
    `${b.date} 开${b.open.toFixed(2)} 高${b.high.toFixed(2)} 低${b.low.toFixed(2)} 收`;
  if(idx<n-1){
    const pc=idx>0?V.bars[idx-1].close:
      (V.off>0?A.bars[V.off-1].close:A.prevClose);
    const chg=((b.close/pc-1)*100).toFixed(2);
    hv+=`<b class="${b.close>=pc?'up':'dn'}">${b.close.toFixed(2)}(${chg>=0?"+":""}${chg}%)</b>`;
    if(b.vol!=null)hv+=` 量${fmtV(b.vol)}手`;
    const mode=document.getElementById("ind").value;
    const ai=V.off+idx;   // 全量数组中的下标
    if(mode==="MACD"&&A.ind.dif[ai]!=null&&A.ind.dea[ai]!=null)
      hv+=` | DIF:${A.ind.dif[ai].toFixed(3)} DEA:${A.ind.dea[ai].toFixed(3)} MACD:${A.ind.mh[ai]!=null?A.ind.mh[ai].toFixed(3):"-"}`;
    else if(mode==="KDJ")
      hv+=` | K:${A.ind.k[ai].toFixed(1)} D:${A.ind.d[ai].toFixed(1)} J:${A.ind.j[ai].toFixed(1)}`;
    else if(mode==="RSI")
      hv+=` | RSI6:${A.ind.r6[ai]!=null?A.ind.r6[ai].toFixed(1):"-"} RSI12:${A.ind.r12[ai]!=null?A.ind.r12[ai].toFixed(1):"-"}`;
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
        elif parsed.path == "/healthz":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"stock_web v2 listening on 127.0.0.1:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
