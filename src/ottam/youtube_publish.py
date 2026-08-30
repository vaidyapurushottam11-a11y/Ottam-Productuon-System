from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx
import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD = "https://www.googleapis.com/upload/youtube/v3"


class YouTubePublishError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise YouTubePublishError(f"{name} is required")
    return value


def _key_material() -> bytes:
    raw = os.getenv("YOUTUBE_TOKEN_KEY") or os.getenv("MAGNIFIC_API_KEY")
    if not raw:
        raise YouTubePublishError("YOUTUBE_TOKEN_KEY or MAGNIFIC_API_KEY is required to encrypt OAuth state")
    return hashlib.sha256(("ottam-youtube-v1:" + raw).encode("utf-8")).digest()


def save_encrypted_session(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    nonce = os.urandom(12)
    aes = AESGCM(_key_material())
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ciphertext = aes.encrypt(nonce, plaintext, b"ottam-youtube-oauth-v1")
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_encrypted_session(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise YouTubePublishError(f"YouTube OAuth session not found: {path}")
    wrapped = json.loads(path.read_text(encoding="utf-8"))
    aes = AESGCM(_key_material())
    plaintext = aes.decrypt(
        base64.b64decode(wrapped["nonce"]),
        base64.b64decode(wrapped["ciphertext"]),
        b"ottam-youtube-oauth-v1",
    )
    return json.loads(plaintext)


def authorize_device(session_path: Path) -> dict[str, Any]:
    client_id = _require_env("YOUTUBE_CLIENT_ID")
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": YOUTUBE_SCOPE},
        )
        response.raise_for_status()
        device = response.json()
        verification_url = device.get("verification_url") or device.get("verification_uri")
        print(
            f"YOUTUBE_AUTH_REQUIRED url={verification_url} code={device['user_code']}",
            flush=True,
        )

        interval = int(device.get("interval", 5))
        deadline = time.time() + int(device.get("expires_in", 1800))
        while time.time() < deadline:
            token = client.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "device_code": device["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            data = token.json()
            if token.status_code == 200 and data.get("access_token"):
                data["client_id"] = client_id
                save_encrypted_session(data, session_path)
                channel = verify_channel(data["access_token"])
                print(
                    f"YOUTUBE_AUTHORIZED channel_id={channel.get('id')} "
                    f"channel_title={channel.get('snippet', {}).get('title')}",
                    flush=True,
                )
                return data
            error = data.get("error")
            if error == "authorization_pending":
                time.sleep(interval)
                continue
            if error == "slow_down":
                interval += 5
                time.sleep(interval)
                continue
            raise YouTubePublishError(f"YouTube device authorization failed: {data}")
    raise YouTubePublishError("YouTube device authorization expired before approval")


def refresh_access_token(session: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    refresh_token = session.get("refresh_token")
    client_id = session.get("client_id") or _require_env("YOUTUBE_CLIENT_ID")
    if not refresh_token:
        raise YouTubePublishError("OAuth session contains no refresh_token")
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        data = response.json()
    session.update(data)
    session["client_id"] = client_id
    return data["access_token"], session


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def verify_channel(access_token: str) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{YOUTUBE_API}/channels",
            params={"part": "snippet", "mine": "true"},
            headers=_auth_headers(access_token),
        )
        response.raise_for_status()
        items = response.json().get("items") or []
    if len(items) != 1:
        raise YouTubePublishError(f"Expected exactly one authorized YouTube channel, got {len(items)}")
    return items[0]


@dataclass
class PublishMetadata:
    title: str
    description: str
    tags: list[str]
    hashtags: list[str]
    category_id: str = "27"
    privacy_status: str = "private"
    notify_subscribers: bool = False
    made_for_kids: bool = False
    thumbnail_text: str = ""
    thumbnail_scene: int | None = None
    expected_channel_id: str | None = None

    def validate(self) -> None:
        if not (1 <= len(self.title) <= 100):
            raise YouTubePublishError(f"YouTube title must be 1-100 chars, got {len(self.title)}")
        if len(self.description) > 5000:
            raise YouTubePublishError("YouTube description exceeds 5000 characters")
        if self.privacy_status not in {"private", "unlisted", "public"}:
            raise YouTubePublishError(f"Invalid privacy status: {self.privacy_status}")
        if len(self.tags) > 35:
            self.tags[:] = self.tags[:35]


def load_publish_metadata(episode_id: str, repo_root: Path = Path(".")) -> PublishMetadata:
    config_path = repo_root / "config" / "publish" / f"{episode_id}.yaml"
    if not config_path.exists():
        raise YouTubePublishError(f"Publish config missing: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    metadata = PublishMetadata(
        title=str(raw["title"]).strip(),
        description=str(raw["description"]).strip(),
        tags=[str(x).strip() for x in raw.get("tags", []) if str(x).strip()],
        hashtags=[str(x).strip().lstrip("#") for x in raw.get("hashtags", []) if str(x).strip()],
        category_id=str(raw.get("category_id", "27")),
        privacy_status=str(raw.get("privacy_status", "private")),
        notify_subscribers=bool(raw.get("notify_subscribers", False)),
        made_for_kids=bool(raw.get("made_for_kids", False)),
        thumbnail_text=str(raw.get("thumbnail_text", "")).strip(),
        thumbnail_scene=int(raw["thumbnail_scene"]) if raw.get("thumbnail_scene") else None,
        expected_channel_id=str(raw["expected_channel_id"]).strip() if raw.get("expected_channel_id") else None,
    )
    metadata.validate()
    return metadata


def build_thumbnail(episode_dir: Path, metadata: PublishMetadata) -> Path:
    images_dir = episode_dir / "images"
    candidates = sorted(images_dir.glob("*.png"))
    if not candidates:
        raise YouTubePublishError("No episode images available for thumbnail")

    if metadata.thumbnail_scene:
        source = images_dir / f"{metadata.thumbnail_scene:04d}.png"
        if not source.exists():
            raise YouTubePublishError(f"Configured thumbnail scene does not exist: {source.name}")
    else:
        source = candidates[max(0, min(len(candidates) - 1, int(len(candidates) * 0.70)))]

    img = Image.open(source).convert("RGB")
    target_w, target_h = 1280, 720
    ratio = target_w / target_h
    if img.width / img.height > ratio:
        crop_w = int(img.height * ratio)
        left = (img.width - crop_w) // 2
        img = img.crop((left, 0, left + crop_w, img.height))
    else:
        crop_h = int(img.width / ratio)
        top = (img.height - crop_h) // 2
        img = img.crop((0, top, img.width, top + crop_h))
    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Sharpness(img).enhance(1.08)

    text = metadata.thumbnail_text or metadata.title
    words = text.upper().split()
    lines: list[str] = []
    current = ""
    for word in words:
        proposed = f"{current} {word}".strip()
        if len(proposed) > 12 and current:
            lines.append(current)
            current = word
        else:
            current = proposed
    if current:
        lines.append(current)
    lines = lines[:3]

    draw = ImageDraw.Draw(img, "RGBA")
    draw.rounded_rectangle((34, 54, 600, 666), radius=28, fill=(15, 18, 22, 205))
    font_path = next(
        (
            p
            for p in [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            ]
            if Path(p).exists()
        ),
        None,
    )
    font = ImageFont.truetype(font_path, 86) if font_path else ImageFont.load_default()
    y = 115
    for index, line in enumerate(lines):
        fill = (255, 226, 94, 255) if index == len(lines) - 1 else (255, 255, 255, 255)
        draw.text(
            (72, y),
            line,
            font=font,
            fill=fill,
            stroke_width=2,
            stroke_fill=(0, 0, 0, 180),
        )
        y += 118

    output = episode_dir / "thumbnail.jpg"
    img.save(output, "JPEG", quality=92, optimize=True)
    if output.stat().st_size > 2_000_000:
        img.save(output, "JPEG", quality=84, optimize=True)
    return output


def _description_with_hashtags(metadata: PublishMetadata) -> str:
    suffix = " ".join(f"#{tag}" for tag in metadata.hashtags[:15])
    if suffix and suffix not in metadata.description:
        return f"{metadata.description.rstrip()}\n\n{suffix}"
    return metadata.description


def upload_video(access_token: str, video_path: Path, metadata: PublishMetadata) -> dict[str, Any]:
    body = {
        "snippet": {
            "title": metadata.title,
            "description": _description_with_hashtags(metadata),
            "tags": metadata.tags,
            "categoryId": metadata.category_id,
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": metadata.privacy_status,
            "selfDeclaredMadeForKids": metadata.made_for_kids,
            "embeddable": True,
            "publicStatsViewable": True,
        },
    }
    headers = _auth_headers(access_token)
    headers.update(
        {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(video_path.stat().st_size),
        }
    )
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        init = client.post(
            f"{YOUTUBE_UPLOAD}/videos",
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": str(metadata.notify_subscribers).lower(),
            },
            headers=headers,
            json=body,
        )
        init.raise_for_status()
        location = init.headers.get("Location")
        if not location:
            raise YouTubePublishError("YouTube resumable upload did not return a session URL")
        with video_path.open("rb") as stream:
            upload = client.put(
                location,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(video_path.stat().st_size),
                },
                content=stream,
                timeout=1800.0,
            )
        upload.raise_for_status()
        return upload.json()


