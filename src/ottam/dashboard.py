from __future__ import annotations

import io
import json
import os
import re
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

STAGE_LABELS = {
    "research": (1, "Research"),
    "write_script": (2, "Write script"),
    "fact_check": (3, "Fact check"),
    "script_qa": (4, "Script QA"),
    "generate_tts": (5, "Generate narration"),
    "plan_visuals": (6, "Plan visuals"),
    "generate_images": (7, "Generate images"),
    "visual_qa": (8, "Visual QA"),
    "assemble_video": (9, "Assemble video"),
    "video_qa": (10, "Video QA"),
}

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
    response = _gh(
        "GET",
        f"actions/workflows/{workflow}/runs",
        params={"event": "workflow_dispatch", "per_page": 50},
    )
    return response.json().get("workflow_runs") or []


def _find_run(workflow: str, marker: str) -> dict[str, Any] | None:
    for run in _runs(workflow):
        if marker in str(run.get("display_title") or ""):
            return run
    return None


def _episode_from_run(run: dict[str, Any]) -> str | None:
    title = str(run.get("display_title") or "")
    match = re.search(r"OTTAM Production (OTTAM-[^\s—]+)", title)
    return match.group(1) if match else None


def _topic_from_run(run: dict[str, Any]) -> str | None:
    title = str(run.get("display_title") or "")
    return title.split(" — ", 1)[1].strip() if " — " in title else None


def _iso_seconds(start: str | None, end: str | None = None) -> int | None:
    if not start:
        return None
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else datetime.now(timezone.utc)
        return max(0, int((e - s).total_seconds()))
    except ValueError:
        return None


def _run_progress(run: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": run.get("id"),
        "workflow_status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "run_url": run.get("html_url"),
        "started_at": run.get("run_started_at") or run.get("created_at"),
        "elapsed_seconds": _iso_seconds(
            run.get("run_started_at") or run.get("created_at"),
            run.get("updated_at") if run.get("status") == "completed" else None,
        ),
        "current_stage": "Waiting for runner",
        "stage_index": 0,
        "stage_total": 10,
        "stage_elapsed_seconds": None,
        "completed_stages": 0,
        "progress_percent": 2,
        "timeline": [],
    }
    try:
        jobs = _gh("GET", f"actions/runs/{run['id']}/jobs", params={"per_page": 20}).json().get("jobs") or []
    except httpx.HTTPError:
        return result
    if not jobs:
        return result

    job = jobs[0]
    timeline: list[dict[str, Any]] = []
    completed_stages = 0
    current = None
    last_visible = None
    for step in job.get("steps") or []:
        name = str(step.get("name") or "")
        visible = (
            name.startswith("Stage ")
            or name.startswith("Setup ·")
            or name.startswith("Packaging ·")
            or name.startswith("Finalize ·")
        )
        if not visible:
            continue
        duration = _iso_seconds(step.get("started_at"), step.get("completed_at"))
        entry = {
            "name": name,
            "status": step.get("status"),
            "conclusion": step.get("conclusion"),
            "started_at": step.get("started_at"),
            "duration_seconds": duration,
        }
        timeline.append(entry)
        last_visible = entry
        if name.startswith("Stage ") and step.get("status") == "completed" and step.get("conclusion") == "success":
            completed_stages += 1
        if step.get("status") == "in_progress":
            current = entry
        elif step.get("conclusion") == "failure" and current is None:
            current = entry

    if current is None:
        current = last_visible
    if current:
        result["current_stage"] = current["name"]
        result["stage_elapsed_seconds"] = (
            _iso_seconds(current.get("started_at"))
            if current.get("status") == "in_progress"
            else current.get("duration_seconds")
        )
        match = re.match(r"Stage (\d+)/10", current["name"])
        if match:
            result["stage_index"] = int(match.group(1))

    result["completed_stages"] = completed_stages
    result["progress_percent"] = (
        100
        if run.get("conclusion") == "success"
        else max(3, min(99, completed_stages * 9 + (5 if result["stage_index"] > completed_stages else 0)))
    )
    result["timeline"] = timeline
    return result


def _artifact(run_id: int, name: str) -> dict[str, Any] | None:
    artifacts = _gh("GET", f"actions/runs/{run_id}/artifacts", params={"per_page": 100}).json().get("artifacts") or []
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
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"episode_id": episode_id}


