"""Authentification et droits.

Le navigateur se connecte avec Supabase Auth (Google ou lien magique) et envoie son
jeton dans l'en-tête « Authorization: Bearer … ». Un seul appel à la base,
veilleao_request_access(), fait les deux vérifications :
  * le jeton est valide (sinon Supabase répond 401) ;
  * le profil Veille AO existe — il est créé en « pending » à la première visite,
    ce qui constitue la demande d'accès.

Niveaux : current_user (connecté) < approved_user (validé) < admin_user.
Les routes /api/cron/* sont protégées par CRON_SECRET.
"""
import hmac
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

import config
import supa

CACHE_TTL = 30        # s — une révocation prend effet en 30 s au plus
CACHE_MAX = 500

_cache: "dict[str, tuple[float, User]]" = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class User:
    id: str
    email: str
    full_name: "str | None"
    role: str
    status: str
    requested_at: "str | None"
    admin_notified_at: "str | None"
    token: str

    @property
    def is_approved(self) -> bool:
        return self.status == "approved"

    @property
    def is_admin(self) -> bool:
        return self.is_approved and self.role == "admin"

    @property
    def display_name(self) -> str:
        return self.full_name or self.email

    def public(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.display_name,
                "role": self.role, "status": self.status, "requested_at": self.requested_at}


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer ") or not header[7:].strip():
        raise HTTPException(status_code=401, detail="Connexion requise.")
    return header[7:].strip()


def load_user(token: str) -> User:
    try:
        row = supa.as_user(token).rpc("veilleao_request_access")
    except supa.SupabaseError as e:
        if e.status in (401, 403):
            raise HTTPException(status_code=401, detail="Session invalide ou expirée.") from e
        raise HTTPException(status_code=502, detail="Base de données indisponible.") from e
    if not row or not row.get("id"):
        raise HTTPException(status_code=401, detail="Session invalide ou expirée.")
    return User(id=row["id"], email=row.get("email") or "", full_name=row.get("full_name"),
                role=row.get("role") or "user", status=row.get("status") or "pending",
                requested_at=row.get("requested_at"),
                admin_notified_at=row.get("admin_notified_at"), token=token)


def forget_all() -> None:
    """Vide le cache local (après une décision admin sur un compte)."""
    with _lock:
        _cache.clear()


def current_user(request: Request) -> User:
    token = _bearer_token(request)
    now = time.time()
    with _lock:
        hit = _cache.get(token)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    user = load_user(token)
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[token] = (now, user)
    return user


def approved_user(user: User = Depends(current_user)) -> User:
    if not user.is_approved:
        raise HTTPException(status_code=403, detail={
            "code": user.status,
            "message": {"pending": "Votre demande d'accès est en attente de validation.",
                        "rejected": "Votre demande d'accès a été refusée.",
                        "revoked": "Votre accès a été révoqué."}.get(
                            user.status, "Accès non autorisé.")})
    return user


def admin_user(user: User = Depends(approved_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs.")
    return user


def cron_auth(request: Request) -> None:
    secret = config.CRON_SECRET
    given = request.headers.get("Authorization", "")
    if not secret or not hmac.compare_digest(given.encode(), f"Bearer {secret}".encode()):
        raise HTTPException(status_code=401, detail="Secret de tâche planifiée invalide.")
