from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from .xkiro import XKiroClient, write_json


SYSTEM_PROMPT = """You are the writing engine for OTTAM, a long-form YouTube channel about psychology and human behavior.
The established format is a visually driven stickman documentary/explainer. Write for natural spoken narration, not an essay.
Priorities, in order: factual discipline, curiosity, story progression, clarity, retention, and visualizability.
Do not manufacture studies, quotations, statistics, dates, researchers, or scientific consensus.
Avoid generic self-help language, padded introductions, repetitive conclusions, fake drama, and listicle structure.
Every section must advance the central question. The ending should pay off the opening question rather than simply summarize.
Scene prompts must describe what should be visible on screen and must be suitable for OTTAM's consistent stickman visual language.
Do not ask the image model to render important readable text.
"""


def _load_config(path: Path = Path("config/llm.yaml")) -> dict:
    return yaml.safe_load(path.read_text())


def _prompt(topic: str) -> str:
    return f"""Create a production test package for this OTTAM topic:

TOPIC: {topic}

Return exactly these sections:

# CENTRAL QUESTION
One sentence.

# STORY ANGLE
Explain the contradiction/curiosity that makes the topic worth watching.

# NARRATION
Write a cohesive 7-8 minute narration. Start immediately with a compelling idea or situation; do not greet the audience. Use conversational American English. Build one continuous story with a clear reveal/payoff. Where a claim would require evidence you do not possess in this prompt, phrase it cautiously rather than inventing support.

# SCENE SAMPLE
For the first 12 narration beats only, create numbered visual directions. Each must contain:
- narration beat
- concrete stickman scene
- composition/action
- image-generation prompt
Keep prompts 16:9, visually simple and easy to understand at a glance.

# SELF-CHECK
Briefly identify any claims that should be researched/verified before production.
"""


def run_benchmark(topic: str, root: Path = Path("runtime/benchmarks")) -> dict:
    cfg = _load_config()
    client = XKiroClient(
        base_url=cfg["base_url"],
        timeout_seconds=float(cfg["request"]["timeout_seconds"]),
    )
    catalog = client.list_models()
    by_id = {m.id: m for m in catalog}

    requested = cfg.get("preferred_models", [])[: int(cfg["benchmark"].get("max_models", 3))]
    candidates = []
    skipped = []
    for model_id in requested:
        model = by_id.get(model_id)
        if model is None:
            skipped.append({"model": model_id, "reason": "not_in_live_catalog"})
        elif not model.is_free:
            skipped.append({"model": model_id, "reason": "not_confirmed_free"})
        else:
            candidates.append(model)

    if not candidates:
        raise RuntimeError(f"No preferred model is confirmed free; skipped={skipped}")

    run_id = os.getenv("GITHUB_RUN_ID", "local")
    out_dir = root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for model in candidates:
        text = client.chat_stream(
            model=model.id,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _prompt(topic)},
            ],
            temperature=float(cfg["request"]["temperature"]),
            max_tokens=int(cfg["request"]["max_tokens"]),
        )
        safe_name = model.id.replace("/", "__").replace(":", "_")
        path = out_dir / f"{safe_name}.md"
        path.write_text(text)
        results.append({"model": model.id, "output": str(path), "characters": len(text)})

    manifest = {
        "topic": topic,
        "results": results,
        "skipped": skipped,
        "free_only": True,
    }
    write_json(out_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    topic = os.getenv("OTTAM_TEST_TOPIC")
    if not topic:
        raise SystemExit("Set OTTAM_TEST_TOPIC before running the benchmark")
    print(json.dumps(run_benchmark(topic), indent=2))


if __name__ == "__main__":
    main()
