from __future__ import annotations

import json
from pathlib import Path

from .orchestrator import QuarantineEpisode
from .xkiro import XKiroClient


STORYBOARD_SYSTEM = """You create visual plans for OTTAM, a long-form psychology/human-behavior YouTube channel using simple stickman animation.
Visuals must be easy to understand instantly, compositionally clean, and tightly matched to narration. Do not invent on-screen text unless explicitly required. Keep a consistent visual language across the episode."""


class StoryboardPlanner:
    def __init__(self, preferred_models: list[str]):
        self.client = XKiroClient()
        self.preferred_models = preferred_models

    def _model(self) -> str:
        return self.client.select_free_model(self.preferred_models).id

    def plan(self, episode_dir: Path) -> None:
        script_path = episode_dir / "script.txt"
        timings_path = episode_dir / "sentences.json"
        if not script_path.exists() or not timings_path.exists():
            raise QuarantineEpisode("Storyboard requires script.txt and sentences.json")
        script = script_path.read_text()
        timings = json.loads(timings_path.read_text())
        model = self._model()
        prompt = f"""Create the complete OTTAM storyboard from the narration and sentence timing data below.
You may combine adjacent short sentences into one scene when they share one visual idea; split long sentences only when needed. Scene count must be driven by narration, never a fixed number.
Return strict JSON with key scenes. Each scene must contain:
scene_id, start, end, narration, visual_concept, magnific_prompt, motion, transition.
Magnific prompts must request a clean 16:9 OTTAM-style stickman scene, no text, no logos, no watermark, with a clear focal action.
Allowed motion values: static, push_in, pull_out, pan_left, pan_right.
Allowed transition values: cut, dissolve.

SCRIPT:\n{script}\n\nTIMINGS:\n{json.dumps(timings, ensure_ascii=False)}"""
        out = self.client.chat_stream(
            model=model,
            messages=[{"role":"system","content":STORYBOARD_SYSTEM},{"role":"user","content":prompt}],
            temperature=0.55,
            max_tokens=14000,
        )
        (episode_dir / "storyboard.json").write_text(out)


def build_storyboard_handler(root: Path, preferred_models: list[str]):
    return lambda episode_id: StoryboardPlanner(preferred_models).plan(root / episode_id)
