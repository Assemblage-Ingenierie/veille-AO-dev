"""Client Supabase, file d'emails des demandes d'accès, collecte."""
from datetime import datetime, timedelta, timezone

import pytest

import collector
import config
import database
import mailer
import notifications
import supa


# ── Client Supabase (supa.py) ────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else []
        self.content = b"x"
        self.text = str(self._body)

    def json(self):
        return self._body


class RecordingSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers})
        return self.responses.pop(0)


def test_as_user_envoie_cle_publique_et_jeton(monkeypatch):
    s = RecordingSession([FakeResponse(body=[])])
    monkeypatch.setattr(supa, "_session", lambda: s)
    supa.as_user("jeton-utilisateur").select("veilleao_notices")
    h = s.calls[0]["headers"]
    assert h["apikey"] == "sb_publishable_test"
    assert h["Authorization"] == "Bearer jeton-utilisateur"


def test_service_cle_secrete_sans_authorization(monkeypatch):
    s = RecordingSession([FakeResponse(body=[])])
    monkeypatch.setattr(supa, "_session", lambda: s)
    supa.service().select("veilleao_notices")
    assert s.calls[0]["headers"]["apikey"] == "sb_secret_test"
    assert "Authorization" not in s.calls[0]["headers"]


def test_service_sans_cle_refuse(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "")
    with pytest.raises(supa.SupabaseError):
        supa.service()


def test_pagination_complete(monkeypatch):
    s = RecordingSession([FakeResponse(body=[{"i": n} for n in range(1000)]),
                          FakeResponse(body=[{"i": n} for n in range(5)])])
    monkeypatch.setattr(supa, "_session", lambda: s)
    rows = supa.as_user("t").select("veilleao_notices", {"select": "id"}, all_rows=True)
    assert len(rows) == 1005
    assert [c["params"]["offset"] for c in s.calls] == [0, 1000]


def test_erreur_supabase_remontee(monkeypatch):
    s = RecordingSession([FakeResponse(status=401, body={"message": "JWT expired", "code": "PGRST301"})])
    monkeypatch.setattr(supa, "_session", lambda: s)
    with pytest.raises(supa.SupabaseError) as e:
        supa.as_user("vieux").rpc("veilleao_request_access")
    assert e.value.status == 401 and e.value.code == "PGRST301"


def test_filtre_in_echappe():
    assert supa.in_(["TED-1", 'a"b']) == 'in.("TED-1","a\\"b")'


# ── File d'emails des demandes d'accès ───────────────────────────────────────

class FakeDB:
    """Mini-PostgREST en mémoire pour les filtres utilisés par notifications.py."""
    IGNORED = {"select", "order", "limit", "offset"}

    def __init__(self, tables):
        self.tables = tables

    @staticmethod
    def _ok(row, key, cond):
        v = row.get(key)
        if cond == "is.null":
            return v is None
        if cond.startswith("eq."):
            return str(v) == cond[3:]
        if cond.startswith("gte."):
            return (v or "") >= cond[4:]
        if cond.startswith("in.("):
            return str(v) in [x.strip('"') for x in cond[4:-1].split(",")]
        raise NotImplementedError(cond)

    def _rows(self, table, params):
        return [r for r in self.tables.setdefault(table, [])
                if all(self._ok(r, k, c) for k, c in params.items() if k not in self.IGNORED)]

    def select(self, table, params=None, all_rows=False):
        return [dict(r) for r in self._rows(table, params or {})]

    def insert(self, table, rows, **kw):
        now = datetime.now(timezone.utc).isoformat()
        for r in rows:
            self.tables.setdefault(table, []).append({"sent_at": now, "error": None, **r})

    def update(self, table, filters, body):
        rows = self._rows(table, filters)
        for r in rows:
            r.update(body)
        return rows


def _db(pending=1, sent_last_hour=0):
    profiles = [{"id": "a1", "email": "admin@assemblage.net", "role": "admin", "status": "approved",
                 "admin_notified_at": "x"}]
    profiles += [{"id": f"p{i}", "email": f"p{i}@assemblage.net", "full_name": None, "role": "user",
                  "status": "pending", "requested_at": "2026-11-17T10:00:00+00:00",
                  "admin_notified_at": None} for i in range(pending)]
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    log = [{"kind": "access_request", "sent_at": recent, "error": None} for _ in range(sent_last_hour)]
    return FakeDB({"veilleao_profiles": profiles, "veilleao_mail_log": log})


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(config, "SMTP_USER", "veille@assemblage.net")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "mot-de-passe-app")
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, text, html: sent.append((to, subject)))
    return sent


