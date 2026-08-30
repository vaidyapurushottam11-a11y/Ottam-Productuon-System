from __future__ import annotations

import io
import json
import os
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from flask import Flask, Response, jsonify, request, send_file

DATA_ROOT = Path(os.getenv("OTTAM_DASHBOARD_DATA", "dashboard_data"))
DATA_ROOT.mkdir(parents=True, exist_ok=True)
REPO = os.getenv("OTTAM_GITHUB_REPOSITORY", "vaidyapurushottam11-a11y/Ottam-Productuon-System")
WORKFLOW_REF = os.getenv("OTTAM_WORKFLOW_REF", "main")

app = Flask(__name__)


def _token() -> str:
    value = os.getenv("OTTAM_GITHUB_TOKEN", "").strip()
    if not value:
        raise RuntimeError("OTTAM_GITHUB_TOKEN is required for dashboard workflow control")
    return value


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh(method: str, path: str, **kwargs) -> httpx.Response:
    url = f"https://api.github.com/repos/{REPO}/{path.lstrip('/')}"
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.request(method, url, headers=_headers(), **kwargs)
    response.raise_for_status()
    return response


def _dispatch(workflow: str, inputs: dict[str, str]) -> None:
    _gh("POST", f"actions/workflows/{workflow}/dispatches", json={"ref": WORKFLOW_REF, "inputs": inputs})


def _runs(workflow: str) -> list[dict[str, Any]]:
    response = _gh("GET", f"actions/workflows/{workflow}/runs", params={"event": "workflow_dispatch", "per_page": 40})
    return response.json().get("workflow_runs") or []


def _find_run(workflow: str, marker: str) -> dict[str, Any] | None:
    for run in _runs(workflow):
        if marker in str(run.get("display_title") or ""):
            return run
    return None


def _artifact(run_id: int, name: str) -> dict[str, Any] | None:
    response = _gh("GET", f"actions/runs/{run_id}/artifacts", params={"per_page": 100})
    artifacts = response.json().get("artifacts") or []
    matches = [x for x in artifacts if x.get("name") == name and not x.get("expired")]
    return matches[-1] if matches else None


def _extract_artifact(artifact_id: int, destination: Path) -> None:
    response = _gh("GET", f"actions/artifacts/{artifact_id}/zip")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        zf.extractall(destination)


def _job_file(episode_id: str) -> Path:
    return DATA_ROOT / "jobs" / f"{episode_id}.json"


