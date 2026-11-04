"""GIZ — scraping ausschreibungen.giz.de (Satellite/Cosinex CMS, pagination complète)."""
import logging
import re
import time
from datetime import date
from dateutil import parser as dateparser
from filters import classify

log = logging.getLogger(__name__)

GIZ_BASE = "https://ausschreibungen.giz.de"
GIZ_LIST = (
    f"{GIZ_BASE}/Satellite/company/welcome.do"
    "?method=showTable&fromSearch=1"
    "&tableSortPROJECT_RESULT=1"
    "&tableSortAttributePROJECT_RESULT=publicationDate"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8,fr;q=0.7",
    "Referer": GIZ_BASE + "/",
}

# Known German → column index mapping (0-based, from observed page structure)
# Columns: Veröffentlicht | Angebots-/Teilnahmefrist | Bezeichnung | Typ | Ausschreibende Stelle | Aktion
COL_PUB     = 0   # Veröffentlicht
COL_DEADLINE = 1  # Angebots- / Teilnahmefrist
COL_TITLE    = 2  # Bezeichnung (with link)
COL_TYPE     = 3  # Typ
COL_ORG      = 4  # Ausschreibende Stelle


def _parse_date_de(s: str) -> str:
    """Parse German date formats: DD.MM.YYYY."""
    s = s.strip()
    if not s:
        return ""
    try:
        return dateparser.parse(s, dayfirst=True).date().isoformat()
    except Exception:
        return ""


def _total_pages(soup) -> int:
    """Extract total page count from 'Seite: X von Y' text."""
    for text in soup.stripped_strings:
        m = re.search(r'von\s+(\d+)', text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 1


def _parse_page(soup, today: date) -> list:
    """Extract notices from a single page's BeautifulSoup object."""
    notices = []

    # Find the results table: look for table containing German procurement headers
    target_table = None
    for table in soup.find_all("table"):
        headers_text = " ".join(
            th.get_text(strip=True).lower()
            for th in table.find_all(["th", "td"])[:8]
        )
        if "bezeichnung" in headers_text or "frist" in headers_text or "veröffentlicht" in headers_text:
            target_table = table
            break

    if not target_table:
        # Fallback: any table with more than 3 rows and links
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) > 3 and table.find("a"):
                target_table = table
                break

    if not target_table:
        return []

    rows = target_table.find_all("tr")
    if len(rows) < 2:
        return []

    # Detect header row to confirm column order
    header_row = rows[0]
    header_cells = header_row.find_all(["th", "td"])
    header_texts = [c.get_text(strip=True).lower() for c in header_cells]

    # Dynamically assign column indices
    col_pub      = next((i for i, h in enumerate(header_texts) if "veröffentlicht" in h or "publication" in h), COL_PUB)
    col_deadline = next((i for i, h in enumerate(header_texts) if "frist" in h or "deadline" in h or "abgabe" in h), COL_DEADLINE)
    col_title    = next((i for i, h in enumerate(header_texts) if "bezeichnung" in h or "title" in h or "project" in h or "name" in h), COL_TITLE)
    col_type     = next((i for i, h in enumerate(header_texts) if "typ" == h or h == "type"), COL_TYPE)

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        # ── Title & Link ───────────────────────────────────────────────────
        titre, link = "", ""
        if col_title < len(cells):
            a = cells[col_title].find("a")
            if a:
                titre = a.get_text(strip=True)
                href = a.get("href", "")
                link = href if href.startswith("http") else GIZ_BASE + href
            else:
                titre = cells[col_title].get_text(strip=True)
        # Also search for a link anywhere in the row if not found
        if not link:
            for cell in cells:
                a = cell.find("a", href=re.compile(r'projectForwarding|pid=|project'))
                if a:
                    titre = titre or a.get_text(strip=True)
                    href = a.get("href", "")
                    link = href if href.startswith("http") else GIZ_BASE + href
                    break

        if not titre:
            continue

        # ── Dates ──────────────────────────────────────────────────────────
        date_pub_str  = cells[col_pub].get_text(separator=" ", strip=True)      if col_pub < len(cells)      else ""
        date_lim_str  = cells[col_deadline].get_text(separator=" ", strip=True) if col_deadline < len(cells) else ""
        notice_type   = cells[col_type].get_text(separator="/", strip=True)     if col_type < len(cells)     else ""

        # Skip awarded contracts — they are past procurement, not active notices
        if "vergebener" in notice_type.lower() or "awarded" in notice_type.lower():
            continue

        date_pub = _parse_date_de(date_pub_str)
        date_lim = _parse_date_de(date_lim_str)

        # Skip expired
        if date_lim:
            try:
                if dateparser.parse(date_lim_str, dayfirst=True).date() < today:
                    continue
            except Exception:
                pass

        # ── Classification ─────────────────────────────────────────────────
        result = classify(titre)
        secteur, pertinence = result

        import hashlib
        uid = hashlib.md5(f"{titre}{date_lim}".encode()).hexdigest()[:12]

        notices.append({
            "id": f"GIZ-{uid}",
            "bailleur": "GIZ",
            "titre": titre,
            "projet": "",
            "secteur": secteur,
            "type_avis": notice_type or "Tender",
            "mode_selection": "",
            "langue": "Anglais",  # GIZ posts in EN; will detect below
            "date_publication": date_pub,
            "date_limite": date_lim,
            "pays": "",
            "lien": link,
            "pertinence": pertinence,
            "texte_complet": titre,
        })

    return notices


def fetch_giz() -> list:
    import requests
    from bs4 import BeautifulSoup

    session = requests.Session()
    session.headers.update(_HEADERS)

    # ── Fetch page 1 ──────────────────────────────────────────────────────
    try:
        r = session.get(GIZ_LIST, timeout=30)
        r.raise_for_status()
        if len(r.content) < 500:
            raise ValueError("Réponse trop courte — possible bot-detection")
    except Exception as e:
        log.warning(f"GIZ inaccessible : {e}")
        raise

    try:
        soup = BeautifulSoup(r.content, "lxml")
    except Exception:
        soup = BeautifulSoup(r.content, "html.parser")

    today = date.today()
    total_pages = _total_pages(soup)
    log.info(f"GIZ : {total_pages} page(s) détectée(s)")

    all_notices = _parse_page(soup, today)

    # ── Paginate ──────────────────────────────────────────────────────────
    for page_num in range(2, total_pages + 1):
        try:
            url = GIZ_LIST + f"&selectedTablePagePROJECT_RESULT={page_num}"
            time.sleep(0.8)  # polite crawling
            r2 = session.get(url, timeout=30)
            r2.raise_for_status()
            try:
                s2 = BeautifulSoup(r2.content, "lxml")
            except Exception:
                s2 = BeautifulSoup(r2.content, "html.parser")
            page_notices = _parse_page(s2, today)
            all_notices.extend(page_notices)
            log.debug(f"GIZ page {page_num}/{total_pages} : {len(page_notices)} notices")
        except Exception as e:
            log.warning(f"GIZ page {page_num} échouée : {e} — continué")
            break

    log.info(f"GIZ : {len(all_notices)} AO retenus sur {total_pages} pages")
    return all_notices
