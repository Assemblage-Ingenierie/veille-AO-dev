"""Droits par route, tâches planifiées, réglages, administration."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import api
import auth
import collector
import config
import database
import notifications

USERS = {
    "tok-admin":   dict(id="u-admin",  email="admin@assemblage.net", role="admin", status="approved"),
    "tok-membre":  dict(id="u-membre", email="membre@assemblage.net", role="user", status="approved"),
    "tok-attente": dict(id="u-attente", email="attente@assemblage.net", role="user", status="pending"),
    "tok-revoque": dict(id="u-revoque", email="ex@assemblage.net", role="user", status="revoked"),
}


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(monkeypatch):
    calls = {"load_user": 0, "notify": 0}

    def fake_load_user(token):
        calls["load_user"] += 1
        if token not in USERS:
            raise HTTPException(status_code=401, detail="Session invalide ou expirée.")
        u = USERS[token]
        return auth.User(full_name=None, requested_at="2000-01-01T00:00:00+00:00",
                         admin_notified_at=None, token=token, **u)

    monkeypatch.setattr(auth, "load_user", fake_load_user)
    monkeypatch.setattr(database, "get_notices", lambda user, f=None: [{"id": "BM-1", "titre": "AO"}])
    monkeypatch.setattr(database, "collect_status",
                        lambda user: {"is_running": False, "last_run": None, "sources": {}})
    monkeypatch.setattr(notifications, "notify_access_requests",
                        lambda sb=None: calls.__setitem__("notify", calls["notify"] + 1) or {})
    c = TestClient(api.app)
    c.calls = calls
    return c


# ── Authentification ─────────────────────────────────────────────────────────

def test_sans_jeton_401(client):
    r = client.get("/api/notices")
    assert r.status_code == 401
    assert r.json()["error"] == "Connexion requise."


def test_jeton_invalide_401(client):
    assert client.get("/api/notices", headers=H("faux")).status_code == 401


@pytest.mark.parametrize("token,code", [("tok-attente", "pending"), ("tok-revoque", "revoked")])
def test_compte_non_valide_403(client, token, code):
    r = client.get("/api/notices", headers=H(token))
    assert r.status_code == 403
    assert r.json()["code"] == code


def test_membre_valide_lit_les_ao(client):
    r = client.get("/api/notices", headers=H("tok-membre"))
    assert r.status_code == 200
    assert r.json()["notices"][0]["id"] == "BM-1"


def test_cache_du_profil(client):
    client.get("/api/notices", headers=H("tok-membre"))
    client.get("/api/notices", headers=H("tok-membre"))
    assert client.calls["load_user"] == 1


def test_me_accessible_en_attente(client):
    r = client.get("/api/me", headers=H("tok-attente"))
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


def test_config_publique_sans_secret(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    assert r.json()["supabase_publishable_key"] == "sb_publishable_test"
    assert "sb_secret_test" not in r.text


# ── Collecte ─────────────────────────────────────────────────────────────────

def test_collecte_reservee_admin(client, monkeypatch):
    monkeypatch.setattr(collector, "run_source",
                        lambda key, triggered_by=None: {"source": key, "ok": True, "by": triggered_by})
    assert client.post("/api/collect/bm", headers=H("tok-membre")).status_code == 403
    r = client.post("/api/collect/bm", headers=H("tok-admin"))
    assert r.status_code == 200 and r.json()["by"] == "u-admin"
    assert client.post("/api/collect/inconnue", headers=H("tok-admin")).status_code == 404


@pytest.mark.parametrize("header,expected", [
    (None, 401), ("Bearer mauvais", 401), ("Bearer secret-cron-test", 200)])
def test_cron_protege_par_secret(client, monkeypatch, header, expected):
    monkeypatch.setattr(collector, "run_all", lambda triggered_by=None: [])
    headers = {"Authorization": header} if header else {}
    assert client.get("/api/cron/daily", headers=headers).status_code == expected
    assert client.post("/api/cron/notify-access", headers=headers).status_code == expected


def test_cron_refuse_si_secret_non_configure(client, monkeypatch):
    monkeypatch.setattr(config, "CRON_SECRET", "")
    assert client.get("/api/cron/daily", headers={"Authorization": "Bearer "}).status_code == 401


# ── Réglages partagés ────────────────────────────────────────────────────────

def test_reglages(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(database, "set_setting", lambda user, k, v: saved.update({k: v}))
    monkeypatch.setattr(database, "get_setting", lambda user, k: ["solar"])
    h = H("tok-membre")
    assert client.get("/api/settings/smtp_config", headers=h).status_code == 404
    assert client.post("/api/settings/params_keywords", json={"value": "{pas du json"},
                       headers=h).status_code == 400
    assert client.post("/api/settings/params_keywords", json={"value": '{"a": 1}'},
                       headers=h).status_code == 400
    r = client.post("/api/settings/params_keywords", json={"value": '["solar", "AMO"]'}, headers=h)
    assert r.status_code == 200 and saved == {"params_keywords": ["solar", "AMO"]}
    assert client.get("/api/settings/params_keywords", headers=h).json()["value"] == '["solar"]'


def test_tri_etat_invalide_400(client, monkeypatch):
    def set_triage(user, nid, state):
        raise ValueError(f"État de tri invalide : {state}")
    monkeypatch.setattr(database, "set_triage", set_triage)
    r = client.post("/api/triage/BM-1", json={"state": "n-importe"}, headers=H("tok-membre"))
    assert r.status_code == 400


# ── Administration ───────────────────────────────────────────────────────────

def test_admin_ne_peut_pas_se_retirer_ses_droits(client, monkeypatch):
    monkeypatch.setattr(database, "update_profile", lambda *a, **k: {"id": "x"})
    h = H("tok-admin")
    assert client.post("/api/admin/users/u-admin", json={"role": "user"}, headers=h).status_code == 400
    assert client.post("/api/admin/users/u-admin", json={"status": "revoked"},
                       headers=h).status_code == 400
    assert client.post("/api/admin/users/u-membre", json={"status": "approved"},
                       headers=h).status_code == 200
    assert client.post("/api/admin/users/u-membre", json={"status": "n-importe"},
                       headers=h).status_code == 400


def test_admin_reserve(client, monkeypatch):
    monkeypatch.setattr(database, "list_profiles", lambda admin: [])
    assert client.get("/api/admin/users", headers=H("tok-membre")).status_code == 403
    assert client.get("/api/admin/users", headers=H("tok-admin")).status_code == 200
