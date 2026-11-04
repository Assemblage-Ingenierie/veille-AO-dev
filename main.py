"""Point d'entrée — installe les dépendances si nécessaire, démarre l'application."""
import subprocess
import sys
import os


def _ensure_deps():
    """Install requirements if any package is missing."""
    try:
        import fastapi, uvicorn, requests, bs4, dateutil, schedule, lxml  # noqa: F401
    except ImportError:
        print("Installation des dépendances en cours…")
        req = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req])
        print("Dépendances installées — relancement…")
        # Re-exec the process so the newly installed modules are importable
        os.execv(sys.executable, [sys.executable] + sys.argv)


_ensure_deps()

# ── All imports after dep check ───────────────────────────────────────────────
import logging
import threading
import webbrowser

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s : %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger(__name__)


def main():
    from database import init_db
    from scheduler import run_scheduler

    # Initialise SQLite
    init_db()

    # Scheduler in background
    t = threading.Thread(target=run_scheduler, daemon=True, name="scheduler")
    t.start()

    # Open browser after server is likely up
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8000")).start()

    log.info("Démarrage du serveur sur http://localhost:8000")
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False, log_level="warning")


if __name__ == "__main__":
    main()
