"""Single-file zero-build viewer (no CDN, no bundler, works offline)."""

from __future__ import annotations

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>shadow-memory · 影子记忆工作台</title>
<style>
:root{
  --bg:#0b0e14; --panel:#11161f; --panel-2:#151b26; --line:#1f2733;
  --fg:#d7dee9; --fg-dim:#8b98ab; --fg-faint:#5b6879;
  --accent:#3b82f6; --accent-soft:rgba(59,130,246,.14);
  --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; --violet:#a78bfa;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
}
html[data-theme="light"]{
  --bg:#f6f7f9; --panel:#ffffff; --panel-2:#f2f4f7; --line:#e3e7ee;
  --fg:#1c2430; --fg-dim:#5e6b7d; --fg-faint:#8d99a8;
  --accent:#2563eb; --accent-soft:rgba(37,99,235,.10);
  --ok:#16a34a; --warn:#d97706; --bad:#dc2626; --violet:#7c3aed;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);font-size:13px;line-height:1.55;
  -webkit-font-smoothing:antialiased}
button{font-family:inherit;font-size:12px;cursor:pointer}
input,select,textarea{font-family:inherit;font-size:12px;color:var(--fg);background:var(--panel-2);
  border:1px solid var(--line);border-radius:5px;padding:6px 8px;outline:none}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:#2a3441;border-radius:5px}
::-webkit-scrollbar-track{background:transparent}

.app{display:grid;grid-template-columns:236px minmax(0,1fr) 348px;height:100vh;overflow:hidden}

/* ---------- sidebar ---------- */
.sidebar{border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;min-height:0}
.brand{padding:14px 14px 12px;border-bottom:1px solid var(--line)}
.brand h1{margin:0;font-size:13px;letter-spacing:.02em;font-weight:600;display:flex;align-items:center;gap:7px}
.brand .logo{width:9px;height:9px;border-radius:2px;background:var(--accent);box-shadow:0 0 10px var(--accent)}
.brand .sub{margin-top:4px;font-size:11px;color:var(--fg-faint);font-family:var(--mono)}
.status-row{display:flex;align-items:center;gap:7px;padding:8px 14px;border-bottom:1px solid var(--line);
  font-size:11px;color:var(--fg-dim)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok);flex:none}
.dot.off{background:var(--fg-faint);box-shadow:none}
.sec-label{padding:10px 14px 6px;font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--fg-faint)}
.projects{flex:1;overflow-y:auto;padding:0 8px 8px;min-height:0}
.proj{padding:7px 8px;border-radius:5px;cursor:pointer;display:flex;justify-content:space-between;
  gap:8px;align-items:baseline;border:1px solid transparent}
