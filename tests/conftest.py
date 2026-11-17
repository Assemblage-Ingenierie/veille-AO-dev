"""Configuration des tests : aucun appel réseau, aucune vraie clé."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import auth    # noqa: E402
import config  # noqa: E402
import supa    # noqa: E402


class NoNetworkSession:
    def request(self, *args, **kwargs):
        raise AssertionError("Appel réseau inattendu pendant les tests : "
                             f"{args[:2]} — il faut simuler cette dépendance.")


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_URL", "https://exemple.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_test")
    monkeypatch.setattr(config, "CRON_SECRET", "secret-cron-test")
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "")
    monkeypatch.setattr(supa, "_session", lambda: NoNetworkSession())
    auth.forget_all()
    yield
    auth.forget_all()
