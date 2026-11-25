"""Keep every test (including the bench) away from the user's host settings."""
import pytest


@pytest.fixture(autouse=True)
def isolated_model_settings(tmp_path, monkeypatch):
    from backend import private_settings
    monkeypatch.setenv("REVIT_BRIDGE_WEB_HOME", str(tmp_path / "web-home"))
    monkeypatch.delenv("ALLOW_BROWSER_KEYS", raising=False)
    # tmp_path may itself be inside a sandboxed checkout. Simulate a sibling
    # application root so tests exercise the same outside-the-checkout rule.
    app_root = tmp_path / "app"
    (app_root / ".git").mkdir(parents=True)
    monkeypatch.setattr(private_settings, "APP_ROOT", app_root)
