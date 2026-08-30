from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .xkiro import XKiroClient

SYSTEM = """You create YouTube upload packages for OTTAM, a psychology and human-behavior channel.
Be accurate, curiosity-driven, natural, concise, and non-clickbait. Never invent claims beyond the supplied episode.
Return strict JSON only."""

PREFERRED_MODELS = ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"]


def _client() -> tuple[XKiroClient, str]:
    client = XKiroClient.from_env()
    model = client.select_free_model(PREFERRED_MODELS).id
    return client, model


def _episode_context(episode_dir: Path) -> str:
    parts: list[str] = []
    for name in ("topic.json", "research.json", "script.txt"):
        path = episode_dir / name
        if path.exists():
            parts.append(f"\n--- {name} ---\n{path.read_text(encoding='utf-8')}")
    if not parts:
        raise RuntimeError(f"No episode context found in {episode_dir}")
    return "".join(parts)


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    required = ["title", "description", "hashtags", "tags", "thumbnail_text", "thumbnail_prompt"]
    for key in required:
        if key not in payload:
            raise RuntimeError(f"Upload package missing {key}")
    payload["title"] = str(payload["title"]).strip()[:100]
    payload["description"] = str(payload["description"]).strip()[:5000]
    payload["hashtags"] = [str(x).strip().lstrip("#") for x in payload.get("hashtags", []) if str(x).strip()][:8]
    payload["tags"] = [str(x).strip() for x in payload.get("tags", []) if str(x).strip()][:30]
    payload["thumbnail_text"] = str(payload["thumbnail_text"]).strip().upper()[:42]
    payload["thumbnail_prompt"] = str(payload["thumbnail_prompt"]).strip()
    return payload


def generate_upload_package(episode_id: str, runtime_root: Path = Path("runtime/episodes")) -> dict[str, Any]:
    episode_dir = runtime_root / episode_id
    context = _episode_context(episode_dir)
    client, model = _client()
    prompt = f"""Create the complete manual YouTube upload package for this finished OTTAM episode.
Return JSON with exactly these keys: title, description, hashtags, tags, thumbnail_text, thumbnail_prompt.

Rules:
- title: curiosity-driven, accurate, <= 100 chars, strongest searchable phrase naturally included
- description: useful first two lines, concise episode summary, then a natural subscribe line; no fake claims
- hashtags: 3-6 highly relevant items
- tags: 12-25 useful search variants, no spam
- thumbnail_text: 2-5 punchy words, complementary to title rather than repeating it
- thumbnail_prompt: one purpose-built 16:9 OTTAM thumbnail scene; one dominant expressive stickman/action, one clear visual metaphor, saturated orange/yellow focal light against deep blue contrast, simple uncluttered background, clean top area for headline, absolutely no generated text/signage/logos/watermarks

EPISODE:\n{context}"""
    raw = client.chat_stream(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.45,
        max_tokens=3000,
    )
    payload = _validate(json.loads(raw))
    payload["episode_id"] = episode_id
    payload["version"] = 1
    path = episode_dir / "upload_package.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    versions = episode_dir / "upload_package_versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "caption_001.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def regenerate_caption(episode_id: str, instruction: str = "", runtime_root: Path = Path("runtime/episodes")) -> dict[str, Any]:
    episode_dir = runtime_root / episode_id
    current_path = episode_dir / "upload_package.json"
    if not current_path.exists():
        return generate_upload_package(episode_id, runtime_root)
    current = json.loads(current_path.read_text(encoding="utf-8"))
    context = _episode_context(episode_dir)
    client, model = _client()
    prompt = f"""Regenerate ONLY the YouTube publishing text for this OTTAM episode.
Return strict JSON with exactly: title, description, hashtags, tags.
Do not change the factual meaning of the episode. One generation only.
Optional user instruction: {instruction.strip() or 'No extra instruction; improve engagement and SEO while staying accurate.'}

CURRENT PACKAGE:\n{json.dumps({k: current.get(k) for k in ['title','description','hashtags','tags']}, ensure_ascii=False)}

EPISODE:\n{context}"""
    raw = client.chat_stream(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.55,
        max_tokens=2500,
    )
    update = json.loads(raw)
    merged = dict(current)
    for key in ("title", "description", "hashtags", "tags"):
        if key in update:
            merged[key] = update[key]
    merged = _validate(merged)
    versions = episode_dir / "upload_package_versions"
    versions.mkdir(parents=True, exist_ok=True)
    existing = sorted(versions.glob("caption_*.json"))
    merged["version"] = len(existing) + 1
    merged["caption_instruction"] = instruction.strip()
    current_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    (versions / f"caption_{len(existing)+1:03d}.json").write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged


def generate_main() -> None:
    episode_id = os.getenv("OTTAM_EPISODE_ID", "").strip()
    if not episode_id:
        raise SystemExit("OTTAM_EPISODE_ID is required")
    generate_upload_package(episode_id)


def regenerate_main() -> None:
    episode_id = os.getenv("OTTAM_EPISODE_ID", "").strip()
    if not episode_id:
        raise SystemExit("OTTAM_EPISODE_ID is required")
    regenerate_caption(episode_id, os.getenv("OTTAM_REGEN_INSTRUCTION", ""))
