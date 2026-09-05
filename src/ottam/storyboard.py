from __future__ import annotations

import json
import re
from pathlib import Path

from .orchestrator import QuarantineEpisode, RecoverableStageError
from .visual_style import PromptSpec, build_magnific_prompt
from .xkiro import XKiroClient


STORYBOARD_SYSTEM = """You are the visual director for OTTAM, a long-form psychology/human-behavior YouTube channel.
OTTAM uses rough hand-drawn stickman explainer visuals. Your job is NOT to rewrite the fixed visual style boilerplate; code will add that automatically. You only design the scene-specific composition.

Verified prior-episode behavior to preserve:
- Visual changes are narration-driven, not one image per sentence and not a fixed scene count.
- A representative previous episode used 124 visuals across about 14m19s: median visual hold about 5s, average about 7s.
- Prefer roughly 4-8s per visual when the narration naturally allows it. Very short beats may be 1-3s; important explanatory compositions may hold 9-15s.
- Reuse established locations/compositions deliberately when the story returns to the same idea; visual continuity is better than inventing a new setting every sentence.
- Keep compositions sparse, instantly readable, childlike, and concept-first.
- Use simple devices when useful: close-ups, split frames, two/three/four-panel grids, simple arrows, maps without labels, silhouettes, flat symbolic objects.
- Text inside generated images is forbidden by default. Only request a single short hand-lettered word when narration genuinely depends on seeing that exact word.
- No logos, watermarks, posters, labels, interfaces, decorative writing, or extra typography.

OPENING RETENTION CONTRACT:
- The first 30 seconds are the visual hook and must be the most immediately engaging sequence in the episode.
- Opening visuals must feel personally recognizable, emotionally readable, and curiosity-driving at a glance.
- Prefer large expressive characters, close or medium framing, a concrete everyday situation, clear visual tension, and bold foreground/background separation.
- Avoid weak generic opening images such as a tiny centered stick figure, distant landscapes, passive talking-head poses, rows of abstract cards, generic choice boards, empty rooms, or explanatory diagrams with no emotional action.
- Every opening image should create a question, tension, recognition, surprise, or consequence that makes the next image feel necessary.
- Vary composition across the opening sequence: for example close-up reaction -> concrete dilemma -> surprising visual consequence. Do not repeat the same framing.
- Use brighter or stronger color separation in the opening when it improves phone-size readability. Do not default to pale low-contrast backgrounds.
"""


