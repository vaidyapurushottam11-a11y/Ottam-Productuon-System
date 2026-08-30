from pathlib import Path
import json

from ottam.orchestrator import StateStore, Stage


def test_state_store_ignores_unknown_metadata(tmp_path: Path):
    path = tmp_path / "EP-1.json"
    path.write_text(json.dumps({
        "episode_id": "EP-1",
        "stage": "research",
        "status": "PENDING",
        "attempts": 0,
        "last_error": None,
        "quarantined": False,
        "manual_retry": True,
        "previous_error": "old failure"
    }))
    state = StateStore(tmp_path).load("EP-1")
    assert state.episode_id == "EP-1"
    assert state.stage == Stage.RESEARCH
    assert state.quarantined is False
