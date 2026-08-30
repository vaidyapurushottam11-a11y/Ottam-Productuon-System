from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from .orchestrator import RecoverableStageError


class VideoQA:
    def _profile(self, episode_dir: Path) -> dict:
        path = Path("config/episodes") / f"{episode_dir.name}.yaml"
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def run(self, episode_dir: Path) -> None:
        video = episode_dir / "final.mp4"
        if not video.exists() or video.stat().st_size < 100_000:
            raise RecoverableStageError("final.mp4 missing or unexpectedly small")

        cmd = [
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RecoverableStageError(f"ffprobe failed: {proc.stderr[-1500:]}")
        try:
            info = json.loads(proc.stdout)
        except Exception as exc:
            raise RecoverableStageError(f"Could not parse ffprobe output: {exc}") from exc

        streams = info.get("streams") or []
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        blockers: list[str] = []
        if not videos:
            blockers.append("missing video stream")
        if not audios:
            blockers.append("missing audio stream")
        if videos:
            v = videos[0]
            if int(v.get("width") or 0) != 1920 or int(v.get("height") or 0) != 1080:
                blockers.append(f"unexpected resolution {v.get('width')}x{v.get('height')}")
            if v.get("codec_name") not in {"h264", "avc1"}:
                blockers.append(f"unexpected video codec {v.get('codec_name')}")
            frame_rate = str(v.get("avg_frame_rate") or "0/1")
            try:
                num, den = frame_rate.split("/", 1)
                fps = float(num) / float(den)
                if fps < 23.0 or fps > 61.0:
                    blockers.append(f"unexpected frame rate {fps:.3f}")
            except Exception:
                blockers.append(f"unreadable frame rate {frame_rate}")
        if audios and audios[0].get("codec_name") != "aac":
            blockers.append(f"unexpected audio codec {audios[0].get('codec_name')}")

        duration = float((info.get("format") or {}).get("duration") or 0.0)
        profile = self._profile(episode_dir)
        target = profile.get("episode", {}).get("target_minutes", {})
        if target:
            minimum = float(target.get("min", 0.0)) * 60.0
            maximum = float(target.get("max", 10_000.0)) * 60.0
            if not minimum <= duration <= maximum:
                blockers.append(
                    f"duration {duration:.2f}s outside locked upload-candidate window {minimum:.0f}-{maximum:.0f}s"
                )
        elif duration < 60:
            blockers.append(f"video duration suspiciously short: {duration:.2f}s")

        report = {
            "passed": not blockers,
            "production_class": profile.get("episode", {}).get("production_class", "standard"),
            "duration_seconds": round(duration, 3),
            "blockers": blockers,
            "video_size_bytes": video.stat().st_size,
        }
        (episode_dir / "video_qa.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if blockers:
            raise RecoverableStageError("; ".join(blockers))


def build_video_qa_handler(root: Path):
    return lambda episode_id: VideoQA().run(root / episode_id)
