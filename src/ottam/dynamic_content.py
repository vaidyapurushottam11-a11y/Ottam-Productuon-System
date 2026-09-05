from __future__ import annotations

import json
from pathlib import Path

from .content_engine import ContentEngine
from .orchestrator import RecoverableStageError
from .xkiro import XKiroClient


class DynamicContentEngine(ContentEngine):
    """Content engine whose episode length and opening are chosen from the episode itself."""

    estimated_wpm = 205.0
    max_runtime_seconds = 15 * 60
    hook_sample_words = 220

    def _duration_plan(self, episode_dir: Path) -> dict:
        profile = self._profile(episode_dir)
        configured = profile.get("episode", {}).get("target_words", {})
        if configured:
            minimum = int(configured.get("min", 1))
            maximum = int(configured.get("max", minimum))
            return {"source": "episode_profile", "target_words_min": minimum, "target_words_max": maximum}

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
        raw = self.client.chat_stream(model=model, messages=[{"role": "user", "content": prompt}], temperature=0.35, max_tokens=1200)
        payload = self._parse_json(raw, "duration planning")
        minutes = float(payload.get("target_minutes") or 0)
        if not 6.0 <= minutes <= 14.2:
            raise RecoverableStageError(f"Duration planner returned invalid target_minutes={minutes}")

        center = round(minutes * self.estimated_wpm)
        minimum = max(900, round(center * 0.90))
        maximum = min(round((self.max_runtime_seconds / 60) * self.estimated_wpm * 0.97), round(center * 1.10))
        if minimum > maximum:
            minimum = max(900, maximum - 180)

        payload.update({
            "source": "topic_driven_editorial_plan",
            "estimated_wpm": self.estimated_wpm,
            "target_words_min": minimum,
            "target_words_max": maximum,
            "absolute_runtime_ceiling_seconds": self.max_runtime_seconds,
        })
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload

    def _word_bounds(self, episode_dir: Path) -> tuple[int, int]:
        plan = self._duration_plan(episode_dir)
        return int(plan["target_words_min"]), int(plan["target_words_max"])

    def _hook_audit(self, script: str) -> dict:
        opening = " ".join(script.split()[: self.hook_sample_words])
        model = self._model()
        prompt = f"""Audit ONLY the opening hook of this OTTAM YouTube narration.
Return strict JSON with numeric 0-100 scores for: immediacy, relatability, curiosity, specificity, emotional_tension, open_loop, plus issues and revision_instructions.

A strong OTTAM hook should make a viewer immediately think 'that's me' or 'I need to know why this happens.' It should begin inside a concrete human moment, contradiction, uncomfortable truth, surprising observation, or specific question. It should NOT begin with definitions, generic scene-setting, broad educational framing, channel introductions, or abstract explanation.

The hook should create a clear unanswered question that the rest of the video promises to resolve without fake urgency or unsupported claims.

OPENING:\n{opening}"""
        raw = self.client.chat_stream(model=model, messages=[{"role": "user", "content": prompt}], temperature=0.1, max_tokens=2200)
        report = self._parse_json(raw, "hook QA")
        score_keys = ("immediacy", "relatability", "curiosity", "specificity", "emotional_tension", "open_loop")
        scores = {key: int(report.get(key, 0)) for key in score_keys}
        values = list(scores.values())
        report["scores"] = scores
        report["average_score"] = round(sum(values) / len(values), 2) if values else 0.0
        report["minimum_score"] = min(values, default=0)
        report["passed"] = report["average_score"] >= 84 and report["minimum_score"] >= 76
        return report

    def _repair_hook(self, episode_dir: Path, script: str, report: dict) -> str:
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        opening_words = script.split()[: self.hook_sample_words]
        opening = " ".join(opening_words)
        remainder = " ".join(script.split()[self.hook_sample_words :])
        model = self._model()
        prompt = f"""Rewrite ONLY this OTTAM opening hook so it is materially more relatable, immediate and curiosity-driven.
Start inside a concrete everyday human moment or contradiction that fits the topic. Make the viewer recognize themselves quickly. Create one strong unanswered question/open loop that naturally leads into the existing narration.
Do not use clickbait, fake stakes, generic reassurance, definitions, channel introductions, or unsupported factual claims. Stay strictly inside the supplied research boundaries.
Keep roughly the same opening length and return ONLY the revised opening text.

HOOK QA:\n{json.dumps(report, ensure_ascii=False)}

RESEARCH BOUNDARIES:\n{research}

CURRENT OPENING:\n{opening}

THE NEXT PART OF THE SCRIPT BEGINS:\n{remainder[:1200]}"""
        revised_opening = self.client.chat_stream(model=model, messages=[{"role": "user", "content": prompt}], temperature=0.45, max_tokens=2600).strip()
        if not revised_opening:
            raise RecoverableStageError("Hook repair returned an empty opening")
        return (revised_opening + " " + remainder).strip()

    def write_script(self, episode_dir: Path) -> None:
        topic = (episode_dir / "topic.json").read_text(encoding="utf-8")
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        plan = self._duration_plan(episode_dir)
        min_words, max_words = self._word_bounds(episode_dir)
        model = self._model()
        revision_path = episode_dir / "script_revision_instruction.txt"
        script_path = episode_dir / "script.txt"

        if revision_path.exists() and script_path.exists():
            instruction = revision_path.read_text(encoding="utf-8").strip()
            current = script_path.read_text(encoding="utf-8").strip()
            prompt = f"""Revise this OTTAM narration according to the viewer/editor instruction below.
Preserve factual accuracy and stay strictly inside the supplied research boundaries. Keep the episode's natural topic-driven duration instead of forcing a standard length.

The first 10-20 seconds are the highest-retention section. Make them exceptionally relatable, specific and curiosity-driven. Start inside a recognizable human moment, contradiction or uncomfortable truth. Do not begin with definitions, generic educational framing, or channel introductions. Create an open loop that the rest of the episode pays off.

Return narration only, with no headings, notes, bullets, citations or commentary.

EDITOR INSTRUCTION:\n{instruction}

RESEARCH BOUNDARIES:\n{research}

CURRENT SCRIPT:\n{current}"""
            script = self.client.chat_stream(model=model, messages=[{"role": "user", "content": prompt}], temperature=0.55, max_tokens=12000).strip()
            if not script:
                raise RecoverableStageError("Script revision returned empty narration")
            script_path.write_text(script + "\n", encoding="utf-8")
            revision_path.unlink(missing_ok=True)
            return

        prompt = f"""Write a production-ready OTTAM narration script using ONLY the factual boundaries in the topic and research below.

This episode has its own editorial duration plan. Write only as much as this topic needs.
Planned runtime: about {plan.get('target_minutes', 'topic-driven')} minutes.
Editorial word guidance: approximately {min_words}-{max_words} spoken words at OTTAM's locked 1.0x voice speed.
This is guidance, not a reason to pad or repeat. Natural pacing and completeness matter more than landing on a round number.
The final narration must remain below 15 minutes.

RETENTION PRIORITY:
- The first 10-20 seconds are the strongest part of the entire script.
- Start immediately inside a highly relatable everyday human moment, contradiction, uncomfortable truth, or specific question.
- The viewer should quickly feel 'that's me' or 'wait, why does that happen?'
- Create a clear open loop whose answer is earned later in the video.
- Do not start with definitions, generic context, a textbook explanation, channel introduction, or broad statement that could fit any psychology video.
- Keep revealing new understanding throughout the episode instead of giving the whole answer in the first minute.
- Use callbacks and escalating examples so the viewer has a reason to stay through the final payoff.

Use a clear curiosity arc, concrete everyday examples where they add value, escalating understanding, and a satisfying payoff.
No headings, stage directions, citations, scene labels, or bullet points in the spoken script. Natural American-English narration.
Do not introduce neuroscience, diagnoses, statistics, or causal claims that are not explicitly supported by the research package.
Where the research describes an interpretation or account, preserve that uncertainty instead of turning it into a universal fact.

TOPIC:\n{topic}\n\nRESEARCH:\n{research}"""
        script = self.client.chat_stream(model=model, messages=[{"role": "user", "content": prompt}], temperature=0.68, max_tokens=12000).strip()
        if not script:
            raise RecoverableStageError("Script generation returned empty narration")
        script_path.write_text(script + "\n", encoding="utf-8")

    def script_qa(self, episode_dir: Path) -> None:
        script_path = episode_dir / "script.txt"
        script = script_path.read_text(encoding="utf-8").strip()
        first_report = self._hook_audit(script)
        history = [first_report]

        if not first_report.get("passed"):
            script = self._repair_hook(episode_dir, script, first_report)
            script_path.write_text(script + "\n", encoding="utf-8")
            self.fact_check(episode_dir)
            second_report = self._hook_audit(script)
            history.append(second_report)
            if not second_report.get("passed"):
                (episode_dir / "hook_qa.json").write_text(
                    json.dumps({"passed": False, "history": history}, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                raise RecoverableStageError(
                    f"Opening hook is still materially weak after focused repair: average={second_report.get('average_score')}, min={second_report.get('minimum_score')}"
                )

        (episode_dir / "hook_qa.json").write_text(
            json.dumps({"passed": True, "history": history}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        super().script_qa(episode_dir)

    def shorten_for_runtime(self, episode_dir: Path, actual_seconds: float) -> None:
        if actual_seconds <= self.max_runtime_seconds:
            return
        script_path = episode_dir / "script.txt"
        script = script_path.read_text(encoding="utf-8")
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        current_words = len(script.split())
        target_words = max(900, round(current_words * (13.8 * 60.0 / actual_seconds)))
        model = self._model()
        prompt = f"""Shorten this OTTAM narration to about {target_words} words because the actual 1.0x TTS runtime exceeded the 15-minute production ceiling.
Preserve the hook, core evidence, essential examples, explanatory chain, reveal and payoff. Remove repetition and lower-value examples first.
Do not add any new factual claims. Stay strictly inside the research boundaries.
Return narration only, with no headings, notes, citations or commentary.

RESEARCH BOUNDARIES:\n{research}\n\nSCRIPT:\n{script}"""
        revised = self.client.chat_stream(model=model, messages=[{"role": "user", "content": prompt}], temperature=0.25, max_tokens=10000).strip()
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
