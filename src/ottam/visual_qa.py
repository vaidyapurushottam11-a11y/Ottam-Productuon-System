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


def build_visual_qa_handler(root: Path):
    return lambda episode_id: VisualQA().run(root / episode_id)
