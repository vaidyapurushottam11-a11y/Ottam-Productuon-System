from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path

from .orchestrator import Orchestrator, QuarantineEpisode, Stage, StateStore
from .tts import generate_episode_narration


def _not_wired(stage: Stage):
    def handler(episode_id: str) -> None:
        raise QuarantineEpisode(
            f"Stage '{stage.value}' is scaffolded but provider integration is not wired yet for {episode_id}."
        )
    return handler


def _tts_handler(episode_id: str) -> None:
    base_url = os.getenv(
        "KOKORO_BASE_URL",
        "https://theplantrastore--kokoro-tts-web.modal.run",
    )
    voice = os.getenv("KOKORO_VOICE", "am_echo")
    speed = float(os.getenv("KOKORO_SPEED", "1.0"))
    generate_episode_narration(
        episode_id,
        base_url=base_url,
        voice=voice,
        speed=speed,
    )


def build_handlers():
    handlers = {stage: _not_wired(stage) for stage in Stage}
    handlers[Stage.GENERATE_TTS] = _tts_handler
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
