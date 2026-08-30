from __future__ import annotations

import json
import re
from pathlib import Path

from .orchestrator import QuarantineEpisode
from .visual_style import PromptSpec, build_magnific_prompt
from .xkiro import XKiroClient


STORYBOARD_SYSTEM = """You are the visual director for OTTAM, a long-form psychology/human-behavior YouTube channel.
OTTAM uses rough hand-drawn stickman explainer visuals. Your job is NOT to rewrite the fixed visual style boilerplate; code will add that automatically. You only design the scene-specific composition.

Verified prior-episode behavior to preserve:
- Visual changes are narration-driven, not one image per sentence and not a fixed scene count.
- A representative previous episode used 124 visuals across about 14m19s: median visual hold about 5s, average about 7s.
- Prefer roughly 4-8s per visual when the narration naturally allows it. Very short beats may be 1-3s; important explanatory compositions may hold 9-15s.
- Reuse established locations/compositions deliberately (e.g. 'return to the Modern Bedroom scene') when the story returns to the same idea; visual continuity is better than inventing a new setting every sentence.
- Keep compositions sparse, instantly readable, childlike, and concept-first.
- Use simple devices when useful: close-ups, split frames, two/three/four-panel grids, simple arrows, maps without labels, silhouettes, flat symbolic objects.
- Text inside generated images is forbidden by default. Only request a single short hand-lettered word when narration genuinely depends on seeing that exact word.
- No logos, watermarks, posters, labels, interfaces, decorative writing, or extra typography.
"""


class StoryboardPlanner:
    def __init__(self, preferred_models: list[str]):
        self.client = XKiroClient()
        self.preferred_models = preferred_models

    def _model(self) -> str:
        return self.client.select_free_model(self.preferred_models).id

    @staticmethod
    def _parse_json(raw: str) -> dict:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise QuarantineEpisode(f"Storyboard model returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("scenes"), list):
            raise QuarantineEpisode("Storyboard JSON must contain a scenes array")
        return payload

    def plan(self, episode_dir: Path) -> None:
        script_path = episode_dir / "script.txt"
        timings_path = episode_dir / "audio" / "sentences.json"
        if not script_path.exists() or not timings_path.exists():
            raise QuarantineEpisode("Storyboard requires script.txt and audio/sentences.json")

        script = script_path.read_text(encoding="utf-8")
        timings = json.loads(timings_path.read_text(encoding="utf-8"))
        model = self._model()
        prompt = f"""Create the complete OTTAM storyboard from the narration and sentence timing data below.

Return STRICT JSON only, with this shape:
{{
  "scenes": [
    {{
      "scene_id": 1,
      "start": 0.0,
      "end": 5.2,
      "narration": "exact narration covered by this scene",
      "scene_description": "only the scene-specific composition; do not include global style boilerplate",
      "allowed_text": null,
      "motion": "push_in",
      "transition": "cut"
    }}
  ]
}}

Rules:
1. Cover the full narration continuously from the first spoken timestamp to the final one. No gaps.
2. Combine adjacent short sentence cues when one visual idea can cover them cleanly.
3. Split a long sentence only if it contains clearly different visual ideas.
4. Target a natural visual rhythm similar to the verified sample: usually 4-8 seconds, median around 5 seconds, but do not force timing mechanically.
5. Reuse named scene contexts when appropriate. If you establish a recurring location, give it a concise stable name in scene_description and later say 'return to the ... scene'.
6. scene_description should specify shot/composition, subject action, simple props/background, and flat color-wash ideas where relevant.
7. allowed_text must normally be null. If exact visible wording is essential, it may contain ONE short word or phrase only.
8. motion must be one of: static, push_in, pull_out, pan_left, pan_right.
9. transition must be one of: cut, dissolve. Prefer cut.
10. Do not include Magnific/global style boilerplate in scene_description; code adds the locked OTTAM style automatically.

SCRIPT:\n{script}\n\nKOKORO SENTENCE TIMINGS:\n{json.dumps(timings, ensure_ascii=False)}"""

        raw = self.client.chat_stream(
            model=model,
            messages=[
                {"role": "system", "content": STORYBOARD_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.45,
            max_tokens=16000,
        )
        payload = self._parse_json(raw)
        scenes = payload["scenes"]
        if not scenes:
            raise QuarantineEpisode("Storyboard contains no scenes")

        previous_end: float | None = None
        for index, scene in enumerate(scenes, 1):
            scene["scene_id"] = index
            try:
                start = float(scene["start"])
                end = float(scene["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise QuarantineEpisode(f"Invalid timing in storyboard scene {index}") from exc
            if end <= start:
                raise QuarantineEpisode(f"Non-positive duration in storyboard scene {index}")
            if previous_end is not None and abs(start - previous_end) > 0.12:
                raise QuarantineEpisode(
                    f"Storyboard timing gap/overlap between scenes {index-1} and {index}: "
                    f"previous_end={previous_end}, start={start}"
                )
            previous_end = end

            description = str(scene.get("scene_description") or "").strip()
            if not description:
                raise QuarantineEpisode(f"Missing scene_description in storyboard scene {index}")
            allowed_text = scene.get("allowed_text")
            if allowed_text is not None:
                allowed_text = str(allowed_text).strip() or None
                if allowed_text and len(allowed_text.split()) > 4:
                    raise QuarantineEpisode(
                        f"Scene {index} requests too much generated text: {allowed_text!r}"
                    )
            scene["allowed_text"] = allowed_text
            scene["magnific_prompt"] = build_magnific_prompt(
                PromptSpec(scene_description=description, allowed_text=allowed_text)
            )

        payload["model"] = model
        payload["style_contract"] = "verified_episode_05_prompt_shell_v1"
        payload["scene_count"] = len(scenes)
        (episode_dir / "storyboard.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def build_storyboard_handler(root: Path, preferred_models: list[str]):
    return lambda episode_id: StoryboardPlanner(preferred_models).plan(root / episode_id)
