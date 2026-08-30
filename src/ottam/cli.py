from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path

from .content_engine import build_content_handlers
from .magnific_manifest import MagnificManifestBuilder
from .orchestrator import Orchestrator, QuarantineEpisode, Stage, StateStore
from .render import build_render_handler
from .storyboard import StoryboardPlanner
from .tts import generate_episode_narration
from .video_qa import build_video_qa_handler

RUNTIME_ROOT = Path("runtime/episodes")
PREFERRED_MODELS = [
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash",
]


def _not_wired(stage: Stage):
    def handler(episode_id: str) -> None:
        raise QuarantineEpisode(
            f"Stage '{stage.value}' is not wired yet for {episode_id}."
        )
    return handler


def _tts_handler(episode_id: str) -> None:
    base_url = os.getenv("KOKORO_BASE_URL", "https://theplantrastore--kokoro-tts-web.modal.run")
    voice = os.getenv("KOKORO_VOICE", "am_echo")
    speed = float(os.getenv("KOKORO_SPEED", "1.0"))
    generate_episode_narration(
        episode_id,
        runtime_root=RUNTIME_ROOT,
        base_url=base_url,
        voice=voice,
        speed=speed,
    )


def _plan_visuals(episode_id: str) -> None:
    episode_dir = RUNTIME_ROOT / episode_id
    StoryboardPlanner(PREFERRED_MODELS).plan(episode_dir)
    MagnificManifestBuilder().build(episode_dir)


def build_handlers():
    handlers = {stage: _not_wired(stage) for stage in Stage}

    content = build_content_handlers(RUNTIME_ROOT, PREFERRED_MODELS)
    handlers[Stage.DISCOVER_TOPIC] = content["discover_topic"]
    handlers[Stage.RESEARCH] = content["research"]
    handlers[Stage.FACT_CHECK] = content["fact_check"]
    handlers[Stage.WRITE_SCRIPT] = content["write_script"]
    handlers[Stage.SCRIPT_QA] = content["script_qa"]
    handlers[Stage.GENERATE_TTS] = _tts_handler
    handlers[Stage.PLAN_VISUALS] = _plan_visuals
    # GENERATE_IMAGES and VISUAL_QA remain blocked until the Magnific transport is verified.
    handlers[Stage.ASSEMBLE_VIDEO] = build_render_handler(RUNTIME_ROOT)
    handlers[Stage.VIDEO_QA] = build_video_qa_handler(RUNTIME_ROOT)
    return handlers


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    episode_id = os.getenv("OTTAM_EPISODE_ID") or datetime.now(timezone.utc).strftime("EP-%Y%m%d-%H%M%S")
    store = StateStore(Path("runtime/state"))
    orchestrator = Orchestrator(store=store, handlers=build_handlers())
    state = orchestrator.run(episode_id)
    logging.getLogger("ottam").info(
        "episode=%s stage=%s status=%s quarantined=%s",
        state.episode_id,
        state.stage.value,
        state.status,
        state.quarantined,
    )


if __name__ == "__main__":
    main()
