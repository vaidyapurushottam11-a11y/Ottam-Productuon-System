from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

from .orchestrator import QuarantineEpisode, RecoverableStageError


@dataclass(frozen=True)
class XKiroModel:
    id: str
    raw: dict

    @property
    def is_free(self) -> bool:
        # xKiro currently marks free models in the live catalog/model IDs with :free.
        # We intentionally use a conservative check: uncertainty means NOT free.
        if self.id.endswith(":free"):
            return True
        tier = str(self.raw.get("tier") or self.raw.get("pricing_tier") or "").lower()
        if tier == "free":
            return True
        pricing = self.raw.get("pricing") or {}
        if isinstance(pricing, dict):
            values = [pricing.get(k) for k in ("input", "output", "prompt", "completion")]
            numeric = []
            for value in values:
                if value is None:
                    continue
                try:
                    numeric.append(float(value))
                except (TypeError, ValueError):
                    pass
            if numeric and all(v == 0 for v in numeric):
                return True
        return False


class XKiroClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.xkiro.com/v1",
        timeout_seconds: float = 180.0,
    ) -> None:
        self.api_key = api_key or os.getenv("XKIRO_API_KEY")
        if not self.api_key:
            raise QuarantineEpisode("XKIRO_API_KEY is not configured")
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds, connect=20.0)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def list_models(self) -> list[XKiroModel]:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(f"{self.base_url}/models")
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RecoverableStageError(f"xKiro model catalog timeout: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429 or code >= 500:
                raise RecoverableStageError(f"xKiro model catalog HTTP {code}") from exc
            raise QuarantineEpisode(f"xKiro model catalog HTTP {code}: {exc.response.text[:500]}") from exc
        except httpx.HTTPError as exc:
            raise RecoverableStageError(f"xKiro model catalog network error: {exc}") from exc

        payload = response.json()
        data = payload.get("data", payload if isinstance(payload, list) else [])
        return [XKiroModel(id=str(item.get("id", "")), raw=item) for item in data if item.get("id")]

    def select_free_model(self, preferred: Iterable[str]) -> XKiroModel:
        catalog = {m.id: m for m in self.list_models()}
        for model_id in preferred:
            model = catalog.get(model_id)
            if model and model.is_free:
                return model
        free_ids = [m.id for m in catalog.values() if m.is_free]
        raise QuarantineEpisode(
            "None of the preferred xKiro models are currently confirmed free. "
            f"Confirmed free models visible in catalog: {free_ids[:25]}"
        )

    def chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.8,
        max_tokens: int = 12000,
    ) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        chunks: list[str] = []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=payload,
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise RecoverableStageError(f"xKiro chat HTTP {response.status_code}")
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", errors="replace")
                        raise QuarantineEpisode(f"xKiro chat HTTP {response.status_code}: {body[:800]}")
                    for line in response.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        text = delta.get("content")
                        if text:
                            chunks.append(str(text))
        except RecoverableStageError:
            raise
        except QuarantineEpisode:
            raise
        except httpx.TimeoutException as exc:
            raise RecoverableStageError(f"xKiro chat timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RecoverableStageError(f"xKiro chat network error: {exc}") from exc

        output = "".join(chunks).strip()
        if not output:
            raise RecoverableStageError("xKiro returned an empty streamed response")
        return output


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
