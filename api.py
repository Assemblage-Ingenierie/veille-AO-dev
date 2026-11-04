import json
import logging
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import (get_notices, get_stats, get_last_refresh_info, get_notice_by_id,
                       get_all_triage, set_triage, get_setting, set_setting,
                       assign_notice, remove_assignment, get_all_assignments,
                       get_assignments_for, mark_assignments_seen,
                       get_notice_detail_cache, set_notice_detail_cache, clear_notice_detail_cache)
from config import STRONG_KEYWORDS, INFRA_KEYWORDS, KEYWORD_DISPLAY_GROUPS, TEAM_MEMBERS

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Veille AO — AI-DEV", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Gestion d'erreurs globale ─────────────────────────────────────────────────
# Toute exception non rattrapée renvoie un JSON propre (et non un "Internal
# Server Error" brut que le front n'arrive pas à parser -> "Unexpected token I").
@app.exception_handler(Exception)
async def _json_error_handler(request: Request, exc: Exception):
    log.error(f"Erreur non gérée sur {request.url.path} : {exc!r}")
    detail = str(exc)
    hint = None
    low = detail.lower()
    if "locked" in low or "i/o" in low or "disk" in low or "accès refusé" in low \
            or "access" in low or "unable to open" in low:
        hint = ("La base de données est inaccessible. Vérifiez qu'elle n'est pas "
                "stockée sur Google Drive et qu'aucune autre instance ne tourne.")
    return JSONResponse(
        {"error": detail, "hint": hint, "path": str(request.url.path)},
        status_code=500,
    )


# ── Static files ──────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/analyse", include_in_schema=False)
async def analyse():
    return FileResponse(str(STATIC_DIR / "analyse.html"))


@app.get("/pertinentes", include_in_schema=False)
async def pertinentes():
    return FileResponse(str(STATIC_DIR / "pertinentes.html"))


@app.get("/params", include_in_schema=False)
async def params_page():
    return FileResponse(str(STATIC_DIR / "params.html"))


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Email notifications ───────────────────────────────────────────────────────

def _get_smtp_config() -> dict | None:
    """Retourne la config SMTP depuis SQLite, ou None si non configurée."""
    raw = get_setting("smtp_config")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _get_member_email(name: str) -> str:
    """Retourne l'email d'un membre de l'équipe."""
    raw = get_setting("team_members")
    if raw:
        try:
            members = json.loads(raw)
        except Exception:
            members = TEAM_MEMBERS
    else:
        members = TEAM_MEMBERS
    for m in members:
        if m.get("name") == name:
            return m.get("email") or ""
    return ""


def _build_email(assignee: str, assigned_by: str, notice: dict) -> MIMEMultipart:
    titre    = notice.get("titre", "—")
    bailleur = notice.get("bailleur", "—")
    deadline = notice.get("date_limite") or "—"
    lien     = notice.get("lien") or ""
    by_label = assigned_by or "Un collègue"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Veille AO] Nouvel AO assigné : {titre[:70]}"

    # Plain text
    text_parts = [
        f"Bonjour {assignee},",
        "",
        f"{by_label} vous a assigné un appel d'offres à analyser :",
        "",
        f"  Titre    : {titre}",
        f"  Bailleur : {bailleur}",
        f"  Deadline : {deadline}",
        "",
        "Voir vos assignations : http://localhost:8000/pertinentes",
    ]
    if lien:
        text_parts.append(f"Lien source : {lien}")
    text_parts += ["", "—", "Plateforme de veille AO — Assemblage ingénierie"]

    # HTML
    lien_btn = (
        f'<a href="{lien}" style="color:#E30513;font-size:13px">Voir l\'avis source →</a>'
        if lien else ""
    )
    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:20px;background:#F2F2F2;font-family:'Open Sans',sans-serif">
