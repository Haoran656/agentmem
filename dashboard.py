# -*- coding: utf-8 -*-
"""dashboard.py — 从 agentmem.db 生成自包含静态 HTML 工作台

用法: python dashboard.py [--out 路径]   默认输出 <agentmem>/dashboard.html
纯内联 CSS/JS + 内嵌数据 JSON，离线可开。
"""
import argparse
import json
import time
from pathlib import Path

import agentmem_db
import agentmem_config as _cfg

AM_DIR = Path(__file__).resolve().parent
AGENTS = ["workbuddy", "codex", "claude", "kilo", "opencode", "dsh"]
AGENT_LABEL = {"workbuddy": "WorkBuddy", "codex": "Codex", "claude": "Claude Code",
               "kilo": "Kilo", "opencode": "OpenCode", "dsh": "DS Harness"}
AGENT_COLOR = {"workbuddy": "#e5484d", "codex": "#0090ff", "claude": "#d9734a",
               "kilo": "#8e4ec6", "opencode": "#2b9a6f", "dsh": "#f0a72e"}


def build_data(con):
    d = {}
    d["generated"] = time.strftime("%Y-%m-%d %H:%M")
    d["totals"] = dict(con.execute(
        "SELECT count(*) sessions, (SELECT count(*) FROM events) events, "
        "(SELECT count(*) FROM usage) usage_rows FROM sessions").fetchone())

    # 近 45 天 按天×端 事件数
    days = []
    rows = con.execute(f"""SELECT date(ts/1000,'unixepoch','{_cfg.tz()}') d, agent, count(*) c
        FROM events GROUP BY 1,2""").fetchall()
    idx = {}
    all_agents = set()
    for r in rows:
        idx.setdefault(r["d"], {})[r["agent"]] = r["c"]
        all_agents.add(r["agent"])
    recent = sorted(idx)[-45:]
    for dd in recent:
        days.append({"d": dd, **{a: idx[dd].get(a, 0) for a in all_agents}})
    d["days"] = days

    # 按端汇总
    per = {}
    for r in con.execute("""SELECT agent, count(DISTINCT session_id) sess, count(*) ev
        FROM events GROUP BY agent"""):
        per[r["agent"]] = dict(sess=r["sess"], ev=r["ev"])
    for r in con.execute("SELECT agent, sum(cost_usd) usd, sum(credit) credit FROM sessions GROUP BY agent"):
        per.setdefault(r["agent"], {})
    for r in con.execute("SELECT agent, sum(total_tokens) tok FROM usage GROUP BY agent"):
        per[r["agent"]]["tok"] = r["tok"] or 0
    for r in con.execute("SELECT agent, sum(cost_usd) usd, sum(credit) credit FROM sessions GROUP BY agent"):
        per[r["agent"]]["usd"] = r["usd"] or 0; per[r["agent"]]["credit"] = r["credit"] or 0
    d["per_agent"] = per

    # 工具 TOP15
    d["tools"] = [tuple(r) for r in con.execute("""
        SELECT agent, tool_name, count(*) c FROM events WHERE kind='tool_call' AND tool_name IS NOT NULL
        GROUP BY 1,2 ORDER BY c DESC LIMIT 15""")]

    # 模型足迹
    d["models"] = [tuple(r) for r in con.execute("""
        SELECT agent, model, sum(total_tokens) t FROM usage WHERE model!='' AND model IS NOT NULL
        GROUP BY 1,2 ORDER BY t DESC LIMIT 20""")]

    # 会话表（全部，页面里筛/搜）
    d["sessions"] = [[r["agent"], r["session_id"], r["project_name"] or "", r["title"] or "",
                      r["started_at"] or 0, r["ended_at"] or 0, r["n_events"],
                      r["total"] or 0, r["models"] or "[]"] for r in con.execute("""
        SELECT s.agent, s.session_id, s.project_name, s.title, s.started_at, s.ended_at, s.n_events,
               (SELECT sum(u.total_tokens) FROM usage u WHERE u.agent=s.agent AND u.session_id=s.session_id) total,
               s.models
        FROM sessions s ORDER BY s.ended_at DESC""")]
    return d


