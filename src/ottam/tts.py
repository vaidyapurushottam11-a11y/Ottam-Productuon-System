from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import time

import httpx

from .orchestrator import RecoverableStageError, QuarantineEpisode


@dataclass(frozen=True)
class KokoroConfig:
    base_url: str
    voice: str = "am_echo"
    speed: float = 1.0
    poll_interval_seconds: float = 3.0
    timeout_seconds: float = 1800.0


class KokoroModalClient:
    """Async client for the deployed OTTAM Kokoro Modal web API.

    Contract is based on the deployed service source:
      POST /api/submit -> {call_id}
      GET  /api/status?call_id=... -> running/done metadata
      GET  /api/audio?call_id=... -> WAV bytes
      GET  /api/srt?call_id=...&style=sentences -> SRT
      GET  /api/json?call_id=... -> sentence timing JSON
    """

    def __init__(self, config: KokoroConfig):
        self.config = config
        self.base_url = config.base_url.rstrip("/")

    def generate(self, script: str, output_dir: Path) -> dict:
        if not script.strip():
            raise QuarantineEpisode("Cannot generate narration from an empty script")

        output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "text": script,
            "voice": self.config.voice,
            "speed": self.config.speed,
        }

        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                submit = client.post(f"{self.base_url}/api/submit", json=payload)
                submit.raise_for_status()
                body = submit.json()
                call_id = body.get("call_id")
                if not call_id:
                    raise RecoverableStageError(
                        f"Kokoro submit returned no call_id: {body}"
                    )

                status = self._poll(client, call_id)
                if status.get("ok") is False:
                    raise RecoverableStageError(
                        f"Kokoro generation failed: {status.get('error', 'unknown error')}"
                    )

                audio = client.get(f"{self.base_url}/api/audio", params={"call_id": call_id})
                audio.raise_for_status()
                srt = client.get(
                    f"{self.base_url}/api/srt",
                    params={"call_id": call_id, "style": "sentences"},
                )
                srt.raise_for_status()
                timings = client.get(f"{self.base_url}/api/json", params={"call_id": call_id})
                timings.raise_for_status()

        except httpx.TimeoutException as exc:
            raise RecoverableStageError(f"Kokoro HTTP timeout: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            msg = exc.response.text[:500]
            if code >= 500 or code == 429:
                raise RecoverableStageError(f"Kokoro HTTP {code}: {msg}") from exc
            raise QuarantineEpisode(f"Kokoro request rejected with HTTP {code}: {msg}") from exc
        except httpx.RequestError as exc:
            raise RecoverableStageError(f"Kokoro network error: {exc}") from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise RecoverableStageError(f"Kokoro returned invalid JSON: {exc}") from exc

        narration_path = output_dir / "narration.wav"
        captions_path = output_dir / "captions.srt"
        timings_path = output_dir / "sentences.json"
        metadata_path = output_dir / "tts_metadata.json"

        narration_path.write_bytes(audio.content)
        captions_path.write_text(srt.text, encoding="utf-8")
        sentence_data = timings.json()
        timings_path.write_text(json.dumps(sentence_data, indent=2, ensure_ascii=False), encoding="utf-8")

        metadata = {
            "provider": "kokoro_modal",
            "call_id": call_id,
            "voice": status.get("voice", self.config.voice),
            "speed": status.get("speed", self.config.speed),
            "sample_rate": status.get("sample_rate"),
            "duration_seconds": status.get("duration_seconds"),
            "num_words": status.get("num_words"),
            "num_sentences": status.get("num_sentences"),
            "warnings": status.get("warnings", []),
            "timing": status.get("timing", {}),
            "artifacts": {
                "audio": str(narration_path),
                "captions": str(captions_path),
                "sentences": str(timings_path),
            },
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return metadata

    def _poll(self, client: httpx.Client, call_id: str) -> dict:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            response = client.get(f"{self.base_url}/api/status", params={"call_id": call_id})
            response.raise_for_status()
            body = response.json()
            if body.get("status") == "running":
                time.sleep(self.config.poll_interval_seconds)
                continue
            return body
        raise RecoverableStageError(
            f"Kokoro job {call_id} did not finish within {self.config.timeout_seconds:.0f}s"
        )


def generate_episode_narration(
    episode_id: str,
    *,
    runtime_root: Path = Path("runtime/episodes"),
    base_url: str,
    voice: str = "am_echo",
    speed: float = 1.0,
) -> dict:
    episode_dir = runtime_root / episode_id
    script_path = episode_dir / "script.txt"
    if not script_path.exists():
        raise QuarantineEpisode(f"Missing approved script: {script_path}")

    client = KokoroModalClient(KokoroConfig(base_url=base_url, voice=voice, speed=speed))
    return client.generate(script_path.read_text(encoding="utf-8"), episode_dir / "audio")