<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)">
  <div style="background:#E30513;padding:16px 24px">
    <p style="color:#fff;margin:0;font-size:18px;font-weight:700">Nouvel AO assigné</p>
    <p style="color:rgba(255,255,255,.8);margin:4px 0 0;font-size:12px">Plateforme de veille — Assemblage ingénierie</p>
  </div>
  <div style="padding:24px">
    <p style="margin:0 0 12px">Bonjour <strong>{assignee}</strong>,</p>
    <p style="margin:0 0 16px;color:#4D4D4D">{by_label} vous a assigné un appel d'offres à analyser :</p>
    <div style="background:#F9E1E3;border-left:4px solid #E30513;padding:14px 18px;border-radius:0 4px 4px 0;margin-bottom:20px">
      <p style="margin:0 0 6px;font-weight:700;color:#000">{titre}</p>
      <p style="margin:0;font-size:13px;color:#4D4D4D">{bailleur} &nbsp;·&nbsp; Deadline : <strong>{deadline}</strong></p>
    </div>
    <a href="http://localhost:8000/pertinentes"
       style="display:inline-block;background:#30323E;color:#fff;padding:10px 22px;text-decoration:none;border-radius:4px;font-weight:600;font-size:13px">
      Voir sur la plateforme →
    </a>
    &nbsp;&nbsp;{lien_btn}
  </div>
  <div style="background:#F2F2F2;padding:10px 24px;font-size:11px;color:#4D4D4D;border-top:1px solid #DFE4E8">
    Assemblage ingénierie · Plateforme de veille AO
  </div>
</div>
</body>
</html>"""

    msg.attach(MIMEText("\n".join(text_parts), "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def _send_email_now(to_addr: str, msg: MIMEMultipart, smtp_cfg: dict) -> None:
    """Envoie l'email via Gmail SMTP (appelé dans un thread)."""
    user     = smtp_cfg.get("user", "")
    password = smtp_cfg.get("password", "")
    msg["From"] = user
    msg["To"]   = to_addr
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, to_addr, msg.as_string())
    log.info(f"Email envoyé à {to_addr} — sujet : {msg['Subject']}")


def _notify_assignee(assignee: str, assigned_by: str, notice: dict) -> None:
    """Lance l'envoi en arrière-plan (ne bloque pas la réponse API)."""
    smtp_cfg = _get_smtp_config()
    if not smtp_cfg:
        log.debug("Notifications email désactivées (SMTP non configuré)")
        return
    to_addr = _get_member_email(assignee)
    if not to_addr:
        log.debug(f"Pas d'email pour {assignee!r}, notification ignorée")
        return
    try:
        msg = _build_email(assignee, assigned_by, notice)
        threading.Thread(
            target=_send_email_now,
            args=(to_addr, msg, smtp_cfg),
            daemon=True,
        ).start()
    except Exception as e:
        log.warning(f"Impossible de préparer l'email : {e}")


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/notices")
async def api_notices(
    bailleur: list[str] = Query(default=[]),
    langue:   list[str] = Query(default=[]),
    pertinence: list[str] = Query(default=[]),
    deadline_from: str = Query(default=""),
    deadline_to:   str = Query(default=""),
):
    filters = {}
    if bailleur:
        filters["bailleur"] = bailleur
    if langue:
        filters["langue"] = langue
    if pertinence:
        filters["pertinence"] = pertinence
    if deadline_from:
        filters["deadline_from"] = deadline_from
    if deadline_to:
        filters["deadline_to"] = deadline_to

    notices = get_notices(filters if filters else None)

    # Strip heavy texte_complet from the list response
    for n in notices:
        n.pop("texte_complet", None)
        n.pop("date_ajout", None)

    from scheduler import state as sched_state
    return {
        "total": len(notices),
        "last_update": sched_state.get("last_run"),
        "is_refreshing": sched_state.get("is_running", False),
        "notices": notices,
    }


@app.get("/api/notices/stats")
async def api_stats():
    return get_stats()


@app.get("/api/refresh/status")
async def api_refresh_status():
    from scheduler import state as sched_state
    return {
        "is_running": sched_state.get("is_running", False),
        "last_run":   sched_state.get("last_run"),
        "counts":     sched_state.get("counts", {}),
        "errors":     sched_state.get("errors", []),
    }


@app.post("/api/refresh")
async def api_refresh():
    from scheduler import state as sched_state, collect_all_sources
    if sched_state.get("is_running"):
        return JSONResponse({"status": "already_running", "message": "Collecte déjà en cours"})
    threading.Thread(target=collect_all_sources, daemon=True).start()
    return {"status": "started", "message": "Collecte démarrée en arrière-plan"}


@app.post("/api/refresh/ted")
async def api_refresh_ted():
    """Rafraîchit uniquement la source TED (le front lit toujours la base)."""
    from scheduler import state as sched_state, collect_ted
    if sched_state.get("is_running"):
        return JSONResponse({"status": "already_running", "message": "Collecte déjà en cours"})
    threading.Thread(target=collect_ted, daemon=True).start()
    return {"status": "started", "message": "Collecte TED démarrée en arrière-plan"}