def _save_job(episode_id: str, payload: dict[str, Any]) -> None:
    path = _job_file(episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_job(episode_id: str) -> dict[str, Any]:
    path = _job_file(episode_id)
    if not path.exists():
        return {"episode_id": episode_id}
    return json.loads(path.read_text(encoding="utf-8"))


def _episode_cache(episode_id: str) -> Path:
    return DATA_ROOT / "episodes" / episode_id


@app.get("/")
def index() -> Response:
    return Response(PAGE, mimetype="text/html")


@app.post("/api/topics")
def generate_topics():
    request_id = f"TOPIC-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2)}"
    instruction = str((request.get_json(silent=True) or {}).get("instruction") or "").strip()
    _dispatch("dashboard-topics.yml", {"request_id": request_id, "instruction": instruction})
    return jsonify({"request_id": request_id})


@app.get("/api/topics/<request_id>")
def topic_status(request_id: str):
    marker = f"Topics {request_id}"
    run = _find_run("dashboard-topics.yml", marker)
    if not run:
        return jsonify({"status": "queued"})
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status != "completed":
        return jsonify({"status": status, "run_id": run.get("id")})
    if conclusion != "success":
        return jsonify({"status": "failed", "conclusion": conclusion, "run_id": run.get("id")}), 500
    cache = DATA_ROOT / "topics" / request_id
    result = cache / "topic_candidates.json"
    if not result.exists():
        artifact = _artifact(int(run["id"]), f"dashboard-topics-{request_id}")
        if not artifact:
            return jsonify({"status": "finalizing", "run_id": run.get("id")})
        _extract_artifact(int(artifact["id"]), cache)
    return jsonify({"status": "ready", "candidates": json.loads(result.read_text(encoding="utf-8"))["candidates"]})


@app.post("/api/produce")
def produce():
    body = request.get_json(force=True)
    topic = body.get("topic")
    if not isinstance(topic, dict):
        return jsonify({"error": "topic object is required"}), 400
    episode_id = f"OTTAM-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"
    _dispatch(
        "production.yml",
        {
            "episode_id": episode_id,
            "selected_topic_json": json.dumps(topic, separators=(",", ":"), ensure_ascii=False),
        },
    )
    job = {"episode_id": episode_id, "topic": topic, "status": "queued"}
    _save_job(episode_id, job)
    return jsonify(job)


def _production_snapshot(episode_id: str) -> dict[str, Any]:
    run = _find_run("production.yml", f"OTTAM Production {episode_id}")
    job = _load_job(episode_id)
    if not run:
        job["status"] = "queued"
        return job
    job["run_id"] = run.get("id")
    job["status"] = run.get("status")
    job["conclusion"] = run.get("conclusion")
    if run.get("status") == "completed" and run.get("conclusion") == "success":
        cache = _episode_cache(episode_id)
        package = cache / "episodes" / episode_id / "upload_package.json"
        artifact = _artifact(int(run["id"]), f"ottam-production-{episode_id}")
        if artifact and not package.exists():
            _extract_artifact(int(artifact["id"]), cache)
        state_path = cache / "state" / f"{episode_id}.json"
        if state_path.exists():
            job["episode_state"] = json.loads(state_path.read_text(encoding="utf-8"))
            job["status"] = job["episode_state"].get("status", job["status"])
        if package.exists():
            job["package"] = json.loads(package.read_text(encoding="utf-8"))
            job["ready"] = True
    _save_job(episode_id, job)
    return job


@app.get("/api/jobs/<episode_id>")
def job_status(episode_id: str):
    return jsonify(_production_snapshot(episode_id))


@app.post("/api/jobs/<episode_id>/regenerate-caption")
def regenerate_caption(episode_id: str):
    job = _production_snapshot(episode_id)
    if not job.get("ready"):
        return jsonify({"error": "episode is not ready"}), 409
    instruction = str((request.get_json(silent=True) or {}).get("instruction") or "").strip()
    _dispatch("dashboard-caption.yml", {"episode_id": episode_id, "instruction": instruction})
    return jsonify({"status": "queued", "episode_id": episode_id})


@app.post("/api/jobs/<episode_id>/regenerate-thumbnail")
def regenerate_thumbnail(episode_id: str):
    job = _production_snapshot(episode_id)
    if not job.get("ready"):
        return jsonify({"error": "episode is not ready"}), 409
    instruction = str((request.get_json(silent=True) or {}).get("instruction") or "").strip()
    _dispatch("dashboard-thumbnail.yml", {"episode_id": episode_id, "instruction": instruction})
    return jsonify({"status": "queued", "episode_id": episode_id})


def _refresh_variant(episode_id: str, workflow: str, marker: str, artifact_name: str) -> dict[str, Any]:
    run = _find_run(workflow, marker)
    if not run:
        return {"status": "queued"}
    if run.get("status") != "completed":
        return {"status": run.get("status"), "run_id": run.get("id")}
    if run.get("conclusion") != "success":
        return {"status": "failed", "run_id": run.get("id"), "conclusion": run.get("conclusion")}
    artifact = _artifact(int(run["id"]), artifact_name)
    if not artifact:
        return {"status": "finalizing", "run_id": run.get("id")}
    cache = _episode_cache(episode_id)
    _extract_artifact(int(artifact["id"]), cache)
    return {"status": "ready", "run_id": run.get("id")}


@app.get("/api/jobs/<episode_id>/caption-status")
def caption_status(episode_id: str):
    state = _refresh_variant(episode_id, "dashboard-caption.yml", f"Caption {episode_id}", f"ottam-caption-{episode_id}")
    if state["status"] == "ready":
        path = _episode_cache(episode_id) / "episodes" / episode_id / "upload_package.json"
        state["package"] = json.loads(path.read_text(encoding="utf-8"))
    return jsonify(state)


@app.get("/api/jobs/<episode_id>/thumbnail-status")
def thumbnail_status(episode_id: str):
    state = _refresh_variant(episode_id, "dashboard-thumbnail.yml", f"Thumbnail {episode_id}", f"ottam-thumbnail-{episode_id}")
    return jsonify(state)


@app.get("/files/<episode_id>/<kind>")
def episode_file(episode_id: str, kind: str):
    episode_dir = _episode_cache(episode_id) / "episodes" / episode_id
    mapping = {
        "video": (episode_dir / "final.mp4", "video/mp4", f"{episode_id}.mp4"),
        "thumbnail": (episode_dir / "thumbnail.jpg", "image/jpeg", f"{episode_id}-thumbnail.jpg"),
        "captions": (episode_dir / "audio" / "captions.srt", "text/plain", f"{episode_id}.srt"),
    }
    if kind not in mapping:
        return jsonify({"error": "unknown file kind"}), 404
    path, mimetype, name = mapping[kind]
    if not path.exists():
        return jsonify({"error": "file not ready"}), 404
    return send_file(path, mimetype=mimetype, download_name=name, as_attachment=(kind != "video"))


PAGE = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OTTAM Production Studio</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--text:#f0f6fc;--muted:#8b949e;--accent:#f2cc60;--green:#3fb950;--blue:#58a6ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,-apple-system,Segoe UI,sans-serif}.wrap{max-width:1120px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}h1{font-size:28px;margin:0}.muted{color:var(--muted)}button,.btn{background:#238636;color:white;border:0;border-radius:8px;padding:10px 14px;font-weight:650;cursor:pointer}.secondary{background:#21262d;border:1px solid var(--line)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:14px 0}.topic{display:grid;grid-template-columns:76px 1fr auto;gap:14px;align-items:center}.score{font-size:26px;font-weight:800;color:var(--accent)}input,textarea{width:100%;background:#0d1117;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px}textarea{min-height:86px}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:18px}.thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#090c10;border-radius:10px}.copyrow{display:flex;gap:8px;margin-top:8px}.copyrow textarea{flex:1}.pill{display:inline-block;padding:5px 8px;border-radius:99px;background:#21262d;margin:3px;font-size:13px}.progress{height:9px;background:#21262d;border-radius:99px;overflow:hidden}.progress span{display:block;height:100%;background:var(--green);width:0}.hidden{display:none}@media(max-width:800px){.grid{grid-template-columns:1fr}.topic{grid-template-columns:60px 1fr}.topic button{grid-column:1/-1}.wrap{padding:16px}}
</style></head><body><div class="wrap">
<div class="top"><div><h1>OTTAM Production Studio</h1><div class="muted">On-demand video factory · manual YouTube upload</div></div><button id="newBtn">Generate 5 Topics</button></div>
<div id="topicPanel" class="card"><h2>Topic candidates</h2><div class="copyrow"><input id="topicInstruction" placeholder="Optional: e.g. more relatable everyday psychology"><button class="secondary" id="regenTopics">Regenerate</button></div><div id="topics" class="muted" style="margin-top:14px">Click Generate 5 Topics to start.</div></div>
<div id="jobPanel" class="card hidden"><h2 id="jobTitle">Production</h2><div id="jobStatus" class="muted"></div><div class="progress" style="margin-top:12px"><span id="bar"></span></div></div>
<div id="resultPanel" class="hidden">
<div class="grid"><div class="card"><h2>Video</h2><video id="video" controls style="width:100%;border-radius:10px"></video><div class="copyrow"><a id="downloadVideo" class="btn" href="#">Download MP4</a><a id="downloadSrt" class="btn secondary" href="#">Download Captions</a></div></div>
<div class="card"><h2>Thumbnail</h2><img id="thumbnail" class="thumb"><div class="copyrow"><a id="downloadThumb" class="btn" href="#">Download JPG</a></div><input id="thumbInstruction" placeholder="Small instruction: brighter, more dramatic, less clutter" style="margin-top:12px"><div class="copyrow"><button id="regenThumb">Regenerate Thumbnail</button><button id="usePrevThumb" class="secondary">Use Previous</button></div><div id="thumbStatus" class="muted"></div></div></div>
<div class="card"><h2>YouTube upload text</h2><label>Title</label><div class="copyrow"><input id="title"><button class="secondary copy" data-target="title">Copy</button></div><label>Description</label><div class="copyrow"><textarea id="description"></textarea><button class="secondary copy" data-target="description">Copy</button></div><label>Hashtags</label><div class="copyrow"><textarea id="hashtags"></textarea><button class="secondary copy" data-target="hashtags">Copy</button></div><label>Tags</label><div class="copyrow"><textarea id="tags"></textarea><button class="secondary copy" data-target="tags">Copy</button></div><input id="captionInstruction" placeholder="Small instruction: stronger curiosity title, shorter description" style="margin-top:12px"><div class="copyrow"><button id="regenCaption">Regenerate Caption</button><button id="usePrevCaption" class="secondary">Use Previous</button></div><div id="captionStatus" class="muted"></div></div>
</div></div>
<script>
let currentEpisode=null,currentPackage=null,previousPackage=null,previousThumb=null;
const $=id=>document.getElementById(id); const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function post(url,data={}){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok)throw new Error(await r.text());return r.json()}
async function get(url){let r=await fetch(url);if(!r.ok)throw new Error(await r.text());return r.json()}
async function topics(){ $('topics').textContent='Generating and scoring 5 topics…';let x=await post('/api/topics',{instruction:$('topicInstruction').value});for(;;){await sleep(2500);let s=await get('/api/topics/'+x.request_id);if(s.status==='ready'){renderTopics(s.candidates);return}if(s.status==='failed')throw new Error('Topic generation failed');}}
function renderTopics(items){$('topics').innerHTML='';items.forEach(t=>{let d=document.createElement('div');d.className='card topic';d.innerHTML=`<div class="score">${t.score}</div><div><b>${t.title}</b><div class="muted">${t.reason||t.central_question||''}</div></div><button>Select</button>`;d.querySelector('button').onclick=()=>produce(t);$('topics').appendChild(d)})}
async function produce(topic){let j=await post('/api/produce',{topic});currentEpisode=j.episode_id;$('jobPanel').classList.remove('hidden');$('jobTitle').textContent='Production — '+currentEpisode;$('jobStatus').textContent='Queued';pollJob()}
async function pollJob(){for(;;){await sleep(3500);let j=await get('/api/jobs/'+currentEpisode);$('jobStatus').textContent=(j.episode_state?.stage||j.status)+' · '+(j.episode_state?.status||j.conclusion||'');let map={discover_topic:5,research:12,write_script:22,fact_check:32,script_qa:40,generate_tts:50,plan_visuals:60,generate_images:72,visual_qa:82,assemble_video:91,video_qa:97};$('bar').style.width=(j.ready?100:(map[j.episode_state?.stage]||8))+'%';if(j.ready){showResult(j.package);return}if(j.conclusion&&j.conclusion!=='success')return}}
function showResult(pkg){currentPackage=pkg;$('resultPanel').classList.remove('hidden');$('video').src='/files/'+currentEpisode+'/video';$('downloadVideo').href='/files/'+currentEpisode+'/video';$('downloadSrt').href='/files/'+currentEpisode+'/captions';$('thumbnail').src='/files/'+currentEpisode+'/thumbnail?x='+Date.now();$('downloadThumb').href='/files/'+currentEpisode+'/thumbnail';fillPackage(pkg)}
function fillPackage(p){$('title').value=p.title||'';$('description').value=p.description||'';$('hashtags').value=(p.hashtags||[]).map(x=>'#'+x).join(' ');$('tags').value=(p.tags||[]).join(', ')}
async function regenCaption(){previousPackage=currentPackage;$('captionStatus').textContent='Regenerating one caption package…';await post('/api/jobs/'+currentEpisode+'/regenerate-caption',{instruction:$('captionInstruction').value});for(;;){await sleep(2500);let s=await get('/api/jobs/'+currentEpisode+'/caption-status');if(s.status==='ready'){currentPackage=s.package;fillPackage(s.package);$('captionStatus').textContent='New version ready';return}if(s.status==='failed'){ $('captionStatus').textContent='Regeneration failed';return}}}
async function regenThumb(){previousThumb=$('thumbnail').src;$('thumbStatus').textContent='Generating one new thumbnail…';await post('/api/jobs/'+currentEpisode+'/regenerate-thumbnail',{instruction:$('thumbInstruction').value});for(;;){await sleep(2500);let s=await get('/api/jobs/'+currentEpisode+'/thumbnail-status');if(s.status==='ready'){$('thumbnail').src='/files/'+currentEpisode+'/thumbnail?x='+Date.now();$('thumbStatus').textContent='New version ready';return}if(s.status==='failed'){ $('thumbStatus').textContent='Regeneration failed';return}}}
$('newBtn').onclick=topics;$('regenTopics').onclick=topics;$('regenCaption').onclick=regenCaption;$('regenThumb').onclick=regenThumb;$('usePrevCaption').onclick=()=>{if(previousPackage){let t=currentPackage;currentPackage=previousPackage;previousPackage=t;fillPackage(currentPackage)}};$('usePrevThumb').onclick=()=>{if(previousThumb){let t=$('thumbnail').src;$('thumbnail').src=previousThumb;previousThumb=t}};document.querySelectorAll('.copy').forEach(b=>b.onclick=()=>navigator.clipboard.writeText($(b.dataset.target).value));
</script></body></html>'''


def main() -> None:
    app.run(host=os.getenv("OTTAM_DASHBOARD_HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")), debug=False)


if __name__ == "__main__":
    main()
