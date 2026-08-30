from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .orchestrator import QuarantineEpisode, RecoverableStageError
from .xkiro import XKiroClient


SYSTEM = """You are the editorial engine for OTTAM, a long-form YouTube channel about psychology and human behavior.
The channel uses concise, cinematic narration and simple stickman visuals. Write for retention, not academia.
Never invent evidence. Separate supported facts from theories. Avoid sensational medical claims.
Return strict JSON only when requested."""


@dataclass
class ContentEngine:
    client: XKiroClient
    preferred_models: list[str]

    def _model(self) -> str:
        return self.client.select_free_model(self.preferred_models).id

    def discover_topic(self, episode_dir: Path) -> None:
        model = self._model()
        prompt = """Generate 12 evergreen OTTAM topic candidates about psychology or human behavior.
For each include: title, central_question, why_it_is_surprising, evidence_availability, visual_potential, duplicate_risk_note.
Rank them and choose one winner. Return JSON object with keys candidates and winner."""
        text = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.85, max_tokens=5000)
        episode_dir.mkdir(parents=True, exist_ok=True)
        (episode_dir / "topic.json").write_text(text)

    def research(self, episode_dir: Path) -> None:
        topic = (episode_dir / "topic.json").read_text()
        model = self._model()
        prompt = f"""Using this selected topic package:\n{topic}\n\nBuild a research brief for scriptwriting.
Do NOT claim you browsed the web. Distinguish established knowledge, contested ideas, and claims that require external verification.
Return JSON with: thesis, known_background, claims_to_verify, contested_points, story_angles, unsafe_or_overstated_claims_to_avoid."""
        out = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.35, max_tokens=7000)
        (episode_dir / "research.json").write_text(out)

    def write_script(self, episode_dir: Path) -> None:
        topic = (episode_dir / "topic.json").read_text()
        research = (episode_dir / "research.json").read_text()
        model = self._model()
        prompt = f"""Write a production-ready OTTAM narration script using the topic and research below.
Target 7-8 minutes. Strong hook in first 15 seconds. Use a clear curiosity arc, escalating reveals, and a satisfying payoff.
No headings, stage directions, citations, or scene labels in the spoken script. Natural American-English narration.
Avoid unsupported certainty where the research marks claims as contested or unverified.

TOPIC:\n{topic}\n\nRESEARCH:\n{research}"""
        script = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.72, max_tokens=9000)
        (episode_dir / "script.txt").write_text(script)

    def script_qa(self, episode_dir: Path) -> None:
        script = (episode_dir / "script.txt").read_text()
        model = self._model()
        prompt = f"""Audit the OTTAM script below. Return strict JSON with numeric 0-100 scores for hook, retention, clarity, naturalness, factual_discipline, ottam_style, plus blocking_issues and revision_instructions.
A blocking issue is unsupported certainty, obvious factual fabrication, severe repetition, incoherence, or unusable narration.

SCRIPT:\n{script}"""
        out = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.2, max_tokens=3500)
        (episode_dir / "script_qa.json").write_text(out)


def build_content_handlers(root: Path, preferred_models: list[str]):
    def engine() -> ContentEngine:
        return ContentEngine(XKiroClient(), preferred_models)

    return {
        "discover_topic": lambda eid: engine().discover_topic(root / eid),
        "research": lambda eid: engine().research(root / eid),
        "fact_check": lambda eid: None,
        "write_script": lambda eid: engine().write_script(root / eid),
        "script_qa": lambda eid: engine().script_qa(root / eid),
    }
