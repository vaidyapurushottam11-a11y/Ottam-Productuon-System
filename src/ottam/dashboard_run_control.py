from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import jsonify, request

from . import dashboard
from . import dashboard_review_direct as review


_dispatch_lock = threading.Lock()
_selection_memory: dict[str, str] = {}


def _runs_for_episode(episode_id: str) -> list[dict[str, Any]]:
    marker = f"OTTAM Production {episode_id}"
    return [
        run
        for run in dashboard._runs("production.yml")
        if marker in str(run.get("display_title") or "")
    ]


def _best_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer live work, then meaningful terminal runs, then cancelled runs.

    A user-cancelled duplicate must not hide an earlier valid run or script-review
    checkpoint simply because GitHub returned the cancelled run first.
    """
    if not runs:
        return None
    active = next((run for run in runs if run.get("status") != "completed"), None)
    if active:
        return active
    meaningful = next((run for run in runs if run.get("conclusion") != "cancelled"), None)
    return meaningful or runs[0]


def _current_meaningful_run() -> dict[str, Any] | None:
    return _best_run(dashboard._runs("production.yml"))


def _snapshot_for_run(run: dict[str, Any]) -> dict[str, Any]:
    episode_id = dashboard._episode_from_run(run)
    if not episode_id:
        return {"active": False}
    payload = review.snapshot(episode_id, run)
    payload["active"] = run.get("status") != "completed"
    return payload


def current_job_controlled():
    run = _current_meaningful_run()
    if not run:
        return jsonify({"active": False})
    return jsonify(_snapshot_for_run(run))


def job_status_controlled(episode_id: str):
    run = _best_run(_runs_for_episode(episode_id))
    if not run:
        return jsonify(dashboard._production_snapshot(episode_id, run=None))
    return jsonify(review.snapshot(episode_id, run))


def _selection_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]
    return dashboard.DATA_ROOT / "dispatches" / f"{safe}.json"


def _read_selection(key: str) -> str | None:
    cached = _selection_memory.get(key)
    if cached:
        return cached
    path = _selection_path(key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    episode_id = str(payload.get("episode_id") or "").strip()
    if episode_id:
        _selection_memory[key] = episode_id
        return episode_id
    return None


def _remember_selection(key: str, episode_id: str) -> None:
    _selection_memory[key] = episode_id
    path = _selection_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"episode_id": episode_id, "saved_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def _job_for_existing_episode(episode_id: str, *, deduplicated: bool) -> dict[str, Any]:
    run = _best_run(_runs_for_episode(episode_id))
    if run:
        payload = review.snapshot(episode_id, run)
    else:
        payload = dashboard._load_job(episode_id)
        payload["episode_id"] = episode_id
    payload["deduplicated"] = deduplicated
    return payload


def _same_active_topic(title: str) -> dict[str, Any] | None:
    wanted = " ".join(title.casefold().split())
    if not wanted:
        return None
    for run in dashboard._runs("production.yml"):
        if run.get("status") == "completed":
            continue
        existing = " ".join(str(dashboard._topic_from_run(run) or "").casefold().split())
        if existing == wanted:
            return run
    return None


def produce_controlled():
    body = request.get_json(force=True)
    topic = body.get("topic")
    if not isinstance(topic, dict):
        return jsonify({"error": "topic object is required"}), 400

    title = str(topic.get("title") or "Selected topic")[:120]
    key = str(body.get("idempotency_key") or "").strip()
    if not key:
        key = f"legacy-{secrets.token_hex(12)}"

    with _dispatch_lock:
        existing_episode = _read_selection(key)
        if existing_episode:
            return jsonify(_job_for_existing_episode(existing_episode, deduplicated=True))

        active_same_topic = _same_active_topic(title)
        if active_same_topic:
            episode_id = dashboard._episode_from_run(active_same_topic)
            if episode_id:
                _remember_selection(key, episode_id)
                return jsonify(_job_for_existing_episode(episode_id, deduplicated=True))

        episode_id = f"OTTAM-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"
        dashboard._dispatch(
            "production.yml",
            {
                "episode_id": episode_id,
                "selected_topic_title": title,
                "selected_topic_json": json.dumps(topic, separators=(",", ":"), ensure_ascii=False),
                "resume_failed": "false",
            },
        )
        job = {
            "episode_id": episode_id,
            "topic": topic,
            "topic_title": title,
            "status": "queued",
            "retry_count": 0,
            "deduplicated": False,
        }
        dashboard._save_job(episode_id, job)
        _remember_selection(key, episode_id)
        return jsonify(job)


def _hidden_history_ids() -> set[str]:
    hidden: set[str] = set()
    try:
        runs = dashboard._runs("history-hide.yml")
    except Exception:
        return hidden
    for run in runs:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        title = str(run.get("display_title") or "")
        match = re.search(r"Hide history (OTTAM-[^\s]+)", title)
        if match:
            hidden.add(match.group(1))
    return hidden


def production_history_controlled():
    hidden = _hidden_history_ids()
    rows = [row for row in review.history_rows_direct() if row.get("episode_id") not in hidden]
    return jsonify({"items": rows, "hidden_count": len(hidden), "build": "script-review-state-v2", "run_control_build": "run-control-v1"})


def delete_history_episode(episode_id: str):
    if not re.fullmatch(r"OTTAM-[A-Za-z0-9-]+", episode_id):
        return jsonify({"error": "invalid episode id"}), 400
    dashboard._dispatch("history-hide.yml", {"episode_id": episode_id})
    return jsonify(
        {
            "status": "hidden",
            "episode_id": episode_id,
            "message": "Removed from dashboard history. GitHub production artifacts were not deleted.",
        }
    )


dashboard.app.view_functions["produce"] = produce_controlled
dashboard.app.view_functions["current_job"] = current_job_controlled
dashboard.app.view_functions["job_status"] = job_status_controlled
dashboard.app.view_functions["production_history"] = production_history_controlled


@dashboard.app.post("/api/history/<episode_id>/delete")
def delete_history(episode_id: str):
    return delete_history_episode(episode_id)


RUN_CONTROL_FRAGMENT = r'''
<style>
.historyDelete{background:#3d1d21!important;color:#ffb3b0!important;border-color:#6e2b31!important}
.topic button:disabled{opacity:.55;cursor:not-allowed}
</style>
<script>
(function(){
let starting=false, pollEpoch=0;
const selectionKeys=new WeakMap();
const hiddenKey='ottam.hiddenHistory';
function hiddenSet(){try{return new Set(JSON.parse(localStorage.getItem(hiddenKey)||'[]'))}catch(e){return new Set()}}
function saveHidden(s){localStorage.setItem(hiddenKey,JSON.stringify([...s]))}
function keyFor(topic){let k=selectionKeys.get(topic);if(!k){k=(crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random());selectionKeys.set(topic,k)}return k}
function clearOldProduction(topic){
  pollEpoch++; polling=false; currentPackage=null; previousPackage=null; previousThumb=null; lastProgress=null;
  $('jobPanel').classList.remove('hidden'); $('resultPanel').classList.add('hidden'); $('failureBox').classList.add('hidden');
  const reviewPanel=document.getElementById('scriptReviewPanel'); if(reviewPanel)reviewPanel.classList.add('hidden');
  $('jobTitle').textContent='Starting selected production…'; $('topicTitle').textContent=topic.title||'';
  $('workflowState').textContent='dispatching'; $('currentStage').textContent='Creating production run'; $('jobStatus').textContent='Starting this topic once…';
  $('bar').style.width='2%'; $('progressTrack').classList.remove('failed'); $('timeline').innerHTML='';
  $('totalElapsed').textContent='—'; $('stageElapsed').textContent='—';
}
renderTopics=function(items){
  $('topics').innerHTML='';
  items.forEach(t=>{let d=document.createElement('div');d.className='card topic';d.innerHTML=`<div class="score">${t.score}</div><div><b>${t.title}</b><div class="muted">${t.reason||t.central_question||''}</div></div><button>Select</button>`;
    const b=d.querySelector('button'); b.onclick=()=>produce(t,b); $('topics').appendChild(d)});
};
produce=async function(topic,button){
  if(starting)return; starting=true; const buttons=[...document.querySelectorAll('.topic button')]; buttons.forEach(b=>b.disabled=true); if(button)button.textContent='Starting…'; clearOldProduction(topic);
  try{
    const j=await post('/api/produce',{topic,idempotency_key:keyFor(topic)});
    currentEpisode=j.episode_id; localStorage.setItem('ottam.currentEpisode',currentEpisode); showJob(j); await pollControlled();
  }catch(e){$('jobStatus').textContent='Unable to start: '+e.message; buttons.forEach(b=>b.disabled=false); if(button)button.textContent='Select'}
  finally{starting=false}
};
async function pollControlled(){
  if(!currentEpisode)return; const episode=currentEpisode, token=++pollEpoch; polling=true;
  try{for(;;){const j=await get('/api/jobs/'+encodeURIComponent(episode)); if(token!==pollEpoch||episode!==currentEpisode)return; showJob(j);
    if(j.awaiting_script_approval)return; if(j.ready){showResult(j.package);return} if(j.failure||j.conclusion&&j.conclusion!=='success')return; await sleep(3500)}}
  finally{if(token===pollEpoch)polling=false}
}
pollJob=pollControlled;
async function recoverMeaningfulCurrent(){
  try{const j=await get('/api/current-job'); if(!j.episode_id)return; if(currentEpisode!==j.episode_id){pollEpoch++;polling=false} currentEpisode=j.episode_id; localStorage.setItem('ottam.currentEpisode',currentEpisode); showJob(j);
    if(j.awaiting_script_approval)return; if(j.ready){showResult(j.package);return} if(!j.failure)pollControlled();}
  catch(e){console.error('current production recovery failed',e)}
}
function enhanceHistory(root=document){
  const hidden=hiddenSet();
  root.querySelectorAll('.historyItem').forEach(card=>{
    const meta=card.querySelector('.historyMeta')?.textContent||''; const m=meta.match(/(OTTAM-[A-Za-z0-9-]+)/); if(!m)return; const ep=m[1];
    if(hidden.has(ep)){card.remove();return} if(card.querySelector('.historyDelete'))return;
    const actions=card.querySelector('.historyActions'); if(!actions)return; const b=document.createElement('button'); b.className='historyDelete'; b.textContent='Delete';
    b.onclick=async()=>{if(!confirm('Remove this episode from OTTAM history? The GitHub video artifact will be kept.'))return; b.disabled=true; b.textContent='Removing…';
      const s=hiddenSet(); s.add(ep); saveHidden(s); card.remove(); try{await post('/api/history/'+encodeURIComponent(ep)+'/delete',{})}catch(e){console.error(e)}}; actions.appendChild(b);
  });
}
const observer=new MutationObserver(()=>enhanceHistory()); const history=document.getElementById('historyList'); if(history)observer.observe(history,{childList:true,subtree:true}); enhanceHistory();
setTimeout(recoverMeaningfulCurrent,250);
})();
</script>
'''

if "run-control-v1" not in dashboard.PAGE:
    dashboard.PAGE = dashboard.PAGE.replace("</body>", "<!-- run-control-v1 -->" + RUN_CONTROL_FRAGMENT + "</body>")

app = dashboard.app
