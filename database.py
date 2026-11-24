"""Accès aux données Veille AO dans Supabase (tables veilleao_* du projet INTERNAL).

Mêmes fonctions qu'avec l'ancienne base SQLite. Celles qui touchent aux données
d'un utilisateur reçoivent son compte (`user`) et passent par son jeton : la
sécurité de la base (RLS) s'applique. Celles qui servent la collecte, le cache et
les journaux utilisent la clé serveur.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import supa
from nature import nature_of
from supa import eq, in_

log = logging.getLogger(__name__)

TRIAGE_STATES = ("non-pertinent", "pre-tri", "pertinent")
SHARED_SETTINGS = ("params_keywords", "exclude_keywords")
# Colonnes renvoyées à l'interface (sans texte_complet, réservé au scoring/détail).
NOTICE_LIST_COLS = ("id,bailleur,titre,projet,secteur,type_avis,mode_selection,langue,"
                    "date_publication,date_limite,pays,lien,pertinence,triage")
UPSERT_COLS = ("id", "bailleur", "titre", "projet", "secteur", "type_avis", "mode_selection",
               "langue", "date_publication", "date_limite", "pays", "lien", "pertinence",
               "texte_complet")
MAX_BATCH_BYTES = 900_000
RUN_STALE_AFTER = timedelta(minutes=10)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Avis ────────────────────────────────────────────────────────────────────

def get_notices(user, filters: "dict | None" = None) -> list:
    params = {"select": NOTICE_LIST_COLS, "actif": "is.true",
              "order": "date_limite.asc.nullslast,id.asc"}
    f = filters or {}
    if f.get("bailleur"):
        params["bailleur"] = in_(f["bailleur"])
    if f.get("langue"):
        params["langue"] = in_(f["langue"])
    if f.get("pertinence"):
        params["pertinence"] = in_(f["pertinence"])
    if f.get("deadline_from") and f.get("deadline_to"):
        params["and"] = f"(date_limite.gte.{f['deadline_from']},date_limite.lte.{f['deadline_to']})"
    elif f.get("deadline_from"):
        params["date_limite"] = f"gte.{f['deadline_from']}"
    elif f.get("deadline_to"):
        params["date_limite"] = f"lte.{f['deadline_to']}"
    rows = supa.as_user(user.token).select("veilleao_notices", params, all_rows=True)
    for r in rows:   # nature du marché : calculée à la volée (voir nature.py)
        r["nature"] = nature_of(r)
    return rows


def get_stats(user) -> dict:
    stats = {"total": 0, "by_bailleur": {}, "by_langue": {}, "by_secteur": {}, "by_pertinence": {}}
    for n in get_notices(user):
        stats["total"] += 1
        for key, col, default in (("by_bailleur", "bailleur", "Autre"),
                                  ("by_langue", "langue", "Inconnu"),
                                  ("by_secteur", "secteur", "Autre"),
                                  ("by_pertinence", "pertinence", "Inconnu")):
            v = n.get(col) or default
            stats[key][v] = stats[key].get(v, 0) + 1
    return stats


def get_notice_by_id(notice_id: str) -> "dict | None":
    """Notice complète (avec texte_complet) — appelant déjà vérifié comme membre validé."""
    rows = supa.service().select("veilleao_notices", {"select": "*", "id": eq(notice_id)})
    return rows[0] if rows else None


def batches_by_size(rows: list, max_bytes: int = MAX_BATCH_BYTES):
    batch, size = [], 0
    for row in rows:
        b = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        if batch and size + b > max_bytes:
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += b
    if batch:
        yield batch


def save_notices(notices: list, refresh: bool = False, sb: "supa.Client | None" = None) -> int:
    """Écrit les avis collectés. refresh=False : un AO existant n'est jamais écrasé ;
    refresh=True (TED) : deadline et date de collecte mises à jour."""
    if not notices:
        return 0
    sb = sb or supa.service()
    rows = [{c: (n.get(c) if n.get(c) is not None else "") for c in UPSERT_COLS} for n in notices]
    written = 0
    for batch in batches_by_size(rows):
        written += sb.rpc("veilleao_upsert_notices", {"p_rows": batch, "p_refresh": refresh}) or 0
    return written


def mark_expired(sb: "supa.Client | None" = None) -> int:
    return (sb or supa.service()).rpc("veilleao_mark_expired") or 0


# ── Tri commun ──────────────────────────────────────────────────────────────

def get_all_triage(user) -> dict:
    """{"triage": {id: état}, "meta": {id: {"by": nom, "at": horodatage}}}."""
    sb = supa.as_user(user.token)
    rows = sb.select("veilleao_notices", {"select": "id,triage,triaged_by,triaged_at",
                                          "triage": "not.is.null"}, all_rows=True)
    names = {p["id"]: (p.get("full_name") or p.get("email"))
             for p in sb.select("veilleao_profiles", {"select": "id,email,full_name"})}
    return {"triage": {r["id"]: r["triage"] for r in rows},
            "meta": {r["id"]: {"by": names.get(r.get("triaged_by")), "at": r.get("triaged_at")}
                     for r in rows}}


def set_triage(user, notice_id: str, state: "str | None") -> bool:
    if state is not None and state not in TRIAGE_STATES:
        raise ValueError(f"État de tri invalide : {state}")
    rows = supa.as_user(user.token).update("veilleao_notices", {"id": eq(notice_id)},
                                           {"triage": state})
    return bool(rows)


# ── Réglages partagés ───────────────────────────────────────────────────────

def get_setting(user, key: str):
    rows = supa.as_user(user.token).select("veilleao_settings",
                                           {"select": "value", "key": eq(key)})
    return rows[0]["value"] if rows else None


def set_setting(user, key: str, value) -> None:
    supa.as_user(user.token).insert("veilleao_settings", [{"key": key, "value": value}],
                                    on_conflict="key", resolution="merge-duplicates")


# ── Équipe / profils ────────────────────────────────────────────────────────

def list_team(user) -> list:
    """Membres validés (liste d'assignation)."""
    return supa.as_user(user.token).select(
        "veilleao_profiles", {"select": "id,email,full_name,role", "status": "eq.approved",
                              "order": "full_name.asc.nullslast,email.asc"})


def list_profiles(admin) -> list:
    return supa.as_user(admin.token).select(
        "veilleao_profiles",
        {"select": "id,email,full_name,role,status,requested_at,decided_at,decided_by",
         "order": "requested_at.desc"}, all_rows=True)


def update_profile(admin, profile_id: str, status: "str | None" = None,
                   role: "str | None" = None) -> "dict | None":
    body = {k: v for k, v in (("status", status), ("role", role)) if v is not None}
    if not body:
        return None
    rows = supa.as_user(admin.token).update("veilleao_profiles", {"id": eq(profile_id)}, body)
    return rows[0] if rows else None


def get_profile(profile_id: str) -> "dict | None":
    rows = supa.service().select("veilleao_profiles", {"select": "*", "id": eq(profile_id)})
    return rows[0] if rows else None


# ── Assignations ────────────────────────────────────────────────────────────

def _names(user) -> dict:
    return {p["id"]: (p.get("full_name") or p.get("email")) for p in list_team(user)}


def get_all_assignments(user) -> dict:
    """{notice_id: [{assignee, assignee_name, assigned_by, assigned_by_name, assigned_at, note, seen}]}"""
    rows = supa.as_user(user.token).select(
        "veilleao_assignments", {"select": "*", "order": "assigned_at.desc"}, all_rows=True)
    names = _names(user)
    out: dict = {}
    for r in rows:
        out.setdefault(r["notice_id"], []).append({
            "assignee": r["assignee"], "assignee_name": names.get(r["assignee"]),
            "assigned_by": r.get("assigned_by"), "assigned_by_name": names.get(r.get("assigned_by")),
            "assigned_at": r.get("assigned_at"), "note": r.get("note"), "seen": r.get("seen")})
    return out


def assign_notice(user, notice_id: str, assignee_id: str, note: "str | None" = None) -> None:
    supa.as_user(user.token).insert(
        "veilleao_assignments", [{"notice_id": notice_id, "assignee": assignee_id, "note": note}])


def remove_assignment(user, notice_id: str, assignee_id: str) -> bool:
    return bool(supa.as_user(user.token).delete(
        "veilleao_assignments", {"notice_id": eq(notice_id), "assignee": eq(assignee_id)}))


def get_assignments_for(user) -> list:
    rows = supa.as_user(user.token).select(
        "veilleao_assignments", {"select": "notice_id,assigned_by,assigned_at,note,seen",
                                 "assignee": eq(user.id), "order": "assigned_at.desc"})
    names = _names(user)
    for r in rows:
        r["assigned_by_name"] = names.get(r.get("assigned_by"))
    return rows


def mark_assignments_seen(user) -> None:
    supa.as_user(user.token).update("veilleao_assignments",
                                    {"assignee": eq(user.id), "seen": "is.false"}, {"seen": True})


# ── Cache des détails enrichis (clé serveur) ────────────────────────────────

def get_notice_detail_cache(notice_id: str) -> "dict | None":
    rows = supa.service().select("veilleao_notice_details",
                                 {"select": "detail,fetched_at", "notice_id": eq(notice_id)})
    if not rows:
        return None
    data = dict(rows[0]["detail"] or {})
    data["_fetched_at"] = rows[0]["fetched_at"]
    return data


def set_notice_detail_cache(notice_id: str, detail: dict) -> None:
    supa.service().insert("veilleao_notice_details",
                          [{"notice_id": notice_id, "detail": detail, "fetched_at": _now()}],
                          on_conflict="notice_id", resolution="merge-duplicates")


def clear_notice_detail_cache(notice_id: str) -> None:
    supa.service().delete("veilleao_notice_details", {"notice_id": eq(notice_id)})


# ── Journal des collectes ───────────────────────────────────────────────────

def start_run(source: str, triggered_by: "str | None", sb: "supa.Client | None" = None) -> int:
    rows = (sb or supa.service()).insert(
        "veilleao_collect_runs", [{"source": source, "triggered_by": triggered_by}],
        returning=True)
    return rows[0]["id"]


def last_success(source: str, sb: "supa.Client | None" = None) -> "datetime | None":
    """Début de la dernière collecte réussie (terminée, sans erreur) de cette source."""
    rows = (sb or supa.service()).select("veilleao_collect_runs", {
        "select": "started_at", "source": eq(source), "finished_at": "not.is.null",
        "error": "is.null", "order": "started_at.desc", "limit": 1})
    return datetime.fromisoformat(rows[0]["started_at"]) if rows else None


def finish_run(run_id: int, fetched: "int | None" = None, inserted: "int | None" = None,
               error: "str | None" = None, sb: "supa.Client | None" = None) -> None:
    (sb or supa.service()).update("veilleao_collect_runs", {"id": eq(run_id)},
                                  {"finished_at": _now(), "fetched": fetched,
                                   "inserted": inserted, "error": error})


def collect_status(user) -> dict:
    """Dernière exécution par source + indicateur « collecte en cours »."""
    rows = supa.as_user(user.token).select(
        "veilleao_collect_runs", {"select": "*", "order": "started_at.desc", "limit": 200})
    last: dict = {}
    for r in rows:
        last.setdefault(r["source"], r)
    cutoff = datetime.now(timezone.utc) - RUN_STALE_AFTER
    running = any(r["finished_at"] is None
                  and datetime.fromisoformat(r["started_at"]) > cutoff for r in rows)
    finished = [r["finished_at"] for r in rows if r.get("finished_at")]
    return {"is_running": running, "last_run": max(finished) if finished else None,
            "sources": last}
