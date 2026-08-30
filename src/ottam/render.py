from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .orchestrator import QuarantineEpisode, RecoverableStageError


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecoverableStageError(proc.stderr[-3000:])


class FFmpegRenderer:
    """Deterministic renderer driven by storyboard timings and numbered scene images."""

    def render(self, episode_dir: Path) -> None:
        storyboard_path = episode_dir / "storyboard.json"
        narration = episode_dir / "narration.wav"
        if not storyboard_path.exists() or not narration.exists():
            raise QuarantineEpisode("Render requires storyboard.json and narration.wav")

        try:
            payload = json.loads(storyboard_path.read_text())
            scenes = payload["scenes"]
        except Exception as exc:
            raise QuarantineEpisode(f"Invalid storyboard JSON: {exc}") from exc
        if not scenes:
            raise QuarantineEpisode("Storyboard contains no scenes")

        temp_dir = episode_dir / "render_tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        concat_entries: list[str] = []

        for idx, scene in enumerate(scenes, 1):
            src = episode_dir / "images" / f"{idx:04d}.png"
            if not src.exists():
                raise QuarantineEpisode(f"Missing scene image: {src.name}")
            try:
                duration = float(scene["end"]) - float(scene["start"])
            except Exception as exc:
                raise QuarantineEpisode(f"Invalid timing for scene {idx}") from exc
            if duration <= 0:
                raise QuarantineEpisode(f"Non-positive duration for scene {idx}")

            clip = temp_dir / f"{idx:04d}.mp4"
            motion = str(scene.get("motion", "static"))
            frames = max(1, round(duration * 30))
            if motion == "push_in":
                vf = f"scale=1920:1080,zoompan=z='min(zoom+0.0007,1.08)':d={frames}:s=1920x1080:fps=30"
            elif motion == "pull_out":
                vf = f"scale=2048:1152,zoompan=z='if(eq(on,1),1.08,max(zoom-0.0007,1.0))':d={frames}:s=1920x1080:fps=30"
            else:
                vf = "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30"

            _run([
                "ffmpeg","-y","-loop","1","-i",str(src),"-t",f"{duration:.3f}",
                "-vf",vf,"-an","-c:v","libx264","-pix_fmt","yuv420p","-r","30",str(clip)
            ])
            concat_entries.append(f"file '{clip.resolve().as_posix()}'")

        concat_file = temp_dir / "concat.txt"
        concat_file.write_text("\n".join(concat_entries) + "\n")
        visuals = temp_dir / "visuals.mp4"
        _run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat_file),"-c","copy",str(visuals)])

        final = episode_dir / "final.mp4"
        _run([
            "ffmpeg","-y","-i",str(visuals),"-i",str(narration),
            "-map","0:v:0","-map","1:a:0","-c:v","copy","-c:a","aac","-b:a","192k",
            "-shortest","-movflags","+faststart",str(final)
        ])


def build_render_handler(root: Path):
    return lambda episode_id: FFmpegRenderer().render(root / episode_id)
