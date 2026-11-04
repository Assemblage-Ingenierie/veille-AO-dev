import logging
import threading
import time

import schedule

from database import save_notices, upsert_notices, mark_expired
from sources.bm import fetch_bm
from sources.undp import fetch_undp
from sources.giz import fetch_giz
from sources.ebrd import fetch_ebrd
from sources.afd import fetch_afd
from sources.ted import fetch_ted
from sources.expertise_france import fetch_expertise_france
from config import SCHEDULER_TIME

log = logging.getLogger(__name__)

# Shared state readable by the API
state = {
    "is_running": False,
    "last_run": None,
    "counts": {},
    "errors": [],
}

_lock = threading.Lock()


def collect_all_sources():
    with _lock:
        if state["is_running"]:
            log.info("Collecte déjà en cours, ignorée")
            return
        state["is_running"] = True
        state["errors"] = []

    log.info("=== Démarrage collecte ===")
    counts = {}

    # (nom, fonction de collecte, fonction d'écriture en base)
    sources = [
        ("Banque Mondiale", fetch_bm,   save_notices),
        ("PNUD",            fetch_undp, save_notices),
        ("GIZ",             fetch_giz,  save_notices),
        ("BERD",            fetch_ebrd, save_notices),
        ("AFD",             fetch_afd,  save_notices),
        ("TED",             fetch_ted,  upsert_notices),
        ("Expertise France", fetch_expertise_france, save_notices),
    ]

    for name, fn, saver in sources:
        try:
            notices = fn()
            inserted = saver(notices)
            counts[name] = {"fetched": len(notices), "inserted": inserted}
            log.info(f"{name} : {len(notices)} récupérés, {inserted} nouveaux")
        except Exception as e:
            msg = f"{name} indisponible : {e}"
            log.warning(msg)
            counts[name] = {"fetched": 0, "inserted": 0, "error": str(e)}
            state["errors"].append(msg)

    try:
        mark_expired()
    except Exception as e:
        log.warning(f"mark_expired : {e}")

    from datetime import datetime
    with _lock:
        state["is_running"] = False
        state["last_run"] = datetime.now().isoformat()
        state["counts"] = counts

    log.info(f"=== Collecte terminée : {counts} ===")


def collect_ted():
    """Collecte TED uniquement (déclenchée par POST /api/refresh/ted)."""
    with _lock:
        if state["is_running"]:
            log.info("Collecte déjà en cours, TED ignoré")
            return
        state["is_running"] = True
        state["errors"] = []

    log.info("=== Démarrage collecte TED ===")
    counts = {}
    try:
        notices = fetch_ted()
        inserted = upsert_notices(notices)
        counts["TED"] = {"fetched": len(notices), "inserted": inserted}
        log.info(f"TED : {len(notices)} récupérés, {inserted} insérés/màj")
    except Exception as e:
        msg = f"TED indisponible : {e}"
        log.warning(msg)
        counts["TED"] = {"fetched": 0, "inserted": 0, "error": str(e)}
        state["errors"].append(msg)

    try:
        mark_expired()
    except Exception as e:
        log.warning(f"mark_expired : {e}")

    from datetime import datetime
    with _lock:
        state["is_running"] = False
        state["last_run"] = datetime.now().isoformat()
        state["counts"] = counts

    log.info(f"=== Collecte TED terminée : {counts} ===")


def run_scheduler():
    """Run the daily scheduler. Executes immediately on first call."""
    log.info(f"Scheduler démarré — collecte quotidienne à {SCHEDULER_TIME}")

    # Immediate first run
    threading.Thread(target=collect_all_sources, daemon=True).start()

    schedule.every().day.at(SCHEDULER_TIME).do(
        lambda: threading.Thread(target=collect_all_sources, daemon=True).start()
    )

    while True:
        schedule.run_pending()
        time.sleep(30)
