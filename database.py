import json
import sqlite3
import logging
from datetime import date
from config import DB_PATH

log = logging.getLogger(__name__)

_FTS_AVAILABLE = True


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=MEMORY")
    return conn


def _db_is_healthy() -> bool:
    """Vrai contrôle d'intégrité : lit réellement les pages de la base.
    Un SELECT 1 ne suffit PAS (il ne touche aucune page) — il faut quick_check
    + une lecture du schéma pour détecter une base 'malformed'."""
    import os
    if not os.path.exists(DB_PATH):
        return True  # base absente : elle sera créée à neuf, donc OK
    test = None
    try:
        test = sqlite3.connect(DB_PATH, timeout=5)
        row = test.execute("PRAGMA quick_check").fetchone()
        test.execute("PRAGMA journal_mode=MEMORY")          # le point qui plantait
        test.execute("SELECT name FROM sqlite_master LIMIT 1")
        return bool(row) and row[0] == "ok"
    except Exception as e:
        log.warning(f"Contrôle d'intégrité : base illisible ({e})")
        return False
    finally:
        if test is not None:
            try:
                test.close()
            except Exception:
                pass


def init_db():
    global _FTS_AVAILABLE
    # Si la base est corrompue (ex. synchro Google Drive), la supprimer et repartir à zéro
    if not _db_is_healthy():
        import os
        for s in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(DB_PATH + s)
            except (FileNotFoundError, PermissionError, OSError):
                pass
        log.warning("Base de données corrompue — réinitialisée à neuf")
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS assignments (
            notice_id   TEXT NOT NULL,
            assignee    TEXT NOT NULL,
            assigned_by TEXT,
            assigned_at TEXT DEFAULT (datetime('now')),
            note        TEXT,
            seen        INTEGER DEFAULT 0,
            PRIMARY KEY (notice_id, assignee)
        );
        CREATE TABLE IF NOT EXISTS notices (
            id               TEXT PRIMARY KEY,
            bailleur         TEXT NOT NULL,
            titre            TEXT NOT NULL,
            projet           TEXT,
            secteur          TEXT,
            type_avis        TEXT,
            mode_selection   TEXT,
            langue           TEXT,
            date_publication TEXT,
            date_limite      TEXT,
            pays             TEXT,
            lien             TEXT,
            pertinence       TEXT,
            texte_complet    TEXT,
            date_ajout       TEXT DEFAULT (datetime('now')),
            actif            INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_bailleur    ON notices(bailleur);
        CREATE INDEX IF NOT EXISTS idx_langue      ON notices(langue);
        CREATE INDEX IF NOT EXISTS idx_date_limite ON notices(date_limite);
        CREATE INDEX IF NOT EXISTS idx_actif       ON notices(actif);
        CREATE TABLE IF NOT EXISTS notice_details (
            notice_id   TEXT PRIMARY KEY,
            detail_json TEXT NOT NULL,
            fetched_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()

    # Migration : ajout de la colonne triage si elle n'existe pas encore
    try:
        cur.execute("ALTER TABLE notices ADD COLUMN triage TEXT")
        conn.commit()
        log.info("Colonne 'triage' ajoutée à la table notices")
    except Exception:
        pass  # Colonne déjà présente

    try:
        cur.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS notices_fts USING fts5(
                id, titre, projet, texte_complet,
                content=notices, content_rowid=rowid
            )
        """)
        conn.commit()
    except Exception as e:
        log.warning(f"FTS5 non disponible : {e} — recherche full-text désactivée")
        _FTS_AVAILABLE = False

    conn.close()
    log.info("Base de données initialisée : %s", DB_PATH)


def save_notices(notices: list) -> int:
    if not notices:
        return 0
    conn = get_connection()
    cur = conn.cursor()
    inserted = 0
    for n in notices:
        try:
            cur.execute(
                """
                INSERT OR IGNORE INTO notices
                (id, bailleur, titre, projet, secteur, type_avis, mode_selection,
                 langue, date_publication, date_limite, pays, lien, pertinence,
                 texte_complet, actif)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    n.get("id"), n.get("bailleur"), n.get("titre"), n.get("projet"),
                    n.get("secteur"), n.get("type_avis"), n.get("mode_selection"),
                    n.get("langue"), n.get("date_publication"), n.get("date_limite"),
                    n.get("pays"), n.get("lien"), n.get("pertinence"),
                    n.get("texte_complet"),
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
        except Exception as e:
            log.warning(f"Erreur insertion {n.get('id')} : {e}")

    conn.commit()

    if _FTS_AVAILABLE and inserted:
        try:
            cur.execute("INSERT INTO notices_fts(notices_fts) VALUES('rebuild')")
            conn.commit()
        except Exception as e:
            log.debug(f"FTS rebuild : {e}")

    conn.close()
    return inserted


def upsert_notices(notices: list) -> int:
    """Insère ou met à jour des avis en base, clé de conflit = `id` (PK).

    Contrairement à `save_notices` (INSERT OR IGNORE), un avis déjà présent
    voit sa deadline et sa date de collecte rafraîchies, et est réactivé
    (`actif=1`) — `mark_expired()` le repassera à 0 si la deadline est dépassée.
    Utilisé par les sources qui republient des avis évolutifs (ex. TED).
    """
    if not notices:
        return 0
    conn = get_connection()
    cur = conn.cursor()
    affected = 0
    for n in notices:
        try:
            cur.execute(
                """
                INSERT INTO notices
                (id, bailleur, titre, projet, secteur, type_avis, mode_selection,
                 langue, date_publication, date_limite, pays, lien, pertinence,
                 texte_complet, actif)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET
                    date_limite = excluded.date_limite,
                    date_ajout  = datetime('now'),
                    actif       = 1
                """,
                (
                    n.get("id"), n.get("bailleur"), n.get("titre"), n.get("projet"),
                    n.get("secteur"), n.get("type_avis"), n.get("mode_selection"),
                    n.get("langue"), n.get("date_publication"), n.get("date_limite"),
                    n.get("pays"), n.get("lien"), n.get("pertinence"),
                    n.get("texte_complet"),
                ),
            )
            affected += 1
        except Exception as e:
            log.warning(f"Erreur upsert {n.get('id')} : {e}")

    conn.commit()

    if _FTS_AVAILABLE and affected:
        try:
            cur.execute("INSERT INTO notices_fts(notices_fts) VALUES('rebuild')")
            conn.commit()
        except Exception as e:
            log.debug(f"FTS rebuild : {e}")

    conn.close()
    return affected


def mark_expired():
    today = date.today().isoformat()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE notices SET actif=0 WHERE date_limite < ? AND date_limite IS NOT NULL AND date_limite != ''",
        (today,),
    )
    count = cur.rowcount
    conn.commit()
    conn.close()
    if count:
        log.info(f"{count} AO marqués expirés")


def get_notices(filters: dict = None) -> list:
    conn = get_connection()
    cur = conn.cursor()
    query = "SELECT * FROM notices WHERE actif = 1"
    params = []

    if filters:
        if filters.get("bailleur"):
            ph = ",".join("?" * len(filters["bailleur"]))
            query += f" AND bailleur IN ({ph})"
            params.extend(filters["bailleur"])
        if filters.get("langue"):
            ph = ",".join("?" * len(filters["langue"]))
            query += f" AND langue IN ({ph})"
            params.extend(filters["langue"])
        if filters.get("pertinence"):
            ph = ",".join("?" * len(filters["pertinence"]))
            query += f" AND pertinence IN ({ph})"
            params.extend(filters["pertinence"])
        if filters.get("deadline_from"):
            query += " AND date_limite >= ?"
            params.append(filters["deadline_from"])
        if filters.get("deadline_to"):
            query += " AND date_limite <= ?"
            params.append(filters["deadline_to"])

    query += " ORDER BY date_limite ASC NULLS LAST"
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_stats() -> dict:
    conn = get_connection()
    cur = conn.cursor()
    stats: dict = {"total": 0, "by_bailleur": {}, "by_langue": {}, "by_secteur": {}, "by_pertinence": {}}

    cur.execute("SELECT COUNT(*) FROM notices WHERE actif=1")
    stats["total"] = cur.fetchone()[0]

    for row in cur.execute("SELECT bailleur, COUNT(*) FROM notices WHERE actif=1 GROUP BY bailleur"):
        stats["by_bailleur"][row[0]] = row[1]
    for row in cur.execute("SELECT langue, COUNT(*) FROM notices WHERE actif=1 GROUP BY langue"):
        stats["by_langue"][row[0] or "Inconnu"] = row[1]
    for row in cur.execute("SELECT secteur, COUNT(*) FROM notices WHERE actif=1 GROUP BY secteur"):
        stats["by_secteur"][row[0] or "Autre"] = row[1]
    for row in cur.execute("SELECT pertinence, COUNT(*) FROM notices WHERE actif=1 GROUP BY pertinence"):
        stats["by_pertinence"][row[0] or "Inconnu"] = row[1]

    conn.close()
    return stats


def get_notice_by_id(notice_id: str) -> dict | None:
    """Retourne une notice complète (avec texte_complet) par son ID."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM notices WHERE id = ?", (notice_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_setting(key: str) -> "str | None":
    """Retourne la valeur d'un paramètre partagé."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_setting(key: str, value: "str | None"):
    """Crée ou met à jour un paramètre partagé (None = suppression)."""
    conn = get_connection()
    cur = conn.cursor()
    if value is None:
        cur.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def assign_notice(notice_id: str, assignee: str, assigned_by: str = None, note: str = None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO assignments (notice_id, assignee, assigned_by, note, seen) VALUES (?,?,?,?,0)",
        (notice_id, assignee, assigned_by, note),
    )
    conn.commit()
    conn.close()


def remove_assignment(notice_id: str, assignee: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM assignments WHERE notice_id=? AND assignee=?", (notice_id, assignee))
    conn.commit()
    conn.close()


def get_all_assignments() -> dict:
    """Retourne {notice_id: [{assignee, assigned_by, assigned_at, seen}]}"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT notice_id, assignee, assigned_by, assigned_at, seen FROM assignments ORDER BY assigned_at DESC")
    result: dict = {}
    for row in cur.fetchall():
        nid = row[0]
        if nid not in result:
            result[nid] = []
        result[nid].append({"assignee": row[1], "assigned_by": row[2], "assigned_at": row[3], "seen": bool(row[4])})
    conn.close()
    return result


def get_assignments_for(assignee: str) -> list:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT notice_id, assigned_by, assigned_at, seen FROM assignments WHERE assignee=? ORDER BY assigned_at DESC",
        (assignee,),
    )
    rows = [{"notice_id": r[0], "assigned_by": r[1], "assigned_at": r[2], "seen": bool(r[3])} for r in cur.fetchall()]
    conn.close()
    return rows


def mark_assignments_seen(assignee: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE assignments SET seen=1 WHERE assignee=?", (assignee,))
    conn.commit()
    conn.close()


def get_all_triage() -> dict:
    """Retourne {notice_id: state} pour toutes les notices ayant un triage défini."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, triage FROM notices WHERE triage IS NOT NULL")
    result = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()
    return result


def set_triage(notice_id: str, state: "str | None"):
    """Met à jour l'état de triage d'une notice (None = non trié)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE notices SET triage = ? WHERE id = ?", (state, notice_id))
    conn.commit()
    conn.close()


def get_last_refresh_info() -> dict:
    conn = get_connection()
    cur = conn.cursor()
    result = {}
    for row in cur.execute(
        "SELECT bailleur, MAX(date_ajout) FROM notices GROUP BY bailleur"
    ):
        result[row[0]] = row[1]
    conn.close()
    return result


# ── Cache des détails AO ──────────────────────────────────────────────────────

def get_notice_detail_cache(notice_id: str) -> "dict | None":
    """Retourne le détail mis en cache pour une notice, ou None si absent / table inexistante."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT detail_json, fetched_at FROM notice_details WHERE notice_id = ?",
            (notice_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["_fetched_at"] = row[1]
        return data
    except Exception:
        return None
    finally:
        conn.close()


def set_notice_detail_cache(notice_id: str, detail: dict) -> None:
    """Enregistre (ou remplace) le détail d'une notice dans le cache SQLite."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO notice_details (notice_id, detail_json) VALUES (?, ?)",
            (notice_id, json.dumps(detail, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def clear_notice_detail_cache(notice_id: str) -> None:
    """Supprime le cache détail d'une notice (forcer une relecture à la prochaine ouverture)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM notice_details WHERE notice_id = ?", (notice_id,))
        conn.commit()
    finally:
        conn.close()
