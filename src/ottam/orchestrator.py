from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
import json
import logging
from typing import Callable

log = logging.getLogger("ottam.orchestrator")


class Stage(str, Enum):
    DISCOVER_TOPIC = "discover_topic"
    RESEARCH = "research"
    WRITE_SCRIPT = "write_script"
    FACT_CHECK = "fact_check"
    SCRIPT_QA = "script_qa"
    GENERATE_TTS = "generate_tts"
    PLAN_VISUALS = "plan_visuals"
    GENERATE_IMAGES = "generate_images"
    VISUAL_QA = "visual_qa"
    ASSEMBLE_VIDEO = "assemble_video"
    VIDEO_QA = "video_qa"


STAGE_ORDER = list(Stage)


@dataclass
class EpisodeState:
    episode_id: str
    stage: Stage = Stage.DISCOVER_TOPIC
    status: str = "PENDING"
    attempts: int = 0
    last_error: str | None = None
    quarantined: bool = False


class StateStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, episode_id: str) -> Path:
        return self.root / f"{episode_id}.json"

    def load(self, episode_id: str) -> EpisodeState:
        path = self.path_for(episode_id)
        if not path.exists():
            state = EpisodeState(episode_id=episode_id)
            self.save(state)
            return state
        data = json.loads(path.read_text())
        data["stage"] = Stage(data["stage"])
        return EpisodeState(**data)

    def save(self, state: EpisodeState) -> None:
        payload = asdict(state)
        payload["stage"] = state.stage.value
        self.path_for(state.episode_id).write_text(json.dumps(payload, indent=2))


class RecoverableStageError(RuntimeError):
    pass


class QuarantineEpisode(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, store: StateStore, handlers: dict[Stage, Callable[[str], None]], max_stage_attempts: int = 4):
        self.store = store
        self.handlers = handlers
        self.max_stage_attempts = max_stage_attempts

    def run(self, episode_id: str, stop_after: Stage | None = None) -> EpisodeState:
        state = self.store.load(episode_id)
        if state.quarantined or state.status == "VIDEO_READY":
            return state

        start_index = STAGE_ORDER.index(state.stage)
        for stage in STAGE_ORDER[start_index:]:
            state.stage = stage
            state.status = "RUNNING"
            self.store.save(state)
            try:
                self._run_stage_with_recovery(state)
            except QuarantineEpisode as exc:
                state.status = "QUARANTINED"
                state.quarantined = True
                state.last_error = str(exc)
                self.store.save(state)
                log.error("Episode %s quarantined at %s: %s", episode_id, stage.value, exc)
                return state

            state.status = "COMPLETE"
            state.attempts = 0
            state.last_error = None
            next_index = STAGE_ORDER.index(stage) + 1
            if next_index < len(STAGE_ORDER):
                state.stage = STAGE_ORDER[next_index]
            else:
                state.status = "VIDEO_READY"
            self.store.save(state)

            if stop_after == stage:
                return state

        state.status = "VIDEO_READY"
        self.store.save(state)
        return state

    def _run_stage_with_recovery(self, state: EpisodeState) -> None:
        handler = self.handlers.get(state.stage)
        if handler is None:
            raise QuarantineEpisode(f"No handler registered for stage {state.stage.value}")
        for attempt in range(1, self.max_stage_attempts + 1):
            state.attempts = attempt
            self.store.save(state)
            try:
                handler(state.episode_id)
                return
            except RecoverableStageError as exc:
                state.last_error = str(exc)
                self.store.save(state)
                log.warning("Recoverable failure episode=%s stage=%s attempt=%s/%s error=%s", state.episode_id, state.stage.value, attempt, self.max_stage_attempts, exc)
        raise QuarantineEpisode(f"Stage {state.stage.value} exhausted {self.max_stage_attempts} recovery attempts")