.proj:hover{background:var(--panel-2)}
.proj.active{background:var(--accent-soft);border-color:rgba(59,130,246,.35)}
.proj .nm{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.proj .ct{font-family:var(--mono);font-size:10px;color:var(--fg-faint);flex:none}
.side-foot{padding:10px;border-top:1px solid var(--line)}
.btn{background:var(--panel-2);border:1px solid var(--line);color:var(--fg);border-radius:5px;
  padding:7px 10px;transition:.12s}
.btn:hover{border-color:#2f3a4a;background:#1a2130}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{filter:brightness(1.08)}
.btn.ghost{background:transparent}
.btn.danger{color:var(--bad);border-color:rgba(239,68,68,.35)}
.btn.block{width:100%}
.btn.sm{padding:4px 8px;font-size:11px}

/* ---------- main ---------- */
.main{display:flex;flex-direction:column;min-width:0;min-height:0;background:var(--bg)}
.topbar{display:flex;align-items:center;gap:10px;padding:0 14px;border-bottom:1px solid var(--line);
  background:var(--panel);height:44px;flex:none}
.tabs{display:flex;gap:2px;flex:1;min-width:0}
.tab{padding:6px 12px;border-radius:5px;background:transparent;border:1px solid transparent;color:var(--fg-dim)}
.tab:hover{color:var(--fg);background:var(--panel-2)}
.tab.active{color:var(--fg);background:var(--accent-soft);border-color:rgba(59,130,246,.3)}
.tab .n{font-family:var(--mono);font-size:10px;color:var(--fg-faint);margin-left:5px}
.icon-btn{width:28px;height:28px;display:grid;place-items:center;border-radius:5px;border:1px solid var(--line);
  background:var(--panel-2);color:var(--fg-dim);padding:0}
.icon-btn:hover{color:var(--fg)}
.panel{flex:1;overflow:auto;padding:14px;min-height:0}
.panel[hidden]{display:none}

.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.toolbar .grow{flex:1;min-width:160px}
.toolbar input.grow{width:100%}

table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;font-weight:500;color:var(--fg-faint);font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;padding:6px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;
  background:var(--bg);z-index:1}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr.row{cursor:pointer}
tr.row:hover td{background:var(--panel-2)}
tr.row.sel td{background:var(--accent-soft)}
.mono{font-family:var(--mono);font-size:11px}
.id{font-family:var(--mono);font-size:11px;color:var(--accent);white-space:nowrap}
.clip{max-width:640px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10px;border:1px solid var(--line);
  background:var(--panel-2);color:var(--fg-dim);white-space:nowrap}
.chip.k{color:var(--violet);border-color:rgba(167,139,250,.3)}
.chip.constraint{color:var(--bad);border-color:rgba(239,68,68,.3)}
.chip.decision{color:var(--accent);border-color:rgba(59,130,246,.32)}
.chip.preference{color:var(--ok);border-color:rgba(34,197,94,.3)}
.chip.handoff,.chip.todo{color:var(--warn);border-color:rgba(245,158,11,.3)}
.st{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--fg-dim);white-space:nowrap}
.st i{width:6px;height:6px;border-radius:50%;background:var(--fg-faint);display:inline-block}
.st.active i{background:var(--ok);box-shadow:0 0 7px var(--ok)}
.st.superseded i{background:var(--warn)}
.st.forgotten i{background:var(--bad)}
.st.expired i{background:var(--fg-faint)}
.files{display:flex;gap:4px;flex-wrap:wrap}
.file-chip{font-family:var(--mono);font-size:10px;padding:1px 6px;border-radius:4px;
  background:var(--panel-2);border:1px solid var(--line);color:var(--fg-dim);max-width:190px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* pulse strip */
.pulse{display:flex;gap:6px;align-items:center;overflow-x:auto;padding:8px 10px;border:1px solid var(--line);
  border-radius:6px;background:var(--panel);margin-bottom:12px}
.pulse .p{display:flex;align-items:center;gap:6px;padding:3px 9px;border-radius:12px;font-size:11px;
  border:1px solid var(--line);background:var(--panel-2);white-space:nowrap;cursor:pointer;flex:none}
.pulse .p.RUNNING{border-color:rgba(34,197,94,.4);color:var(--ok)}
.pulse .p.RECENT{border-color:rgba(245,158,11,.32);color:var(--fg-dim)}
.pulse .empty{color:var(--fg-faint);font-size:11px}
.pulse .lead{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--fg-faint);flex:none}

/* shadow preview */
.file-switch{display:flex;gap:4px;align-items:center;flex-wrap:wrap}
.file-switch .btn.active{background:var(--accent-soft);border-color:rgba(59,130,246,.4);color:var(--fg)}
.budget{margin:0 0 10px;padding:9px 11px;border:1px solid var(--line);border-radius:6px;background:var(--panel)}
.budget .top{display:flex;justify-content:space-between;font-size:11px;color:var(--fg-dim);margin-bottom:6px}
.budget .bar{height:4px;border-radius:3px;background:var(--panel-2);overflow:hidden}
.budget .fill{height:100%;background:var(--accent);width:0;transition:width .25s}
.budget .fill.warn{background:var(--warn)}
.budget .fill.bad{background:var(--bad)}
pre.src{margin:0;padding:12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);
  font-family:var(--mono);font-size:11.5px;line-height:1.65;white-space:pre-wrap;word-break:break-word;
  color:var(--fg);max-height:calc(100vh - 250px);overflow:auto}

/* agents tab */
.cards{display:grid;gap:10px}
.card{border:1px solid var(--line);border-radius:6px;background:var(--panel);padding:12px 14px}
.card h3{margin:0 0 8px;font-size:12px;font-weight:600;display:flex;align-items:center;gap:8px}
.agent-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 0;
  border-bottom:1px solid var(--line)}
.agent-row:last-child{border-bottom:none}
.agent-row .p{font-family:var(--mono);font-size:10.5px;color:var(--fg-faint);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;max-width:340px}
.cmd{display:flex;align-items:center;gap:8px;margin:6px 0}
.cmd code{flex:1;font-family:var(--mono);font-size:11.5px;background:var(--panel-2);border:1px solid var(--line);
  border-radius:5px;padding:7px 9px;overflow:auto;white-space:nowrap;color:var(--fg)}
.privacy{font-size:11.5px;color:var(--fg-dim);line-height:1.8}

/* inspector */
.inspector{border-left:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;
  min-height:0}
.insp-head{display:flex;align-items:center;justify-content:space-between;padding:11px 13px;
  border-bottom:1px solid var(--line);flex:none}
