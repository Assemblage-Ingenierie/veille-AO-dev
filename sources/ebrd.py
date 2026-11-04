"""BERD/ECEPP — scraping HTML du tableau server-rendered noticeResultsTable."""
import logging
import re
from datetime import date
from dateutil import parser as dateparser
from filters import classify

log = logging.getLogger(__name__)

ECEPP_BASE   = "https://ecepp.ebrd.com/delta"
ECEPP_LIST   = f"{ECEPP_BASE}/noticeSearchResults.html"
ECEPP_NOTICE = f"{ECEPP_BASE}/viewNotice.html"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Statuts à exclure (AO clos)
_SKIP_STATUSES = {"closed", "fermé", "cancelled", "annulé"}

# Types à exclure (attributions passées, addendums administratifs)
_SKIP_TYPES_RE = re.compile(
    r'contract\s*award|award\s*notice|addendum|corrigendum',
    re.IGNORECASE
)

# Nettoyage date : "20/05/2026 08:49Heure britannique" ou "20/05/2026 08:49UK Time"
_TIME_SUFFIX_RE = re.compile(
    r'\s*(?:UK\s*Time|Heure\s+britannique)\s*$',
    re.IGNORECASE
)
_NOTICE_ID_RE = re.compile(r'displayNoticeId=(\d+)', re.IGNORECASE)

# Colonnes HTML du tableau (10 total, dont 4 DataTables hidden)
# [0]=Titre+link  [1]=Type d'Avis  [2]=Projet  [3]=Publié  [4]=Date de Clôture
# [5]=État Actuel  [6]=date_pub_DD/MM/YYYY  [7]=pub_YYYYMMDDhhmm  [8]=dl_YYYYMMDDhhmm  [9]=desc_JSON
COL_TITRE    = 0
COL_TYPE     = 1
COL_PROJET   = 2
COL_PUB      = 3
COL_DEADLINE = 4
COL_STATUT   = 5
COL_TS_PUB   = 7   # compact timestamp "202605200849"
COL_TS_DL    = 8   # compact timestamp "202606301500" (empty if none)
COL_DESC     = 9   # "[project, id, country, sector, owner, bizgroup, type]"


def _parse_date(s: str) -> str:
    """Parse 'DD/MM/YYYY hh:mmHeure britannique' → ISO date, or empty."""
    if not s or s.strip().upper() == "N/A":
        return ""
    s = _TIME_SUFFIX_RE.sub("", s).strip()
    try:
        return dateparser.parse(s, dayfirst=True).date().isoformat()
    except Exception:
        return ""


def _parse_compact_ts(s: str) -> str:
    """Parse DataTables hidden compact timestamp '202606301500' → '2026-06-30'."""
    s = s.strip()
    if len(s) >= 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return ""


def _extract_country(desc: str) -> str:
    """Extract country from DataTables description cell '[proj, id, COUNTRY, ...]'."""
    # Format: "[proj_name, project_id, country, ...]"
    inner = desc.strip().lstrip("[").rstrip("]")
    parts = [p.strip() for p in inner.split(",")]
    # Field 2 (0-indexed) is the country when the project name has no commas,
    # but project names can contain commas — use a heuristic: look for a part
    # that matches a country-like token (no digits, reasonable length)
    if len(parts) >= 3:
        # parts[1] is usually numeric project ID; country follows it
        for i, p in enumerate(parts[1:], 1):
            if p.strip().isdigit():
                # next part should be country
                if i + 1 < len(parts):
                    return parts[i + 1].strip().title()
    return ""


def fetch_ebrd() -> list:
    import requests
    from bs4 import BeautifulSoup

    try:
        r = requests.get(ECEPP_LIST, headers=_HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        log.error(f"BERD inaccessible : {e}")
        raise

    try:
        soup = BeautifulSoup(r.content, "lxml")
    except Exception:
        soup = BeautifulSoup(r.content, "html.parser")

    table = soup.find("table", id="noticeResultsTable")
    if not table:
        log.warning("BERD : table#noticeResultsTable introuvable")
        raise RuntimeError("Table BERD introuvable")

    rows = table.find_all("tr")
    log.info(f"BERD : {len(rows) - 1} notices dans le tableau HTML")

    today   = date.today()
    notices = []

    for row in rows[1:]:           # skip header
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        # ── Titre & lien ──────────────────────────────────────────────────
        a     = cells[COL_TITRE].find("a")
        titre = (a.get_text(strip=True) if a else cells[COL_TITRE].get_text(strip=True))
        href  = a.get("href", "") if a else ""
        if href and not href.startswith("http"):
            href = f"{ECEPP_BASE}/{href.lstrip('/')}"

        if not titre:
            continue

        # ── Type d'Avis ───────────────────────────────────────────────────
        type_avis = cells[COL_TYPE].get_text(strip=True)

        # Exclure attributions et addendums
        if type_avis and _SKIP_TYPES_RE.search(type_avis):
            continue

        # ── Statut ────────────────────────────────────────────────────────
        statut = cells[COL_STATUT].get_text(strip=True)
        if statut.lower() in _SKIP_STATUSES:
            continue

        # ── ID ────────────────────────────────────────────────────────────
        id_match = _NOTICE_ID_RE.search(href)
        notice_id = id_match.group(1) if id_match else None
        if not notice_id:
            import hashlib
            notice_id = hashlib.md5(titre.encode()).hexdigest()[:12]

        # ── Projet ────────────────────────────────────────────────────────
        projet = cells[COL_PROJET].get_text(strip=True)
        if projet.upper() == "N/A":
            projet = ""

        # ── Dates — prefer compact timestamp (hidden DataTables column) ──
        ts_pub = cells[COL_TS_PUB].get_text(strip=True) if len(cells) > COL_TS_PUB else ""
        ts_dl  = cells[COL_TS_DL].get_text(strip=True)  if len(cells) > COL_TS_DL  else ""

        date_pub = _parse_compact_ts(ts_pub)
        date_lim = _parse_compact_ts(ts_dl)

        # Fallback to visible text columns if compact timestamps unavailable
        if not date_pub:
            date_pub = _parse_date(cells[COL_PUB].get_text(strip=True))
        if not date_lim:
            date_lim = _parse_date(cells[COL_DEADLINE].get_text(strip=True))

        # Exclure par date échue
        if date_lim:
            try:
                if dateparser.parse(date_lim).date() < today:
                    continue
            except Exception:
                pass

        # ── Pays depuis colonne description DataTables ────────────────────
        desc = cells[COL_DESC].get_text(strip=True) if len(cells) > COL_DESC else ""
        pays = _extract_country(desc)

        # ── Classification ────────────────────────────────────────────────
        secteur, pertinence = classify(titre, projet, desc)

        notices.append({
            "id":               f"BERD-{notice_id}",
            "bailleur":         "BERD",
            "titre":            titre,
            "projet":           projet,
            "secteur":          secteur,
            "type_avis":        type_avis,
            "mode_selection":   "",
            "langue":           "Anglais",
            "date_publication": date_pub,
            "date_limite":      date_lim,
            "pays":             pays,
            "lien":             href,
            "pertinence":       pertinence,
            "texte_complet":    f"{titre} {projet} {desc}"[:3000],
        })

    log.info(f"BERD : {len(notices)} AO retenus (Open + Information Only, hors attributions)")
    return notices
