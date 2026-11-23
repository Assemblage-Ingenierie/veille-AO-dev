"""Migration unique : base SQLite locale de Veille AO -> Supabase INTERNAL (tables veilleao_*).

Ce qui est migré
  * AO actifs, avec texte_complet ;
  * AO triés mais expirés, SANS texte_complet (on garde l'historique du tri, pas le texte) ;
  * tri : attribué au compte admin indiqué (l'auteur et la date d'origine ne sont pas connus) ;
  * réglages partagés (mots-clés) ; cache des détails (notice_details).
Ce qui n'est PAS migré
  * AO expirés jamais triés (jamais affichés par l'application) ;
  * assignations (aucune à ce jour ; les anciennes désignaient des prénoms, pas des comptes).

La migration est idempotente : la relancer n'écrase ni les AO déjà présents, ni un tri
déjà posé dans Supabase.

Usage
  python scripts/migrate_sqlite.py --dry-run     # n'envoie rien, affiche le plan
  python scripts/migrate_sqlite.py               # migration réelle

Identifiants (variables d'environnement, ou fichier .env à la racine, non versionné) :
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                          "VeilleAO", "ao_database.db")
SHARED_SETTINGS = ("params_keywords", "exclude_keywords")
NOTICE_COLS = ("id", "bailleur", "titre", "projet", "secteur", "type_avis", "mode_selection",
               "langue", "date_publication", "date_limite", "pays", "lien", "pertinence",
               "texte_complet")
MAX_BATCH_BYTES = 900_000   # reste sous la limite de taille d'une requête
TRIAGE_BATCH = 150          # identifiants par requête de mise à jour du tri


# ── Lecture de la base SQLite (sur une copie, jamais l'original) ─────────────

def read_sqlite(path: str) -> dict:
    tmp = os.path.join(tempfile.mkdtemp(prefix="veilleao_migr_"), "copy.db")
    shutil.copy2(path, tmp)
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    if con.execute("pragma quick_check").fetchone()[0] != "ok":
        sys.exit(f"Base SQLite corrompue : {path}")

    notices = []
    for r in con.execute(
            "select * from notices where actif = 1 or triage is not null order by id"):
        row = {c: (r[c] if r[c] is not None else "") for c in NOTICE_COLS}
        if not r["actif"]:
            row["texte_complet"] = ""           # AO expiré : seul le tri nous intéresse
        row["_triage"] = r["triage"]
        notices.append(row)

    settings = {}
    for r in con.execute("select key, value from settings"):
        if r["key"] in SHARED_SETTINGS:
            try:
                settings[r["key"]] = json.loads(r["value"]) if r["value"] else []
            except ValueError:
                print(f"  ! réglage {r['key']} illisible, ignoré")

    ids = {n["id"] for n in notices}
    details = []
    for r in con.execute("select notice_id, detail_json, fetched_at from notice_details"):
        if r["notice_id"] not in ids:
            continue
        try:
            details.append({"notice_id": r["notice_id"],
                            "detail": json.loads(r["detail_json"]),
                            "fetched_at": r["fetched_at"]})
        except ValueError:
            print(f"  ! détail {r['notice_id']} illisible, ignoré")

    n_assign = con.execute("select count(*) from assignments").fetchone()[0]
    con.close()
    return {"notices": notices, "settings": settings, "details": details, "n_assign": n_assign}


# ── Accès Supabase (PostgREST, clé serveur) ──────────────────────────────────

def load_env() -> None:
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Supabase:
    def __init__(self, url: str, key: str):
        import requests
        self.s = requests.Session()
        self.base = url.rstrip("/") + "/rest/v1"
        self.s.headers.update({"apikey": key, "Content-Type": "application/json"})
        if key.startswith("eyJ"):            # ancienne clé service_role (JWT)
            self.s.headers["Authorization"] = f"Bearer {key}"

    def _check(self, r):
        if r.status_code >= 400:
            sys.exit(f"Erreur Supabase {r.status_code} sur {r.request.method} "
                     f"{r.url.split('?')[0]} : {r.text[:400]}")
        return r

    def rpc(self, fn: str, payload: dict):
        return self._check(self.s.post(f"{self.base}/rpc/{fn}", json=payload, timeout=120)).json()

    def get(self, table: str, params: dict):
        return self._check(self.s.get(f"{self.base}/{table}", params=params, timeout=60)).json()

    def upsert(self, table: str, rows: list, on_conflict: str, ignore: bool):
        pref = "resolution=ignore-duplicates" if ignore else "resolution=merge-duplicates"
        self._check(self.s.post(f"{self.base}/{table}", params={"on_conflict": on_conflict},
                                json=rows, headers={"Prefer": pref}, timeout=120))

    def patch(self, table: str, params: dict, body: dict) -> int:
        r = self._check(self.s.patch(f"{self.base}/{table}", params=params, json=body,
                                     headers={"Prefer": "return=minimal,count=exact"},
                                     timeout=60))
        rng = r.headers.get("Content-Range", "*/0")
        return int(rng.split("/")[-1]) if rng.split("/")[-1].isdigit() else 0

    def count(self, table: str, params: dict | None = None) -> int:
        r = self._check(self.s.get(f"{self.base}/{table}", params={"select": "id", **(params or {})},
                                   headers={"Prefer": "count=exact", "Range": "0-0"}, timeout=60))
        return int(r.headers.get("Content-Range", "*/0").split("/")[-1])


def batches_by_size(rows: list, max_bytes: int):
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


def in_filter(ids: list) -> str:
    return "in.(" + ",".join('"' + i.replace('"', '\\"') + '"' for i in ids) + ")"


# ── Programme principal ──────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="base SQLite source (défaut : base locale)")
    ap.add_argument("--admin-email", default="louis@assemblage.net",
                    help="compte auquel attribuer les tris existants")
    ap.add_argument("--dry-run", action="store_true", help="n'envoie rien, affiche le plan")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"Base introuvable : {args.db}")
    data = read_sqlite(args.db)
    notices = data["notices"]
    by_state = {}
    for n in notices:
        if n["_triage"]:
            by_state.setdefault(n["_triage"], []).append(n["id"])
    rows = [{k: v for k, v in n.items() if not k.startswith("_")} for n in notices]
    nb_batches = sum(1 for _ in batches_by_size(rows, MAX_BATCH_BYTES))

    print(f"Source : {args.db}")
    print(f"  AO à migrer          : {len(rows)} (en {nb_batches} lots)")
    print(f"  tris à reporter      : " + ", ".join(f"{k} {len(v)}" for k, v in sorted(by_state.items())))
    print(f"  réglages partagés    : {data['settings']}")
    print(f"  détails en cache     : {len(data['details'])}")
    if data["n_assign"]:
        print(f"  ! {data['n_assign']} assignation(s) non migrée(s) (noms libres, pas des comptes)")
    if args.dry_run:
        print("\n--dry-run : rien n'a été envoyé.")
        return

    load_env()
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key or key == "a_renseigner":
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY manquants (voir .env.example).")
    sb = Supabase(url, key)

    admin = sb.get("veilleao_profiles", {"select": "id,email", "email": f"eq.{args.admin_email}",
                                         "role": "eq.admin", "status": "eq.approved"})
    if not admin:
        sys.exit(f"Aucun admin validé {args.admin_email} dans veilleao_profiles.")
    admin_id = admin[0]["id"]

    print("\n1/5 AO…")
    inserted = 0
    for i, batch in enumerate(batches_by_size(rows, MAX_BATCH_BYTES), 1):
        inserted += sb.rpc("veilleao_upsert_notices", {"p_rows": batch, "p_refresh": False})
        print(f"    lot {i}/{nb_batches} : {inserted} insérés au total", flush=True)

    print("2/5 AO expirés…")
    print(f"    {sb.rpc('veilleao_mark_expired', {})} désactivés")

    print("3/5 Tris…")
    reported = 0
    for state, ids in sorted(by_state.items()):
        for j in range(0, len(ids), TRIAGE_BATCH):
            reported += sb.patch("veilleao_notices",
                                 {"id": in_filter(ids[j:j + TRIAGE_BATCH]), "triage": "is.null"},
                                 {"triage": state, "triaged_by": admin_id})
    print(f"    {reported} tris reportés (déjà présents dans Supabase : ignorés)")

    print("4/5 Réglages partagés…")
    if data["settings"]:
        sb.upsert("veilleao_settings",
                  [{"key": k, "value": v, "updated_by": admin_id} for k, v in data["settings"].items()],
                  on_conflict="key", ignore=True)
    print(f"    {len(data['settings'])} réglage(s)")

    print("5/5 Cache des détails…")
    for batch in batches_by_size(data["details"], MAX_BATCH_BYTES):
        sb.upsert("veilleao_notice_details", batch, on_conflict="notice_id", ignore=True)
    print(f"    {len(data['details'])} détail(s)")

    print("\nVérification dans Supabase :")
    print(f"    AO          : {sb.count('veilleao_notices')} "
          f"(actifs {sb.count('veilleao_notices', {'actif': 'is.true'})})")
    print(f"    AO triés    : {sb.count('veilleao_notices', {'triage': 'not.is.null'})}")
    print(f"    détails     : {sb.count('veilleao_notice_details', {'select': 'notice_id'})}")


if __name__ == "__main__":
    main()