.insp-head h2{margin:0;font-size:12px;font-weight:600}
.insp-body{flex:1;overflow:auto;padding:13px;min-height:0}
.empty-hint{color:var(--fg-faint);font-size:12px;padding:26px 8px;text-align:center;line-height:1.9}
.field{margin-bottom:11px}
.field .lbl{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--fg-faint);margin-bottom:4px}
.field .val{font-size:12.5px;white-space:pre-wrap;word-break:break-word}
.field .val.mono{font-size:11px}
.chain{display:flex;flex-direction:column;gap:0;margin:6px 0 12px}
.chain .node{display:flex;gap:9px;align-items:flex-start;position:relative;padding-bottom:14px}
.chain .node:last-child{padding-bottom:0}
.chain .node:not(:last-child)::before{content:"";position:absolute;left:8px;top:16px;bottom:0;
  width:1px;background:var(--line)}
.chain .mark{width:17px;height:17px;border-radius:50%;border:1px solid var(--line);background:var(--panel-2);
  flex:none;display:grid;place-items:center;font-family:var(--mono);font-size:9px;color:var(--fg-faint);z-index:1}
.chain .node.current .mark{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.chain .node.old .txt{color:var(--fg-faint);text-decoration:line-through}
.chain .txt{font-size:11.5px;flex:1;min-width:0}
.chain .txt .meta{font-family:var(--mono);font-size:10px;color:var(--fg-faint);margin-top:2px}
.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}

/* modal */
.mask{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;place-items:center;z-index:40;
  backdrop-filter:blur(1.5px)}
.mask.show{display:grid}
.modal{width:min(520px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:16px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal h3{margin:0 0 12px;font-size:13px}
.modal label{display:block;font-size:11px;color:var(--fg-dim);margin:9px 0 4px}
.modal input,.modal select,.modal textarea{width:100%}
.modal textarea{min-height:92px;resize:vertical}
.modal .row{display:flex;gap:8px;margin-top:14px;justify-content:flex-end}

.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%) translateY(20px);opacity:0;
  background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 14px;font-size:12px;
  box-shadow:0 8px 30px rgba(0,0,0,.4);transition:.22s;pointer-events:none;z-index:60}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.err{border-color:rgba(239,68,68,.5);color:var(--bad)}
