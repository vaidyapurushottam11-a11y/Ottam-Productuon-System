from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .orchestrator import QuarantineEpisode


class MagnificManifestBuilder:
    """Creates an idempotent generation manifest from storyboard.json.

    The manifest is provider-neutral at the orchestration boundary. A Magnific
    transport can fill each scene independently and resume from completed files.
    """

    def build(self, episode_dir: Path) -> None:
        storyboard_path = episode_dir / "storyboard.json"
        if not storyboard_path.exists():
            raise QuarantineEpisode("Magnific manifest requires storyboard.json")
        try:
            data = json.loads(storyboard_path.read_text())
            scenes = data["scenes"]
        except Exception as exc:
            raise QuarantineEpisode(f"Invalid storyboard JSON: {exc}") from exc

        items = []
        for idx, scene in enumerate(scenes, 1):
            prompt = str(scene.get("magnific_prompt") or "").strip()
            if not prompt:
                raise QuarantineEpisode(f"Scene {idx} has no Magnific prompt")
            items.append({
                "scene_id": str(scene.get("scene_id") or idx),
                "index": idx,
                "start": float(scene.get("start") or 0.0),
                "end": float(scene.get("end") or 0.0),
                "filename": f"{idx:04d}.png",
                "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "provider": "magnific",
                "model": "gpt-2",
                "aspect_ratio": "16:9",
                "quality": "low",
                "count": 1,
                "status": "pending",
                "attempts": 0,
            })

        output = {
            "version": 2,
            "provider": "magnific",
            "opening_hook_seconds": float(data.get("opening_hook_seconds") or 30.0),
            "generation_policy": {
                "regenerate_failed_scene_only": True,
                "fixed_scene_count": False,
                "expected_images": len(items),
            },
            "items": items,
        }
        (episode_dir / "magnific_manifest.json").write_text(json.dumps(output, indent=2, ensure_ascii=False))


def build_magnific_manifest_handler(root: Path):
    return lambda episode_id: MagnificManifestBuilder().build(root / episode_id)