HTML_TMPL = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Agent Memory L2 工作台</title>
<style>
:root{--bg:#f7f7f8;--card:#fff;--ink:#1a1d21;--mut:#6b7280;--line:#e5e7eb;--accent:#2563eb}
*{box-sizing:border-box}body{margin:0;font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);font-size:14px}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mut);margin-bottom:22px}
.grid{display:grid;gap:16px}.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.kpi .num{font-size:24px;font-weight:700}.kpi .lab{color:var(--mut);font-size:12px;margin-top:2px}
h2{font-size:16px;margin:0 0 12px}
.row2{grid-template-columns:1fr 1fr}@media(max-width:900px){.row2{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--mut);font-weight:600;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--card);cursor:pointer}
td{padding:5px 8px;border-bottom:1px solid #f0f1f3}
tr:hover td{background:#f4f7ff}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:1px}
.tag{display:inline-block;font-size:11px;color:var(--mut);background:#f0f1f3;border-radius:4px;padding:1px 6px;margin-left:6px}
input[type=text],select{border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px;background:#fff}
.ctl{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.bar{display:flex;height:26px;border-radius:6px;overflow:hidden;background:#f0f1f3;margin:4px 0 14px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin-bottom:6px}
.scroll{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:8px}
#chart{width:100%;height:260px}
.small{font-size:11px;color:var(--mut)}
.mono{font-family:Consolas,monospace;font-size:11.5px}
</style></head><body><div class="wrap">
<h1>跨 Agent 共享记忆 · L2 工作台</h1>
<div class="sub">数据源 agentmem.db（脱敏后）· 生成于 <span id="gen"></span></div>
<div class="kpi" id="kpi"></div>
<div class="card" style="margin-bottom:16px"><h2>近 45 天各端事件量</h2>
<div class="legend" id="legend"></div><svg id="chart" viewBox="0 0 1120 260" preserveAspectRatio="none"></svg></div>
<div class="grid row2" style="margin-bottom:16px">
<div class="card"><h2>工具调用 TOP15</h2><div id="tools"></div></div>
<div class="card"><h2>模型足迹（按转录 token）</h2><div id="models"></div></div>
</div>
<div class="card"><h2>会话浏览器 <span class="small">（点表头排序 · 支持搜索/筛选，即 L3 检索的本地雏形）</span></h2>
<div class="ctl"><input type="text" id="q" placeholder="搜索标题/项目/会话id…" style="flex:1;min-width:200px">
<select id="fa"><option value="">全部端</option></select>
<input type="text" id="fp" placeholder="项目过滤…" style="width:160px">
<span class="small" id="cnt"></span></div>
<div class="scroll"><table><thead><tr>
<th data-k="end">最后活动</th><th data-k="0">端</th><th data-k="2">项目</th><th data-k="3">标题</th>
<th data-k="6" style="text-align:right">事件</th><th data-k="7" style="text-align:right">tokens</th><th>模型</th></tr></thead><tbody id="rows"></tbody></table></div></div>
<script>
const D = __DATA__;
// 未知端兜底：颜色给中性灰，名字原样显示（自加 parser 的端不需注册色表）
const AGC = new Proxy(__AGENTCOLOR__, {get:(t,k)=>t[k]||'#8a8f98'});
const AGL = new Proxy(__AGENTLABEL__, {get:(t,k)=>t[k]||String(k)});
const fmt = n => n>=1e8? (n/1e8).toFixed(2)+"亿" : n>=1e4? (n/1e4).toFixed(1)+"万" : (n||0).toLocaleString();
const dt = ms => { const d=new Date(ms); return d.getFullYear()==new Date().getFullYear()? d.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}) : d.toLocaleString("zh-CN",{year:"2-digit",month:"2-digit",day:"2-digit"}); };
document.getElementById("gen").textContent = D.generated;
// KPI
const totTok = Object.values(D.per_agent).reduce((s,p)=>s+(p.tok||0),0);
const kk=[["会话",D.totals.sessions],["事件",D.totals.events],["Token 记录",D.totals.usage_rows],["转录 tokens 合计",fmt(totTok)],["kilo 花费","$"+Object.values(D.per_agent).reduce((s,p)=>s+(p.usd||0),0).toFixed(2)],["WB credit",Object.values(D.per_agent).reduce((s,p)=>s+(p.credit||0),0).toFixed(1)]];
document.getElementById("kpi").innerHTML = kk.map(k=>`<div class="card"><div class="num">${typeof k[1]==="number"?k[1].toLocaleString():k[1]}</div><div class="lab">${k[0]}</div></div>`).join("");
// legend + stacked area chart（端序按事件量降序，含自加端）
const AG = Object.keys(D.per_agent).sort((a,b)=>(D.per_agent[b].ev||0)-(D.per_agent[a].ev||0));
document.getElementById("legend").innerHTML = AG.map(a=>`<span><span class="dot" style="background:${AGC[a]}"></span>${AGL[a]} ${fmt(D.per_agent[a].ev)}事件 · ${fmt(D.per_agent[a].tok)} tok</span>`).join("");
(function(){const W=1120,H=260,P=28,n=D.days.length; if(!n)return;
 const max=Math.max(...D.days.map(d=>AG.reduce((s,a)=>s+d[a],0)))||1;
 const x=i=>P+(W-2*P)*i/(n-1), y=v=>H-P-(H-2*P)*v/max;
 let svg="";
 AG.forEach(a=>{let up="",dn="";D.days.forEach((d,i)=>{up+=(i?"L":"M")+x(i).toFixed(1)+","+y(d[a]).toFixed(1)});});
 // stacked
let base=D.days.map(()=>0);
[...AG].reverse().forEach(a=>{const top=base.map((b,i)=>b+D.days[i][a]);
 let p=top.map((v,i)=>(i?"L":"M")+x(i).toFixed(1)+","+y(v).toFixed(1)).join("");
 p+=base.slice().reverse().map((v,i)=>"L"+x(n-1-i).toFixed(1)+","+y(v).toFixed(1)).join("")+"Z";
 svg+=`<path d="${p}" fill="${AGC[a]}" opacity="0.75"/>`;base=top;});
 for(let g=1;g<=4;g++){const yy=y(max*g/4);svg+=`<line x1="${P}" y1="${yy}" x2="${W-P}" y2="${yy}" stroke="#e5e7eb" stroke-dasharray="3"/>`;}
 svg+=`<text x="${P}" y="${H-8}" font-size="11" fill="#6b7280">${D.days[0].d}</text><text x="${W-P}" y="${H-8}" font-size="11" fill="#6b7280" text-anchor="end">${D.days[n-1].d}</text>`;
 document.getElementById("chart").innerHTML=svg;})();
// tools
const tmax=D.tools.length?D.tools[0][2]:1;
document.getElementById("tools").innerHTML=D.tools.map(t=>`<div style="margin:3px 0"><span class="dot" style="background:${AGC[t[0]]}"></span><b>${t[1]}</b><span class="small"> ${AGL[t[0]]}</span>
<div class="bar" style="height:12px;margin:2px 0 0"><div style="width:${(t[2]/tmax*100).toFixed(1)}%;background:${AGC[t[0]]}"></div></div><span class="small">${t[2].toLocaleString()} 次</span></div>`).join("");
// models
document.getElementById("models").innerHTML="<table>"+D.models.map(m=>`<tr><td><span class="dot" style="background:${AGC[m[0]]}"></span>${AGL[m[0]]}</td><td class="mono">${m[1]}</td><td style="text-align:right">${fmt(m[2])}</td></tr>`).join("")+"</table>";
// sessions browser
let sortK="end",sortAsc=false;
const sel=document.getElementById("fa");AG.forEach(a=>{const o=document.createElement("option");o.value=a;o.textContent=AGL[a];sel.appendChild(o);});
function render(){const q=document.getElementById("q").value.toLowerCase(),a=sel.value,p=document.getElementById("fp").value.toLowerCase();
 let rs=D.sessions.filter(s=>(!a||s[0]===a)&&(!p||s[2].toLowerCase().includes(p))&&(!q||[s[1],s[2],s[3]].join(" ").toLowerCase().includes(q)));
 rs.sort((x,y)=>{const vx=sortK==="end"?x[5]:x[sortK],vy=sortK==="end"?y[5]:y[sortK];return (typeof vx==="string"? vx.localeCompare(vy):vx-vy)*(sortAsc?1:-1);});
 document.getElementById("cnt").textContent=rs.length+" / "+D.sessions.length+" 会话";
 document.getElementById("rows").innerHTML=rs.slice(0,400).map(s=>{let mdl;try{mdl=JSON.parse(s[8]).join(",")}catch(e){mdl=s[8]}
 return `<tr><td class="small">${dt(s[5])}</td><td><span class="dot" style="background:${AGC[s[0]]}"></span>${AGL[s[0]]}</td><td>${s[2]}</td><td title="${(s[3]||'').replace(/"/g,'&quot;')}">${s[3]||'<span class="small">(无标题)</span>'}</td><td style="text-align:right">${s[6]}</td><td style="text-align:right">${fmt(s[7])}</td><td class="small">${(mdl||"").slice(0,40)}</td></tr>`;}).join("");}
document.querySelectorAll("th[data-k]").forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(sortK===k)sortAsc=!sortAsc;else{sortK=k;sortAsc=false}render();});
document.getElementById("q").oninput=render;document.getElementById("fp").oninput=render;sel.onchange=render;
render();
</script></div></body>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(AM_DIR / "dashboard.html"))
    args = ap.parse_args()
    con = agentmem_db.connect()
    d = build_data(con)
    html = (HTML_TMPL
            .replace("__DATA__", json.dumps(d, ensure_ascii=False))
            .replace("__AGENTCOLOR__", json.dumps(AGENT_COLOR))
            .replace("__AGENTLABEL__", json.dumps(AGENT_LABEL)))
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"dashboard -> {args.out}  ({len(html)//1024} KB, sessions={len(d['sessions'])})")


if __name__ == "__main__":
    main()
