from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .xkiro import XKiroClient

SYSTEM = """You create YouTube upload packages for OTTAM, a psychology and human-behavior channel.
Be accurate, curiosity-driven, natural, and non-clickbait. Never invent claims beyond the supplied episode.
Every episode should feel individually packaged rather than generated from a repeated template.
Return strict JSON only."""

PREFERRED_MODELS = ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"]


def _client() -> tuple[XKiroClient, str]:
    client = XKiroClient()
    model = client.select_free_model(PREFERRED_MODELS).id
    return client, model


def _episode_context(episode_dir: Path) -> str:
    parts: list[str] = []
    for name in ("topic.json", "research.json", "script.txt", "duration_plan.json"):
        path = episode_dir / name
        if path.exists():
            parts.append(f"\n--- {name} ---\n{path.read_text(encoding='utf-8')}")
    if not parts:
        raise RuntimeError(f"No episode context found in {episode_dir}")
    return "".join(parts)


def _unique_strings(values: Any, *, strip_hash: bool = False) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value).strip()
        if strip_hash:
            text = text.lstrip("#")
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _fit_tag_budget(tags: list[str], max_chars: int = 450) -> list[str]:
    """Keep a technical buffer under YouTube's tag field limit without imposing a count quota."""
    selected: list[str] = []
    used = 0
    for tag in tags:
        extra = len(tag) + (2 if selected else 0)
        if used + extra > max_chars:
            break
        selected.append(tag)
        used += extra
    return selected


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    required = ["title", "description", "hashtags", "tags", "thumbnail_text", "thumbnail_prompt"]
    for key in required:
        if key not in payload:
            raise RuntimeError(f"Upload package missing {key}")

    # These are platform/technical ceilings only, never creative targets.
    payload["title"] = str(payload["title"]).strip()[:100]
    payload["description"] = str(payload["description"]).strip()[:5000]
    payload["hashtags"] = _unique_strings(payload.get("hashtags"), strip_hash=True)[:15]
    payload["tags"] = _fit_tag_budget(_unique_strings(payload.get("tags")))
    payload["thumbnail_text"] = str(payload["thumbnail_text"]).strip().upper()[:60]
    payload["thumbnail_prompt"] = str(payload["thumbnail_prompt"]).strip()
    return payload


def generate_upload_package(episode_id: str, runtime_root: Path = Path("runtime/episodes")) -> dict[str, Any]:
    episode_dir = runtime_root / episode_id
    context = _episode_context(episode_dir)
    client, model = _client()
    prompt = f"""Create the complete manual YouTube upload package for this finished OTTAM episode.
Return JSON with exactly these keys: title, description, hashtags, tags, thumbnail_text, thumbnail_prompt.

The package must be designed specifically for THIS episode. Do not use fixed counts, repeated formulas, recurring sentence patterns, or a standard thumbnail layout merely because previous OTTAM videos used them.

Rules:
- title: curiosity-driven, accurate, <=100 chars; use the strongest natural framing for this topic. Do not force a repeated title formula.
- description: use only as much detail as this episode benefits from. The opening should earn the click/watch continuation, then summarize accurately. A subscribe line is optional when it reads naturally; do not force one every time.
- hashtags: choose only the hashtags that genuinely help this particular topic. There is NO preferred count. A narrow topic may need very few; a broader topic may justify more. Avoid filler and repeated channel-wide bundles.
- tags: choose a content-specific set based on actual search intent, synonyms, concepts and likely queries for this episode. There is NO preferred count. Do not pad to a quota and do not reuse the same tag bundle mechanically across videos.
- thumbnail_text: create the hook wording that best complements this video's title and visual idea. Keep it phone-readable, but there is NO fixed word-count template. Do not reuse the same hook structure across episodes.
- thumbnail_prompt: design one purpose-built 16:9 thumbnail concept for this exact episode. The visual metaphor, composition, subject scale, background, lighting and palette must be chosen from the topic itself. Do NOT default to dark navy/deep blue, orange/yellow, the same left-text/right-character layout, or any other recurring template. Prefer strong phone-size separation and high visibility. Palettes may be bright, light, mid-tone, dark, warm, cool or mixed when the concept calls for it. The exact hook text is supplied separately to Magnific and must be rendered as part of the finished image. No logos, watermarks, UI, signage, labels or unrelated text.

EPISODE:\n{context}"""
    raw = client.chat_stream(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.55,
        max_tokens=3500,
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

Do not preserve a fixed count or formatting pattern from the previous version. Re-evaluate what THIS episode actually needs:
- title structure may change if another accurate framing is stronger
- description length and structure should follow the content, not a template
- hashtag count is dynamic; include only useful ones
- tag count is dynamic; include only useful search variants and concepts
- avoid mechanically repeating generic channel-wide tags/hashtags when they add little value

Optional user instruction: {instruction.strip() or 'No extra instruction; improve engagement and discoverability while staying accurate and natural.'}

CURRENT PACKAGE:\n{json.dumps({k: current.get(k) for k in ['title','description','hashtags','tags']}, ensure_ascii=False)}

EPISODE:\n{context}"""
    raw = client.chat_stream(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.62,
        max_tokens=3000,
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