@app.get("/api/debug/afd")
async def api_debug_afd():
    """Diagnostic du scraper AFD : teste la connexion et l'analyse HTML."""
    import requests
    from sources.afd import _make_session, _parse_page, _total_pages, AFD_LIST
    from datetime import date
    from bs4 import BeautifulSoup

    result = {"url": AFD_LIST, "status_code": None, "content_length": None,
              "tables_found": [], "notice_table": None, "total_pages": None,
              "notices_page1": None, "error": None}
    try:
        session = _make_session()
        r = session.get(AFD_LIST, timeout=30, allow_redirects=True)
        result["status_code"] = r.status_code
        result["content_length"] = len(r.content)
        result["final_url"] = r.url

        try:
            soup = BeautifulSoup(r.content, "lxml")
        except Exception:
            soup = BeautifulSoup(r.content, "html.parser")

        # Recenser toutes les tables et leurs id/class
        tables = soup.find_all("table")
        result["tables_found"] = [
            {"id": t.get("id"), "class": t.get("class"), "rows": len(t.find_all("tr"))}
            for t in tables
        ]
        result["notice_table"] = soup.find("table", id="notice") is not None
        result["total_pages"] = _total_pages(soup)
        result["notices_page1"] = len(_parse_page(soup, date.today()))

        # Extrait de texte autour de la pagination
        for text in soup.stripped_strings:
            if "de" in text.lower() and any(c.isdigit() for c in text):
                result.setdefault("pagination_hints", []).append(text[:120])
                if len(result.get("pagination_hints", [])) >= 5:
                    break

    except Exception as e:
        result["error"] = str(e)

    return result


