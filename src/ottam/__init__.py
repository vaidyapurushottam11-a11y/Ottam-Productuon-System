"""OTTAM package bootstrap.

Render has historically been configured with both ``ottam.dashboard:app`` and
``ottam.dashboard_app:app``. The production dashboard extensions must therefore
be installed regardless of which import path starts the service.

Importing ``dashboard_app`` here is intentionally side-effectful: it loads the
base Flask app and installs cold recovery, history, script review, run-control,
state-truth, build-info and cache-control handlers before callers access
``ottam.dashboard.app``.
"""

from __future__ import annotations

import importlib


def _bootstrap_dashboard() -> None:
    importlib.import_module(".dashboard_app", __name__)


_bootstrap_dashboard()
