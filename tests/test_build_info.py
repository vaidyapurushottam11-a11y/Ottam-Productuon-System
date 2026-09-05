from ottam import dashboard
from ottam import dashboard_app  # noqa: F401


def test_build_endpoint_exists():
    client = dashboard.app.test_client()
    response = client.get('/api/build')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['service'] == 'ottam-dashboard'
    assert 'git_commit' in payload
