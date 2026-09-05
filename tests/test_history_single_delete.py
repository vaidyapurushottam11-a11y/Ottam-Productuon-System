from ottam import dashboard
from ottam import dashboard_app  # noqa: F401


def test_history_has_only_one_delete_injector():
    page = dashboard.PAGE
    assert "historyDelete" in page
    assert "historyDeleteV2" not in page
    assert page.count("textContent='Delete'") == 1
