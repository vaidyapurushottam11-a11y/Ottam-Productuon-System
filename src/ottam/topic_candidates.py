from __future__ import annotations

import json
import os
from pathlib import Path

from .xkiro import XKiroClient

PREFERRED_MODELS = ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"]
SYSTEM = """You are the topic strategist for OTTAM, a long-form YouTube channel about psychology and human behavior.
Favor evergreen, highly relatable, surprising topics with a clear question, enough evidence to research responsibly, and strong stickman visual storytelling.
Avoid medical diagnosis, fake neuroscience, generic self-help, and topics too close to recent candidates supplied in the prompt.
Return strict JSON only."""


def generate_candidates(request_id: str, instruction: str = "", output_root: Path = Path("runtime/dashboard")) -> dict:
    client = XKiroClient()
    model = client.select_free_model(PREFERRED_MODELS).id
    prompt = f"""Generate exactly 5 distinct OTTAM topic candidates and rank them by production potential.
Optional user direction: {instruction.strip() or 'none'}

Return JSON object with key candidates. Every candidate must contain:
- title
- score: integer 0-100
- central_question
- contradiction_or_surprise
- evidence_availability: one short sentence
- visual_potential: one short sentence
- reason: concise reason for the score

Scoring weights: hook/curiosity 30, relatability 25, evidence availability 20, visual storytelling 15, evergreen/share potential 10.
Do not use scores below 70 unless the whole batch is weak. Do not fabricate specific studies or statistics at this stage."""
    raw = client.chat_stream(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.75,
        max_tokens=3000,
    )
    payload = json.loads(raw)
    candidates = payload.get("candidates") or []
    if len(candidates) != 5:
        raise RuntimeError(f"Expected exactly 5 candidates, got {len(candidates)}")
    for item in candidates:
        item["score"] = int(item.get("score", 0))
    candidates.sort(key=lambda x: x["score"], reverse=True)
    result = {"request_id": request_id, "candidates": candidates}
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "topic_candidates.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> None:
    request_id = os.getenv("OTTAM_TOPIC_REQUEST_ID", "").strip()
    if not request_id:
        raise SystemExit("OTTAM_TOPIC_REQUEST_ID is required")
    generate_candidates(request_id, os.getenv("OTTAM_TOPIC_INSTRUCTION", ""))


if __name__ == "__main__":
    main()
