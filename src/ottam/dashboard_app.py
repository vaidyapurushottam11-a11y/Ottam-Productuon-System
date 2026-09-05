from __future__ import annotations

from . import dashboard
from . import dashboard_cold_recovery  # noqa: F401 - installs cold-safe views
from . import dashboard_history  # noqa: F401 - installs history routes/UI
from . import dashboard_script_review  # noqa: F401 - installs script approval routes/UI
from . import dashboard_review_state_fix  # noqa: F401 - backwards-compatible classifier
from . import dashboard_review_direct  # noqa: F401 - final live request-path overrides
from . import build_info  # noqa: F401 - deployment commit verification endpoint

app = dashboard.app


def main() -> None:
    dashboard.main()
