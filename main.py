"""Point d'entrée Veille AO.

* En production, Vercel importe `app` depuis ce fichier (fonction Python).
* En local (développement) : `python main.py` → http://localhost:8000
  Variables lues dans le fichier .env (voir .env.example). La collecte se lance
  depuis le bouton « Actualiser » (admin) ; il n'y a plus de planificateur local.
"""
import logging

from api import app  # noqa: F401  (point d'entrée Vercel)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s : %(message)s",
                    datefmt="%H:%M:%S")

if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run("api:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8000")),
                reload=False, log_level="info")
