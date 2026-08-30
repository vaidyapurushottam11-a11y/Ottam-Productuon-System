from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .orchestrator import QuarantineEpisode, RecoverableStageError


class VideoQA:
    def run(self, episode_dir: Path) -> None:
        video = episode_dir / "final.mp4"
        narration = episode_dir / "narration.wav"
        if not video.exists() or video.stat().st_size < 100_000:
            raise RecoverableStageError("final.mp4 missing or unexpectedly small")

        cmd = [
            "ffprobe","-v","error","-show_streams","-show_format","-of","json",str(video)
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
        if audios and audios[0].get("codec_name") != "aac":
            blockers.append(f"unexpected audio codec {audios[0].get('codec_name')}")

        duration = float((info.get("format") or {}).get("duration") or 0.0)
        if duration < 60:
            blockers.append(f"video duration suspiciously short: {duration:.2f}s")

        report = {
            "passed": not blockers,
            "duration_seconds": round(duration, 3),
            "blockers": blockers,
            "video_size_bytes": video.stat().st_size,
        }
        (episode_dir / "video_qa.json").write_text(json.dumps(report, indent=2))
        if blockers:
            raise RecoverableStageError("; ".join(blockers))


def build_video_qa_handler(root: Path):
    return lambda episode_id: VideoQA().run(root / episode_id)
