from __future__ import annotations

import hashlib
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
    """Plan a long-form storyboard in small checkpointed timing chunks.

    The old implementation requested an entire 6-15 minute storyboard as one
    very large streamed JSON response. Long xKiro streams can be interrupted or
    truncated, forcing the whole stage to restart. Chunking keeps each response
    small and lets stage retries reuse every chunk that already validated.
    """

    MAX_AUTO_TIMING_ADJUSTMENT = 2.0
    HOOK_WINDOW_SECONDS = 30.0
    CHUNK_TARGET_SECONDS = 75.0
    CHUNK_MAX_SENTENCES = 28
    CHUNK_ATTEMPTS = 2
    CHUNK_MAX_TOKENS = 5000
    CHUNK_FORMAT_VERSION = "sentence-ranges-v1"

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
            payload = json.loads(text, strict=False)
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

    @staticmethod
    def _cue_number(cue: dict) -> int:
        try:
            return int(cue["index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoverableStageError("Kokoro sentence timing is missing a numeric index") from exc

    @staticmethod
    def _cue_time(cue: dict, key: str) -> float:
        try:
            return float(cue[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoverableStageError(f"Kokoro sentence timing is missing numeric {key}") from exc

    def _timing_chunks(self, timings: list[dict]) -> list[list[dict]]:
        if not isinstance(timings, list) or not timings:
            raise QuarantineEpisode("Storyboard requires non-empty audio/sentences.json timings")
        chunks: list[list[dict]] = []
        current: list[dict] = []
        for cue in timings:
            self._cue_number(cue)
            self._cue_time(cue, "start")
            self._cue_time(cue, "end")
            if current:
                span = self._cue_time(cue, "end") - self._cue_time(current[0], "start")
                if len(current) >= self.CHUNK_MAX_SENTENCES or span > self.CHUNK_TARGET_SECONDS:
                    chunks.append(current)
                    current = []
            current.append(cue)
        if current:
            chunks.append(current)
        return chunks

    def _chunk_fingerprint(self, cues: list[dict], chunk_end: float) -> str:
        compact = {
            "format": self.CHUNK_FORMAT_VERSION,
            "end": round(chunk_end, 3),
            "cues": [
                {
                    "index": self._cue_number(c),
                    "start": round(self._cue_time(c, "start"), 3),
                    "end": round(self._cue_time(c, "end"), 3),
                    "text": str(c.get("text") or ""),
                }
                for c in cues
            ],
        }
        return hashlib.sha256(json.dumps(compact, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _chunk_cache_path(episode_dir: Path, chunk_index: int) -> Path:
        return episode_dir / "storyboard_chunks" / f"chunk_{chunk_index:03d}.json"

    def _load_cached_chunk(self, episode_dir: Path, chunk_index: int, fingerprint: str) -> list[dict] | None:
        path = self._chunk_cache_path(episode_dir, chunk_index)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("fingerprint") != fingerprint or not isinstance(payload.get("scenes"), list):
            return None
        return payload["scenes"]

    def _save_cached_chunk(self, episode_dir: Path, chunk_index: int, fingerprint: str, scenes: list[dict]) -> None:
        path = self._chunk_cache_path(episode_dir, chunk_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"format": self.CHUNK_FORMAT_VERSION, "fingerprint": fingerprint, "scenes": scenes},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _materialize_chunk_scenes(self, raw_scenes: list[dict], cues: list[dict], chunk_end: float) -> list[dict]:
        if not raw_scenes:
            raise RecoverableStageError("Storyboard chunk contains no scenes")
        first_index = self._cue_number(cues[0])
        last_index = self._cue_number(cues[-1])
        cue_by_index = {self._cue_number(cue): cue for cue in cues}
        normalized_ranges: list[tuple[int, int, dict]] = []
        expected = first_index
        for pos, scene in enumerate(raw_scenes, 1):
            try:
                first = int(scene["first_sentence_index"])
                last = int(scene["last_sentence_index"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RecoverableStageError(f"Storyboard chunk scene {pos} is missing sentence range indexes") from exc
            if first != expected:
                raise RecoverableStageError(
                    f"Storyboard chunk sentence ranges are not contiguous: expected {expected}, got {first}"
                )
            if last < first or last > last_index:
                raise RecoverableStageError(f"Storyboard chunk scene {pos} has invalid sentence range {first}-{last}")
            if first not in cue_by_index or last not in cue_by_index:
                raise RecoverableStageError(f"Storyboard chunk scene {pos} references a sentence outside this chunk")
            normalized_ranges.append((first, last, scene))
            expected = last + 1
        if expected != last_index + 1:
            raise RecoverableStageError(
                f"Storyboard chunk did not cover all sentences; expected through {last_index}, stopped at {expected - 1}"
            )

        scenes: list[dict] = []
        for pos, (first, last, scene) in enumerate(normalized_ranges):
            start = self._cue_time(cue_by_index[first], "start")
            if pos + 1 < len(normalized_ranges):
                next_first = normalized_ranges[pos + 1][0]
                end = self._cue_time(cue_by_index[next_first], "start")
            else:
                end = float(chunk_end)
            if end <= start:
                raise RecoverableStageError(f"Storyboard chunk produced non-positive scene duration at sentence {first}")
            description = str(scene.get("scene_description") or "").strip()
            if not description:
                raise RecoverableStageError(f"Storyboard chunk returned empty scene_description at sentence {first}")
            allowed_text = scene.get("allowed_text")
            if allowed_text is not None:
                allowed_text = str(allowed_text).strip() or None
                if allowed_text and len(allowed_text.split()) > 4:
                    allowed_text = None
            motion = str(scene.get("motion") or "static").strip()
            if motion not in {"static", "push_in", "pull_out", "pan_left", "pan_right"}:
                motion = "static"
            transition = str(scene.get("transition") or "cut").strip()
            if transition not in {"cut", "dissolve"}:
                transition = "cut"
            narration = " ".join(str(cue_by_index[i].get("text") or "").strip() for i in range(first, last + 1)).strip()
            scenes.append(
                {
                    "scene_id": len(scenes) + 1,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "narration": narration,
                    "scene_description": description,
                    "allowed_text": allowed_text,
                    "motion": motion,
                    "transition": transition,
                    "first_sentence_index": first,
                    "last_sentence_index": last,
                }
            )
        return scenes

    def _generate_chunk(
        self,
        *,
        model: str,
        cues: list[dict],
        chunk_index: int,
        chunk_count: int,
        chunk_end: float,
    ) -> list[dict]:
        first_index = self._cue_number(cues[0])
        last_index = self._cue_number(cues[-1])
        timing_payload = [
            {
                "index": self._cue_number(cue),
                "start": round(self._cue_time(cue, "start"), 3),
                "end": round(self._cue_time(cue, "end"), 3),
                "text": str(cue.get("text") or ""),
            }
            for cue in cues
        ]
        opening_note = (
            "This chunk contains the opening hook. Make its first 30 seconds especially relatable, emotionally active, "
            "curiosity-driving and phone-readable. "
            if chunk_index == 1
            else ""
        )
        prompt = f"""Create storyboard scene GROUPINGS for chunk {chunk_index} of {chunk_count} of one OTTAM episode.
{opening_note}
Return STRICT JSON only with this shape:
{{
  "scenes": [
    {{
      "first_sentence_index": {first_index},
      "last_sentence_index": {first_index + 1},
      "scene_description": "scene-specific shot/composition/action only",
      "allowed_text": null,
      "motion": "push_in",
      "transition": "cut"
    }}
  ]
}}

Rules:
1. Cover every sentence index from {first_index} through {last_index} exactly once, in order, using contiguous non-overlapping ranges. No skipped or repeated sentence indexes.
2. Do NOT output start/end times and do NOT repeat narration text. Code calculates exact timing and narration deterministically.
3. Group adjacent sentence cues when one visual idea can cover them naturally. Prefer roughly 4-8 seconds per visual from the supplied cue timings, but allow 1-3 second beats or 9-15 second holds where the story needs them.
4. scene_description must specify a concrete shot/composition, recurring stickman action/expression, simple props/background and useful color separation. Preserve the same recurring OTTAM character identity; vary pose/expression/framing, not anatomy/design.
5. allowed_text should normally be null. If exact visible wording is essential, use only one short word or phrase.
6. motion must be one of static, push_in, pull_out, pan_left, pan_right. transition must be cut or dissolve; prefer cut.
7. Do not include global style boilerplate; code adds the locked OTTAM style.
8. Avoid generic diagrams or passive filler. Every scene should add recognition, tension, explanation, contrast or payoff.
9. The last scene must include sentence {last_index}. The chunk visual coverage will end at {chunk_end:.3f}s.

KOKORO CUES:\n{json.dumps(timing_payload, ensure_ascii=False)}"""
        last_error: RecoverableStageError | None = None
        for attempt in range(1, self.CHUNK_ATTEMPTS + 1):
            try:
                raw = self.client.chat_stream(
                    model=model,
                    messages=[{"role": "system", "content": STORYBOARD_SYSTEM}, {"role": "user", "content": prompt}],
                    temperature=0.4 if attempt == 1 else 0.2,
                    max_tokens=self.CHUNK_MAX_TOKENS,
                )
                payload = self._parse_json(raw)
                return self._materialize_chunk_scenes(payload["scenes"], cues, chunk_end)
            except RecoverableStageError as exc:
                last_error = exc
        raise RecoverableStageError(
            f"Storyboard chunk {chunk_index}/{chunk_count} failed after {self.CHUNK_ATTEMPTS} attempts: {last_error}"
        )

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

        timings = json.loads(timings_path.read_text(encoding="utf-8"))
        chunks = self._timing_chunks(timings)
        model = self._model()
        all_scenes: list[dict] = []
        for index, cues in enumerate(chunks, 1):
            if index < len(chunks):
                chunk_end = self._cue_time(chunks[index][0], "start")
            else:
                chunk_end = self._cue_time(cues[-1], "end")
            fingerprint = self._chunk_fingerprint(cues, chunk_end)
            scenes = self._load_cached_chunk(episode_dir, index, fingerprint)
            if scenes is None:
                scenes = self._generate_chunk(
                    model=model,
                    cues=cues,
                    chunk_index=index,
                    chunk_count=len(chunks),
                    chunk_end=chunk_end,
                )
                self._save_cached_chunk(episode_dir, index, fingerprint, scenes)
            all_scenes.extend(scenes)

        scenes = self._normalize_scenes(all_scenes)
        if not scenes:
            raise RecoverableStageError("Storyboard contains no scenes")

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
        payload = {
            "scenes": scenes,
            "model": model,
            "style_contract": "verified_episode_05_prompt_shell_v1",
            "scene_count": len(scenes),
            "opening_hook_seconds": self.HOOK_WINDOW_SECONDS,
            "opening_visual_qa": {"passed": True, "history": history},
            "planner": "checkpointed_storyboard_chunks_v1",
            "chunk_count": len(chunks),
        }
        (episode_dir / "hook_visual_qa.json").write_text(
            json.dumps(payload["opening_visual_qa"], indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (episode_dir / "storyboard.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def build_storyboard_handler(root: Path, preferred_models: list[str]):
    return lambda episode_id: StoryboardPlanner(preferred_models).plan(root / episode_id)
