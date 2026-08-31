"""Render-specific startup hook for OTTAM dashboard recovery and history.

Python imports sitecustomize after processing installed site paths. We only activate
this hook for the Render dashboard process, identified by Render's PORT plus the
private dashboard GitHub token. GitHub Actions production jobs do not set that
combination, so their runtime is unchanged.
"""

from __future__ import annotations

import os


if os.getenv("PORT") and os.getenv("OTTAM_GITHUB_TOKEN"):
    # Importing dashboard_history also imports dashboard_cold_recovery, so the
    # existing Flask app receives cold-start recovery, safe file serving, and the
    # GitHub-backed Production History routes/UI without changing Render's
    # `gunicorn ottam.dashboard:app` start command.
    import ottam.dashboard_history  # noqa: F401
