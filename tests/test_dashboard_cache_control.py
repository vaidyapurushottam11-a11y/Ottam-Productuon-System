from ottam import dashboard
from ottam import dashboard_app  # noqa: F401


def test_dashboard_html_is_never_cached():
    client = dashboard.app.test_client()
    response = client.get('/')
    assert response.status_code == 200
    assert 'no-store' in response.headers['Cache-Control']
    assert response.headers['Pragma'] == 'no-cache'
    assert 'X-OTTAM-Build' in response.headers


def test_dashboard_api_is_never_cached(monkeypatch):
    monkeypatch.setattr(dashboard, '_runs', lambda workflow: [])
    client = dashboard.app.test_client()
    response = client.get('/api/current-job')
    assert response.status_code == 200
    assert 'no-store' in response.headers['Cache-Control']
    assert response.headers['Pragma'] == 'no-cache'
