# -*- coding: utf-8 -*-
"""研究轮进度面板（零侵入）。

不改 pipeline：靠读 cycle 目录里的 candidate_*.json / *.result.json（每个候选跑完
会落 result.json）+ PowerShell 拿到的 python 进程列表，推断整轮进度。

用法：
    H:\\Ashare\\.venv313\\Scripts\\python.exe H:\\Ashare\\tools\\progress_dashboard.py
然后浏览器打开 http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CYCLES_ROOT = Path(r"H:\Ashare\data\research_hub_integrated\cycles")
PORT = 8765
START_TS = time.time()

_CAND_RE = re.compile(r"candidate_(\d+)_([0-9a-fA-F]+)")


def _latest_cycle_dir() -> Path | None:
    if not CYCLES_ROOT.exists():
        return None
    dirs = [p for p in CYCLES_ROOT.iterdir() if p.is_dir() and p.name.startswith("cycle_")]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def _ps_python_procs() -> list[dict]:
    """用 PowerShell 拿 python 进程，输出纯 ASCII 管道分隔文本（避开 JSON+中文乱码）。

    每行：pid|memMB|runsec|kind|cand_idx
    kind 在 PowerShell 端就判好，cmd 本身不输出，所以任何进程命令行里的乱码都进不来。
    """
    ps = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | ForEach-Object { "
        "$c=$_.CommandLine; $kind='other'; "
        "if($c -match 'run_single_candidate'){$kind='candidate'} "
        "elseif($c -match 'launch_canonical|main_research_runner|run_research_hub_local'){$kind='pipeline'} "
        "elseif($c -match 'build_industry|build_factor|build_hard|data_scout|event_ingest|event_extract'){$kind='prep'}; "
        "$idx=''; if($c -match 'candidate_(\\d+)_'){$idx=$matches[1]}; "
        "'{0}|{1}|{2}|{3}|{4}' -f $_.ProcessId,[math]::Round($_.WorkingSetSize/1MB),"
        "[math]::Round((New-TimeSpan -Start $_.CreationDate).TotalSeconds),$kind,$idx }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        ).stdout
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            try:
                rows.append({
                    "pid": int(parts[0]),
                    "mem": int(float(parts[1])),
                    "runsec": int(float(parts[2])),
                    "kind": parts[3].strip(),
                    "idx": int(parts[4]) if parts[4].strip().isdigit() else None,
                })
            except Exception:
                continue
        return rows
    except Exception:
        return []


def _parse_procs(rows: list[dict]) -> dict:
    pipeline_alive = False
    prep_alive = False
    running = []  # 正在跑的候选子进程
    for r in rows:
        k = r.get("kind")
        if k == "pipeline":
            pipeline_alive = True
        elif k == "prep":
            prep_alive = True
        elif k == "candidate":
            running.append({
                "pid": r.get("pid"),
                "mem_mb": r.get("mem"),
                "run_sec": r.get("runsec"),
                "cand_idx": r.get("idx"),
            })
    return {"pipeline_alive": pipeline_alive, "prep_alive": prep_alive, "running_procs": running}


def _read_result(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        rec = d.get("record", d)
        return {
            "status": rec.get("status"),
            "model": rec.get("effective_model_family") or rec.get("model_family"),
            "gpu_used": rec.get("gpu_used"),
            "elapsed": rec.get("elapsed_seconds"),
            "best_iter": rec.get("best_iteration"),
            "es": rec.get("early_stopping_applied"),
            "error": rec.get("error_message"),
        }
    except Exception:
        return None


STALE_SEC = 1800  # cycle 目录超过这个时间没动过，就当它是上一轮的残留


def build_progress() -> dict:
    cyc = _latest_cycle_dir()
    procs = _parse_procs(_ps_python_procs())
    # 一个候选会起多个 python 子进程（launcher+worker），按 cand_idx 去重，取 run_sec
    # 最大的当代表，否则并行数翻倍、ETA 的 run_remaining 也被重复累加撑大。
    _by_idx: dict = {}
    for _rp in procs["running_procs"]:
        _k = _rp.get("cand_idx")
        if _k is None:
            continue
        if _k not in _by_idx:
            _by_idx[_k] = dict(_rp)
        else:
            cur = _by_idx[_k]
            cur["run_sec"] = max(cur["run_sec"], _rp["run_sec"])
            cur["mem_mb"] = max(cur["mem_mb"], _rp["mem_mb"])  # worker 的真实内存
    procs["running_procs"] = sorted(_by_idx.values(), key=lambda x: x["cand_idx"])
    busy_pipeline = procs["pipeline_alive"] or bool(procs["running_procs"])
    busy_any = busy_pipeline or procs["prep_alive"]
    now = time.time()

    # 判“本轮”不靠面板启动时刻（面板可能在 cycle 建好之后才重启），而靠：
    #   管线进程还活着 + cycle 目录最近动过。管线死了，再新的 cycle 也只是残留。
    cyc_recent = cyc is not None and (now - cyc.stat().st_mtime) < STALE_SEC
    cyc_is_current = cyc_recent and busy_pipeline

    cands: list[Path] = []
    if cyc_is_current:
        cfg_dir = cyc / "configs"
        if cfg_dir.exists():
            cands = [c for c in sorted(cfg_dir.glob("candidate_*.json"))
                     if not c.name.endswith(".result.json")]

    # 还没生成候选（LLM 还在诊断/路由）或管线没在跑 → preparing / idle
    if not cyc_is_current or not cands:
        if busy_any:
            stage = "数据/因子层准备中…" if procs["prep_alive"] else "研究轮初始化中…"
            return {"state": "preparing", "msg": stage,
                    "elapsed_total": round(now - START_TS), **procs}
        return {"state": "idle", "msg": "当前没有正在运行的研究轮",
                "elapsed_total": round(now - START_TS), **procs}

    running_idx = {rp["cand_idx"] for rp in procs["running_procs"] if rp.get("cand_idx")}
    items = []
    done = 0
    elapsed_list = []
    for c in cands:
        m = _CAND_RE.search(c.name)
        idx = int(m.group(1)) if m else 0
        res = _read_result(c.with_suffix(".result.json"))
        # 读 config 拿到模型/策略名（轻量）
        model = None
        try:
            cc = json.loads(c.read_text(encoding="utf-8")).get("candidate", {})
            model = cc.get("model_family")
        except Exception:
            pass
        if res is not None and res.get("status") in ("ok", "failed", "skipped_budget_guard"):
            state = res["status"]
            done += 1
            if isinstance(res.get("elapsed"), (int, float)):
                elapsed_list.append(res["elapsed"])
        elif idx in running_idx:
            state = "running"
        else:
            state = "pending"
        items.append({
            "idx": idx,
            "model": (res and res.get("model")) or model or "?",
            "state": state,
            "gpu_used": res and res.get("gpu_used"),
            "elapsed": res and res.get("elapsed"),
            "best_iter": res and res.get("best_iter"),
            "es": res and res.get("es"),
            "error": res and res.get("error"),
        })

    total = len(cands)
    remaining = total - done
    # 已完成候选的平均耗时；一个都还没完成时用提速后的先验值兜底。
    PRIOR_SEC = 240
    avg = (sum(elapsed_list) / len(elapsed_list)) if elapsed_list else PRIOR_SEC
    par = max(len(running_idx), 1)
    # 正在跑的候选：剩余 = avg - 已跑时长（随 run_sec 增长而递减，所以 eta 会动）。
    run_remaining = sum(max(avg - rp["run_sec"], 5) for rp in procs["running_procs"])
    pending_cnt = max(remaining - len(running_idx), 0)
    work = pending_cnt * avg + run_remaining
    eta = 0 if remaining <= 0 else round(work / par)

    return {
        "state": "running" if (procs["pipeline_alive"] or procs["running_procs"] or remaining > 0) else "done",
        "cycle": cyc.name,
        "total": total,
        "done": done,
        "items": sorted(items, key=lambda x: x["idx"]),
        "avg_sec": round(avg) if elapsed_list else None,
        "eta_sec": eta,
        "elapsed_total": round(time.time() - START_TS),
        **procs,
    }


HTML = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>研究轮进度</title><style>
*{box-sizing:border-box;font-family:'Segoe UI',-apple-system,sans-serif}
body{background:#0f1115;color:#e6e6e6;margin:0;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8a93a2;font-size:13px;margin-bottom:18px}
.bar-wrap{background:#1b1f27;border-radius:10px;height:34px;overflow:hidden;position:relative;margin-bottom:8px}
.bar{height:100%;background:linear-gradient(90deg,#2563eb,#3b82f6);transition:width .4s;display:flex;align-items:center;justify-content:flex-end;padding-right:10px;color:#fff;font-weight:600;font-size:13px}
.stats{display:flex;gap:24px;margin:14px 0 22px;flex-wrap:wrap}
.stat{background:#161a21;border:1px solid #232936;border-radius:8px;padding:10px 16px;min-width:120px}
.stat .k{color:#8a93a2;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.stat .v{font-size:20px;font-weight:700;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
.card{background:#161a21;border:1px solid #232936;border-radius:8px;padding:12px;font-size:13px}
.card.running{border-color:#3b82f6;box-shadow:0 0 0 1px #3b82f6}
.card.ok{border-color:#1f7a3f}
.card.failed{border-color:#a33}
.row{display:flex;justify-content:space-between;margin-top:6px;color:#aab}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600}
.b-ok{background:#143d24;color:#5ed98a}.b-running{background:#13315c;color:#7db4ff}
.b-pending{background:#23262e;color:#888}.b-failed{background:#4a1d1d;color:#ff9a9a}
.b-skipped_budget_guard{background:#3a2e10;color:#e3c069}
.procs{margin-top:24px;font-size:12px;color:#8a93a2}
.procs code{color:#9fd0ff}
.pulse{animation:p 1.4s infinite}@keyframes p{50%{opacity:.45}}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.live{background:#3b82f6}.dead{background:#555}
</style></head><body>
<h1>研究轮进度面板 <span id="livedot"></span></h1>
<div class="sub" id="sub">连接中…</div>
<div class="bar-wrap"><div class="bar" id="bar" style="width:0%">0%</div></div>
<div class="stats" id="stats"></div>
<div class="grid" id="grid"></div>
<div class="procs" id="procs"></div>
<script>
let etaBase=null, etaAt=0;
function fmt(s){if(s==null)return '—';s=Math.round(s);if(s<0)s=0;if(s<60)return s+'s';let m=Math.floor(s/60);return m+'m'+(s%60)+'s';}
function etaText(){if(etaBase==null)return '—';let v=etaBase-(Date.now()-etaAt)/1000;return v<=1?'即将完成':fmt(v);}
async function tick(){
 let r; try{r=await (await fetch('/api')).json();}catch(e){document.getElementById('sub').textContent='面板服务断开';return;}
 const dot=document.getElementById('livedot');
 const alive=r.pipeline_alive||r.prep_alive||((r.running_procs||[]).length>0);
 if(r.state!=='running'&&r.state!=='done'){
   // waiting / preparing / idle
   etaBase=null;
   document.getElementById('sub').textContent=r.msg||r.state;
   const bar=document.getElementById('bar');bar.style.width='0%';bar.textContent='';
   dot.innerHTML='<span class="dot '+(alive?'live pulse':'dead')+'"></span>';
   document.getElementById('stats').innerHTML=[
     ['状态', alive?(r.prep_alive?'数据准备':'初始化'):'空闲'],
     ['面板已运行', fmt(r.elapsed_total)],
   ].map(s=>'<div class="stat"><div class="k">'+s[0]+'</div><div class="v">'+s[1]+'</div></div>').join('');
   document.getElementById('grid').innerHTML='';
 } else {
   const pct=r.total? Math.round(r.done/r.total*100):0;
   const bar=document.getElementById('bar');bar.style.width=pct+'%';bar.textContent=pct+'%';
   dot.innerHTML='<span class="dot '+(alive?'live pulse':'dead')+'"></span>';
   document.getElementById('sub').textContent='cycle '+r.cycle+' · '+(alive?'运行中':'已结束');
   etaBase=(r.state==='done'||r.eta_sec===0)?0:r.eta_sec; etaAt=Date.now();
   document.getElementById('stats').innerHTML=[
     ['进度', r.done+' / '+r.total],
     ['并行', (r.running_procs?r.running_procs.length:0)+' 路'],
     ['平均/候选', fmt(r.avg_sec)],
     ['预计剩余', '<span id="eta">'+etaText()+'</span>'],
     ['面板已运行', fmt(r.elapsed_total)],
   ].map(s=>'<div class="stat"><div class="k">'+s[0]+'</div><div class="v">'+s[1]+'</div></div>').join('');
   document.getElementById('grid').innerHTML=(r.items||[]).map(it=>{
     const g=it.gpu_used===true?'GPU':(it.gpu_used===false?'CPU':'');
     const es=it.best_iter?('早停@'+it.best_iter):'';
     const el=it.elapsed?fmt(it.elapsed):'';
     const err=it.error?('<div class="row" style="color:#ff9a9a">'+String(it.error).slice(0,60)+'</div>'):'';
     return '<div class="card '+it.state+'"><div><b>#'+it.idx+'</b> <span class="badge b-'+it.state+'">'+it.state+'</span></div>'
       +'<div class="row"><span>'+it.model+'</span><span>'+g+'</span></div>'
       +'<div class="row"><span>'+es+'</span><span>'+el+'</span></div>'+err+'</div>';
   }).join('');
 }
 const procs=(r.running_procs||[]);
 document.getElementById('procs').innerHTML='活跃训练进程: '+(procs.length?procs.map(p=>'<code>候选#'+(p.cand_idx||'?')+'</code> pid '+p.pid+' · '+p.mem_mb+'MB · '+fmt(p.run_sec)).join(' &nbsp;|&nbsp; '):'无');
}
setInterval(()=>{const e=document.getElementById('eta');if(e)e.textContent=etaText();},1000);
tick();setInterval(tick,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api"):
            body = json.dumps(build_progress(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"进度面板已启动: http://127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
