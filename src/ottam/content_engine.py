from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .orchestrator import QuarantineEpisode, RecoverableStageError
from .xkiro import XKiroClient


SYSTEM = """You are the editorial engine for OTTAM, a long-form YouTube channel about psychology and human behavior.
The channel uses concise, cinematic narration and simple stickman visuals. Write for retention, not academia.
Never invent evidence. Separate supported facts from theories. Avoid sensational medical claims.
Avoid stock reassurance such as 'you're not broken' or equivalent filler.
Return strict JSON only when requested."""


@dataclass
class ContentEngine:
    client: XKiroClient
    preferred_models: list[str]
    profile_root: Path = Path("config/episodes")

    def _model(self) -> str:
        return self.client.select_free_model(self.preferred_models).id

    def _profile(self, episode_dir: Path) -> dict:
        path = self.profile_root / f"{episode_dir.name}.yaml"
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise QuarantineEpisode(f"Invalid episode profile: {path}")
        return data

    @staticmethod
    def _parse_json(text: str, label: str) -> dict:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RecoverableStageError(f"{label} returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RecoverableStageError(f"{label} must return a JSON object")
        return data

    def _word_bounds(self, episode_dir: Path) -> tuple[int, int]:
        words = self._profile(episode_dir).get("episode", {}).get("target_words", {})
        return int(words.get("min", 950)), int(words.get("max", 1200))

    def discover_topic(self, episode_dir: Path) -> None:
        profile = self._profile(episode_dir)
        episode_dir.mkdir(parents=True, exist_ok=True)
        if profile.get("topic"):
            payload = {
                "winner": profile["topic"],
                "episode": profile.get("episode", {}),
                "source": "locked_episode_profile",
            }
            (episode_dir / "topic.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return

        model = self._model()
        prompt = """Generate 12 evergreen OTTAM topic candidates about psychology or human behavior.
For each include: title, central_question, why_it_is_surprising, evidence_availability, visual_potential, duplicate_risk_note.
Rank them and choose one winner. Return JSON object with keys candidates and winner."""
        text = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.85, max_tokens=5000)
        self._parse_json(text, "topic discovery")
        (episode_dir / "topic.json").write_text(text, encoding="utf-8")

    def research(self, episode_dir: Path) -> None:
        profile = self._profile(episode_dir)
        if profile.get("research"):
            payload = {
                "research": profile["research"],
                "editorial_guardrails": profile.get("editorial_guardrails", []),
                "verification_status": "externally_verified_seed",
            }
            (episode_dir / "research.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return

        topic = (episode_dir / "topic.json").read_text(encoding="utf-8")
        model = self._model()
        prompt = f"""Using this selected topic package:\n{topic}\n\nBuild a research brief for scriptwriting.
Do NOT claim you browsed the web. Distinguish established knowledge, contested ideas, and claims that require external verification.
Return JSON with: thesis, known_background, claims_to_verify, contested_points, story_angles, unsafe_or_overstated_claims_to_avoid."""
        out = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.35, max_tokens=7000)
        self._parse_json(out, "research")
        (episode_dir / "research.json").write_text(out, encoding="utf-8")

    def write_script(self, episode_dir: Path) -> None:
        topic = (episode_dir / "topic.json").read_text(encoding="utf-8")
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        min_words, max_words = self._word_bounds(episode_dir)
        model = self._model()
        prompt = f"""Write a production-ready OTTAM narration script using ONLY the factual boundaries in the topic and research below.
Target {min_words}-{max_words} spoken words. Strong hook in the first 10-15 seconds. Use one clear curiosity arc, escalating reveals, concrete everyday examples, and a satisfying payoff.
No headings, stage directions, citations, scene labels, or bullet points in the spoken script. Natural American-English narration.
Do not introduce neuroscience, diagnoses, statistics, or causal claims that are not explicitly supported by the research package.
Where the research describes an interpretation or account, preserve that uncertainty instead of turning it into a universal fact.

TOPIC:\n{topic}\n\nRESEARCH:\n{research}"""
        script = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.68, max_tokens=6500)
        (episode_dir / "script.txt").write_text(script.strip() + "\n", encoding="utf-8")

    def fact_check(self, episode_dir: Path) -> None:
        script = (episode_dir / "script.txt").read_text(encoding="utf-8")
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        model = self._model()
        prompt = f"""Fact-check this OTTAM narration strictly against the supplied research package. Do not add outside knowledge.
Return strict JSON with keys: passed, supported_claims, unsupported_claims, overstatements, medical_or_neuroscience_risks, required_edits.
Set passed=false for any material spoken claim that exceeds, contradicts, or universalizes the research evidence.

RESEARCH:\n{research}\n\nSCRIPT:\n{script}"""
        raw = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.1, max_tokens=4500)
        report = self._parse_json(raw, "fact check")
        (episode_dir / "fact_check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if report.get("passed") is not True:
            edits = report.get("required_edits") or []
            raise RecoverableStageError(f"Script failed evidence gate: {edits}")

    def _resize_script(self, episode_dir: Path, script: str, min_words: int, max_words: int) -> str:
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        model = self._model()
        target = (min_words + max_words) // 2
        prompt = f"""Rewrite the narration below to approximately {target} words and keep it strictly inside {min_words}-{max_words} words.
Preserve the hook, central contradiction, evidence, reveal and payoff. Remove repetition before removing story value.
Do not add any new factual claim, statistic, diagnosis, neuroscience explanation, or source. Stay strictly inside the supplied research boundaries.
Return narration only: no headings, notes, bullets, citations, or commentary.

RESEARCH BOUNDARIES:\n{research}\n\nCURRENT SCRIPT:\n{script}"""
        revised = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.35, max_tokens=6000).strip()
        count = len(revised.split())
        if not min_words <= count <= max_words:
            raise RecoverableStageError(
                f"Automatic script resize returned {count} words; required {min_words}-{max_words}"
            )
        (episode_dir / "script.txt").write_text(revised + "\n", encoding="utf-8")
        self.fact_check(episode_dir)
        return revised

    def _audit_script(self, script: str) -> dict:
        model = self._model()
        prompt = f"""Audit the OTTAM script below. Return strict JSON with numeric 0-100 scores for hook, retention, clarity, naturalness, factual_discipline, ottam_style, plus blocking_issues and revision_instructions.
A blocking issue is unsupported certainty, obvious factual fabrication, severe repetition, incoherence, weak payoff, generic filler, or unusable narration.
Judge this as a real YouTube upload candidate, not a prototype.

SCRIPT:\n{script}"""
        raw = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.15, max_tokens=3500)
        return self._parse_json(raw, "script QA")

    def script_qa(self, episode_dir: Path) -> None:
        script = (episode_dir / "script.txt").read_text(encoding="utf-8")
        min_words, max_words = self._word_bounds(episode_dir)
        actual_words = len(script.split())
        if not min_words <= actual_words <= max_words:
            script = self._resize_script(episode_dir, script, min_words, max_words)
            actual_words = len(script.split())

        report = self._audit_script(script)
        report["word_count"] = actual_words
        score_keys = ("hook", "retention", "clarity", "naturalness", "factual_discipline", "ottam_style")
        scores = [int(report.get(k, 0)) for k in score_keys]
        blockers = report.get("blocking_issues") or []
        passed = not blockers and min(scores, default=0) >= 82

        if not passed:
            research = (episode_dir / "research.json").read_text(encoding="utf-8")
            instructions = report.get("revision_instructions") or []
            model = self._model()
            prompt = f"""Revise this OTTAM narration to fix the QA issues below while staying inside {min_words}-{max_words} words.
Preserve all factual boundaries. Do not add new claims. Improve hook, retention, clarity, natural spoken rhythm and payoff without adding filler.
Return narration only.

QA REVISION INSTRUCTIONS:\n{json.dumps(instructions, ensure_ascii=False)}\n\nRESEARCH BOUNDARIES:\n{research}\n\nSCRIPT:\n{script}"""
            revised = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.3, max_tokens=6000).strip()
            revised_count = len(revised.split())
            if not min_words <= revised_count <= max_words:
                raise RecoverableStageError(
                    f"QA revision returned {revised_count} words; required {min_words}-{max_words}"
                )
            (episode_dir / "script.txt").write_text(revised + "\n", encoding="utf-8")
            self.fact_check(episode_dir)
            script = revised
            actual_words = revised_count
            report = self._audit_script(script)
            scores = [int(report.get(k, 0)) for k in score_keys]
            blockers = report.get("blocking_issues") or []
            passed = not blockers and min(scores, default=0) >= 82

        report["word_count"] = actual_words
        report["passed"] = passed
        (episode_dir / "script_qa.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if not passed:
            raise RecoverableStageError(
                f"Script QA failed after automatic repair; min score={min(scores, default=0)}, blockers={blockers}"
            )


def build_content_handlers(root: Path, preferred_models: list[str]):
    def engine() -> ContentEngine:
        return ContentEngine(XKiroClient(), preferred_models)

    return {
        "discover_topic": lambda eid: engine().discover_topic(root / eid),
        "research": lambda eid: engine().research(root / eid),
        "write_script": lambda eid: engine().write_script(root / eid),
        "fact_check": lambda eid: engine().fact_check(root / eid),
        "script_qa": lambda eid: engine().script_qa(root / eid),
    }
