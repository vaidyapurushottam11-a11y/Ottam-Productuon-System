from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import jsonify

from . import dashboard as dashboard
from . import dashboard_cold_recovery as cold


def _history_rows() -> list[dict[str, Any]]:
    """Return one row per episode, newest episode first.

    A manual retry creates another workflow run with the same episode id. History
    should represent that as one production, using the newest run for status while
    retaining the original episode creation date.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for run in dashboard._runs("production.yml"):
        episode_id = dashboard._episode_from_run(run)
        if not episode_id:
            continue
        created = run.get("created_at") or run.get("run_started_at")
        if episode_id not in groups:
            groups[episode_id] = {
                "episode_id": episode_id,
                "title": dashboard._topic_from_run(run) or episode_id,
                "latest_run": run,
                "created_at": created,
            }
            order.append(episode_id)
        else:
            previous = groups[episode_id].get("created_at")
            if created and (not previous or created < previous):
                groups[episode_id]["created_at"] = created

    rows: list[dict[str, Any]] = []
    for episode_id in order:
        item = groups[episode_id]
        run = item.pop("latest_run")
        run_status = str(run.get("status") or "queued")
        conclusion = run.get("conclusion")
        if run_status != "completed":
            status = "IN_PROGRESS"
        elif conclusion == "success":
            status = "COMPLETED"
        else:
            status = "FAILED"

        row = {
            **item,
            "status": status,
            "workflow_status": run_status,
            "conclusion": conclusion,
            "run_id": run.get("id"),
            "run_url": run.get("html_url"),
        }
        if status == "COMPLETED":
            row["video_url"] = f"/files/{episode_id}/video"
            row["thumbnail_url"] = f"/files/{episode_id}/thumbnail"
            row["captions_url"] = f"/files/{episode_id}/captions"
            package_path = dashboard._episode_cache(episode_id) / "episodes" / episode_id / "upload_package.json"
            row["details_cached"] = package_path.is_file()
        rows.append(row)
    return rows


@dashboard.app.get("/api/history")
def production_history():
    return jsonify({"items": _history_rows()})


@dashboard.app.get("/api/history/<episode_id>/details")
def production_history_details(episode_id: str):
    run = dashboard._find_run("production.yml", f"OTTAM Production {episode_id}")
    if not run:
        return jsonify({"error": "episode not found"}), 404

    if run.get("status") == "completed" and run.get("conclusion") == "success":
        snapshot = cold._snapshot_without_blocking(episode_id, run)
        if snapshot.get("ready"):
            return jsonify(
                {
                    "status": "ready",
                    "episode_id": episode_id,
                    "package": snapshot.get("package") or {},
                    "video_url": f"/files/{episode_id}/video",
                    "thumbnail_url": f"/files/{episode_id}/thumbnail",
                    "captions_url": f"/files/{episode_id}/captions",
                }
            )
        return jsonify(
            {
                "status": "restoring",
                "episode_id": episode_id,
                "message": (snapshot.get("progress") or {}).get("current_stage")
                or "Restoring completed assets from GitHub",
            }
        )

    snapshot = dashboard._production_snapshot(episode_id, run=run)
    failure = snapshot.get("failure") or {}
    return jsonify(
        {
            "status": "failed" if run.get("status") == "completed" else "in_progress",
            "episode_id": episode_id,
            "failure": failure,
            "run_url": run.get("html_url"),
        }
    )


HISTORY_FRAGMENT = r'''
<style>
.historyHead{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}.historyList{display:grid;gap:10px}.historyItem{background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:14px}.historyTop{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:start}.historyTitle{font-weight:750;font-size:16px}.historyMeta{color:#8b949e;font-size:13px;margin-top:5px}.historyBadge{font-size:12px;font-weight:750;padding:5px 8px;border-radius:99px;white-space:nowrap}.historyCompleted{color:#7ee787;background:#17351f}.historyFailed{color:#ffb3b0;background:#3d1d21}.historyRunning{color:#f2cc60;background:#352d16}.historyActions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.historyActions button,.historyActions a{font-size:13px;padding:8px 10px}.historyDetails{margin-top:12px;padding-top:12px;border-top:1px solid #21262d}.historyField{margin:8px 0}.historyField strong{display:block;font-size:12px;color:#8b949e;margin-bottom:4px}.historyText{white-space:pre-wrap;word-break:break-word;background:#161b22;border:1px solid #30363d;border-radius:7px;padding:9px}.historyLoading{color:#8b949e;padding:8px 0}@media(max-width:650px){.historyTop{grid-template-columns:1fr}.historyBadge{justify-self:start}}
</style>
<div class="wrap" id="historySection">
  <div class="card">
    <div class="historyHead"><div><h2 style="margin:0">Production History</h2><div class="muted">Older episodes remain recoverable from GitHub after Render restarts.</div></div><button class="secondary" id="refreshHistory">Refresh</button></div>
    <div id="historyList" class="historyList"><div class="historyLoading">Loading production history…</div></div>
  </div>
</div>
<script>
(function(){
const list=document.getElementById('historyList');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const wait=ms=>new Promise(r=>setTimeout(r,ms));
function dateText(v){if(!v)return 'Date unavailable';try{return new Date(v).toLocaleString()}catch(e){return v}}
async function api(url){const r=await fetch(url);if(!r.ok)throw new Error(await r.text());return r.json()}
async function copyText(text,button){await navigator.clipboard.writeText(text||'');const old=button.textContent;button.textContent='Copied';setTimeout(()=>button.textContent=old,900)}
function packageHtml(p){const hashtags=(p.hashtags||[]).map(x=>'#'+x).join(' '),tags=(p.tags||[]).join(', ');return `<div class="historyField"><strong>Caption / Description</strong><div class="historyText">${esc(p.description||'')}</div><button class="secondary historyCopy" data-copy="description" style="margin-top:6px">Copy caption</button></div><div class="historyField"><strong>Hashtags</strong><div class="historyText">${esc(hashtags)}</div><button class="secondary historyCopy" data-copy="hashtags" style="margin-top:6px">Copy hashtags</button></div><div class="historyField"><strong>Tags</strong><div class="historyText">${esc(tags)}</div><button class="secondary historyCopy" data-copy="tags" style="margin-top:6px">Copy tags</button></div>`}
async function ensureReady(ep,box){for(;;){const d=await api('/api/history/'+encodeURIComponent(ep)+'/details');if(d.status==='ready')return d;if(d.status==='failed')throw new Error(d.failure?.reason||'Production failed');box.innerHTML='<div class="historyLoading">'+esc(d.message||'Restoring assets from GitHub…')+'</div>';await wait(2500)}}
async function showDetails(ep,box){box.classList.remove('hidden');box.innerHTML='<div class="historyLoading">Loading upload text…</div>';try{const d=await ensureReady(ep,box);const p=d.package||{};box.innerHTML=packageHtml(p);box.querySelectorAll('.historyCopy').forEach(b=>b.onclick=()=>{const key=b.dataset.copy;let text=key==='hashtags'?(p.hashtags||[]).map(x=>'#'+x).join(' '):key==='tags'?(p.tags||[]).join(', '):(p[key]||'');copyText(text,b)})}catch(e){box.innerHTML='<div class="bad">'+esc(e.message)+'</div>'}}
async function download(ep,kind,box){try{await ensureReady(ep,box);window.location.href='/files/'+encodeURIComponent(ep)+'/'+kind}catch(e){box.classList.remove('hidden');box.innerHTML='<div class="bad">'+esc(e.message)+'</div>'}}
function render(items){if(!items.length){list.innerHTML='<div class="muted">No productions yet.</div>';return}list.innerHTML='';items.forEach(x=>{const d=document.createElement('div');d.className='historyItem';const badge=x.status==='COMPLETED'?'historyCompleted':x.status==='FAILED'?'historyFailed':'historyRunning';let actions='';if(x.status==='COMPLETED'){actions=`<button class="historyDownload" data-kind="video">Download MP4</button><button class="secondary historyDownload" data-kind="thumbnail">Download Thumbnail</button><button class="secondary historyDownload" data-kind="captions">Download Captions</button><button class="secondary historyShowText">Caption / Tags / Hashtags</button>`}else if(x.run_url){actions=`<a class="btn secondary" target="_blank" rel="noopener" href="${esc(x.run_url)}">Open run</a>`}d.innerHTML=`<div class="historyTop"><div><div class="historyTitle">${esc(x.title)}</div><div class="historyMeta">${esc(dateText(x.created_at))} · ${esc(x.episode_id)}</div></div><span class="historyBadge ${badge}">${esc(x.status.replace('_',' '))}</span></div><div class="historyActions">${actions}</div><div class="historyDetails hidden"></div>`;const box=d.querySelector('.historyDetails');d.querySelectorAll('.historyDownload').forEach(b=>b.onclick=()=>download(x.episode_id,b.dataset.kind,box));const show=d.querySelector('.historyShowText');if(show)show.onclick=()=>{if(!box.classList.contains('hidden')){box.classList.add('hidden');return}showDetails(x.episode_id,box)};list.appendChild(d)})}
async function load(){list.innerHTML='<div class="historyLoading">Loading production history…</div>';try{const d=await api('/api/history');render(d.items||[])}catch(e){list.innerHTML='<div class="bad">Unable to load history: '+esc(e.message)+'</div>'}}
document.getElementById('refreshHistory').onclick=load;load();
})();
</script>
'''

if "id=\"historySection\"" not in dashboard.PAGE:
    dashboard.PAGE = dashboard.PAGE.replace("</body>", HISTORY_FRAGMENT + "</body>")

app = dashboard.app
