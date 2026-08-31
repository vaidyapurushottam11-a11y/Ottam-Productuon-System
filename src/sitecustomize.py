"""Render-specific startup hook for OTTAM dashboard cold-start recovery.

Python imports sitecustomize after processing installed site paths. We only activate
this hook for the Render dashboard process, identified by Render's PORT plus the
private dashboard GitHub token. GitHub Actions production jobs do not set that
combination, so their runtime is unchanged.
"""

from __future__ import annotations

import os


if os.getenv("PORT") and os.getenv("OTTAM_GITHUB_TOKEN"):
    # Importing this module patches the existing Flask app view functions in place.
    # Gunicorn can continue using `ottam.dashboard:app` with no Render command change.
    import ottam.dashboard_cold_recovery  # noqa: F401
