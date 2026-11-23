"""Collecte des avis, source par source (remplace scheduler.py).

Chaque source tient en un appel court (< 20 s mesurés, plafond Vercel : 300 s) :
le bouton « Actualiser » les enchaîne depuis le navigateur, la tâche planifiée
quotidienne les enchaîne côté serveur. Chaque exécution est journalisée dans
veilleao_collect_runs.
"""
import importlib
import logging
import time

import database
import supa

log = logging.getLogger(__name__)

# clé → (libellé, module, fonction, refresh)
#   refresh=True : un avis déjà en base voit sa deadline mise à jour (TED republie).
SOURCES = {
    "bm":   ("Banque Mondiale",  "sources.bm",               "fetch_bm",               False),
    "pnud": ("PNUD",             "sources.undp",             "fetch_undp",             False),
    "giz":  ("GIZ",              "sources.giz",              "fetch_giz",              False),
    "berd": ("BERD",             "sources.ebrd",             "fetch_ebrd",             False),
    "afd":  ("AFD",              "sources.afd",              "fetch_afd",              False),
    "ted":  ("TED",              "sources.ted",              "fetch_ted",              True),
    "ef":   ("Expertise France", "sources.expertise_france", "fetch_expertise_france", False),
    "ungm": ("UNGM",             "sources.ungm",             "fetch_ungm",             False),
}

# Sources qui savent ne demander que les avis publiés depuis la dernière lecture
# réussie : leur fonction de collecte reçoit since=<datetime> (None = tout relire).
INCREMENTAL = {"ungm"}


def list_sources() -> list:
    return [{"key": k, "label": v[0]} for k, v in SOURCES.items()]


def run_source(key: str, triggered_by: "str | None" = None) -> dict:
    if key not in SOURCES:
        raise KeyError(key)
    label, module, fn_name, refresh = SOURCES[key]
    sb = supa.service()
    since = database.last_success(key, sb=sb) if key in INCREMENTAL else None
    run_id = database.start_run(key, triggered_by, sb=sb)
    t0 = time.time()
    try:
        fetch = getattr(importlib.import_module(module), fn_name)
        notices = fetch(since=since) if key in INCREMENTAL else fetch()
        written = database.save_notices(notices, refresh=refresh, sb=sb)
        expired = database.mark_expired(sb=sb)
        database.finish_run(run_id, fetched=len(notices), inserted=written, sb=sb)
        log.info(f"{label} : {len(notices)} récupérés, {written} écrits, {expired} expirés")
        return {"source": key, "label": label, "ok": True, "fetched": len(notices),
                "inserted": written, "expired": expired, "seconds": round(time.time() - t0, 1)}
    except Exception as e:  # une source en panne ne doit pas bloquer les autres
        msg = f"{type(e).__name__}: {e}"[:500]
        log.warning(f"{label} indisponible : {msg}")
        try:
            database.finish_run(run_id, error=msg, sb=sb)
        except Exception as log_err:
            log.error(f"Journal de collecte non mis à jour : {log_err}")
        return {"source": key, "label": label, "ok": False, "error": msg,
                "seconds": round(time.time() - t0, 1)}


def run_all(triggered_by: "str | None" = None) -> list:
    return [run_source(k, triggered_by) for k in SOURCES]
