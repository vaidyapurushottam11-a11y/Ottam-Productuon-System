from __future__ import annotations

import os

from flask import jsonify

from . import dashboard


@dashboard.app.get('/api/build')
def build_info():
    return jsonify({
        'service': 'ottam-dashboard',
        'git_commit': os.getenv('RENDER_GIT_COMMIT') or os.getenv('GIT_COMMIT') or 'unknown',
    })

app = dashboard.app
