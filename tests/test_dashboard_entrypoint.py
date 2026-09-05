from __future__ import annotations

import subprocess
import sys


def _run(code: str) -> None:
    subprocess.run([sys.executable, "-c", code], check=True)


def test_direct_dashboard_entrypoint_loads_full_production_ui():
    _run(
        "from ottam.dashboard import app, PAGE; "
        "assert 'scriptReviewPanel' in PAGE; "
        "assert 'historyDeleteV2' in PAGE or 'historyDelete' in PAGE; "
        "assert app.view_functions['current_job'].__module__.endswith('dashboard_state_truth'); "
        "assert 'delete_history' in app.view_functions"
    )


def test_composed_dashboard_entrypoint_matches_direct_entrypoint():
    _run(
        "from ottam.dashboard import app as direct; "
        "from ottam.dashboard_app import app as composed; "
        "assert direct is composed; "
        "assert direct.view_functions['current_job'].__module__.endswith('dashboard_state_truth')"
    )
