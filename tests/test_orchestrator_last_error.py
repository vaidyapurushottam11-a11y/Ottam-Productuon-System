from pathlib import Path

from ottam.orchestrator import Orchestrator, RecoverableStageError, Stage, StateStore


def test_exhausted_stage_keeps_the_actual_last_error(tmp_path: Path):
    store = StateStore(tmp_path / "state")
    state = store.load("OTTAM-FAIL")
    state.stage = Stage.PLAN_VISUALS
    store.save(state)

    def fail(_episode_id: str) -> None:
        raise RecoverableStageError("xKiro returned malformed storyboard chunk JSON")

    orchestrator = Orchestrator(
        store=store,
        handlers={Stage.PLAN_VISUALS: fail},
        max_stage_attempts=2,
    )
    result = orchestrator.run("OTTAM-FAIL", stop_after=Stage.PLAN_VISUALS)

    assert result.quarantined is True
    assert "exhausted 2 recovery attempts" in (result.last_error or "")
    assert "xKiro returned malformed storyboard chunk JSON" in (result.last_error or "")
