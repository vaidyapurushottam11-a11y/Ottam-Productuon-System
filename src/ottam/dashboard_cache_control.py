from __future__ import annotations

import os

from flask import request

from . import dashboard


@dashboard.app.after_request
def disable_dashboard_cache(response):
    """Dashboard state is live; never let browsers reuse an old HTML/API build."""
    if request.path == "/" or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-OTTAM-Build"] = (
            os.getenv("RENDER_GIT_COMMIT")
            or os.getenv("GIT_COMMIT")
            or "unknown"
        )
    return response


app = dashboard.app