def set_thumbnail(access_token: str, video_id: str, thumbnail_path: Path) -> dict[str, Any]:
    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            f"{YOUTUBE_UPLOAD}/thumbnails/set",
            params={"videoId": video_id, "uploadType": "media"},
            headers={**_auth_headers(access_token), "Content-Type": "image/jpeg"},
            content=thumbnail_path.read_bytes(),
        )
        response.raise_for_status()
        return response.json()


def verify_video(access_token: str, video_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{YOUTUBE_API}/videos",
            params={"part": "snippet,status", "id": video_id},
            headers=_auth_headers(access_token),
        )
        response.raise_for_status()
        items = response.json().get("items") or []
    if len(items) != 1:
        raise YouTubePublishError(f"Uploaded video {video_id} was not returned by videos.list")
    return items[0]


def publish_episode(
    episode_id: str,
    runtime_root: Path = Path("runtime/episodes"),
    repo_root: Path = Path("."),
    session_path: Path = Path("runtime/youtube-auth/youtube-oauth-session.enc.json"),
) -> dict[str, Any]:
    episode_dir = runtime_root / episode_id
    video_path = episode_dir / "final.mp4"
    if not video_path.exists():
        raise YouTubePublishError(f"final.mp4 missing for {episode_id}")
    state_path = Path("runtime/state") / f"{episode_id}.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") != "VIDEO_READY":
            raise YouTubePublishError(f"Refusing upload: episode status is {state.get('status')}")

    metadata = load_publish_metadata(episode_id, repo_root)
    session = load_encrypted_session(session_path)
    access_token, session = refresh_access_token(session)
    save_encrypted_session(session, session_path)

    channel = verify_channel(access_token)
    if metadata.expected_channel_id and channel.get("id") != metadata.expected_channel_id:
        raise YouTubePublishError(
            f"Authorized channel mismatch: expected {metadata.expected_channel_id}, got {channel.get('id')}"
        )

    thumbnail_path = build_thumbnail(episode_dir, metadata)
    result_path = episode_dir / "youtube_publish_result.json"
    result: dict[str, Any] = {}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))

    video_id = result.get("video_id")
    if not video_id:
        uploaded = upload_video(access_token, video_path, metadata)
        video_id = uploaded["id"]
        result.update(
            {
                "episode_id": episode_id,
                "video_id": video_id,
                "upload_complete": True,
                "thumbnail_complete": False,
                "verified": False,
                "metadata": asdict(metadata),
            }
        )
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if not result.get("thumbnail_complete"):
        set_thumbnail(access_token, video_id, thumbnail_path)
        result["thumbnail_complete"] = True
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    verified = verify_video(access_token, video_id)
    if verified.get("snippet", {}).get("title") != metadata.title:
        raise YouTubePublishError("YouTube verification returned an unexpected title")
    result["verified"] = True
    result["youtube_url"] = f"https://youtu.be/{video_id}"
    result["remote_status"] = verified.get("status", {})
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        f"YOUTUBE_PUBLISH_COMPLETE video_id={video_id} url={result['youtube_url']}",
        flush=True,
    )
    return result


def authorize_main() -> None:
    path = Path(
        os.getenv(
            "YOUTUBE_OAUTH_SESSION_FILE",
            "runtime/youtube-auth/youtube-oauth-session.enc.json",
        )
    )
    authorize_device(path)


def publish_main() -> None:
    episode_id = os.getenv("OTTAM_EPISODE_ID", "").strip()
    if not episode_id:
        states = sorted(Path("runtime/state").glob("*.json"))
        if len(states) != 1:
            raise SystemExit(
                "OTTAM_EPISODE_ID is required when runtime/state does not contain exactly one episode"
            )
        episode_id = states[0].stem
    path = Path(
        os.getenv(
            "YOUTUBE_OAUTH_SESSION_FILE",
            "runtime/youtube-auth/youtube-oauth-session.enc.json",
        )
    )
    publish_episode(episode_id, session_path=path)