@media (max-width:1180px){.app{grid-template-columns:210px minmax(0,1fr) 300px}}
@media (max-width:920px){.app{grid-template-columns:200px minmax(0,1fr)}.inspector{display:none}}
</style>
</head>
<body>
<div class="app">
  <!-- ============ LEFT ============ -->
  <aside class="sidebar">
    <div class="brand">
      <h1><span class="logo"></span>shadow-memory</h1>
      <div class="sub" id="ver">v0.1.0 · 127.0.0.1</div>
    </div>
    <div class="status-row"><span class="dot off" id="liveDot"></span><span id="liveTxt">连接中…</span></div>
    <div class="sec-label">项目</div>
    <div class="projects" id="projects"></div>
    <div class="side-foot">
      <button class="btn primary block" id="btnSync">↻ 立即同步</button>
    </div>
  </aside>

  <!-- ============ MAIN ============ -->
  <main class="main">
    <div class="topbar">
      <div class="tabs">
        <button class="tab active" data-tab="facts">记忆账本<span class="n" id="nFacts">0</span></button>
        <button class="tab" data-tab="sessions">跨会话现场<span class="n" id="nSessions">0</span></button>
        <button class="tab" data-tab="shadow">影子文件</button>
        <button class="tab" data-tab="agents">接入与状态</button>
      </div>
      <button class="icon-btn" id="btnTheme" title="切换深浅色">◐</button>
    </div>

    <!-- Tab 1 -->
    <section class="panel" id="panel-facts">
      <div class="toolbar">
        <input class="grow" id="q" placeholder="搜索记忆（中英文均可，FTS5 全文检索）">
        <select id="fKind">
          <option value="">全部类型</option>
          <option value="constraint">constraint</option>
          <option value="decision">decision</option>
          <option value="preference">preference</option>
          <option value="handoff">handoff</option>
          <option value="fact">fact</option>
          <option value="note">note</option>
        </select>
        <select id="fStatus">
          <option value="">全部状态</option>
          <option value="active">active</option>
          <option value="superseded">superseded</option>
          <option value="forgotten">forgotten</option>
          <option value="expired">expired</option>
        </select>
        <button class="btn" id="btnWrite">+ 手动记一条</button>
      </div>
      <table>
        <thead><tr>
          <th style="width:70px">短ID</th><th style="width:104px">类型</th><th>正文</th>
          <th style="width:96px">来源Agent</th><th style="width:88px">更新时间</th><th style="width:78px">状态</th>
        </tr></thead>
        <tbody id="factsBody"></tbody>
      </table>
      <div class="empty-hint" id="factsEmpty" hidden>暂无记忆。左侧「立即同步」或点「+ 手动记一条」。</div>
    </section>

    <!-- Tab 2 -->
    <section class="panel" id="panel-sessions" hidden>
      <div class="pulse" id="pulse"><span class="lead">LIVE</span><span class="empty">加载中…</span></div>
      <table>
        <thead><tr>
          <th style="width:96px">Agent</th><th>标题</th><th style="width:250px">涉及文件</th><th style="width:86px">更新时间</th>
        </tr></thead>
        <tbody id="sessionsBody"></tbody>
      </table>
      <div class="empty-hint" id="sessionsEmpty" hidden>暂无跨 Agent 会话。确认已 `smem init --yes` 并同步过。</div>
    </section>

    <!-- Tab 3 -->
    <section class="panel" id="panel-shadow" hidden>
      <div class="toolbar">
        <div class="file-switch" id="fileSwitch">
          <button class="btn sm active" data-file="MEMORY.md">MEMORY.md</button>
          <button class="btn sm" data-file="decisions.md">decisions.md</button>
          <button class="btn sm" data-file="LIVE_PULSE.md">LIVE_PULSE.md</button>
          <button class="btn sm" data-file="inbox.processed.jsonl">inbox.processed.jsonl</button>
        </div>
        <div class="grow"></div>
        <button class="btn sm" id="btnCompile">立即重编译</button>
        <button class="btn sm" id="btnConsume">消费 inbox</button>
      </div>
      <div class="budget" id="budgetBox">
        <div class="top"><span id="budgetName">MEMORY.md</span><span class="mono" id="budgetVal">— / 450 tokens</span></div>
        <div class="bar"><div class="fill" id="budgetFill"></div></div>
      </div>
      <pre class="src" id="shadowSrc">加载中…</pre>
    </section>

    <!-- Tab 4 -->
    <section class="panel" id="panel-agents" hidden>
      <div class="cards">
        <div class="card">
          <h3>本机 Agent 接入状态</h3>
          <div id="agentRows"></div>
        </div>
        <div class="card">
          <h3>命令</h3>
          <div class="cmd"><code>smem init</code><button class="btn sm" data-copy="smem init">复制</button></div>
          <div class="cmd"><code>smem init --yes</code><button class="btn sm" data-copy="smem init --yes">复制</button></div>
          <div class="cmd"><code>smem sync --all --inject-agents-md</code><button class="btn sm" data-copy="smem sync --all --inject-agents-md">复制</button></div>
        </div>
        <div class="card">
          <h3>隐私与安全</h3>
          <div class="privacy">
            · 服务仅监听 <b>127.0.0.1</b>，不对外网开放，无遥测、无外链。<br>
            · 所有写入记忆与归档流水<b>先经脱敏</b>：`sk-`、`ghp_`、`AKIA`、Bearer、私钥块、手机号、邮箱一律拦截。<br>
            · 数据仅存于本机 <span class="mono" id="dbPath">~/.shadow-memory/memory.sqlite</span>。<br>
            · 卸载：`smem uninstall --yes`（先备份再移除受控块，不留残迹）。
          </div>
        </div>
      </div>
    </section>
  </main>

  <!-- ============ RIGHT ============ -->
  <aside class="inspector">
    <div class="insp-head">
      <h2 id="inspTitle">检查器</h2>
      <button class="icon-btn" id="btnCloseInsp" title="清空">✕</button>
    </div>
    <div class="insp-body" id="inspBody">
      <div class="empty-hint">点击左侧记忆行查看完整正文与<b>版本演进链</b>；<br>点击会话行查看摘要、末轮结论与涉及文件。</div>
    </div>
  </aside>
</div>

<!-- modal -->
<div class="mask" id="mask">
  <div class="modal">
    <h3>记一条记忆</h3>
    <label>正文</label>
    <textarea id="mContent" placeholder="一句话结论，≤200 字为宜"></textarea>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div><label>类型</label>
        <select id="mKind">
          <option value="decision">decision</option>
          <option value="constraint">constraint</option>
          <option value="preference">preference</option>
          <option value="handoff">handoff</option>
          <option value="fact">fact</option>
        </select>
      </div>
      <div><label>取代（可选，#短ID）</label><input id="mReplace" placeholder="#63bd40"></div>
    </div>
    <label>依据（可选）</label>
    <input id="mEvidence" placeholder="文件路径 / 命令结果 / 用户原话">
    <div class="row">
      <button class="btn ghost" id="mCancel">取消</button>
      <button class="btn primary" id="mSave">写入并重编译</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
