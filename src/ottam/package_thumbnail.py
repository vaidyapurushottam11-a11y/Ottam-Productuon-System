from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from .magnific_api import MagnificApiClient

REFERENCE_STYLE = """OTTAM YouTube thumbnail style:
- purpose-built thumbnail illustration, never a random video frame
- one dominant expressive stickman/action readable instantly on a phone
- one simple visual metaphor, minimal clutter
- saturated warm orange/yellow focal lighting against deep blue contrast
- hand-drawn cartoon/explainer feel with bold black outlines and expressive face/body language
- reserve the top 22-28% as calm space for headline text
- absolutely no generated words, letters, labels, logos, watermarks, UI or signage
- 16:9 composition
"""


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _fit(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    w, h = 1280, 720
    ratio = w / h
    current = img.width / img.height
    if current > ratio:
        crop_w = int(img.height * ratio)
        left = (img.width - crop_w) // 2
        img = img.crop((left, 0, left + crop_w, img.height))
    else:
        crop_h = int(img.width / ratio)
        top = (img.height - crop_h) // 2
        img = img.crop((0, top, img.width, top + crop_h))
    img = img.resize((w, h), Image.Resampling.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.08)
    return ImageEnhance.Sharpness(img).enhance(1.08)


def _headline(img: Image.Image, text: str) -> Image.Image:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    words = text.upper().split()
    best_font = _font(92)
    lines = [" ".join(words)]
    for size in range(132, 67, -4):
        font = _font(size)
        candidate: list[str] = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            box = draw.textbbox((0, 0), test, font=font)
            if current and box[2] > 1180:
                candidate.append(current)
                current = word
            else:
                current = test
        if current:
            candidate.append(current)
        if len(candidate) <= 2:
            best_font, lines = font, candidate
            break
    y = 20
    for line in lines:
        box = draw.textbbox((0, 0), line, font=best_font)
        width = box[2] - box[0]
        x = (1280 - width) // 2
        draw.text((x + 8, y + 10), line, font=best_font, fill=(125, 32, 10, 255), stroke_width=15, stroke_fill=(0, 0, 0, 255))
        draw.text((x, y), line, font=best_font, fill=(255, 224, 38, 255), stroke_width=12, stroke_fill=(5, 5, 5, 255))
        y += (box[3] - box[1]) + 2
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


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
    prompt = f"{REFERENCE_STYLE}\n\nEPISODE CONCEPT:\n{base_prompt}"
    if extra:
        prompt += f"\n\nUSER REVISION INSTRUCTION:\n{extra}\nKeep all other OTTAM thumbnail rules unchanged."
    prompt += "\n\nDo not render the headline; it will be added in code."

    content, metadata = MagnificApiClient().generate_image(prompt)
    versions = episode_dir / "thumbnail_versions"
    versions.mkdir(parents=True, exist_ok=True)
    existing = sorted(versions.glob("thumbnail_*.jpg"))
    version = len(existing) + 1
    generated_path = versions / f"generated_{version:03d}.png"
    generated_path.write_bytes(content)

    image = _headline(_fit(Image.open(generated_path)), headline)
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
        "headline": headline,
        "prompt": prompt,
        "magnific": metadata,
        "output": str(output),
    }
    (episode_dir / "thumbnail_metadata.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OTTAM_THUMBNAIL_READY episode={episode_id} version={version} credits={metadata.get('credits')}", flush=True)
    return output


def main() -> None:
    episode_id = os.getenv("OTTAM_EPISODE_ID", "").strip()
    if not episode_id:
        raise SystemExit("OTTAM_EPISODE_ID is required")
    generate_thumbnail(episode_id, os.getenv("OTTAM_REGEN_INSTRUCTION", ""))
