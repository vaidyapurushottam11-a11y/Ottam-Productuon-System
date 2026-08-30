from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .magnific_api import MagnificApiClient
from .youtube_publish import (
    YouTubePublishError,
    load_encrypted_session,
    refresh_access_token,
    save_encrypted_session,
    set_thumbnail,
)


REFERENCE_STYLE = """OTTAM YouTube thumbnail style derived from the channel's existing thumbnails:
- purpose-built thumbnail illustration, never a random video frame
- one dominant emotional subject/action readable at phone size
- simple single visual metaphor with minimal clutter
- saturated warm orange/yellow focal lighting against strong blue/orange contrast
- hand-drawn cartoon/explainer feel with bold black outlines and expressive face/body language
- high contrast, dramatic but playful, instantly understandable
- reserve the top 22-28% of the image as clean composition space for headline text
- absolutely no generated words, letters, captions, logos, watermarks, UI, labels, or signage
- 16:9 YouTube thumbnail composition
"""


def _load_raw_publish_config(episode_id: str, repo_root: Path) -> dict:
    path = repo_root / "config" / "publish" / f"{episode_id}.yaml"
    if not path.exists():
        raise YouTubePublishError(f"Publish config missing: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _fit_thumbnail(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    target_w, target_h = 1280, 720
    target_ratio = target_w / target_h
    current_ratio = img.width / img.height
    if current_ratio > target_ratio:
        crop_w = int(img.height * target_ratio)
        left = (img.width - crop_w) // 2
        img = img.crop((left, 0, left + crop_w, img.height))
    else:
        crop_h = int(img.width / target_ratio)
        top = (img.height - crop_h) // 2
        img = img.crop((0, top, img.width, top + crop_h))
    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.08)
    img = ImageEnhance.Sharpness(img).enhance(1.08)
    return img


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _fit_headline(draw: ImageDraw.ImageDraw, text: str, max_width: int = 1190) -> tuple[ImageFont.ImageFont, list[str]]:
    words = text.upper().split()
    if not words:
        raise YouTubePublishError("thumbnail_text is required")

    for size in range(132, 67, -4):
        font = _font(size)
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), candidate, font=font, stroke_width=0)
            if current and bbox[2] - bbox[0] > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        if len(lines) <= 2:
            heights = [draw.textbbox((0, 0), line, font=font)[3] for line in lines]
            if sum(heights) + 8 * max(0, len(lines) - 1) <= 205:
                return font, lines

    font = _font(68)
    return font, [" ".join(words[:3]), " ".join(words[3:])][:2]


def _draw_reference_headline(img: Image.Image, text: str) -> Image.Image:
    # Draw text on a separate transparent layer so the thick outline/shadow is crisp.
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font, lines = _fit_headline(draw, text)

    line_boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=0) for line in lines]
    heights = [b[3] - b[1] for b in line_boxes]
    total_h = sum(heights) + 2 * max(0, len(lines) - 1)
    y = max(16, int((205 - total_h) / 2))

    for line, box, h in zip(lines, line_boxes, heights):
        w = box[2] - box[0]
        x = (1280 - w) // 2

        # Deep offset shadow like the existing channel thumbnails.
        draw.text(
            (x + 8, y + 11),
            line,
            font=font,
            fill=(125, 32, 10, 255),
            stroke_width=15,
            stroke_fill=(0, 0, 0, 255),
        )
        # Main warm yellow headline with heavy black keyline.
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(255, 216, 30, 255),
            stroke_width=13,
            stroke_fill=(5, 5, 5, 255),
        )
        # Small highlight to make the yellow pop at phone size.
        draw.text(
            (x, y - 2),
            line,
            font=font,
            fill=(255, 232, 70, 255),
            stroke_width=3,
            stroke_fill=(255, 183, 18, 255),
        )
        y += h + 2

    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def generate_thumbnail(episode_id: str, runtime_root: Path = Path("runtime/episodes"), repo_root: Path = Path(".")) -> Path:
    episode_dir = runtime_root / episode_id
    raw = _load_raw_publish_config(episode_id, repo_root)
    text = str(raw.get("thumbnail_text") or "").strip()
    scene_prompt = str(raw.get("thumbnail_prompt") or "").strip()
    if not scene_prompt:
        raise YouTubePublishError(
            f"thumbnail_prompt is required for {episode_id}; refusing to fall back to a random episode frame"
        )

    prompt = f"{REFERENCE_STYLE}\n\nEPISODE-SPECIFIC THUMBNAIL CONCEPT:\n{scene_prompt}\n\nDo not render the headline; text will be added later in code."

    content, metadata = MagnificApiClient().generate_image(prompt)
    generated = episode_dir / "thumbnail_generated.png"
    generated.write_bytes(content)

    img = _fit_thumbnail(Image.open(generated))
    img = _draw_reference_headline(img, text)

    output = episode_dir / "thumbnail.jpg"
    img.save(output, "JPEG", quality=94, optimize=True, subsampling=0)
    if output.stat().st_size > 1_950_000:
        img.save(output, "JPEG", quality=88, optimize=True)

    record = {
        "episode_id": episode_id,
        "headline": text,
        "prompt": prompt,
        "magnific": metadata,
        "reference_contract": "ottam_existing_thumbnails_v1",
        "output": str(output),
    }
    (episode_dir / "thumbnail_metadata.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"OTTAM_THUMBNAIL_READY episode={episode_id} path={output} credits={metadata.get('credits')}",
        flush=True,
    )
    return output


def replace_remote_thumbnail(episode_id: str, session_path: Path) -> None:
    episode_dir = Path("runtime/episodes") / episode_id
    result_path = episode_dir / "youtube_publish_result.json"
    if not result_path.exists():
        raise YouTubePublishError("youtube_publish_result.json missing; video must be uploaded before thumbnail replacement")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    video_id = result.get("video_id")
    if not video_id:
        raise YouTubePublishError("Published video id missing")

    thumbnail = generate_thumbnail(episode_id)
    session = load_encrypted_session(session_path)
    access_token, session = refresh_access_token(session)
    save_encrypted_session(session, session_path)
    set_thumbnail(access_token, video_id, thumbnail)

    result["thumbnail_complete"] = True
    result["thumbnail_contract"] = "ottam_existing_thumbnails_v1"
    result["thumbnail_source"] = "dedicated_magnific_gpt2"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"OTTAM_THUMBNAIL_REPLACED video_id={video_id}", flush=True)


def main() -> None:
    episode_id = os.getenv("OTTAM_EPISODE_ID", "").strip()
    if not episode_id:
        raise SystemExit("OTTAM_EPISODE_ID is required")
    session_path = Path(
        os.getenv(
            "YOUTUBE_OAUTH_SESSION_FILE",
            "runtime/youtube-auth/youtube-oauth-session.enc.json",
        )
    )
    replace_remote_thumbnail(episode_id, session_path)