"use strict";
const S = { projectId:"", tab:"facts", facts:[], sessions:[], projects:[], shadowFile:"MEMORY.md", selectedFact:null };

const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => { const n=document.createElement(tag); if(cls)n.className=cls; if(txt!==undefined)n.textContent=txt; return n; };
const esc = (s) => String(s==null?"":s).replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:{"Content-Type":"application/json"}}, opts||{}));
  const t = await r.text();
  let d; try{ d = t? JSON.parse(t):{}; }catch(e){ d = {ok:false, error:t}; }
  if(!r.ok || d.ok===false){ throw new Error(d.error || ("HTTP "+r.status)); }
  return d;
}
function toast(msg, isErr){
  const t=$("toast"); t.textContent=msg; t.className="toast show"+(isErr?" err":"");
  clearTimeout(t._h); t._h=setTimeout(()=>{t.className="toast"+(isErr?" err":"");},2200);
}
const ago = (s)=>{ s=Math.round(s||0); if(s<60)return "刚刚"; if(s<3600)return Math.floor(s/60)+"m"; if(s<86400)return Math.floor(s/3600)+"h"; return Math.floor(s/86400)+"d"; };
const qs = () => S.projectId ? ("?project_id="+encodeURIComponent(S.projectId)) : "";

/* ---------- sidebar ---------- */
async function loadStatus(){
  try{
    const d = await api("/api/status");
    $("ver").textContent = "v"+d.version+" · "+d.host+":"+d.port;
    $("liveDot").className = "dot";
    $("liveTxt").textContent = "运行中 · "+(d.counts.projects)+" 项目 / "+(d.counts.facts_active)+" 记忆";
    if(d.data && d.data.db) $("dbPath").textContent = d.data.db;
    S.projectsRaw = d.counts;
  }catch(e){ $("liveDot").className="dot off"; $("liveTxt").textContent="离线 · "+e.message; }
}
async function loadProjects(){
  const d = await api("/api/projects");
  S.projects = d.projects || [];
  const box = $("projects"); box.innerHTML="";
  const all = el("div","proj"+(S.projectId?"":" active"));
  all.appendChild(el("div","nm")); all.firstChild.textContent="全部项目";
  all.appendChild(el("div","ct", String(S.projects.reduce((a,p)=>a+(p.fact_count||0),0))));
  all.onclick = ()=>{ S.projectId=""; loadProjects(); refreshAll(); };
  box.appendChild(all);
  for(const p of S.projects){
    const row = el("div","proj"+(S.projectId===p.project_id?" active":""));
    const nm = el("div","nm", p.name||p.root); nm.title = p.root;
    const ct = el("div","ct", (p.fact_count||0)+"·"+(p.session_count||0));
    row.append(nm,ct);
    row.onclick = ()=>{ S.projectId=p.project_id; loadProjects(); refreshAll(); };
    box.appendChild(row);
  }
}

/* ---------- facts ---------- */
function kindChip(k){ return '<span class="chip '+(esc(k))+'">'+esc(k)+'</span>'; }
function stateDot(st){
  const m={"active":"运行","superseded":"已取代","forgotten":"已废弃","expired":"已过期"};
  return '<span class="st '+esc(st)+'"><i></i>'+esc(m[st]||st)+'</span>';
}
async function loadFacts(){
  const p = new URLSearchParams();
  if(S.projectId) p.set("project_id", S.projectId);
  if($("q").value.trim()) p.set("q", $("q").value.trim());
  if($("fKind").value) p.set("kind", $("fKind").value);
  if($("fStatus").value) p.set("status", $("fStatus").value);
  p.set("limit","300");
  const d = await api("/api/facts?"+p.toString());
  S.facts = d.facts||[];
  $("nFacts").textContent = S.facts.length;
  const body=$("factsBody"); body.innerHTML="";
  for(const f of S.facts){
    const tr = el("tr","row"+(S.selectedFact===f.fact_id?" sel":""));
    tr.innerHTML =
      '<td class="id">#'+esc(f.fact_id.slice(0,6))+'</td>'+
      '<td>'+kindChip(f.kind)+'</td>'+
      '<td><div class="clip" title="'+esc(f.content)+'">'+esc(f.content)+'</div></td>'+
      '<td class="mono">'+esc(f.source_agent||"—")+'</td>'+
      '<td class="mono">'+esc((f.updated_at||"").slice(5,16).replace("T"," "))+'</td>'+
      '<td>'+stateDot(f.status)+'</td>';
    tr.onclick = ()=>{ S.selectedFact=f.fact_id; showFact(f.fact_id); markRow(tr); };
    body.appendChild(tr);
  }
  $("factsEmpty").hidden = S.facts.length>0;
}
function markRow(tr){ document.querySelectorAll("#factsBody tr.sel").forEach(n=>n.classList.remove("sel")); tr.classList.add("sel"); }

