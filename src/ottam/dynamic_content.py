from __future__ import annotations

import json
from pathlib import Path

from .content_engine import ContentEngine
from .orchestrator import RecoverableStageError
from .xkiro import XKiroClient


class DynamicContentEngine(ContentEngine):
    """Content engine whose episode length is chosen from the episode itself.

    There is no channel-wide preferred runtime. Dashboard episodes get an
    editorial duration plan from their topic/research. The only hard runtime
    constraint is the YouTube production ceiling of 15 minutes.
    """

    estimated_wpm = 205.0
    max_runtime_seconds = 15 * 60

    def _duration_plan(self, episode_dir: Path) -> dict:
        profile = self._profile(episode_dir)
        configured = profile.get("episode", {}).get("target_words", {})
        if configured:
            minimum = int(configured.get("min", 1))
            maximum = int(configured.get("max", minimum))
            return {
                "source": "episode_profile",
                "target_words_min": minimum,
                "target_words_max": maximum,
            }

        path = episode_dir / "duration_plan.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("target_minutes"):
                    return payload
            except (OSError, json.JSONDecodeError):
                pass

        topic = (episode_dir / "topic.json").read_text(encoding="utf-8")
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        model = self._model()
        prompt = f"""Choose the natural narration length for this OTTAM episode.
The runtime must be driven only by how much explanation, evidence, examples and story development this particular topic genuinely needs.

Rules:
- Do NOT target a standard channel length.
- Do NOT prefer 8, 10, 12, or any other repeated round duration.
- A concise topic may be around 6-8 minutes; a richer topic may need 9-12 minutes; an unusually deep topic may go longer.
- Never plan beyond 14.2 minutes, leaving safety margin under the absolute 15-minute delivery ceiling.
- Do not pad a simple topic and do not compress a complex topic merely to hit a template.
- Return strict JSON with exactly: target_minutes, rationale, depth_level.
- target_minutes must be a decimal number between 6.0 and 14.2 and should reflect this topic rather than a preset.

TOPIC:\n{topic}\n\nRESEARCH:\n{research}"""
        raw = self.client.chat_stream(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.35,
            max_tokens=1200,
        )
        payload = self._parse_json(raw, "duration planning")
        minutes = float(payload.get("target_minutes") or 0)
        if not 6.0 <= minutes <= 14.2:
            raise RecoverableStageError(f"Duration planner returned invalid target_minutes={minutes}")

        center = round(minutes * self.estimated_wpm)
        # This is an editorial tolerance band, not a fixed episode template.
        # It lets the script breathe while keeping production near the topic-led plan.
        minimum = max(900, round(center * 0.90))
        maximum = min(round((self.max_runtime_seconds / 60) * self.estimated_wpm * 0.97), round(center * 1.10))
        if minimum > maximum:
            minimum = max(900, maximum - 180)

        payload.update(
            {
                "source": "topic_driven_editorial_plan",
                "estimated_wpm": self.estimated_wpm,
                "target_words_min": minimum,
                "target_words_max": maximum,
                "absolute_runtime_ceiling_seconds": self.max_runtime_seconds,
            }
        )
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload

    def _word_bounds(self, episode_dir: Path) -> tuple[int, int]:
        plan = self._duration_plan(episode_dir)
        return int(plan["target_words_min"]), int(plan["target_words_max"])

    def write_script(self, episode_dir: Path) -> None:
        topic = (episode_dir / "topic.json").read_text(encoding="utf-8")
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        plan = self._duration_plan(episode_dir)
        min_words, max_words = self._word_bounds(episode_dir)
        model = self._model()
        prompt = f"""Write a production-ready OTTAM narration script using ONLY the factual boundaries in the topic and research below.

This episode has its own editorial duration plan. Write only as much as this topic needs.
Planned runtime: about {plan.get('target_minutes', 'topic-driven')} minutes.
Editorial word guidance: approximately {min_words}-{max_words} spoken words at OTTAM's locked 1.0x voice speed.
This is guidance, not a reason to pad or repeat. Natural pacing and completeness matter more than landing on a round number.
The final narration must remain below 15 minutes.

Use a strong hook in the first 10-15 seconds, a clear curiosity arc, concrete everyday examples where they add value, escalating understanding, and a satisfying payoff.
No headings, stage directions, citations, scene labels, or bullet points in the spoken script. Natural American-English narration.
Do not introduce neuroscience, diagnoses, statistics, or causal claims that are not explicitly supported by the research package.
Where the research describes an interpretation or account, preserve that uncertainty instead of turning it into a universal fact.

TOPIC:\n{topic}\n\nRESEARCH:\n{research}"""
        script = self.client.chat_stream(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.68,
            max_tokens=12000,
        )
        (episode_dir / "script.txt").write_text(script.strip() + "\n", encoding="utf-8")

    def script_qa(self, episode_dir: Path) -> None:
        # Reuse the proven QA logic, but its bounds now come from the per-episode
        # duration plan rather than a global 1900-2200-word template.
        super().script_qa(episode_dir)

    def shorten_for_runtime(self, episode_dir: Path, actual_seconds: float) -> None:
        """Shorten only when TTS proves the narration would exceed 15 minutes."""
        if actual_seconds <= self.max_runtime_seconds:
            return
        script_path = episode_dir / "script.txt"
        script = script_path.read_text(encoding="utf-8")
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        current_words = len(script.split())
        # Aim around 13.8 minutes to leave a real buffer for delivery variation.
        target_words = max(900, round(current_words * (13.8 * 60.0 / actual_seconds)))
        model = self._model()
        prompt = f"""Shorten this OTTAM narration to about {target_words} words because the actual 1.0x TTS runtime exceeded the 15-minute production ceiling.
Preserve the hook, core evidence, essential examples, explanatory chain, reveal and payoff. Remove repetition and lower-value examples first.
Do not add any new factual claims. Stay strictly inside the research boundaries.
Return narration only, with no headings, notes, citations or commentary.

RESEARCH BOUNDARIES:\n{research}\n\nSCRIPT:\n{script}"""
        revised = self.client.chat_stream(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.25,
            max_tokens=10000,
        ).strip()
        if not revised:
            raise RecoverableStageError("Runtime shortening returned an empty script")
        script_path.write_text(revised + "\n", encoding="utf-8")
        self.fact_check(episode_dir)


def build_dynamic_content_handlers(root: Path, preferred_models: list[str]):
    def engine() -> DynamicContentEngine:
        return DynamicContentEngine(XKiroClient(), preferred_models)

    return {
        "discover_topic": lambda eid: engine().discover_topic(root / eid),
        "research": lambda eid: engine().research(root / eid),
        "write_script": lambda eid: engine().write_script(root / eid),
        "fact_check": lambda eid: engine().fact_check(root / eid),
        "script_qa": lambda eid: engine().script_qa(root / eid),
    }


def shorten_episode_for_runtime(episode_id: str, root: Path, preferred_models: list[str], actual_seconds: float) -> None:
    DynamicContentEngine(XKiroClient(), preferred_models).shorten_for_runtime(root / episode_id, actual_seconds)