def _episode_cache(episode_id: str) -> Path:
    return DATA_ROOT / "episodes" / episode_id


def _hydrate_episode_cache(episode_id: str, run: dict[str, Any]) -> Path:
    cache = _episode_cache(episode_id)
    artifact = _artifact(int(run["id"]), f"ottam-production-{episode_id}")
    if not artifact:
        return cache
    marker = cache / f".artifact-{artifact['id']}"
    if not marker.exists():
        _extract_artifact(int(artifact["id"]), cache)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok", encoding="utf-8")
    return cache


def _episode_state(cache: Path, episode_id: str) -> dict[str, Any] | None:
    state_path = cache / "state" / f"{episode_id}.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _topic_from_cache(cache: Path, episode_id: str) -> dict[str, Any] | None:
    topic_path = cache / "episodes" / episode_id / "topic.json"
    if not topic_path.exists():
        return None
    try:
        payload = json.loads(topic_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    topic = payload.get("winner") if isinstance(payload, dict) else None
    return topic if isinstance(topic, dict) else None


def _failure_payload(state: dict[str, Any] | None, progress: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    stage = str((state or {}).get("stage") or "").strip()
    stage_index, stage_label = STAGE_LABELS.get(stage, (progress.get("stage_index") or 0, stage.replace("_", " ").title() if stage else "Unknown stage"))
    reason = str((state or {}).get("last_error") or "").strip()
    if not reason:
        failed_steps = [x for x in progress.get("timeline") or [] if x.get("conclusion") == "failure"]
        if failed_steps:
            reason = f"GitHub Actions step failed: {failed_steps[-1].get('name')}"
        else:
            reason = f"Production workflow ended with conclusion: {run.get('conclusion') or 'failure'}"
    return {
        "stage": stage,
        "stage_index": stage_index,
        "stage_label": stage_label,
        "reason": reason,
        "run_url": run.get("html_url"),
    }


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
    run = _find_run("dashboard-topics.yml", f"Topics {request_id}")
    if not run:
        return jsonify({"status": "queued"})
    if run.get("status") != "completed":
        return jsonify({"status": run.get("status"), "run_id": run.get("id")})
    if run.get("conclusion") != "success":
        return jsonify({"status": "failed", "conclusion": run.get("conclusion"), "run_id": run.get("id")}), 500
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
    title = str(topic.get("title") or "Selected topic")[:120]
    _dispatch(
        "production.yml",
        {
            "episode_id": episode_id,
            "selected_topic_title": title,
            "selected_topic_json": json.dumps(topic, separators=(",", ":"), ensure_ascii=False),
            "resume_failed": "false",
        },
    )
    job = {"episode_id": episode_id, "topic": topic, "topic_title": title, "status": "queued", "retry_count": 0}
    _save_job(episode_id, job)
    return jsonify(job)


@app.get("/api/current-job")
def current_job():
    runs = _runs("production.yml")
    if not runs:
        return jsonify({"active": False})
    active = next((r for r in runs if r.get("status") != "completed"), None)
    run = active or runs[0]
    episode_id = _episode_from_run(run)
    if not episode_id:
        return jsonify({"active": False})
    snapshot = _production_snapshot(episode_id, run=run)
    snapshot["active"] = run.get("status") != "completed"
    return jsonify(snapshot)


def _production_snapshot(episode_id: str, run: dict[str, Any] | None = None) -> dict[str, Any]:
    run = run or _find_run("production.yml", f"OTTAM Production {episode_id}")
    job = _load_job(episode_id)
    job["episode_id"] = episode_id
    job.pop("failure", None)
    job["can_retry"] = False
    if not run:
        job["status"] = "queued"
        return job

    job["topic_title"] = job.get("topic_title") or _topic_from_run(run)
    job["status"] = run.get("status")
    job["conclusion"] = run.get("conclusion")
    progress = _run_progress(run)
    job["progress"] = progress

    if run.get("status") == "completed":
        cache = _hydrate_episode_cache(episode_id, run)
        state = _episode_state(cache, episode_id)
        if state:
            job["episode_state"] = state
        if not job.get("topic"):
            cached_topic = _topic_from_cache(cache, episode_id)
            if cached_topic:
                job["topic"] = cached_topic

        if run.get("conclusion") == "success":
            package = cache / "episodes" / episode_id / "upload_package.json"
            if package.exists():
                job["package"] = json.loads(package.read_text(encoding="utf-8"))
                job["ready"] = True
                job["status"] = "VIDEO_READY"
        else:
            failure = _failure_payload(state, progress, run)
            job["failure"] = failure
            job["can_retry"] = bool(job.get("topic"))
            job["status"] = "FAILED"
            progress["workflow_status"] = "failed"
            if failure.get("stage_index"):
                progress["stage_index"] = failure["stage_index"]
                progress["progress_percent"] = max(
                    progress.get("progress_percent") or 3,
                    min(95, int(failure["stage_index"]) * 10 - 5),
                )
            progress["current_stage"] = f"Failed at {failure['stage_label']}"

    _save_job(episode_id, job)
    return job


@app.get("/api/jobs/<episode_id>")
def job_status(episode_id: str):
    return jsonify(_production_snapshot(episode_id))


@app.post("/api/jobs/<episode_id>/retry")
def retry_job(episode_id: str):
    job = _production_snapshot(episode_id)
    if job.get("ready"):
        return jsonify({"error": "episode is already VIDEO_READY"}), 409
    if not job.get("conclusion") or job.get("conclusion") == "success":
        return jsonify({"error": "episode is not in a failed state"}), 409

    active = next(
        (
            run
            for run in _runs("production.yml")
            if f"OTTAM Production {episode_id}" in str(run.get("display_title") or "") and run.get("status") != "completed"
        ),
        None,
    )
    if active:
        return jsonify({"error": "a retry is already running", "run_id": active.get("id")}), 409

    topic = job.get("topic")
    if not isinstance(topic, dict):
        return jsonify({"error": "selected topic could not be recovered for retry"}), 409
    title = str(job.get("topic_title") or topic.get("title") or "Selected topic")[:120]
    _dispatch(
        "production.yml",
        {
            "episode_id": episode_id,
            "selected_topic_title": title,
            "selected_topic_json": json.dumps(topic, separators=(",", ":"), ensure_ascii=False),
            "resume_failed": "true",
        },
    )
    job["status"] = "queued"
    job["conclusion"] = None
    job["ready"] = False
    job["failure"] = None
    job["can_retry"] = False
    job["retry_count"] = int(job.get("retry_count") or 0) + 1
    _save_job(episode_id, job)
    return jsonify({"status": "queued", "episode_id": episode_id, "retry_count": job["retry_count"]})


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
    _extract_artifact(int(artifact["id"]), _episode_cache(episode_id))
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
    return jsonify(_refresh_variant(episode_id, "dashboard-thumbnail.yml", f"Thumbnail {episode_id}", f"ottam-thumbnail-{episode_id}"))


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


PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OTTAM Production Studio</title>
<style>:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--text:#f0f6fc;--muted:#8b949e;--accent:#f2cc60;--green:#3fb950;--blue:#58a6ff;--red:#f85149;--redbg:#2b1619}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,-apple-system,Segoe UI,sans-serif}.wrap{max-width:1120px;margin:auto;padding:28px}.top,.row{display:flex;justify-content:space-between;align-items:center;gap:12px}.top{margin-bottom:24px}h1{font-size:28px;margin:0}.muted{color:var(--muted)}button,.btn{background:#238636;color:white;border:0;border-radius:8px;padding:10px 14px;font-weight:650;cursor:pointer;text-decoration:none}.secondary{background:#21262d;border:1px solid var(--line)}.retry{background:#b62324}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:14px 0}.topic{display:grid;grid-template-columns:76px 1fr auto;gap:14px;align-items:center}.score{font-size:26px;font-weight:800;color:var(--accent)}input,textarea{width:100%;background:#0d1117;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px}textarea{min-height:86px}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:18px}.thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#090c10;border-radius:10px}.copyrow{display:flex;gap:8px;margin-top:8px}.progress{height:11px;background:#21262d;border-radius:99px;overflow:hidden;margin:14px 0}.progress span{display:block;height:100%;background:var(--green);width:0;transition:width .4s}.progress.failed span{background:var(--red)}.hidden{display:none}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0}.stat{background:#0d1117;border:1px solid var(--line);border-radius:9px;padding:12px}.stat b{display:block;font-size:18px;margin-top:4px}.timeline{margin-top:14px}.step{display:flex;gap:10px;padding:7px 0;border-bottom:1px solid #21262d}.ok{color:var(--green)}.now{color:var(--accent)}.bad{color:var(--red)}.pending{color:var(--muted)}.failure{margin-top:14px;border:1px solid #6e292e;background:var(--redbg);border-radius:10px;padding:14px}.failure b{color:#ffb3b0}.failureReason{white-space:pre-wrap;line-height:1.45;margin:8px 0 12px}.actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}@media(max-width:800px){.grid,.stats{grid-template-columns:1fr}.topic{grid-template-columns:60px 1fr}.topic button{grid-column:1/-1}.wrap{padding:16px}}</style></head>
<body><div class="wrap"><div class="top"><div><h1>OTTAM Production Studio</h1><div class="muted">On-demand video factory · manual YouTube upload</div></div><button id="newBtn">Generate 5 Topics</button></div>
<div id="topicPanel" class="card"><h2>Topic candidates</h2><div class="copyrow"><input id="topicInstruction" placeholder="Optional: e.g. more relatable everyday psychology"><button class="secondary" id="regenTopics">Regenerate</button></div><div id="topics" class="muted" style="margin-top:14px">Click Generate 5 Topics to start.</div></div>
<div id="jobPanel" class="card hidden"><div class="row"><div><h2 id="jobTitle" style="margin:0">Production</h2><div id="topicTitle" class="muted"></div></div><span id="workflowState" class="muted"></span></div><div id="progressTrack" class="progress"><span id="bar"></span></div><div class="stats"><div class="stat"><span class="muted">Current stage</span><b id="currentStage">—</b></div><div class="stat"><span class="muted">Total elapsed</span><b id="totalElapsed">—</b></div><div class="stat"><span class="muted">Stage elapsed</span><b id="stageElapsed">—</b></div></div><div id="jobStatus" class="muted"></div><div id="failureBox" class="failure hidden"><b id="failureTitle">Production failed</b><div id="failureReason" class="failureReason"></div><div class="actions"><button id="retryProduction" class="retry">Retry from failed stage</button><a id="openRun" class="btn secondary" target="_blank" rel="noopener">Open run details</a><span id="retryStatus" class="muted"></span></div></div><div id="timeline" class="timeline"></div></div>
<div id="resultPanel" class="hidden"><div class="grid"><div class="card"><h2>Video</h2><video id="video" controls style="width:100%;border-radius:10px"></video><div class="copyrow"><a id="downloadVideo" class="btn">Download MP4</a><a id="downloadSrt" class="btn secondary">Download Captions</a></div></div><div class="card"><h2>Thumbnail</h2><img id="thumbnail" class="thumb"><div class="copyrow"><a id="downloadThumb" class="btn">Download JPG</a></div><input id="thumbInstruction" placeholder="Small instruction: brighter, more dramatic, less clutter" style="margin-top:12px"><div class="copyrow"><button id="regenThumb">Regenerate Thumbnail</button><button id="usePrevThumb" class="secondary">Use Previous</button></div><div id="thumbStatus" class="muted"></div></div></div><div class="card"><h2>YouTube upload text</h2><label>Title</label><div class="copyrow"><input id="title"><button class="secondary copy" data-target="title">Copy</button></div><label>Description</label><div class="copyrow"><textarea id="description"></textarea><button class="secondary copy" data-target="description">Copy</button></div><label>Hashtags</label><div class="copyrow"><textarea id="hashtags"></textarea><button class="secondary copy" data-target="hashtags">Copy</button></div><label>Tags</label><div class="copyrow"><textarea id="tags"></textarea><button class="secondary copy" data-target="tags">Copy</button></div><input id="captionInstruction" placeholder="Small instruction: stronger curiosity title, shorter description" style="margin-top:12px"><div class="copyrow"><button id="regenCaption">Regenerate Caption</button><button id="usePrevCaption" class="secondary">Use Previous</button></div><div id="captionStatus" class="muted"></div></div></div></div>
<script>
let currentEpisode=null,currentPackage=null,previousPackage=null,previousThumb=null,lastProgress=null,polling=false;const $=id=>document.getElementById(id),sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function post(url,data={}){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok)throw new Error(await r.text());return r.json()}async function get(url){let r=await fetch(url);if(!r.ok)throw new Error(await r.text());return r.json()}
function fmt(sec){if(sec==null)return '—';sec=Math.max(0,Math.floor(sec));let h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;return h?`${h}h ${m}m ${s}s`:m?`${m}m ${s}s`:`${s}s`}
async function topics(){$('topics').textContent='Generating and scoring 5 topics…';let x=await post('/api/topics',{instruction:$('topicInstruction').value});for(;;){await sleep(2500);let s=await get('/api/topics/'+x.request_id);if(s.status==='ready'){renderTopics(s.candidates);return}if(s.status==='failed')throw new Error('Topic generation failed')}}
function renderTopics(items){$('topics').innerHTML='';items.forEach(t=>{let d=document.createElement('div');d.className='card topic';d.innerHTML=`<div class="score">${t.score}</div><div><b>${t.title}</b><div class="muted">${t.reason||t.central_question||''}</div></div><button>Select</button>`;d.querySelector('button').onclick=()=>produce(t);$('topics').appendChild(d)})}
async function produce(topic){let j=await post('/api/produce',{topic});currentEpisode=j.episode_id;localStorage.setItem('ottam.currentEpisode',currentEpisode);showJob(j);pollJob()}
function showJob(j){$('jobPanel').classList.remove('hidden');$('jobTitle').textContent='Production — '+j.episode_id;$('topicTitle').textContent=j.topic_title||j.topic?.title||'';renderProgress(j)}
function renderProgress(j){let p=j.progress||{};lastProgress=p;let failed=!!(j.failure||j.conclusion&&j.conclusion!=='success');$('workflowState').textContent=j.ready?'ready':failed?'failed':(p.workflow_status||j.status||'queued').replaceAll('_',' ');$('currentStage').textContent=p.current_stage||'Waiting for runner';$('totalElapsed').textContent=fmt(p.elapsed_seconds);$('stageElapsed').textContent=fmt(p.stage_elapsed_seconds);$('bar').style.width=(j.ready?100:(p.progress_percent||3))+'%';$('progressTrack').classList.toggle('failed',failed);$('jobStatus').textContent=j.ready?'Video package ready':failed?`Stopped at stage ${j.failure?.stage_index||p.stage_index||'?'} of ${p.stage_total||10}`:`${p.completed_stages||0}/${p.stage_total||10} production stages completed`;$('failureBox').classList.toggle('hidden',!failed);if(failed){$('failureTitle').textContent=`Failed at ${j.failure?.stage_label||'production stage'}`;$('failureReason').textContent=j.failure?.reason||'The production workflow failed before a detailed reason could be recovered.';$('retryProduction').disabled=!j.can_retry;$('retryProduction').textContent=j.can_retry?'Retry from failed stage':'Retry unavailable';$('openRun').href=j.failure?.run_url||p.run_url||'#'}else{$('retryStatus').textContent=''}$('timeline').innerHTML='';(p.timeline||[]).forEach(s=>{let d=document.createElement('div');d.className='step';let isBad=s.conclusion==='failure',icon=s.status==='in_progress'?'●':s.conclusion==='success'?'✓':isBad?'✕':'○';let cls=s.status==='in_progress'?'now':s.conclusion==='success'?'ok':isBad?'bad':'pending';d.innerHTML=`<span class="${cls}">${icon}</span><span style="flex:1">${s.name}</span><span class="muted">${fmt(s.duration_seconds)}</span>`;$('timeline').appendChild(d)});if((p.timeline||[]).length===0&&p.workflow_status==='in_progress')$('timeline').innerHTML='<div class="muted">This run was started before the detailed-stage update. It will still be recovered here; exact internal stage is unavailable for this already-running legacy run.</div>'}
async function pollJob(){if(polling||!currentEpisode)return;polling=true;try{for(;;){let j=await get('/api/jobs/'+currentEpisode);showJob(j);if(j.ready){showResult(j.package);break}if(j.failure||j.conclusion&&j.conclusion!=='success')break;await sleep(3500)}}finally{polling=false}}
async function retryProduction(){if(!currentEpisode)return;$('retryProduction').disabled=true;$('retryStatus').textContent='Resuming saved checkpoint…';try{await post('/api/jobs/'+currentEpisode+'/retry');$('failureBox').classList.add('hidden');$('progressTrack').classList.remove('failed');$('workflowState').textContent='queued';$('currentStage').textContent='Waiting for runner';$('jobStatus').textContent='Retry queued — completed stages and assets will be reused';$('retryStatus').textContent='';await sleep(1500);pollJob()}catch(e){$('retryProduction').disabled=false;$('retryStatus').textContent='Retry failed to start: '+e.message}}
function showResult(pkg){currentPackage=pkg;$('resultPanel').classList.remove('hidden');$('video').src='/files/'+currentEpisode+'/video';$('downloadVideo').href='/files/'+currentEpisode+'/video';$('downloadSrt').href='/files/'+currentEpisode+'/captions';$('thumbnail').src='/files/'+currentEpisode+'/thumbnail?x='+Date.now();$('downloadThumb').href='/files/'+currentEpisode+'/thumbnail';fillPackage(pkg)}
function fillPackage(p){$('title').value=p?.title||'';$('description').value=p?.description||'';$('hashtags').value=(p?.hashtags||[]).map(x=>'#'+x).join(' ');$('tags').value=(p?.tags||[]).join(', ')}
async function restore(){let saved=localStorage.getItem('ottam.currentEpisode');if(saved){try{let j=await get('/api/jobs/'+saved);if(j.progress||j.ready||j.failure){currentEpisode=saved;showJob(j);if(j.ready)showResult(j.package);else if(!j.failure)pollJob();return}}catch(e){}}try{let j=await get('/api/current-job');if(j.episode_id){currentEpisode=j.episode_id;localStorage.setItem('ottam.currentEpisode',currentEpisode);showJob(j);if(j.ready)showResult(j.package);else if(!j.failure&&(j.active||j.status!=='VIDEO_READY'))pollJob()}}catch(e){console.error(e)}}
setInterval(()=>{if(!lastProgress)return;if(lastProgress.workflow_status==='in_progress'){lastProgress.elapsed_seconds=(lastProgress.elapsed_seconds||0)+1;if(lastProgress.stage_elapsed_seconds!=null)lastProgress.stage_elapsed_seconds+=1;$('totalElapsed').textContent=fmt(lastProgress.elapsed_seconds);$('stageElapsed').textContent=fmt(lastProgress.stage_elapsed_seconds)}},1000);
async function regenCaption(){previousPackage=currentPackage;$('captionStatus').textContent='Regenerating one caption package…';await post('/api/jobs/'+currentEpisode+'/regenerate-caption',{instruction:$('captionInstruction').value});for(;;){await sleep(2500);let s=await get('/api/jobs/'+currentEpisode+'/caption-status');if(s.status==='ready'){currentPackage=s.package;fillPackage(s.package);$('captionStatus').textContent='New version ready';return}if(s.status==='failed'){$('captionStatus').textContent='Regeneration failed';return}}}
async function regenThumb(){previousThumb=$('thumbnail').src;$('thumbStatus').textContent='Generating one new thumbnail…';await post('/api/jobs/'+currentEpisode+'/regenerate-thumbnail',{instruction:$('thumbInstruction').value});for(;;){await sleep(2500);let s=await get('/api/jobs/'+currentEpisode+'/thumbnail-status');if(s.status==='ready'){$('thumbnail').src='/files/'+currentEpisode+'/thumbnail?x='+Date.now();$('thumbStatus').textContent='New version ready';return}if(s.status==='failed'){$('thumbStatus').textContent='Regeneration failed';return}}}
$('newBtn').onclick=topics;$('regenTopics').onclick=topics;$('retryProduction').onclick=retryProduction;$('regenCaption').onclick=regenCaption;$('regenThumb').onclick=regenThumb;$('usePrevCaption').onclick=()=>{if(previousPackage){let t=currentPackage;currentPackage=previousPackage;previousPackage=t;fillPackage(currentPackage)}};$('usePrevThumb').onclick=()=>{if(previousThumb){let t=$('thumbnail').src;$('thumbnail').src=previousThumb;previousThumb=t}};document.querySelectorAll('.copy').forEach(b=>b.onclick=()=>navigator.clipboard.writeText($(b.dataset.target).value));restore();
</script></body></html>'''


def main() -> None:
    app.run(
        host=os.getenv("OTTAM_DASHBOARD_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        debug=False,
    )


if __name__ == "__main__":
    main()
