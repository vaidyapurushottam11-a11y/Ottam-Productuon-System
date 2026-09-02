from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageEnhance

from .magnific_api import MagnificApiClient

REFERENCE_STYLE = """OTTAM YouTube thumbnail style:
- purpose-built YouTube thumbnail, never a random video frame
- one dominant expressive stickman/action readable instantly on a phone
- one simple visual metaphor, minimal clutter
- hand-drawn cartoon/explainer feel with bold black outlines and expressive face/body language
- strong visual hierarchy: hook text and main subject must both be readable at phone size
- hook text should feel designed as part of the thumbnail, not pasted on afterward
- large bold condensed display lettering, high contrast, thick outline/shadow where useful
- keep important subject and hook text inside safe margins; do not crowd the edges
- use a topic-driven palette with strong foreground/background separation; do NOT default to dark navy/deep-blue backgrounds
- backgrounds should usually be bright, mid-tone, or strongly contrasting rather than uniformly dark
- deliberately vary palette between episodes: cream, off-white, warm yellow, orange, coral, red, mint, teal, cyan, sky blue, lavender, magenta, lime, or other concept-appropriate colors are all allowed
- dark blue may be used only when the episode concept genuinely benefits from it and the overall thumbnail remains bright and highly legible
- avoid repeating the same blue/orange look across consecutive thumbnails
- no logos, watermarks, UI, signage, labels, or any text except the exact requested hook
- 16:9 composition
"""


def _fit(img: Image.Image) -> Image.Image:
    """Normalize Magnific output for YouTube without redesigning the image.

    The composition, including typography, is generated entirely by Magnific.
    We only resize and lightly normalize the finished image for delivery.
    """
    img = img.convert("RGB")
    img = img.resize((1280, 720), Image.Resampling.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.05)
    return ImageEnhance.Sharpness(img).enhance(1.05)


def generate_thumbnail(
    episode_id: str,
    instruction: str = "",
    runtime_root: Path = Path("runtime/episodes"),
) -> Path:
    episode_dir = runtime_root / episode_id
    package_path = episode_dir / "upload_package.json"
    if not package_path.exists():
        raise RuntimeError("upload_package.json is required before thumbnail generation")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    base_prompt = str(package.get("thumbnail_prompt") or "").strip()
    headline = str(package.get("thumbnail_text") or "").strip()
    if not base_prompt or not headline:
        raise RuntimeError("Upload package is missing thumbnail_prompt or thumbnail_text")

    extra = instruction.strip()
    exact_hook = headline.upper()
    prompt = f"""{REFERENCE_STYLE}

EPISODE CONCEPT:
{base_prompt}

HOOK TEXT — MUST BE RENDERED INSIDE THE IMAGE:
{exact_hook}

COLOR / VISIBILITY REQUIREMENTS:
- Choose the palette from the episode concept, not from a fixed OTTAM background color.
- Do NOT automatically use a dark navy or deep-blue background.
- Prefer a bright or mid-tone background when it gives stronger phone-size visibility.
- The main character/object must separate clearly from the background at a glance.
- The hook text must have very high luminance/edge contrast against whatever sits behind it.
- Avoid large low-detail dark areas. The thumbnail should remain visually clear at small size and in YouTube dark mode.
- Treat palette variety as part of the channel design: this thumbnail should not look like a recolor of the previous episode.

TYPOGRAPHY REQUIREMENTS:
- Render the hook text exactly as written above, with the same words and spelling.
- Do not add, remove, paraphrase, repeat, or invent any other words.
- Make the hook a major designed element of the composition, balanced with the main visual subject.
- Use 1-2 short lines maximum when possible; keep it immediately readable on a small phone thumbnail.
- Choose text color based on the background for maximum contrast; yellow, white, cream, black, red, orange, or another high-contrast choice is allowed.
- Use a thick contrasting outline or shadow when it improves separation.
- Position text where it naturally works with the subject; do not simply reserve an empty strip at the top.
- The finished result must already look like a publish-ready YouTube thumbnail. No later text overlay will be added.
"""
    if extra:
        prompt += f"""

USER REVISION INSTRUCTION:
{extra}
Apply this revision while keeping the exact hook text and all OTTAM thumbnail rules unchanged.
"""

    content, metadata = MagnificApiClient().generate_image(prompt)
    versions = episode_dir / "thumbnail_versions"
    versions.mkdir(parents=True, exist_ok=True)
    existing = sorted(versions.glob("thumbnail_*.jpg"))
    version = len(existing) + 1

    generated_path = versions / f"generated_{version:03d}.png"
    generated_path.write_bytes(content)

    image = _fit(Image.open(generated_path))
    version_path = versions / f"thumbnail_{version:03d}.jpg"
    image.save(version_path, "JPEG", quality=94, optimize=True, subsampling=0)
    if version_path.stat().st_size > 1_950_000:
        image.save(version_path, "JPEG", quality=88, optimize=True)

    output = episode_dir / "thumbnail.jpg"
    output.write_bytes(version_path.read_bytes())
    record = {
        "episode_id": episode_id,
        "version": version,
        "instruction": extra,
        "headline": exact_hook,
        "headline_rendered_by": "magnific",
        "post_generation_text_overlay": False,
        "palette_policy": "topic_driven_high_visibility_v2",
        "prompt": prompt,
        "magnific": metadata,
        "output": str(output),
    }
    (episode_dir / "thumbnail_metadata.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"OTTAM_THUMBNAIL_READY episode={episode_id} version={version} credits={metadata.get('credits')}",
        flush=True,
    )
    return output


def main() -> None:
    episode_id = os.getenv("OTTAM_EPISODE_ID", "").strip()
    if not episode_id:
        raise SystemExit("OTTAM_EPISODE_ID is required")
    generate_thumbnail(episode_id, os.getenv("OTTAM_REGEN_INSTRUCTION", ""))
