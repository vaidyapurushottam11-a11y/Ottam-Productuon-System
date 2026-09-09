from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageStat

from .orchestrator import QuarantineEpisode, RecoverableStageError


class VisualQA:
    HOOK_SECONDS = 30.0

    @staticmethod
    def _opening_metrics(path: Path) -> dict[str, float]:
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            gray = rgb.convert("L")
            hsv = rgb.convert("HSV")
            contrast = float(ImageStat.Stat(gray).stddev[0])
            saturation = float(ImageStat.Stat(hsv).mean[1])
            entropy = float(gray.entropy())
        return {
            "contrast": round(contrast, 2),
            "saturation": round(saturation, 2),
            "entropy": round(entropy, 2),
        }

    def run(self, episode_dir: Path) -> None:
        manifest_path = episode_dir / "magnific_manifest.json"
        if not manifest_path.exists():
            raise QuarantineEpisode("Visual QA requires magnific_manifest.json")
        manifest = json.loads(manifest_path.read_text())
        items = manifest.get("items") or []
        image_dir = episode_dir / "images"
        failures: list[dict] = []
        hashes: dict[str, int] = {}
        hook_seconds = float(manifest.get("opening_hook_seconds") or self.HOOK_SECONDS)
        opening_reports: list[dict] = []

        for item in items:
            idx = int(item["index"])
            path = image_dir / item["filename"]
            reasons: list[str] = []
            if not path.exists() or path.stat().st_size == 0:
                reasons.append("missing")
            else:
                try:
                    with Image.open(path) as im:
                        im.verify()
                    with Image.open(path) as im:
                        w, h = im.size
                        if w <= 0 or h <= 0:
                            reasons.append("invalid_dimensions")
                        ratio = w / h if h else 0
                        if abs(ratio - (16 / 9)) > 0.05:
                            reasons.append(f"wrong_aspect_ratio:{w}x{h}")
                except Exception as exc:
                    reasons.append(f"corrupt:{type(exc).__name__}")

                if not reasons:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    if digest in hashes:
                        reasons.append(f"exact_duplicate_of_scene:{hashes[digest]}")
                    else:
                        hashes[digest] = idx

                # The opening needs a higher bar than the rest of the episode.
                # These deterministic checks do not try to judge art; they catch
                # the obviously washed-out / nearly empty frames that are poor
                # first impressions on a phone. Only opening frames get this gate.
                if not reasons and float(item.get("start") or 0.0) < hook_seconds:
                    metrics = self._opening_metrics(path)
                    opening_reports.append({"index": idx, **metrics})
                    if metrics["contrast"] < 20 and metrics["saturation"] < 24:
                        reasons.append("opening_frame_too_flat_or_washed_out")
                    if metrics["entropy"] < 4.2:
                        reasons.append("opening_frame_too_visually_sparse")

            item["qa"] = {"passed": not reasons, "reasons": reasons}
            item["status"] = "complete" if not reasons else "failed_qa"
            if reasons:
                failures.append({"index": idx, "filename": item["filename"], "reasons": reasons})

        manifest["visual_qa"] = {
            "passed": not failures,
            "checked": len(items),
            "opening_hook_seconds": hook_seconds,
            "opening_reports": opening_reports,
            "failures": failures,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        (episode_dir / "visual_qa.json").write_text(json.dumps(manifest["visual_qa"], indent=2))
        if failures:
            raise RecoverableStageError(f"{len(failures)} scene images failed visual QA")


def _qa_recovery_instruction(reasons: list[str]) -> str:
    guidance: list[str] = []
    if "opening_frame_too_visually_sparse" in reasons:
        guidance.append(
            "Make the frame visually richer and immediately readable on a phone: use a large foreground subject, "
            "clear supporting visual elements, stronger foreground/background separation, and a filled colorful "
            "environment; avoid mostly empty or plain off-white space."
        )
    if "opening_frame_too_flat_or_washed_out" in reasons:
        guidance.append(
            "Increase bold color contrast and saturation with clearly separated subject and background shapes; "
            "avoid pale, washed-out, low-contrast composition."
        )
    if any(reason.startswith("exact_duplicate_of_scene:") for reason in reasons):
        guidance.append(
            "Create a clearly distinct composition and camera framing from the neighboring scenes while preserving "
            "the same recurring character design and narration meaning."
        )
    if not guidance:
        guidance.append(
            "Regenerate this exact failed scene while preserving its narration meaning and character continuity; "
            "correct the listed QA failure without changing unrelated scenes."
        )
    return " QA RECOVERY — " + " ".join(guidance)


def _prepare_failed_items_for_regeneration(episode_dir: Path) -> int:
    """Make only failed-QA manifest entries eligible for a targeted regeneration.

    Completed scene entries stay untouched, so a resume never spends credits on
    images that already passed QA. The recovery prompt is derived from the
    original prompt and replaced in-place rather than appended repeatedly.
    """
    manifest_path = episode_dir / "magnific_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed = 0
    for item in manifest.get("items") or []:
        qa = item.get("qa") or {}
        reasons = list(qa.get("reasons") or [])
        if qa.get("passed") is not False or not reasons:
            continue
        base_prompt = str(item.get("qa_recovery_base_prompt") or item.get("prompt") or "").strip()
        item["qa_recovery_base_prompt"] = base_prompt
        prompt = base_prompt + _qa_recovery_instruction(reasons)
        item["prompt"] = prompt
        item["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        item["status"] = "failed_qa"
        item["qa_recovery_attempts"] = int(item.get("qa_recovery_attempts") or 0) + 1
        changed += 1
    if changed:
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed


def build_visual_qa_handler(root: Path):
    def handler(episode_id: str) -> None:
        episode_dir = root / episode_id
        qa = VisualQA()
        try:
            qa.run(episode_dir)
            return
        except RecoverableStageError:
            # Visual QA used to retry the exact same failed file four times.
            # Instead, regenerate only entries marked failed_qa, then re-check
            # immediately. MagnificEpisodeGenerator skips every completed image.
            failed_count = _prepare_failed_items_for_regeneration(episode_dir)
            if failed_count <= 0:
                raise

        from .magnific_api import MagnificEpisodeGenerator

        MagnificEpisodeGenerator().generate(episode_dir)
        qa.run(episode_dir)

    return handler