/* ---------- sessions ---------- */
async function loadSessions(){
  const d = await api("/api/sessions"+qs());
  S.sessions = d.sessions||[];
  $("nSessions").textContent = S.sessions.length;
  const pulse=$("pulse"); pulse.innerHTML='<span class="lead">LIVE</span>';
  if(!d.pulse.length) pulse.appendChild(el("span","empty","30 分钟内无活跃会话"));
  for(const p of d.pulse){
    const b = el("span","p "+p.state, "["+p.state+"] "+p.agent+" · "+(p.title||"未命名")+" · "+ago(p.age_seconds)+"前");
    b.onclick = ()=>showSession(p.session_key);
    pulse.appendChild(b);
  }
  const body=$("sessionsBody"); body.innerHTML="";
  for(const s of S.sessions){
    const tr=el("tr","row");
    const files=(s.touched_files||[]).slice(0,3).map(f=>'<span class="file-chip" title="'+esc(f)+'">'+esc(f.split(/[\\/]/).pop())+'</span>').join("")
      + ((s.touched_files||[]).length>3? '<span class="file-chip">+'+(s.touched_files.length-3)+'</span>':'');
    tr.innerHTML =
      '<td class="mono">'+esc(s.agent)+'</td>'+
      '<td><div class="clip" title="'+esc(s.clean_title)+'">'+esc(s.clean_title||"(未命名)")+'</div>'+
        '<div class="mono" style="color:var(--fg-faint);font-size:10px">'+esc(s.session_id)+'</div></td>'+
      '<td><div class="files">'+(files||'<span style="color:var(--fg-faint)">—</span>')+'</div></td>'+
      '<td class="mono">'+ago(s.age_seconds)+'前</td>';
    tr.onclick = ()=>showSession(s.session_key);
    body.appendChild(tr);
  }
  $("sessionsEmpty").hidden = S.sessions.length>0;
}

/* ---------- shadow ---------- */
async function loadShadow(){
  const p = new URLSearchParams();
  if(S.projectId) p.set("project_id", S.projectId);
  p.set("file", S.shadowFile);
  try{
    const d = await api("/api/shadow?"+p.toString());
    $("shadowSrc").textContent = d.exists ? d.content : "（尚未生成：点「立即重编译」）";
    const budget = 450;
    $("budgetName").textContent = S.shadowFile;
    $("budgetVal").textContent = (d.est_tokens||0)+" / "+budget+" tokens";
    const pct = Math.min(100, Math.round((d.est_tokens||0)/budget*100));
    const fill=$("budgetFill"); fill.style.width=pct+"%";
    fill.className = "fill"+(pct>100?" bad":(pct>85?" warn":""));
  }catch(e){ $("shadowSrc").textContent = "读取失败："+e.message; }
}

/* ---------- agents ---------- */
async function loadAgents(){
  const d = await api("/api/agents");
  const box=$("agentRows"); box.innerHTML="";
  for(const a of d.agents){
    const row = el("div","agent-row");
    const left = el("div");
    left.innerHTML = '<div style="font-size:12px">'+esc(a.label)+'</div><div class="p">'+esc(a.path)+'</div>';
    const right = el("div");
    const st = a.configured ? '<span class="st active"><i></i>已接入</span>'
      : (a.installed ? '<span class="st superseded"><i></i>已安装·未接入</span>'
      : '<span class="st"><i></i>未安装</span>');
    right.innerHTML = st + ' <span class="mono" style="color:var(--fg-faint);margin-left:8px">'+a.sessions_count+' 会话</span>';
    row.append(left,right); box.appendChild(row);
  }
}