class StoryboardPlanner:
    MAX_AUTO_TIMING_ADJUSTMENT = 2.0
    HOOK_WINDOW_SECONDS = 30.0

    def __init__(self, preferred_models: list[str]):
        self.client = XKiroClient()
        self.preferred_models = preferred_models

    def _model(self) -> str:
        return self.client.select_free_model(self.preferred_models).id

    @staticmethod
    def _parse_object(raw: str) -> dict:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RecoverableStageError(f"Storyboard model returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RecoverableStageError("Storyboard model must return a JSON object")
        return payload

    @classmethod
    def _parse_json(cls, raw: str) -> dict:
        payload = cls._parse_object(raw)
        if not isinstance(payload.get("scenes"), list):
            raise RecoverableStageError("Storyboard JSON must contain a scenes array")
        return payload

    def _opening_scenes(self, scenes: list[dict]) -> list[dict]:
        return [scene for scene in scenes if float(scene.get("start") or 0.0) < self.HOOK_WINDOW_SECONDS]

    def _audit_opening(self, scenes: list[dict]) -> dict:
        opening = self._opening_scenes(scenes)
        model = self._model()
        prompt = f"""Audit the first 30 seconds of an OTTAM storyboard for YouTube retention.
Return strict JSON with numeric 0-100 scores for: relatability, curiosity, emotional_readability, phone_size_readability, visual_variety, visual_tension, plus blocking_issues and revision_instructions.

A strong opening uses concrete recognizable human situations, large expressive subjects, strong composition and visual change. A weak opening uses tiny distant characters, generic diagrams, passive scenes, repetitive framing, pale low-contrast compositions, abstract grids, or images that merely illustrate nouns without creating curiosity.

A blocking issue means the opening is clearly too weak/generic to publish as the first 30 seconds, not merely a subjective style preference.

OPENING SCENES:\n{json.dumps(opening, ensure_ascii=False)}"""
        raw = self.client.chat_stream(
            model=model,
            messages=[{"role": "system", "content": STORYBOARD_SYSTEM}, {"role": "user", "content": prompt}],
            temperature=0.12,
            max_tokens=3000,
        )
        report = self._parse_object(raw)
        score_keys = (
            "relatability",
            "curiosity",
            "emotional_readability",
            "phone_size_readability",
            "visual_variety",
            "visual_tension",
        )
        scores = {key: int(report.get(key, 0)) for key in score_keys}
        values = list(scores.values())
        report["scores"] = scores
        report["average_score"] = round(sum(values) / len(values), 2) if values else 0.0
        report["minimum_score"] = min(values, default=0)
        report["passed"] = not (report.get("blocking_issues") or []) and report["average_score"] >= 84 and report["minimum_score"] >= 76
        return report

    def _repair_opening(self, scenes: list[dict], report: dict) -> list[dict]:
        opening = self._opening_scenes(scenes)
        model = self._model()
        prompt = f"""Rewrite ONLY the scene_description fields for these first-30-second OTTAM storyboard scenes.
Keep every scene_id, start, end, narration, allowed_text, motion and transition unchanged.

Make the opening substantially more relatable and visually magnetic:
- large expressive stickman faces/body language where appropriate
- recognizable everyday setting/action tied directly to the narration
- clear visual tension or consequence
- bold foreground/background separation and phone-size readability
- varied framing across scenes
- each image should make the next beat feel necessary

Do NOT add decorative text or labels. Do NOT turn scenes into generic diagrams or rows of choices. Avoid tiny distant characters and empty backgrounds.
Return strict JSON with exactly one key: scenes. Include the same scenes with improved scene_description only.

QA REPORT:\n{json.dumps(report, ensure_ascii=False)}

OPENING SCENES:\n{json.dumps(opening, ensure_ascii=False)}"""
        raw = self.client.chat_stream(
            model=model,
            messages=[{"role": "system", "content": STORYBOARD_SYSTEM}, {"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=5000,
        )
        payload = self._parse_json(raw)
        revised = payload.get("scenes") or []
        if len(revised) != len(opening):
            raise RecoverableStageError("Opening visual repair changed the number of hook scenes")
        by_id = {int(scene["scene_id"]): scene for scene in revised}
        for scene in scenes:
            sid = int(scene["scene_id"])
            if sid in by_id:
                new_description = str(by_id[sid].get("scene_description") or "").strip()
                if not new_description:
                    raise RecoverableStageError(f"Opening visual repair returned empty description for scene {sid}")
                scene["scene_description"] = new_description
        return scenes

    def _normalize_scenes(self, scenes: list[dict]) -> list[dict]:
        previous_end: float | None = None
        for index, scene in enumerate(scenes, 1):
            scene["scene_id"] = index
            try:
                start = float(scene["start"])
                end = float(scene["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RecoverableStageError(f"Invalid timing in storyboard scene {index}") from exc
            if end <= start:
                raise RecoverableStageError(f"Non-positive duration in storyboard scene {index}")
            if previous_end is not None:
                delta = start - previous_end
                if abs(delta) <= self.MAX_AUTO_TIMING_ADJUSTMENT:
                    start = previous_end
                else:
                    raise RecoverableStageError(
                        f"Storyboard has a material timing discontinuity between scenes {index-1} and {index}: previous_end={previous_end}, start={start}, delta={delta:.3f}s"
                    )
            scene["start"] = round(start, 3)
            scene["end"] = round(end, 3)
            previous_end = end
            description = str(scene.get("scene_description") or "").strip()
            if not description:
                raise RecoverableStageError(f"Missing scene_description in storyboard scene {index}")
            scene["scene_description"] = description
            allowed_text = scene.get("allowed_text")
            if allowed_text is not None:
                allowed_text = str(allowed_text).strip() or None
                if allowed_text and len(allowed_text.split()) > 4:
                    allowed_text = None
            scene["allowed_text"] = allowed_text
        return scenes

    def _attach_prompts(self, scenes: list[dict]) -> None:
        for scene in scenes:
            description = str(scene["scene_description"])
            if float(scene.get("start") or 0.0) < self.HOOK_WINDOW_SECONDS:
                description = (
                    "OPENING HOOK FRAME — prioritize immediate emotional readability, a large expressive subject, strong phone-size composition, clear relatable action, bold contrast, and visual curiosity. "
                    + description
                )
            scene["magnific_prompt"] = build_magnific_prompt(
                PromptSpec(scene_description=description, allowed_text=scene.get("allowed_text"))
            )

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
4. Target a natural visual rhythm, usually 4-8 seconds, but do not force timing mechanically.
5. Reuse named scene contexts when appropriate.
6. scene_description should specify shot/composition, subject action, simple props/background, and flat color-wash ideas where relevant.
7. allowed_text must normally be null. If exact visible wording is essential, it may contain ONE short word or phrase only.
8. motion must be one of: static, push_in, pull_out, pan_left, pan_right.
9. transition must be one of: cut, dissolve. Prefer cut.
10. Do not include Magnific/global style boilerplate in scene_description; code adds the locked OTTAM style automatically.
11. FIRST 30 SECONDS: treat these scenes as premium hook frames. Start with a recognizable human situation and strong emotional action. Use large readable subjects and varied close/medium compositions. Avoid tiny centered characters, passive diagrams, choice-card grids, distant landscapes and low-contrast generic scenes.

SCRIPT:\n{script}\n\nKOKORO SENTENCE TIMINGS:\n{json.dumps(timings, ensure_ascii=False)}"""

        raw = self.client.chat_stream(
            model=model,
            messages=[{"role": "system", "content": STORYBOARD_SYSTEM}, {"role": "user", "content": prompt}],
            temperature=0.45,
            max_tokens=16000,
        )
        payload = self._parse_json(raw)
        scenes = payload["scenes"]
        if not scenes:
            raise RecoverableStageError("Storyboard contains no scenes")
        scenes = self._normalize_scenes(scenes)

        first_report = self._audit_opening(scenes)
        history = [first_report]
        if not first_report.get("passed"):
            scenes = self._repair_opening(scenes, first_report)
            second_report = self._audit_opening(scenes)
            history.append(second_report)
            if not second_report.get("passed"):
                (episode_dir / "hook_visual_qa.json").write_text(
                    json.dumps({"passed": False, "history": history}, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                raise RecoverableStageError(
                    f"Opening storyboard remains too weak after focused repair: average={second_report.get('average_score')}, min={second_report.get('minimum_score')}"
                )

        self._attach_prompts(scenes)
        payload["scenes"] = scenes
        payload["model"] = model
        payload["style_contract"] = "verified_episode_05_prompt_shell_v1"
        payload["scene_count"] = len(scenes)
        payload["opening_hook_seconds"] = self.HOOK_WINDOW_SECONDS
        payload["opening_visual_qa"] = {"passed": True, "history": history}
        (episode_dir / "hook_visual_qa.json").write_text(
            json.dumps(payload["opening_visual_qa"], indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (episode_dir / "storyboard.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def build_storyboard_handler(root: Path, preferred_models: list[str]):
    return lambda episode_id: StoryboardPlanner(preferred_models).plan(root / episode_id)
