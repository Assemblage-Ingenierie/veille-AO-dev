"""Accès à Supabase (PostgREST) par HTTP, sans dépendance supplémentaire.

Deux façons d'appeler la base :
  * as_user(jeton) : au nom de l'utilisateur connecté — la sécurité de la base
    (RLS, droits par colonne) s'applique comme pour lui ;
  * service()      : clé secrète — contourne la RLS. Réservé à la collecte, au
    cache des détails, aux journaux et aux emails système.
"""
import threading

import requests

import config

PAGE_SIZE = 1000          # plafond de lignes par requête côté Supabase (max_rows)

_local = threading.local()


class SupabaseError(Exception):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = _local.session = requests.Session()
    return s


def eq(value) -> str:
    return f"eq.{value}"


def in_(values) -> str:
    quoted = ",".join('"' + str(v).replace('"', '\\"') + '"' for v in values)
    return f"in.({quoted})"


class Client:
    def __init__(self, apikey: str, bearer: "str | None" = None):
        self.base = config.SUPABASE_URL.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": apikey, "Content-Type": "application/json"}
        if bearer:
            self.headers["Authorization"] = f"Bearer {bearer}"

    def _req(self, method: str, path: str, *, params=None, json=None, headers=None,
             timeout: int = 60) -> requests.Response:
        r = _session().request(method, f"{self.base}/{path}", params=params, json=json,
                               headers={**self.headers, **(headers or {})}, timeout=timeout)
        if r.status_code >= 400:
            try:
                body = r.json()
                message, code = body.get("message") or r.text, body.get("code", "")
            except ValueError:
                message, code = r.text, ""
            raise SupabaseError(r.status_code, (message or "")[:500], code or "")
        return r

    def select(self, table: str, params: "dict | None" = None, *, all_rows: bool = False) -> list:
        params = dict(params or {})
        if not all_rows:
            return self._req("GET", table, params=params).json()
        rows, offset = [], 0
        while True:
            batch = self._req("GET", table,
                              params={**params, "limit": PAGE_SIZE, "offset": offset}).json()
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                return rows
            offset += PAGE_SIZE

    def rpc(self, fn: str, payload: "dict | None" = None, timeout: int = 120):
        r = self._req("POST", f"rpc/{fn}", json=payload or {}, timeout=timeout)
        return r.json() if r.content else None

    def insert(self, table: str, rows, *, on_conflict: "str | None" = None,
               resolution: "str | None" = None, returning: bool = False):
        prefer = ["return=representation" if returning else "return=minimal"]
        if resolution:
            prefer.append(f"resolution={resolution}")
        r = self._req("POST", table, json=rows,
                      params={"on_conflict": on_conflict} if on_conflict else None,
                      headers={"Prefer": ",".join(prefer)})
        return r.json() if returning else None

    def update(self, table: str, filters: dict, body: dict) -> list:
        """Met à jour et renvoie les lignes modifiées (visibles par l'appelant)."""
        return self._req("PATCH", table, params={"select": "*", **filters}, json=body,
                         headers={"Prefer": "return=representation"}).json()

    def delete(self, table: str, filters: dict) -> list:
        return self._req("DELETE", table, params={"select": "*", **filters},
                         headers={"Prefer": "return=representation"}).json()


def as_user(token: str) -> Client:
    return Client(config.SUPABASE_PUBLISHABLE_KEY, bearer=token)


def service() -> Client:
    key = config.SUPABASE_SERVICE_ROLE_KEY
    if not key:
        raise SupabaseError(500, "SUPABASE_SERVICE_ROLE_KEY non configurée côté serveur.")
    # Ancienne clé service_role = JWT (à passer aussi en Authorization) ;
    # nouvelle clé secrète « sb_secret_… » = en-tête apikey seul.
    return Client(key, bearer=key if key.startswith("eyJ") else None)
