from __future__ import annotations

from . import dashboard
from . import dashboard_cold_recovery  # noqa: F401 - installs cold-safe views
from . import dashboard_history  # noqa: F401 - installs history routes/UI
from . import dashboard_script_review  # noqa: F401 - installs script approval routes/UI

app = dashboard.app


def main() -> None:
    dashboard.main()
