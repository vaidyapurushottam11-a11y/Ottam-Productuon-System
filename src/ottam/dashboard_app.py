from __future__ import annotations

from . import dashboard
from . import dashboard_cold_recovery  # noqa: F401 - installs cold-safe views
from . import dashboard_history  # noqa: F401 - installs history routes/UI
from . import dashboard_script_review  # noqa: F401 - installs script approval routes/UI
from . import dashboard_review_state_fix  # noqa: F401 - backwards-compatible classifier
from . import dashboard_review_direct  # noqa: F401 - final live request-path overrides
from . import build_info  # noqa: F401 - deployment commit verification endpoint
from . import dashboard_run_control  # noqa: F401 - dedupe/current-run/history-delete controls
from . import dashboard_cache_control  # noqa: F401 - prevent stale browser builds
from . import dashboard_current_details_fix  # noqa: F401 - mutate PAGE with failed/current details restoration before final UI layer
from . import dashboard_script_review_controls  # noqa: F401 - late-bound review button controller
from . import dashboard_script_approval_fix  # noqa: F401 - idempotent approval + prefer active continuation
from . import dashboard_state_truth  # noqa: F401 - FINAL authoritative state + UI layer; intentionally imported last

app = dashboard.app


def main() -> None:
    dashboard.main()
