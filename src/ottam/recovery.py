from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FailureClass(str, Enum):
    RATE_LIMIT = "rate_limit"
    TRANSIENT_PROVIDER = "transient_provider"
    AUTH = "auth"
    INVALID_ASSET = "invalid_asset"
    CONTENT_QA = "content_qa"
    RENDER = "render"
    QUOTA = "quota"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecoveryPlan:
    failure_class: FailureClass
    retry: bool
    max_attempts: int
    backoff_seconds: tuple[int, ...] = ()
    repair_action: str | None = None
    fallback_action: str | None = None


RUNBOOKS: dict[FailureClass, RecoveryPlan] = {
    FailureClass.RATE_LIMIT: RecoveryPlan(
        FailureClass.RATE_LIMIT, True, 6, (30, 60, 120, 240, 480, 900),
        repair_action="respect_retry_after",
        fallback_action="open_provider_circuit",
    ),
    FailureClass.TRANSIENT_PROVIDER: RecoveryPlan(
        FailureClass.TRANSIENT_PROVIDER, True, 5, (15, 30, 60, 120, 300),
        fallback_action="open_provider_circuit",
    ),
    FailureClass.AUTH: RecoveryPlan(
        FailureClass.AUTH, True, 2, (5, 15),
        repair_action="refresh_credentials",
        fallback_action="quarantine_publish_stage",
    ),
    FailureClass.INVALID_ASSET: RecoveryPlan(
        FailureClass.INVALID_ASSET, True, 3, (2, 5, 10),
        repair_action="redownload_then_regenerate_asset",
        fallback_action="use_simplified_visual_strategy",
    ),
    FailureClass.CONTENT_QA: RecoveryPlan(
        FailureClass.CONTENT_QA, True, 3,
        repair_action="targeted_regeneration_only",
        fallback_action="simplify_scene_or_quarantine_episode",
    ),
    FailureClass.RENDER: RecoveryPlan(
        FailureClass.RENDER, True, 3, (2, 5, 10),
        repair_action="inspect_ffmpeg_and_rebuild_manifest",
        fallback_action="safe_encode_profile",
    ),
    FailureClass.QUOTA: RecoveryPlan(
        FailureClass.QUOTA, True, 1,
        repair_action="defer_until_quota_reset",
        fallback_action="use_ready_content_buffer",
    ),
    FailureClass.UNKNOWN: RecoveryPlan(
        FailureClass.UNKNOWN, False, 0,
        fallback_action="diagnostic_agent_then_quarantine",
    ),
}


def classify(message: str) -> FailureClass:
    text = message.lower()
    if "429" in text or "rate limit" in text:
        return FailureClass.RATE_LIMIT
    if "401" in text or "403" in text or "unauthorized" in text or "token" in text:
        return FailureClass.AUTH
    if "quota" in text:
        return FailureClass.QUOTA
    if any(x in text for x in ("timeout", "temporarily unavailable", "502", "503", "504")):
        return FailureClass.TRANSIENT_PROVIDER
    if any(x in text for x in ("corrupt", "invalid image", "invalid media", "ffprobe")):
        return FailureClass.INVALID_ASSET
    if "ffmpeg" in text or "encode" in text or "render" in text:
        return FailureClass.RENDER
    if "qa" in text or "style mismatch" in text or "semantic mismatch" in text:
        return FailureClass.CONTENT_QA
    return FailureClass.UNKNOWN
