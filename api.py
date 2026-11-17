"""Veille AO — API FastAPI (déployée sur Vercel, données dans Supabase INTERNAL).

Droits par route (voir auth.py) :
  * publique   : /api/config (valeurs publiques pour initialiser la connexion)
  * connecté   : /api/me (crée la demande d'accès à la première visite)
  * validé     : lecture des AO, tri, assignations, réglages partagés, détails
  * admin      : collecte manuelle, comptes, test SMTP
  * tâche planifiée (CRON_SECRET) : /api/cron/*
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

import auth
import collector
import config
import database
import mailer
import notifications
import supa
from auth import User, admin_user, approved_user, cron_auth, current_user
from config import INFRA_KEYWORDS, KEYWORD_DISPLAY_GROUPS, STRONG_KEYWORDS

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
PAGES_DIR = BASE_DIR / "public" if (BASE_DIR / "public").exists() else BASE_DIR / "static"

app = FastAPI(title="Veille AO — Assemblage ingénierie", version="2.0.0")


# ── Erreurs : toujours {"error": message, "code": …} en JSON ────────────────

@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        body = {"error": detail.get("message", ""), "code": detail.get("code")}
    else:
        body = {"error": str(detail), "code": None}
    return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(supa.SupabaseError)
async def _supabase_error(request: Request, exc: supa.SupabaseError):
    if exc.status == 401:
        status, msg = 401, "Session invalide ou expirée."
    elif exc.status == 403 or exc.code == "42501":
        status, msg = 403, "Action non autorisée."
    elif exc.status == 404:
        status, msg = 404, "Introuvable."
    elif exc.code == "23505" or exc.status == 409:
        status, msg = 409, "Cet élément existe déjà."
    else:
        status, msg = 502, "Base de données indisponible."
    log.warning(f"Supabase {exc.status}/{exc.code} sur {request.url.path} : {exc.message}")
    return JSONResponse({"error": msg}, status_code=status)


@app.exception_handler(ValueError)
async def _value_error(request: Request, exc: ValueError):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(Exception)
async def _unexpected_error(request: Request, exc: Exception):
    log.error(f"Erreur non gérée sur {request.url.path} : {exc!r}")
    return JSONResponse({"error": "Erreur interne du serveur."}, status_code=500)


# ── Pages (servies directement par Vercel en production) ────────────────────

def _page(name: str) -> FileResponse:
    path = PAGES_DIR / f"{name}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Page introuvable.")
    return FileResponse(str(path))


@app.get("/", include_in_schema=False)
async def page_index():
    return _page("index")


@app.get("/{name}", include_in_schema=False)
async def page_named(name: str):
    if name not in {"analyse", "pertinentes", "params", "admin", "login"}:
        raise HTTPException(status_code=404, detail="Page introuvable.")
    return _page(name)


@app.get("/static/{asset}", include_in_schema=False)
async def static_asset(asset: str):
    path = (PAGES_DIR / asset).resolve()
    if PAGES_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    return FileResponse(str(path))


# ── Connexion / compte ───────────────────────────────────────────────────────

@app.get("/api/config")
async def api_config():
    """Valeurs publiques nécessaires au navigateur pour la connexion Supabase."""
    return {"supabase_url": config.SUPABASE_URL,
            "supabase_publishable_key": config.SUPABASE_PUBLISHABLE_KEY,
            "app_url": config.APP_URL}


@app.get("/api/me")
def api_me(user: User = Depends(current_user)):
    # Première visite d'une personne non validée : on prévient les admins tout de
    # suite (au mieux) ; la tâche planifiée rattrape sinon la file d'attente.
    if user.status == "pending" and not user.admin_notified_at and user.requested_at:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(user.requested_at)
            if age < timedelta(minutes=2):
                notifications.notify_access_requests()
                auth.forget_all()
        except Exception as e:
            log.warning(f"Notification immédiate de demande d'accès impossible : {e}")
    return user.public()


# ── Avis ─────────────────────────────────────────────────────────────────────

@app.get("/api/notices")
def api_notices(bailleur: list[str] = Query(default=[]),
                langue: list[str] = Query(default=[]),
                pertinence: list[str] = Query(default=[]),
                deadline_from: str = Query(default=""),
                deadline_to: str = Query(default=""),
                user: User = Depends(approved_user)):
    notices = database.get_notices(user, {"bailleur": bailleur, "langue": langue,
                                          "pertinence": pertinence,
                                          "deadline_from": deadline_from,
                                          "deadline_to": deadline_to})
    status = database.collect_status(user)
    return {"total": len(notices), "last_update": status["last_run"],
            "is_refreshing": status["is_running"], "notices": notices}


@app.get("/api/notices/stats")
def api_stats(user: User = Depends(approved_user)):
    return database.get_stats(user)


@app.get("/api/keywords")
def api_keywords(user: User = Depends(approved_user)):
    return {"strong": STRONG_KEYWORDS, "infra": INFRA_KEYWORDS, "groups": KEYWORD_DISPLAY_GROUPS}


# ── Collecte ─────────────────────────────────────────────────────────────────

@app.get("/api/collect/status")
def api_collect_status(user: User = Depends(approved_user)):
    return {**database.collect_status(user), "available": collector.list_sources()}


@app.get("/api/refresh/status")      # ancien chemin, conservé pour le front actuel
def api_refresh_status(user: User = Depends(approved_user)):
    return api_collect_status(user)


@app.post("/api/collect/{source}")
def api_collect(source: str, user: User = Depends(admin_user)):
    if source not in collector.SOURCES:
        raise HTTPException(status_code=404, detail=f"Source inconnue : {source}")
    return collector.run_source(source, triggered_by=user.id)


@app.api_route("/api/cron/daily", methods=["GET", "POST"], dependencies=[Depends(cron_auth)])
def api_cron_daily():
    results = collector.run_all()
    return {"collecte": results, "notification": notifications.notify_access_requests()}


@app.api_route("/api/cron/notify-access", methods=["GET", "POST"],
               dependencies=[Depends(cron_auth)])
def api_cron_notify_access():
    return notifications.notify_access_requests()


# ── Détail enrichi d'un AO (cache serveur) ───────────────────────────────────

_USEFUL = {"description", "description_section", "full_text", "criteria_section", "documents"}


@app.get("/api/ao-detail/{notice_id}")
def api_ao_detail(notice_id: str, force: bool = Query(default=False),
                  user: User = Depends(approved_user)):
    notice = database.get_notice_by_id(notice_id)
    if not notice:
        raise HTTPException(status_code=404, detail="Notice introuvable.")

    # Toute entrée en cache (succès ou échec) évite de relire la source ;
    # seul force=true relance la lecture.
    if not force:
        cached = database.get_notice_detail_cache(notice_id)
        if cached is not None:
            if cached.get("_failed"):
                return {"id": notice_id, "bailleur": notice.get("bailleur"), "_cached": True,
                        "_failed": True, "_fetched_at": cached.get("_fetched_at")}
            if any(cached.get(k) for k in _USEFUL):
                return {"id": notice_id, "bailleur": notice.get("bailleur"), **cached,
                        "_cached": True}

    from sources.detail import fetch_detail
    try:
        detail = fetch_detail(notice_id=notice_id, lien=notice.get("lien") or "",
                              bailleur=notice.get("bailleur") or "",
                              texte_complet=notice.get("texte_complet") or "")
    except Exception as e:
        log.error(f"Lecture du détail {notice_id} impossible : {e}")
        detail = {"error": str(e)}

    has_useful = not detail.get("error") and any(detail.get(k) for k in _USEFUL)
    try:
        database.set_notice_detail_cache(notice_id, detail if has_useful else {"_failed": True})
    except Exception as e:
        log.warning(f"Cache détail {notice_id} non écrit : {e}")
    if not has_useful:
        return {"id": notice_id, "bailleur": notice.get("bailleur"), "_failed": True}
    return {"id": notice_id, "bailleur": notice.get("bailleur"), **detail}


@app.delete("/api/ao-detail/{notice_id}/cache")
def api_clear_detail_cache(notice_id: str, user: User = Depends(approved_user)):
    database.clear_notice_detail_cache(notice_id)
    return {"notice_id": notice_id, "status": "cache_cleared"}


# ── Tri commun ───────────────────────────────────────────────────────────────

class TriageBody(BaseModel):
    state: Optional[str] = None


@app.get("/api/triage")
def api_get_triage(user: User = Depends(approved_user)):
    return database.get_all_triage(user)


@app.post("/api/triage/{notice_id}")
def api_set_triage(notice_id: str, body: TriageBody, user: User = Depends(approved_user)):
    if not database.set_triage(user, notice_id, body.state):
        raise HTTPException(status_code=404, detail="Notice introuvable.")
    return {"id": notice_id, "state": body.state, "by": user.display_name}


# ── Réglages partagés (mots-clés) ────────────────────────────────────────────
# Compatibilité front : la valeur circule sous forme de chaîne JSON.

class SettingBody(BaseModel):
    value: Optional[str] = None


def _check_setting_key(key: str) -> None:
    if key not in database.SHARED_SETTINGS:
        raise HTTPException(status_code=404, detail="Réglage inconnu.")


@app.get("/api/settings/{key}")
def api_get_setting(key: str, user: User = Depends(approved_user)):
    _check_setting_key(key)
    value = database.get_setting(user, key)
    return {"key": key, "value": json.dumps(value, ensure_ascii=False) if value is not None else None}


@app.post("/api/settings/{key}")
def api_set_setting(key: str, body: SettingBody, user: User = Depends(approved_user)):
    _check_setting_key(key)
    try:
        value = json.loads(body.value) if body.value else []
    except ValueError:
        raise HTTPException(status_code=400, detail="Valeur JSON invalide.")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise HTTPException(status_code=400, detail="La valeur doit être une liste de mots-clés.")
    database.set_setting(user, key, value)
    return {"key": key, "value": body.value}


# ── Équipe et assignations ───────────────────────────────────────────────────

@app.get("/api/team")
def api_team(user: User = Depends(approved_user)):
    return {"members": [{"id": m["id"], "name": m.get("full_name") or m["email"],
                         "email": m["email"], "role": m["role"]}
                        for m in database.list_team(user)]}


class AssignBody(BaseModel):
    assignee: str                    # identifiant du membre (profil validé)
    note: Optional[str] = None


@app.get("/api/assignments")
def api_get_assignments(user: User = Depends(approved_user)):
    return {"assignments": database.get_all_assignments(user)}


@app.post("/api/assignments/{notice_id}")
def api_assign(notice_id: str, body: AssignBody, user: User = Depends(approved_user)):
    database.assign_notice(user, notice_id, body.assignee, body.note)
    email_sent = False
    if mailer.smtp_configured():
        try:
            assignee = database.get_profile(body.assignee)
            notice = database.get_notice_by_id(notice_id) or {"id": notice_id}
            if assignee and assignee.get("email") and assignee["id"] != user.id:
                mailer.send(assignee["email"], *mailer.assignment_email(
                    assignee.get("full_name") or assignee["email"], user.display_name, notice))
                email_sent = True
        except Exception as e:
            log.warning(f"Email d'assignation non envoyé : {e}")
    return {"notice_id": notice_id, "assignee": body.assignee, "email_sent": email_sent}


@app.delete("/api/assignments/{notice_id}")
async def api_remove_assign(notice_id: str, request: Request,
                            assignee: Optional[str] = Query(default=None),
                            user: User = Depends(approved_user)):
    if not assignee:                 # ancien front : identifiant dans le corps JSON
        try:
            assignee = (await request.json()).get("assignee")
        except Exception:
            assignee = None
    if not assignee:
        raise HTTPException(status_code=400, detail="Paramètre « assignee » manquant.")
    if not database.remove_assignment(user, notice_id, assignee):
        raise HTTPException(status_code=404, detail="Assignation introuvable ou non autorisée.")
    return {"notice_id": notice_id, "assignee": assignee}


@app.get("/api/assignments/me")
def api_my_assignments(mark_seen: bool = Query(default=False),
                       user: User = Depends(approved_user)):
    rows = database.get_assignments_for(user)
    unread = sum(1 for r in rows if not r.get("seen"))
    if mark_seen and unread:
        database.mark_assignments_seen(user)
    return {"assignments": rows, "unread": unread}


@app.get("/api/assignments/user/{ignored}")      # ancien chemin : toujours « mes » assignations
def api_user_assignments(ignored: str, mark_seen: bool = Query(default=False),
                         user: User = Depends(approved_user)):
    return api_my_assignments(mark_seen, user)


# ── Administration ───────────────────────────────────────────────────────────

class ProfileUpdate(BaseModel):
    status: Optional[str] = None     # approved | rejected | revoked | pending
    role: Optional[str] = None       # user | admin


@app.get("/api/admin/users")
def api_admin_users(admin: User = Depends(admin_user)):
    return {"users": database.list_profiles(admin)}


@app.post("/api/admin/users/{profile_id}")
def api_admin_update_user(profile_id: str, body: ProfileUpdate, admin: User = Depends(admin_user)):
    if body.status is not None and body.status not in ("pending", "approved", "rejected", "revoked"):
        raise HTTPException(status_code=400, detail="Statut invalide.")
    if body.role is not None and body.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="Rôle invalide.")
    if profile_id == admin.id and ((body.status not in (None, "approved"))
                                   or (body.role not in (None, "admin"))):
        raise HTTPException(status_code=400,
                            detail="Vous ne pouvez pas retirer vos propres droits d'administration.")
    updated = database.update_profile(admin, profile_id, status=body.status, role=body.role)
    if not updated:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    auth.forget_all()
    return {"user": updated}


class SmtpTestBody(BaseModel):
    to: str


@app.post("/api/smtp/test")
def api_smtp_test(body: SmtpTestBody, admin: User = Depends(admin_user)):
    if not mailer.smtp_configured():
        raise HTTPException(status_code=400,
                            detail="SMTP non configuré (variables SMTP_USER / SMTP_PASSWORD).")
    try:
        mailer.send(body.to, *mailer.test_email())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Envoi impossible : {e}")
    return {"ok": True, "message": f"Email envoyé à {body.to}"}
