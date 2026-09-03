from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path

import yaml

from .dynamic_content import build_dynamic_content_handlers, shorten_episode_for_runtime
from .magnific_api import MagnificEpisodeGenerator, build_magnific_generate_handler
from .magnific_manifest import MagnificManifestBuilder
from .orchestrator import Orchestrator, QuarantineEpisode, RecoverableStageError, Stage, StateStore
from .render import build_render_handler
from .storyboard import StoryboardPlanner
from .tts import generate_episode_narration
from .video_qa import build_video_qa_handler
from .visual_qa import VisualQA

RUNTIME_ROOT = Path("runtime/episodes")
PREFERRED_MODELS = ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"]
MAX_RUNTIME_SECONDS = 15 * 60


def _not_wired(stage: Stage):
    def handler(episode_id: str) -> None:
        raise QuarantineEpisode(f"Stage '{stage.value}' is not wired yet for {episode_id}.")
    return handler


def _episode_profile(episode_id: str) -> dict:
    path = Path("config/episodes") / f"{episode_id}.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _tts_handler(episode_id: str) -> None:
    base_url = os.getenv("KOKORO_BASE_URL", "https://theplantrastore--kokoro-tts-web.modal.run")
    voice = os.getenv("KOKORO_VOICE", "am_echo")
    speed = float(os.getenv("KOKORO_SPEED", "1.0"))
    metadata = generate_episode_narration(episode_id, runtime_root=RUNTIME_ROOT, base_url=base_url, voice=voice, speed=speed)
    duration = float(metadata.get("duration_seconds") or 0.0)

    # The channel no longer has a standard runtime. The only universal rule is
    # that a finished narration must remain below 15 minutes. If real TTS timing
    # exceeds that ceiling, shorten the script once based on measured duration,
    # re-fact-check it, and regenerate narration before any visual spending.
    if duration > MAX_RUNTIME_SECONDS:
        shorten_episode_for_runtime(episode_id, RUNTIME_ROOT, PREFERRED_MODELS, duration)
        metadata = generate_episode_narration(
            episode_id,
            runtime_root=RUNTIME_ROOT,
            base_url=base_url,
            voice=voice,
            speed=speed,
        )
        duration = float(metadata.get("duration_seconds") or 0.0)
        if duration > MAX_RUNTIME_SECONDS:
            raise RecoverableStageError(
                f"Narration is still {duration:.1f}s after runtime-aware shortening; maximum allowed is {MAX_RUNTIME_SECONDS}s"
            )

    # Explicit episode profiles can still define their own locked range. This is
    # only for deliberately configured episodes, never a dashboard-wide template.
    target = _episode_profile(episode_id).get("episode", {}).get("target_minutes", {})
    if target:
        minimum = float(target.get("min", 0)) * 60.0
        maximum = min(float(target.get("max", 15)) * 60.0, MAX_RUNTIME_SECONDS)
        if not minimum <= duration <= maximum:
            raise QuarantineEpisode(
                f"Narration duration {duration:.1f}s is outside this episode's explicit {minimum:.0f}-{maximum:.0f}s window"
            )


def _plan_visuals(episode_id: str) -> None:
    episode_dir = RUNTIME_ROOT / episode_id
    StoryboardPlanner(PREFERRED_MODELS).plan(episode_dir)
    MagnificManifestBuilder().build(episode_dir)


def _visual_qa_handler(episode_id: str) -> None:
    episode_dir = RUNTIME_ROOT / episode_id
    qa = VisualQA()
    try:
        qa.run(episode_dir)
    except RecoverableStageError:
        MagnificEpisodeGenerator().generate(episode_dir)
        qa.run(episode_dir)


def build_handlers():
    handlers = {stage: _not_wired(stage) for stage in Stage}
    content = build_dynamic_content_handlers(RUNTIME_ROOT, PREFERRED_MODELS)
    handlers[Stage.DISCOVER_TOPIC] = content["discover_topic"]
    handlers[Stage.RESEARCH] = content["research"]
    handlers[Stage.WRITE_SCRIPT] = content["write_script"]
    handlers[Stage.FACT_CHECK] = content["fact_check"]
    handlers[Stage.SCRIPT_QA] = content["script_qa"]
    handlers[Stage.GENERATE_TTS] = _tts_handler
    handlers[Stage.PLAN_VISUALS] = _plan_visuals
    handlers[Stage.GENERATE_IMAGES] = build_magnific_generate_handler(RUNTIME_ROOT)
    handlers[Stage.VISUAL_QA] = _visual_qa_handler
    handlers[Stage.ASSEMBLE_VIDEO] = build_render_handler(RUNTIME_ROOT)
    handlers[Stage.VIDEO_QA] = build_video_qa_handler(RUNTIME_ROOT)
    return handlers


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    episode_id = os.getenv("OTTAM_EPISODE_ID") or datetime.now(timezone.utc).strftime("EP-%Y%m%d-%H%M%S")
    stop_after_raw = os.getenv("OTTAM_STOP_AFTER_STAGE", "").strip()
    stop_after = Stage(stop_after_raw) if stop_after_raw else None
    store = StateStore(Path("runtime/state"))
    orchestrator = Orchestrator(store=store, handlers=build_handlers())
    state = orchestrator.run(episode_id, stop_after=stop_after)
    logging.getLogger("ottam").info("episode=%s stage=%s status=%s quarantined=%s", state.episode_id, state.stage.value, state.status, state.quarantined)
    if state.quarantined:
        raise SystemExit(state.last_error or "Episode quarantined")


if __name__ == "__main__":
    main()
