from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from .orchestrator import QuarantineEpisode, RecoverableStageError


class VisualQA:
    def run(self, episode_dir: Path) -> None:
        manifest_path = episode_dir / "magnific_manifest.json"
        if not manifest_path.exists():
            raise QuarantineEpisode("Visual QA requires magnific_manifest.json")
        manifest = json.loads(manifest_path.read_text())
        items = manifest.get("items") or []
        image_dir = episode_dir / "images"
        failures: list[dict] = []
        hashes: dict[str, int] = {}

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

            item["qa"] = {"passed": not reasons, "reasons": reasons}
            item["status"] = "complete" if not reasons else "failed_qa"
            if reasons:
                failures.append({"index": idx, "filename": item["filename"], "reasons": reasons})

        manifest["visual_qa"] = {
            "passed": not failures,
            "checked": len(items),
            "failures": failures,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        (episode_dir / "visual_qa.json").write_text(json.dumps(manifest["visual_qa"], indent=2))
        if failures:
            raise RecoverableStageError(f"{len(failures)} scene images failed visual QA")


def build_visual_qa_handler(root: Path):
    return lambda episode_id: VisualQA().run(root / episode_id)
