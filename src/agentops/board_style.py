"""看板页面的样式和一小段脚本。纯字符串，不含任何数据。"""

from __future__ import annotations

FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='8' fill='%230f766e'/%3E"
    "%3Cpath d='M9 17l5 5 9-11' fill='none' stroke='white' stroke-width='3.2' "
    "stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"
)

CSS = """
:root{color-scheme:light dark;
--bg:#f4f5f7;--surface:#fff;--surface-2:#f8f9fb;--border:#e4e7ec;--text:#1b1f24;--muted:#667085;
--accent:#0f766e;--accent-fg:#fff;--accent-soft:#e3f3f0;
--good-fg:#17713a;--good-bg:#e2f5e8;--warn-fg:#96580a;--warn-bg:#fcefd0;
--bad-fg:#b3261e;--bad-bg:#fde7e5;--info-fg:#1e4fcf;--info-bg:#e4ebff;
--mute-fg:#5b6472;--mute-bg:#eef0f3;--shadow:0 1px 2px rgba(16,24,40,.05)}
@media (prefers-color-scheme:dark){:root{
--bg:#0e1116;--surface:#161a21;--surface-2:#1b2029;--border:#272d38;--text:#e6e9ee;--muted:#98a2b3;
--accent:#2dd4bf;--accent-fg:#06201d;--accent-soft:rgba(45,212,191,.12);
--good-fg:#5fd38a;--good-bg:rgba(95,211,138,.13);--warn-fg:#f2b84b;--warn-bg:rgba(242,184,75,.14);
--bad-fg:#ff8a80;--bad-bg:rgba(255,138,128,.14);--info-fg:#8fb1ff;--info-bg:rgba(143,177,255,.14);
--mute-fg:#a3acba;--mute-bg:rgba(163,172,186,.12);--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;
font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",
"Microsoft YaHei",sans-serif}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.dim{color:var(--muted)}
.nowrap{white-space:nowrap}
.wrap{overflow-wrap:anywhere}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;
gap:16px;padding:10px 24px;background:var(--surface);border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:center;gap:10px}
.logo{width:26px;height:26px;border-radius:7px;display:block}
h1{font-size:15px;font-weight:650;margin:0}
.meta{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:4px 16px;color:var(--muted);
font-size:12px}
.page{max-width:1680px;margin:0 auto;padding:18px 24px 48px}
.msg{margin:0 0 14px;padding:8px 12px;border:1px solid var(--accent);border-radius:10px;
background:var(--accent-soft)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;
margin-bottom:16px}
.tile{display:flex;flex-direction:column;gap:1px;padding:12px 14px;border:1px solid var(--border);
border-radius:12px;background:var(--surface);box-shadow:var(--shadow);color:inherit;
text-decoration:none}
.tile:hover{border-color:var(--accent)}
.tile-label,.tile-sub{color:var(--muted);font-size:12px}
.tile-value{font-size:24px;font-weight:650;line-height:1.25;font-variant-numeric:tabular-nums}
.tile.warn{border-color:var(--warn-fg);background:var(--warn-bg)}
.tile.warn .tile-value{color:var(--warn-fg)}
.panel{min-width:0;border:1px solid var(--border);border-radius:12px;background:var(--surface);
box-shadow:var(--shadow);scroll-margin-top:60px;overflow:hidden}
.panel.wide{margin-bottom:14px}
.panel.hot{border-color:var(--warn-fg)}
.panel>header{display:flex;align-items:center;gap:8px;padding:10px 14px;
border-bottom:1px solid var(--border)}
.panel.hot>header{background:var(--warn-bg)}
h2{font-size:13px;font-weight:650;margin:0}
.count{padding:0 7px;border-radius:999px;background:var(--mute-bg);color:var(--mute-fg);
font-size:12px;font-variant-numeric:tabular-nums}
.grid{columns:3 520px;column-gap:14px}
.grid>.panel{break-inside:avoid;margin-bottom:14px}
.body{overflow:auto;max-height:420px}
.panel.wide>.body{max-height:none}
.empty,.err{margin:0;padding:18px 14px;color:var(--muted)}
.err{color:var(--bad-fg)}
table{width:100%;border-collapse:collapse}
th{position:sticky;top:0;z-index:1;padding:7px 10px;border-bottom:1px solid var(--border);
background:var(--surface-2);color:var(--muted);font-size:12px;font-weight:500;text-align:left;
white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top;
overflow-wrap:break-word}
th:first-child,td:first-child{padding-left:14px}
th:last-child,td:last-child{padding-right:14px}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--surface-2)}
td.ref{font-weight:600;white-space:nowrap}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
td.clip{width:100%;max-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t-good{color:var(--good-fg);font-weight:600}
.t-warn{color:var(--warn-fg);font-weight:600}
.t-bad{color:var(--bad-fg);font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--mute-bg);
color:var(--mute-fg);font-size:12px;font-weight:500;white-space:nowrap}
.pill b{margin-left:4px;font-variant-numeric:tabular-nums}
.pill.good{background:var(--good-bg);color:var(--good-fg)}
.pill.warn{background:var(--warn-bg);color:var(--warn-fg)}
.pill.bad{background:var(--bad-bg);color:var(--bad-fg)}
.pill.info{background:var(--info-bg);color:var(--info-fg)}
.pill.accent{background:var(--accent-soft);color:var(--accent)}
.meter{display:inline-block;width:84px;height:6px;margin-right:8px;border-radius:999px;
background:var(--mute-bg);overflow:hidden;vertical-align:middle}
.meter>i{display:block;height:100%;border-radius:999px;background:var(--accent)}
.meter.hot>i{background:var(--bad-fg)}
td.text{min-width:11em}
td.actions{white-space:nowrap}
.act{display:flex;justify-content:flex-end;align-items:flex-start;gap:6px}
.act form{margin:0}
button{padding:4px 10px;border:1px solid var(--border);border-radius:7px;background:var(--surface);
color:var(--text);font:inherit;font-size:12px;font-weight:550;cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{border-color:var(--accent);background:var(--accent);color:var(--accent-fg)}
button.primary:hover{filter:brightness(1.08)}
button.danger{border-color:var(--bad-fg);background:var(--bad-bg);color:var(--bad-fg)}
details.confirm{text-align:right}
details.confirm form{margin-top:4px}
details.confirm>summary{display:inline-block;padding:4px 6px;border-radius:7px;color:var(--muted);
font-size:12px;cursor:pointer;list-style:none}
details.confirm>summary::-webkit-details-marker{display:none}
details.confirm[open]>summary{color:var(--bad-fg)}
.chips{display:flex;flex-wrap:wrap;gap:6px;padding:10px 14px}
.panel .chips+table,.panel .chips+.empty{border-top:1px solid var(--border)}
.chip{display:inline-flex;align-items:baseline;gap:6px;padding:2px 9px;border-radius:999px;
background:var(--mute-bg);color:var(--mute-fg);font-size:12px}
.chip b{color:var(--text);font-variant-numeric:tabular-nums}
details.fold>summary{padding:9px 14px;border-top:1px solid var(--border);color:var(--muted);
cursor:pointer;user-select:none}
details.fold>summary:hover{color:var(--text)}
details.fold[open]>summary{border-bottom:1px solid var(--border)}
.kpi p{margin:0;padding:8px 14px;border-top:1px solid var(--border)}
.kpi>:first-child{border-top:0}
.kpi .chips{border-top:1px solid var(--border)}
@media (max-width:720px){.top{padding:10px 14px}.page{padding:14px 14px 40px}}
"""

# 记住哪些折叠区是打开的：页面每隔一会儿整页刷新，不记的话刚展开就又合上。
SCRIPT = """
(function(){
  var KEY="agentops-board-open";
  function read(){try{return JSON.parse(sessionStorage.getItem(KEY)||"[]")}catch(e){return []}}
  function write(ids){try{sessionStorage.setItem(KEY,JSON.stringify(ids))}catch(e){}}
  var open=read();
  document.querySelectorAll("details[data-keep]").forEach(function(d){
    if(open.indexOf(d.id)>=0){d.open=true}
    d.addEventListener("toggle",function(){
      var ids=read().filter(function(id){return id!==d.id});
      write(d.open?ids.concat(d.id):ids);
    });
  });
})();
"""