def test_notif_sans_smtp():
    assert notifications.notify_access_requests(_db())["reason"] == "smtp_non_configure"


def test_notif_aucune_demande(smtp):
    assert notifications.notify_access_requests(_db(pending=0))["reason"] == "aucune_demande"
    assert smtp == []


def test_notif_envoie_un_email_groupe(smtp):
    db = _db(pending=3)
    res = notifications.notify_access_requests(db)
    assert res["sent"] is True and res["demandes"] == 3
    assert len(smtp) == 1 and smtp[0][0] == ["admin@assemblage.net"]
    assert all(p["admin_notified_at"] for p in db.tables["veilleao_profiles"])
    assert len(db.tables["veilleao_mail_log"]) == 1


def test_notif_plafond_horaire_garde_en_file(smtp):
    db = _db(pending=3, sent_last_hour=2)
    res = notifications.notify_access_requests(db)
    assert res == {"sent": False, "reason": "plafond_horaire_atteint", "en_file": 3}
    assert smtp == []
    assert all(p["admin_notified_at"] is None
               for p in db.tables["veilleao_profiles"] if p["status"] == "pending")


def test_notif_erreur_smtp_reessai_plus_tard(monkeypatch, smtp):
    def boom(*a):
        raise OSError("serveur SMTP injoignable")
    monkeypatch.setattr(mailer, "send", boom)
    db = _db(pending=1)
    res = notifications.notify_access_requests(db)
    assert res["reason"] == "erreur_smtp"
    assert db.tables["veilleao_mail_log"][-1]["error"]
    # l'échec ne compte pas dans le plafond et la demande reste en file
    monkeypatch.setattr(mailer, "send", lambda *a: smtp.append(a))
    assert notifications.notify_access_requests(db)["sent"] is True


def test_email_demandes_echappe_le_html():
    _, _, html = mailer.access_requests_email(
        [{"email": "x@y.z", "full_name": "<script>alert(1)</script>", "requested_at": ""}])
    assert "<script>" not in html and "&lt;script&gt;" in html


# ── Collecte ─────────────────────────────────────────────────────────────────

def fake_fetch_ok():
    return [{"id": "T-1", "titre": "AO 1"}, {"id": "T-2", "titre": "AO 2"}]


def fake_fetch_ko():
    raise ConnectionError("site indisponible")


@pytest.fixture
def journal(monkeypatch):
    j = {"finish": None}
    monkeypatch.setattr(supa, "service", lambda: object())
    monkeypatch.setattr(database, "start_run", lambda source, by, sb=None: 42)
    monkeypatch.setattr(database, "finish_run",
                        lambda run_id, fetched=None, inserted=None, error=None, sb=None:
                        j.update(finish=(run_id, fetched, inserted, error)))
    monkeypatch.setattr(database, "save_notices", lambda notices, refresh=False, sb=None: len(notices))
    monkeypatch.setattr(database, "mark_expired", lambda sb=None: 0)
    return j


def test_collecte_ok(monkeypatch, journal):
    monkeypatch.setitem(collector.SOURCES, "test", ("Test", __name__, "fake_fetch_ok", False))
    res = collector.run_source("test", triggered_by="u-admin")
    assert res["ok"] and res["fetched"] == 2 and res["inserted"] == 2
    assert journal["finish"] == (42, 2, 2, None)


def test_collecte_source_en_panne(monkeypatch, journal):
    monkeypatch.setitem(collector.SOURCES, "test", ("Test", __name__, "fake_fetch_ko", False))
    res = collector.run_source("test")
    assert res["ok"] is False and "site indisponible" in res["error"]
    assert journal["finish"][3] and "site indisponible" in journal["finish"][3]


def test_lots_par_taille():
    rows = [{"id": str(i), "texte_complet": "x" * 1000} for i in range(50)]
    batches = list(database.batches_by_size(rows, max_bytes=10_000))
    assert sum(len(b) for b in batches) == 50
    assert all(len(b) <= 10 for b in batches)


def test_save_notices_vide_en_texte(monkeypatch):
    sent = []

    class SB:
        def rpc(self, fn, payload):
            sent.append((fn, payload))
            return len(payload["p_rows"])

    n = database.save_notices([{"id": "TED-1", "titre": "x", "date_limite": None}],
                              refresh=True, sb=SB())
    assert n == 1
    fn, payload = sent[0]
    assert fn == "veilleao_upsert_notices" and payload["p_refresh"] is True
    assert payload["p_rows"][0]["date_limite"] == ""      # None → '' → NULL côté SQL
