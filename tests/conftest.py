import pytest


@pytest.fixture(autouse=True)
def no_desktop_notifications(monkeypatch):
    """Never pop real notifications on the machine running the tests."""
    from crptochat import notify
    sent = []
    monkeypatch.setattr(notify.Notifier, "_spawn", staticmethod(lambda argv, env: sent.append((argv, env))))
    return sent


@pytest.fixture(autouse=True)
def no_real_update_server(monkeypatch):
    """The startup update check of the interface must never reach the real
    GitHub or the real site from the tests: point it at a local path that does
    not exist. Tests that exercise sources set (or delete) this themselves."""
    monkeypatch.setenv("CRPTOCHAT_UPDATE_URL", "file:///nonexistent-crptochat-test-updates")
    monkeypatch.setenv("CRPTOCHAT_UPDATE_FALLBACK", "off")