/* ---------- inspector ---------- */
function clearInsp(){
  $("inspTitle").textContent="检查器";
  $("inspBody").innerHTML='<div class="empty-hint">点击左侧记忆行查看完整正文与<b>版本演进链</b>；<br>点击会话行查看摘要、末轮结论与涉及文件。</div>';
  S.selectedFact=null;
}
async function showFact(fid){
  $("inspTitle").textContent="记忆详情";
  $("inspBody").innerHTML='<div class="empty-hint">加载中…</div>';
  try{
    const d = await api("/api/facts/"+encodeURIComponent(fid)+"/chain");
    const f = d.fact;
    const body=$("inspBody"); body.innerHTML="";
    const chainBox = el("div","chain");
    d.chain.forEach((c,i)=>{
      const isCur = c.fact_id===f.fact_id;
      const node = el("div","node"+(isCur?" current":(i<d.chain.length-1?" old":"")));
      node.innerHTML = '<div class="mark">'+(i+1)+'</div><div class="txt">'+
        '<div>'+esc(c.content)+'</div>'+
        '<div class="meta">#'+esc(c.fact_id.slice(0,6))+' · '+esc(c.kind)+' · '+esc((c.updated_at||"").slice(0,16).replace("T"," "))+' · '+esc(c.status)+'</div></div>';
      chainBox.appendChild(node);
    });
    if(d.chain.length>1){
      const h = el("div","field"); h.innerHTML='<div class="lbl">版本演进链（旧 → 新，共 '+d.chain.length+' 版）</div>';
      body.appendChild(h); body.appendChild(chainBox);
    }
    addField(body,"正文",f.content);
    addField(body,"短ID / 全ID","#"+f.fact_id.slice(0,6)+"  ·  "+f.fact_id,true);
    addField(body,"类型 / 状态",f.kind+" / "+f.status);
    if(f.evidence) addField(body,"依据",f.evidence);
    addField(body,"来源",(f.source_agent||"—")+(f.source_session?(" / "+f.source_session):""));
    addField(body,"时间","创建 "+f.created_at+"\n更新 "+f.updated_at,true);
    if(f.supersedes_id) addField(body,"取代","#"+f.supersedes_id.slice(0,6)+"  ("+f.supersedes_id+")",true);
    if(f.forget_reason) addField(body,"废弃原因",f.forget_reason);

    const acts = el("div","actions");
    const bOld = el("button","btn sm","改口取代此条");
    bOld.onclick = ()=>{ openModal(f.fact_id); };
    const bForget = el("button","btn sm danger", f.status==="forgotten"?"恢复":"废弃");
    bForget.onclick = ()=>toggleForget(f);
    acts.append(bOld,bForget);
    body.appendChild(acts);
  }catch(e){ $("inspBody").innerHTML='<div class="empty-hint">'+esc(e.message)+'</div>'; }
}
function addField(root,lbl,val,mono){
  const d=el("div","field");
  d.innerHTML='<div class="lbl">'+esc(lbl)+'</div><div class="val'+(mono?" mono":"")+'">'+esc(val)+'</div>';
  root.appendChild(d);
}
async function toggleForget(f){
  try{
    if(f.status==="forgotten" || f.status==="expired"){
      await api("/api/facts/"+encodeURIComponent(f.fact_id)+"/restore",{method:"POST",body:"{}"});
      toast("已恢复");
    }else{
      await api("/api/facts/"+encodeURIComponent(f.fact_id)+"/forget",{method:"POST",body:JSON.stringify({reason:"web"})});
      toast("已废弃");
    }
    await loadFacts(); await showFact(f.fact_id);
  }catch(e){ toast(e.message,true); }
}
async function showSession(key){
  const s = S.sessions.find(x=>x.session_key===key);
  if(!s){ toast("会话不在当前列表",true); return; }
  $("inspTitle").textContent="会话详情";
  const body=$("inspBody"); body.innerHTML="";
  addField(body,"标题",s.clean_title||"(未命名)");
  addField(body,"Agent / Session",s.agent+"  ·  "+s.session_id,true);
  addField(body,"项目路径",s.cwd||"—",true);
  addField(body,"更新时间",(s.updated_at||"")+"（"+ago(s.age_seconds)+"前）",true);
  addField(body,"摘要",s.summary||"（无）");
  addField(body,"末轮结论",s.outcome||"（无）");
  addField(body,"未完事项",s.next_steps||"（无）");
  const box=el("div","field");
  box.innerHTML='<div class="lbl">涉及文件 ('+(s.touched_files||[]).length+')</div>';
  const fl=el("div","files");
  (s.touched_files||[]).forEach(p=>fl.appendChild(el("span","file-chip",p)));
  if(!(s.touched_files||[]).length) fl.appendChild(el("span","mono",'（无）'));
  box.appendChild(fl); body.appendChild(box);
}

/* ---------- modal ---------- */
function openModal(replace){
  $("mask").classList.add("show");
  $("mContent").value=""; $("mEvidence").value="";
  $("mReplace").value = replace? ("#"+replace.slice(0,6)) : "";
  setTimeout(()=>$("mContent").focus(),30);
}
function closeModal(){ $("mask").classList.remove("show"); }
async function saveModal(){
  const content=$("mContent").value.trim();
  if(!content){ toast("正文不能为空",true); return; }
  try{
    const d = await api("/api/facts",{method:"POST",body:JSON.stringify({
      content, kind:$("mKind").value, evidence:$("mEvidence").value.trim(),
      project_id:S.projectId||undefined,
      supersedes_id:$("mReplace").value.trim()||undefined
    })});
    closeModal();
    toast(d.status==="merged"?"已合并进相近记忆":"已写入 #"+(d.short_id||""));
    await refreshAll();
  }catch(e){ toast(e.message,true); }
}

