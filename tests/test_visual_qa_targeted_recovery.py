from __future__ import annotations

import json

from ottam import magnific_api
from ottam import visual_qa
from ottam.orchestrator import RecoverableStageError


def _manifest():
    return {
        "items": [
            {
                "index": 1,
                "filename": "0001.png",
                "prompt": "passed prompt",
                "prompt_sha256": "passed-sha",
                "status": "complete",
                "qa": {"passed": True, "reasons": []},
            },
            {
                "index": 6,
                "filename": "0006.png",
                "prompt": "sparse prompt",
                "prompt_sha256": "failed-sha",
                "status": "failed_qa",
                "qa": {"passed": False, "reasons": ["opening_frame_too_visually_sparse"]},
            },
        ]
    }


def test_prepare_failed_items_changes_only_failed_scene(tmp_path):
    episode = tmp_path / "EP"
    episode.mkdir()
    path = episode / "magnific_manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")

    changed = visual_qa._prepare_failed_items_for_regeneration(episode)
    result = json.loads(path.read_text(encoding="utf-8"))

    assert changed == 1
    assert result["items"][0]["prompt"] == "passed prompt"
    assert result["items"][0]["status"] == "complete"
    failed = result["items"][1]
    assert failed["status"] == "failed_qa"
    assert failed["qa_recovery_attempts"] == 1
    assert failed["qa_recovery_base_prompt"] == "sparse prompt"
    assert "avoid mostly empty" in failed["prompt"]
    assert failed["prompt_sha256"] != "failed-sha"


def test_visual_qa_handler_regenerates_failed_scene_then_rechecks(monkeypatch, tmp_path):
    episode = tmp_path / "EP"
    episode.mkdir()
    path = episode / "magnific_manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")

    calls = {"qa": 0, "generate": 0}

    class FakeQA:
        def run(self, episode_dir):
            calls["qa"] += 1
            if calls["qa"] == 1:
                raise RecoverableStageError("1 scene images failed visual QA")

    class FakeGenerator:
        def __init__(self, *args, **kwargs):
            pass

        def generate(self, episode_dir):
            calls["generate"] += 1
            manifest = json.loads((episode_dir / "magnific_manifest.json").read_text(encoding="utf-8"))
            eligible = [item["index"] for item in manifest["items"] if item.get("status") != "complete"]
            assert eligible == [6]
            manifest["items"][1]["status"] = "complete"
            (episode_dir / "magnific_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(visual_qa, "VisualQA", FakeQA)
    monkeypatch.setattr(magnific_api, "MagnificEpisodeGenerator", FakeGenerator)

    visual_qa.build_visual_qa_handler(tmp_path)("EP")

    assert calls == {"qa": 2, "generate": 1}
