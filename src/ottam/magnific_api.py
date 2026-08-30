from __future__ import annotations

from io import BytesIO
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .orchestrator import QuarantineEpisode, RecoverableStageError


@dataclass(frozen=True)
class MagnificConfig:
    """Locked OTTAM production settings for Magnific."""

    mode: str = "gpt-2"
    aspect_ratio: str = "16:9"
    quality: str = "low"
    expected_width: int = 1344
    expected_height: int = 752
    max_credits_per_image: int = 15
    timeout_seconds: int = 900
    bridge_script: str = "scripts/magnific_mcp_bridge.mjs"


class MagnificApiClient:
    """Magnific GPT-2 client backed by the authenticated remote MCP.

    The normal Magnific API key does not authorize the GPT-2 MCP endpoint.
    GitHub restores a one-time user-authorized, encrypted OAuth refresh session;
    this client invokes the Node MCP bridge which refreshes that session and
    calls `images_generate` with OTTAM's locked GPT-2/16:9/low settings.
    """

    def __init__(self, config: MagnificConfig | None = None):
        self.config = config or MagnificConfig()
        self.api_key = os.getenv("MAGNIFIC_API_KEY")
        if not self.api_key:
            raise QuarantineEpisode("MAGNIFIC_API_KEY is not configured")
        self.oauth_session_file = Path(
            os.getenv(
                "MAGNIFIC_OAUTH_SESSION_FILE",
                "runtime/magnific-auth/magnific-oauth-session.enc.json",
            )
        )
        if not self.oauth_session_file.exists():
            raise QuarantineEpisode(
                "Magnific OAuth session is unavailable; restore the encrypted "
                "magnific-oauth-session artifact before image generation"
            )
        self.bridge_script = Path(
            os.getenv("MAGNIFIC_MCP_BRIDGE", self.config.bridge_script)
        )
        if not self.bridge_script.exists():
            raise QuarantineEpisode(
                f"Magnific MCP bridge is missing: {self.bridge_script}"
            )

    def generate_image(self, prompt: str) -> tuple[bytes, dict]:
        if not prompt.strip():
            raise QuarantineEpisode("Cannot generate a Magnific image from an empty prompt")

        with tempfile.TemporaryDirectory(prefix="ottam-magnific-") as tmp:
            work = Path(tmp)
            request_path = work / "request.json"
            output_path = work / "image.png"
            metadata_path = work / "metadata.json"
            request_path.write_text(
                json.dumps(
                    {
                        "prompt": prompt,
                        "mode": self.config.mode,
                        "aspect_ratio": self.config.aspect_ratio,
                        "quality": self.config.quality,
                        "output_path": str(output_path),
                        "metadata_path": str(metadata_path),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["MAGNIFIC_OAUTH_SESSION_FILE"] = str(self.oauth_session_file)
            try:
                proc = subprocess.run(
                    ["node", str(self.bridge_script), str(request_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=self.config.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise RecoverableStageError(
                    f"Magnific GPT-2 bridge timed out after {self.config.timeout_seconds}s"
                ) from exc
            except OSError as exc:
                raise QuarantineEpisode(
                    f"Unable to start Magnific MCP bridge (Node.js required): {exc}"
                ) from exc

            if proc.returncode != 0:
                message = (proc.stderr or proc.stdout or "unknown bridge failure")[-3000:]
                lowered = message.lower()
                if any(token in lowered for token in ("429", "timeout", "timed out", "5xx", "502", "503", "504", "network")):
                    raise RecoverableStageError(f"Magnific GPT-2 bridge failed: {message}")
                if any(token in lowered for token in ("refresh", "401", "unauthenticated", "invalid_token")):
                    raise QuarantineEpisode(
                        "Magnific user authorization is no longer valid; reauthorization is required. "
                        f"Bridge detail: {message}"
                    )
                raise RecoverableStageError(f"Magnific GPT-2 bridge failed: {message}")

            if not output_path.exists() or not metadata_path.exists():
                raise RecoverableStageError(
                    f"Magnific GPT-2 bridge exited successfully without expected artifacts: {proc.stdout[-1500:]}"
                )
            content = output_path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self._validate_output(content, metadata)
            return content, metadata

    def _validate_output(self, content: bytes, metadata: dict) -> None:
        if len(content) < 4096:
            raise RecoverableStageError(
                f"Magnific image is unexpectedly small ({len(content)} bytes)"
            )
        if metadata.get("mode") != self.config.mode:
            raise QuarantineEpisode(
                f"Magnific model drift detected: expected {self.config.mode}, got {metadata.get('mode')}"
            )
        if metadata.get("quality") != self.config.quality:
            raise QuarantineEpisode(
                f"Magnific quality drift detected: expected {self.config.quality}, got {metadata.get('quality')}"
            )
        if metadata.get("aspect_ratio") != self.config.aspect_ratio:
            raise QuarantineEpisode(
                "Magnific aspect-ratio drift detected: "
                f"expected {self.config.aspect_ratio}, got {metadata.get('aspect_ratio')}"
            )
        credits = metadata.get("credits")
        if credits is not None and int(credits) > self.config.max_credits_per_image:
            raise QuarantineEpisode(
                f"Magnific cost guard tripped: {credits} credits > "
                f"{self.config.max_credits_per_image} credits/image"
            )
        try:
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                if (width, height) != (
                    self.config.expected_width,
                    self.config.expected_height,
                ):
                    raise RecoverableStageError(
                        "Magnific GPT-2 returned unexpected dimensions "
                        f"{width}x{height}; expected "
                        f"{self.config.expected_width}x{self.config.expected_height}"
                    )
                image.verify()
        except RecoverableStageError:
            raise
        except Exception as exc:
            raise RecoverableStageError(
                f"Magnific returned a non-decodable image: {exc}"
            ) from exc


class MagnificEpisodeGenerator:
    """Resumable per-scene generation; completed scenes are never regenerated."""

    def __init__(self, client: MagnificApiClient | None = None):
        self.client = client or MagnificApiClient()

    def generate(self, episode_dir: Path) -> None:
        manifest_path = episode_dir / "magnific_manifest.json"
        if not manifest_path.exists():
            raise QuarantineEpisode("Image generation requires magnific_manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        images_dir = episode_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        for item in manifest.get("items", []):
            filename = str(item["filename"])
            target = images_dir / filename
            if item.get("status") == "complete" and target.exists() and target.stat().st_size > 0:
                continue

            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["status"] = "running"
            self._save(manifest_path, manifest)

            try:
                content, metadata = self.client.generate_image(str(item["prompt"]))
                target.write_bytes(content)
                item["status"] = "complete"
                item["transport"] = metadata
                item["last_error"] = None
            except Exception as exc:
                item["status"] = "failed"
                item["last_error"] = str(exc)
                self._save(manifest_path, manifest)
                raise
            self._save(manifest_path, manifest)

    @staticmethod
    def _save(path: Path, manifest: dict) -> None:
        path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def build_magnific_generate_handler(root: Path):
    return lambda episode_id: MagnificEpisodeGenerator().generate(root / episode_id)