@app.get("/api/ao-detail/{notice_id}")
async def api_ao_detail(notice_id: str, force: bool = Query(default=False)):
    """Récupère les données enrichies d'un AO. Sert le cache SQLite si disponible."""
    notice = get_notice_by_id(notice_id)
    if not notice:
        return JSONResponse({"error": "Notice introuvable"}, status_code=404)

    _USEFUL = {"description", "description_section", "full_text",
               "criteria_section", "documents"}

    # Lecture du cache (sauf si force=true)
    # Toute entrée existante (succès ou échec) stoppe le retry automatique.
    # Seul force=true permet de relancer le fetch depuis la source.
    if not force:
        cached = get_notice_detail_cache(notice_id)
        if cached is not None:
            if cached.get("_failed"):
                # Échec mis en cache — on ne réessaie pas automatiquement
                log.debug(f"Detail cache (failed) : {notice_id}")
                return {"id": notice_id, "bailleur": notice.get("bailleur"),
                        "_cached": True, "_failed": True,
                        "_fetched_at": cached.get("_fetched_at")}
            if any(cached.get(k) for k in _USEFUL):
                log.debug(f"Detail cache hit : {notice_id}")
                return {"id": notice_id, "bailleur": notice.get("bailleur"),
                        **cached, "_cached": True}

    # Fetch depuis la source
    from sources.detail import fetch_detail
    try:
        detail = fetch_detail(
            notice_id=notice_id,
            lien=notice.get("lien") or "",
            bailleur=notice.get("bailleur") or "",
            texte_complet=notice.get("texte_complet") or "",
        )
    except Exception as e:
        log.error(f"ao-detail fetch error for {notice_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

    # Mise en cache du résultat OU de l'échec
    # → les deux évitent de refetcher inutilement à chaque rechargement
    has_useful = not detail.get("error") and any(detail.get(k) for k in _USEFUL)
    cache_payload = detail if has_useful else {"_failed": True}
    try:
        set_notice_detail_cache(notice_id, cache_payload)
    except Exception as cache_err:
        log.warning(f"Cache write failed for {notice_id}: {cache_err}")

    return {"id": notice_id, "bailleur": notice.get("bailleur"), **detail}


@app.delete("/api/ao-detail/{notice_id}/cache")
async def api_clear_detail_cache(notice_id: str):
    """Invalide le cache détail d'un AO pour forcer une relecture à la prochaine ouverture."""
    clear_notice_detail_cache(notice_id)
    return {"notice_id": notice_id, "status": "cache_cleared"}


@app.get("/api/triage")
async def api_get_triage():
    """Retourne l'état de triage de toutes les notices."""
    return {"triage": get_all_triage()}


class TriageBody(BaseModel):
    state: Optional[str] = None


@app.post("/api/triage/{notice_id}")
async def api_set_triage(notice_id: str, body: TriageBody):
    """Met à jour le triage d'une notice."""
    valid = {"pertinent", "pre-tri", "non-pertinent", None}
    if body.state not in valid:
        return JSONResponse({"error": f"État invalide : {body.state}"}, status_code=400)
    set_triage(notice_id, body.state)
    return {"id": notice_id, "state": body.state}


class SettingBody(BaseModel):
    value: Optional[str] = None


@app.get("/api/settings/{key}")
async def api_get_setting(key: str):
    """Retourne un paramètre partagé (ex. listes de mots-clés)."""
    return {"key": key, "value": get_setting(key)}


@app.post("/api/settings/{key}")
async def api_set_setting(key: str, body: SettingBody):
    """Crée ou met à jour un paramètre partagé."""
    set_setting(key, body.value)
    return {"key": key, "value": body.value}


def _get_team_members() -> list:
    """Retourne les membres depuis SQLite si définis, sinon depuis config.py."""
    import json
    stored = get_setting("team_members")
    if stored:
        try:
            return json.loads(stored)
        except Exception:
            pass
    return TEAM_MEMBERS


@app.get("/api/team")
async def api_team():
    return {"members": _get_team_members()}


class TeamBody(BaseModel):
    members: list


@app.post("/api/team")
async def api_set_team(body: TeamBody):
    import json
    set_setting("team_members", json.dumps(body.members))
    return {"members": body.members}


@app.get("/api/assignments")
async def api_get_assignments():
    return {"assignments": get_all_assignments()}


class AssignBody(BaseModel):
    assignee: str
    assigned_by: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/assignments/{notice_id}")
async def api_assign(notice_id: str, body: AssignBody):
    assign_notice(notice_id, body.assignee, body.assigned_by, body.note)
    # Notification email en arrière-plan
    notice = get_notice_by_id(notice_id) or {"id": notice_id}
    _notify_assignee(body.assignee, body.assigned_by or "", notice)
    return {"notice_id": notice_id, "assignee": body.assignee}


@app.delete("/api/assignments/{notice_id}")
async def api_remove_assign(notice_id: str, body: AssignBody):
    remove_assignment(notice_id, body.assignee)
    return {"notice_id": notice_id, "assignee": body.assignee}


@app.get("/api/assignments/user/{assignee}")
async def api_user_assignments(assignee: str, mark_seen: bool = Query(default=False)):
    rows = get_assignments_for(assignee)
    unread = sum(1 for r in rows if not r["seen"])
    if mark_seen:
        mark_assignments_seen(assignee)
    return {"assignments": rows, "unread": unread}


class SmtpTestBody(BaseModel):
    to: str


@app.post("/api/smtp/test")
async def api_smtp_test(body: SmtpTestBody):
    """Envoie un email de test pour vérifier la configuration SMTP."""
    smtp_cfg = _get_smtp_config()
    if not smtp_cfg:
        return JSONResponse({"ok": False, "error": "SMTP non configuré"}, status_code=400)
    if not body.to:
        return JSONResponse({"ok": False, "error": "Adresse destinataire manquante"}, status_code=400)
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "[Veille AO] Test de configuration email"
        msg.attach(MIMEText(
            "Félicitations ! Votre configuration email fonctionne correctement.\n\n"
            "— Plateforme de veille AO · Assemblage ingénierie",
            "plain", "utf-8"
        ))
        msg.attach(MIMEText(
            """<!DOCTYPE html><html><body style="font-family:sans-serif;padding:20px">
            <div style="max-width:480px;background:#fff;border:1px solid #DFE4E8;border-radius:6px;overflow:hidden">
              <div style="background:#E30513;padding:14px 20px">
                <p style="color:#fff;margin:0;font-weight:700">Test de configuration email ✓</p>
              </div>
              <div style="padding:20px">
                <p>Félicitations ! Votre configuration email fonctionne correctement.</p>
                <p style="color:#4D4D4D;font-size:13px">Les notifications d'assignation seront envoyées automatiquement.</p>
              </div>
              <div style="background:#F2F2F2;padding:10px 20px;font-size:11px;color:#4D4D4D">
                Plateforme de veille AO · Assemblage ingénierie
              </div>
            </div></body></html>""",
            "html", "utf-8"
        ))
        _send_email_now(body.to, msg, smtp_cfg)
        return {"ok": True, "message": f"Email envoyé à {body.to}"}
    except smtplib.SMTPAuthenticationError:
        return JSONResponse({"ok": False, "error": "Authentification échouée — vérifiez l'adresse et le mot de passe d'application."}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/keywords")
async def api_keywords():
    return {
        "strong": STRONG_KEYWORDS,
        "infra":  INFRA_KEYWORDS,
        "groups": KEYWORD_DISPLAY_GROUPS,
    }
