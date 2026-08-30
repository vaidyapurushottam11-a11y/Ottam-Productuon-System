from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .orchestrator import QuarantineEpisode, RecoverableStageError


@dataclass(frozen=True)
class MagnificConfig:
    base_url: str = "https://api.magnific.com"
    endpoint: str = "/v1/ai/text-to-image/seedream-v4-5"
    aspect_ratio: str = "widescreen_16_9"
    poll_interval_seconds: float = 4.0
    timeout_seconds: float = 900.0


class MagnificApiClient:
    """Headless Magnific REST client for GitHub Actions.

    The current public REST documentation does not expose the MCP/web-app `gpt-2`
    image model. Until that endpoint becomes public, the production transport uses
    the documented Seedream 4.5 REST endpoint while preserving the locked OTTAM
    prompt style. The endpoint is configurable via MAGNIFIC_IMAGE_ENDPOINT.
    """

    def __init__(self, api_key: str | None = None, config: MagnificConfig | None = None):
        self.api_key = api_key or os.getenv("MAGNIFIC_API_KEY")
        if not self.api_key:
            raise QuarantineEpisode("MAGNIFIC_API_KEY is not configured")
        endpoint = os.getenv("MAGNIFIC_IMAGE_ENDPOINT")
        cfg = config or MagnificConfig()
        if endpoint:
            cfg = MagnificConfig(
                base_url=cfg.base_url,
                endpoint=endpoint,
                aspect_ratio=cfg.aspect_ratio,
                poll_interval_seconds=cfg.poll_interval_seconds,
                timeout_seconds=cfg.timeout_seconds,
            )
        self.config = cfg

    @property
    def headers(self) -> dict[str, str]:
        return {
            "x-magnific-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def generate_image(self, prompt: str) -> tuple[bytes, dict]:
        if not prompt.strip():
            raise QuarantineEpisode("Cannot generate a Magnific image from an empty prompt")

        payload = {
            "prompt": prompt,
            "aspect_ratio": self.config.aspect_ratio,
            "enable_safety_checker": True,
        }
        url = self.config.base_url.rstrip("/") + self.config.endpoint

        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                response = client.post(url, headers=self.headers, json=payload)
                self._raise_for_status(response, "submit")
                body = response.json().get("data", {})
                task_id = body.get("task_id")
                if not task_id:
                    raise RecoverableStageError(f"Magnific submit returned no task_id: {body}")

                result = self._poll(client, url, str(task_id))
                generated = result.get("generated") or []
                if not generated:
                    raise RecoverableStageError(
                        f"Magnific task {task_id} completed without generated image URL"
                    )
                image_url = str(generated[0])
                image = client.get(image_url)
                self._raise_for_status(image, "download")
                metadata = {
                    "task_id": str(task_id),
                    "status": result.get("status"),
                    "generated_url": image_url,
                    "endpoint": self.config.endpoint,
                    "aspect_ratio": self.config.aspect_ratio,
                }
                return image.content, metadata
        except (RecoverableStageError, QuarantineEpisode):
            raise
        except httpx.TimeoutException as exc:
            raise RecoverableStageError(f"Magnific timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise RecoverableStageError(f"Magnific network error: {exc}") from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise RecoverableStageError(f"Magnific returned invalid JSON: {exc}") from exc

    def _poll(self, client: httpx.Client, base_task_url: str, task_id: str) -> dict:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            response = client.get(f"{base_task_url}/{task_id}", headers=self.headers)
            self._raise_for_status(response, "status")
            data = response.json().get("data", {})
            status = str(data.get("status", "")).upper()
            if status == "COMPLETED":
                return data
            if status in {"FAILED", "ERROR", "CANCELLED"}:
                raise RecoverableStageError(
                    f"Magnific task {task_id} ended with status {status}: {data}"
                )
            time.sleep(self.config.poll_interval_seconds)
        raise RecoverableStageError(
            f"Magnific task {task_id} did not finish within {self.config.timeout_seconds:.0f}s"
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response, operation: str) -> None:
        if response.status_code < 400:
            return
        body = response.text[:1000]
        if response.status_code == 429 or response.status_code >= 500:
            raise RecoverableStageError(
                f"Magnific {operation} HTTP {response.status_code}: {body}"
            )
        raise QuarantineEpisode(
            f"Magnific {operation} rejected with HTTP {response.status_code}: {body}"
        )


class MagnificEpisodeGenerator:
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
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

            try:
                content, metadata = self.client.generate_image(str(item["prompt"]))
                if len(content) < 1024:
                    raise RecoverableStageError(
                        f"Magnific image {filename} is unexpectedly small ({len(content)} bytes)"
                    )
                target.write_bytes(content)
                item["status"] = "complete"
                item["transport"] = metadata
                item["last_error"] = None
            except Exception as exc:
                item["status"] = "failed"
                item["last_error"] = str(exc)
                manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
                raise

            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def build_magnific_generate_handler(root: Path):
    return lambda episode_id: MagnificEpisodeGenerator().generate(root / episode_id)
