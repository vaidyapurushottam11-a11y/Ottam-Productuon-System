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

    @staticmethod
    def _hard_word_bounds(min_words: int, max_words: int) -> tuple[int, int]:
        """Hard limits are wider than editorial targets.

        A tiny miss such as 617 vs a 620 target must never trigger another full
        LLM rewrite. We only spend another generation when duration/length is
        materially off target.
        """
        lower_margin = max(20, round(min_words * 0.05))
        upper_margin = max(30, round(max_words * 0.05))
        return max(1, min_words - lower_margin), max_words + upper_margin

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

    def _fact_check_report(self, script: str, research: str) -> dict:
        model = self._model()
        prompt = f"""Fact-check this OTTAM narration strictly against the supplied research package. Do not add outside knowledge.
Return strict JSON with keys: passed, supported_claims, unsupported_claims, overstatements, medical_or_neuroscience_risks, required_edits.
Set passed=false for any material spoken claim that exceeds, contradicts, or universalizes the research evidence.

RESEARCH:\n{research}\n\nSCRIPT:\n{script}"""
        raw = self.client.chat_stream(
            model=model,
            messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}],
            temperature=0.1,
            max_tokens=4500,
        )
        return self._parse_json(raw, "fact check")

    def fact_check(self, episode_dir: Path) -> None:
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        min_words, max_words = self._word_bounds(episode_dir)
        hard_min, hard_max = self._hard_word_bounds(min_words, max_words)
        script_path = episode_dir / "script.txt"
        script = script_path.read_text(encoding="utf-8")

        # At most one repair generation. Evidence is important, but repeatedly
        # rewriting a nearly-correct script is both expensive and destabilizing.
        history: list[dict] = []
        for repair_round in range(2):
            report = self._fact_check_report(script, research)
            history.append(report)
            final_payload = dict(report)
            final_payload["repair_round"] = repair_round
            final_payload["history"] = history
            (episode_dir / "fact_check.json").write_text(
                json.dumps(final_payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            if report.get("passed") is True:
                return

            if repair_round >= 1:
                break

            required_edits = report.get("required_edits") or []
            unsupported = report.get("unsupported_claims") or []
            overstatements = report.get("overstatements") or []
            model = self._model()
            prompt = f"""Repair ONLY the evidence problems in this OTTAM narration.
Apply every required edit below. Preserve the hook, story structure, examples, pacing and payoff unless a flagged claim requires a small wording change.
Do not add new factual claims. Do not introduce new neuroscience, diagnoses, statistics, causes or mechanisms.
Aim for {min_words}-{max_words} words, but do not damage a good script merely to hit an exact count.
Prefer precise hedging such as 'can', 'may', 'often', 'one factor', or 'one explanation' when the research does not support universal certainty.
Return narration only, with no headings, notes, bullets or commentary.

REQUIRED EDITS:\n{json.dumps(required_edits, ensure_ascii=False)}

UNSUPPORTED CLAIMS:\n{json.dumps(unsupported, ensure_ascii=False)}

OVERSTATEMENTS:\n{json.dumps(overstatements, ensure_ascii=False)}

RESEARCH BOUNDARIES:\n{research}

CURRENT SCRIPT:\n{script}"""
            revised = self.client.chat_stream(
                model=model,
                messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}],
                temperature=0.2,
                max_tokens=6000,
            ).strip()
            revised_count = len(revised.split())
            if not hard_min <= revised_count <= hard_max:
                raise QuarantineEpisode(
                    f"Evidence repair materially changed length to {revised_count} words; hard window is {hard_min}-{hard_max}"
                )
            script = revised
            script_path.write_text(script + "\n", encoding="utf-8")

        edits = history[-1].get("required_edits") if history else []
        raise QuarantineEpisode(f"Script still has material evidence problems after one repair: {edits}")

    def _resize_script(self, episode_dir: Path, script: str, min_words: int, max_words: int) -> str:
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        model = self._model()
        target = (min_words + max_words) // 2
        hard_min, hard_max = self._hard_word_bounds(min_words, max_words)
        prompt = f"""Rewrite the narration below to approximately {target} words. The preferred target window is {min_words}-{max_words} words.
Preserve the hook, central contradiction, evidence, reveal and payoff. Remove repetition before removing story value.
Do not add any new factual claim, statistic, diagnosis, neuroscience explanation, or source. Stay strictly inside the supplied research boundaries.
Return narration only: no headings, notes, bullets, citations, or commentary.

RESEARCH BOUNDARIES:\n{research}\n\nCURRENT SCRIPT:\n{script}"""
        revised = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.35, max_tokens=6000).strip()
        count = len(revised.split())
        if not hard_min <= count <= hard_max:
            raise QuarantineEpisode(
                f"Automatic resize returned {count} words; hard acceptable window is {hard_min}-{hard_max}"
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

    @staticmethod
    def _qa_decision(report: dict) -> tuple[bool, bool, dict]:
        """Return (acceptable, needs_repair, diagnostics).

        Scores are subjective estimates, not exact measurements. A one-point miss
        must not trigger thousands of extra tokens. Repair only meaningful gaps.
        """
        score_keys = ("hook", "retention", "clarity", "naturalness", "factual_discipline", "ottam_style")
        score_map = {k: int(report.get(k, 0)) for k in score_keys}
        scores = list(score_map.values())
        blockers = report.get("blocking_issues") or []
        average = sum(scores) / len(scores) if scores else 0.0
        minimum = min(scores, default=0)

        # Strong-enough upload candidate: no blockers, good overall quality,
        # no genuinely weak dimension. 81 vs 82 is explicitly acceptable.
        acceptable = (
            not blockers
            and average >= 82
            and minimum >= 78
            and score_map["hook"] >= 80
            and score_map["factual_discipline"] >= 80
        )

        # Spend on a rewrite only for a meaningful deficit or a true blocker.
        needs_repair = bool(blockers) or average < 79 or minimum < 74 or score_map["hook"] < 76 or score_map["factual_discipline"] < 78
        return acceptable, needs_repair, {
            "average_score": round(average, 2),
            "minimum_score": minimum,
            "scores": score_map,
            "blocking_issues": blockers,
        }

    def script_qa(self, episode_dir: Path) -> None:
        script_path = episode_dir / "script.txt"
        script = script_path.read_text(encoding="utf-8")
        min_words, max_words = self._word_bounds(episode_dir)
        hard_min, hard_max = self._hard_word_bounds(min_words, max_words)
        actual_words = len(script.split())

        # Preferred word count is a target, not a razor-thin failure boundary.
        # Only rewrite when the miss is material enough to threaten runtime.
        if not hard_min <= actual_words <= hard_max:
            script = self._resize_script(episode_dir, script, min_words, max_words)
            actual_words = len(script.split())

        report = self._audit_script(script)
        acceptable, needs_repair, diagnostics = self._qa_decision(report)
        report.update(diagnostics)
        report["word_count"] = actual_words
        report["target_word_window"] = [min_words, max_words]
        report["hard_word_window"] = [hard_min, hard_max]
        report["within_target_words"] = min_words <= actual_words <= max_words

        if acceptable or not needs_repair:
            # Marginal misses are accepted and recorded instead of spending more
            # tokens. Final Kokoro duration is the authoritative runtime gate.
            report["passed"] = True
            report["accepted_with_tolerance"] = not acceptable
            (episode_dir / "script_qa.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            return

        # One QA repair maximum for a significant quality gap.
        research = (episode_dir / "research.json").read_text(encoding="utf-8")
        instructions = report.get("revision_instructions") or []
        model = self._model()
        prompt = f"""Revise this OTTAM narration only because the QA gap is material.
Fix the blocking/low-scoring issues below while preserving factual boundaries, hook, story structure and payoff.
Aim for {min_words}-{max_words} words, but prioritize a natural production-ready script over exact word-count precision.
Do not add new factual claims. Return narration only.

QA REVISION INSTRUCTIONS:\n{json.dumps(instructions, ensure_ascii=False)}\n\nRESEARCH BOUNDARIES:\n{research}\n\nSCRIPT:\n{script}"""
        revised = self.client.chat_stream(model=model, messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}], temperature=0.3, max_tokens=6000).strip()
        revised_count = len(revised.split())
        if not hard_min <= revised_count <= hard_max:
            raise QuarantineEpisode(
                f"QA repair materially missed length at {revised_count} words; hard window is {hard_min}-{hard_max}"
            )
        script_path.write_text(revised + "\n", encoding="utf-8")
        self.fact_check(episode_dir)

        final_report = self._audit_script(revised)
        final_acceptable, final_needs_repair, final_diag = self._qa_decision(final_report)
        final_report.update(final_diag)
        final_report["word_count"] = revised_count
        final_report["target_word_window"] = [min_words, max_words]
        final_report["hard_word_window"] = [hard_min, hard_max]
        final_report["within_target_words"] = min_words <= revised_count <= max_words

        # No second QA rewrite. If it is only marginal, accept it; if it still
        # has a significant deficit, quarantine without burning more tokens.
        if final_acceptable or not final_needs_repair:
            final_report["passed"] = True
            final_report["accepted_with_tolerance"] = not final_acceptable
            (episode_dir / "script_qa.json").write_text(json.dumps(final_report, indent=2), encoding="utf-8")
            return

        final_report["passed"] = False
        final_report["accepted_with_tolerance"] = False
        (episode_dir / "script_qa.json").write_text(json.dumps(final_report, indent=2), encoding="utf-8")
        raise QuarantineEpisode(
            f"Script still has a significant QA gap after one repair: average={final_diag['average_score']}, min={final_diag['minimum_score']}, blockers={final_diag['blocking_issues']}"
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