/* ---------- wiring ---------- */
let refreshing=false;
async function refreshAll(){
  if(refreshing) return; refreshing=true;
  try{
    if(S.tab==="facts") await loadFacts();
    if(S.tab==="sessions") await loadSessions();
    if(S.tab==="shadow") await loadShadow();
    if(S.tab==="agents") await loadAgents();
  }catch(e){ toast(e.message,true); }
  finally{ refreshing=false; }
}
function switchTab(name){
  S.tab=name;
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active", t.dataset.tab===name));
  ["facts","sessions","shadow","agents"].forEach(k=>{ $("panel-"+k).hidden = (k!==name); });
  if(name==="sessions") loadSessions().catch(e=>toast(e.message,true));
  if(name==="shadow") loadShadow().catch(e=>toast(e.message,true));
  if(name==="agents") loadAgents().catch(e=>toast(e.message,true));
  if(name==="facts") loadFacts().catch(e=>toast(e.message,true));
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>switchTab(t.dataset.tab));

let qTimer=null;
$("q").oninput = ()=>{ clearTimeout(qTimer); qTimer=setTimeout(()=>loadFacts().catch(e=>toast(e.message,true)),220); };
$("fKind").onchange = ()=>loadFacts().catch(e=>toast(e.message,true));
$("fStatus").onchange = ()=>loadFacts().catch(e=>toast(e.message,true));
$("btnWrite").onclick = ()=>openModal(null);
$("mCancel").onclick = closeModal;
$("mSave").onclick = saveModal;
$("mask").onclick = (e)=>{ if(e.target===$("mask")) closeModal(); };
$("btnCloseInsp").onclick = clearInsp;

document.querySelectorAll(".file-switch .btn").forEach(b=>b.onclick=()=>{
  S.shadowFile=b.dataset.file;
  document.querySelectorAll(".file-switch .btn").forEach(x=>x.classList.toggle("active",x===b));
  loadShadow().catch(e=>toast(e.message,true));
});
$("btnCompile").onclick = async ()=>{
  try{ await api("/api/compile",{method:"POST",body:JSON.stringify({project_id:S.projectId||undefined})});
    toast("已重编译"); await loadShadow(); }
  catch(e){ toast(e.message,true); }
};
$("btnConsume").onclick = async ()=>{
  try{ const d=await api("/api/inbox/consume",{method:"POST",body:JSON.stringify({project_id:S.projectId||undefined})});
    toast("消费 inbox "+d.inbox.consumed+" 条"); await loadShadow(); }
  catch(e){ toast(e.message,true); }
};
$("btnSync").onclick = async ()=>{
  const b=$("btnSync"); b.disabled=true; b.textContent="同步中…";
  try{ const d=await api("/api/sync",{method:"POST",body:JSON.stringify({project_id:S.projectId||undefined})});
    toast("同步完成："+((d.projects||[]).length)+" 项目，采集 "+((d.collected&&d.collected.total)||0)+" 会话");
    await loadStatus(); await loadProjects(); await refreshAll(); }
  catch(e){ toast(e.message,true); }
  finally{ b.disabled=false; b.textContent="↻ 立即同步"; }
};
document.querySelectorAll("[data-copy]").forEach(b=>b.onclick=async ()=>{
  try{ await navigator.clipboard.writeText(b.dataset.copy); toast("已复制"); }
  catch(e){ toast("复制失败，请手动选择",true); }
});
$("btnTheme").onclick = ()=>{
  const h=document.documentElement;
  h.dataset.theme = h.dataset.theme==="dark"?"light":"dark";
  try{ localStorage.setItem("smem-theme",h.dataset.theme); }catch(e){}
};

/* ---------- boot ---------- */
(function boot(){
  try{ const t=localStorage.getItem("smem-theme"); if(t) document.documentElement.dataset.theme=t; }catch(e){}
  loadStatus().catch(()=>{});
  loadProjects().then(()=>refreshAll()).catch(e=>toast(e.message,true));
  setInterval(()=>{ loadStatus().catch(()=>{}); }, 15000);
  setInterval(()=>{ if(S.tab==="sessions") loadSessions().catch(()=>{}); }, 20000);
})();
</script>
</body>
</html>
"""