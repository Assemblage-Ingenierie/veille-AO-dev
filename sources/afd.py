"""AFD — scraping afd.dgmarket.com (DisplayTag HTML, pagination complète)."""
import logging
import re
import time
import urllib.request
from datetime import date
from dateutil import parser as dateparser
from filters import classify
from sources.dns_utils import get_with_doh_fallback

log = logging.getLogger(__name__)

AFD_BASE = "https://afd.dgmarket.com"
AFD_LIST = f"{AFD_BASE}/tenders/brandedNoticeList.do"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Correspondance mois français/anglais abrégés → English pour dateutil
_FR_MONTH = {
    "janv": "jan", "jan":  "jan",
    "fév":  "feb", "fevr": "feb", "févr": "feb",
    "mars": "mar", "mar":  "mar",
    "avr":  "apr", "avril":"apr",
    "mai":  "may",
    "juin": "jun", "jun":  "jun",
    "juil": "jul", "jul":  "jul",
    "août": "aug", "aout": "aug", "aoû":  "aug", "aug": "aug",
    "sep":  "sep", "sept": "sep",
    "oct":  "oct",
    "nov":  "nov",
    "déc":  "dec", "dec":  "dec",
}

_NOTICE_ID_RE = re.compile(r'/tender/(\d+)', re.IGNORECASE)
# Détecte "1-20 de 144" ou "Résultats 1-20 de 144"
_TOTAL_RE = re.compile(r'\d+\s*[-–]\s*\d+\s+de\s+(\d+)', re.IGNORECASE)


def _parse_date(s: str) -> str:
    """Parse 'Mai 21, 2026' ou 'Jun 15, 2026' → ISO date."""
    s = s.strip()
    if not s:
        return ""
    parts = s.split()
    if parts:
        key = parts[0].lower().rstrip(".")
        if key in _FR_MONTH:
            parts[0] = _FR_MONTH[key].capitalize()
    try:
        return dateparser.parse(" ".join(parts)).date().isoformat()
    except Exception:
        return ""


def _total_pages(soup) -> int:
    for text in soup.stripped_strings:
        m = _TOTAL_RE.search(text)
        if m:
            total_items = int(m.group(1))
            return max(1, (total_items + 19) // 20)
    return 1


def _parse_page(soup, today: date) -> list:
    table = soup.find("table", id="notice")
    if not table:
        return []

    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

    notices = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        pays  = cells[0].get_text(strip=True)
        a     = cells[1].find("a")
        if not a:
            continue
        titre = a.get_text(strip=True)
        href  = a.get("href", "")
        if href and not href.startswith("http"):
            href = AFD_BASE + href

        id_match  = _NOTICE_ID_RE.search(href)
        notice_id = id_match.group(1) if id_match else None
        if not notice_id:
            import hashlib
            notice_id = hashlib.md5(titre.encode()).hexdigest()[:12]

        date_pub = _parse_date(cells[2].get_text(strip=True))
        date_lim = _parse_date(cells[3].get_text(strip=True))

        # Exclure les AO expirés
        if date_lim:
            try:
                if dateparser.parse(date_lim).date() < today:
                    continue
            except Exception:
                pass

        secteur, pertinence = classify(titre, "", pays)

        notices.append({
            "id":               f"AFD-{notice_id}",
            "bailleur":         "AFD",
            "titre":            titre,
            "projet":           "",
            "secteur":          secteur,
            "type_avis":        "",
            "mode_selection":   "",
            "langue":           "Français",
            "date_publication": date_pub,
            "date_limite":      date_lim,
            "pays":             pays,
            "lien":             href,
            "pertinence":       pertinence,
            "texte_complet":    f"{titre} {pays}",
        })

    return notices


def _make_session():
    """Crée une session requests avec proxy système (WinINET/env vars)."""
    import requests
    import urllib3

    session = requests.Session()
    session.headers.update(_HEADERS)
    # Le certificat de afd.dgmarket.com n'est pas vérifié par le bundle certifi
    # (chaîne intermédiaire absente) alors que Chrome l'accepte via le magasin Windows.
    session.verify = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Récupérer les proxies système (registre Windows / variables d'environnement)
    sys_proxies = urllib.request.getproxies()
    if sys_proxies:
        session.proxies.update(sys_proxies)
        log.debug(f"AFD : proxy système détecté : {list(sys_proxies.keys())}")

    return session


def fetch_afd() -> list:
    from bs4 import BeautifulSoup

    session = _make_session()

    try:
        r = get_with_doh_fallback(session, AFD_LIST, timeout=40, allow_redirects=True)
        r.raise_for_status()
        if len(r.content) < 1000:
            raise ValueError("Réponse trop courte — possible blocage")
    except Exception as e:
        log.error(f"AFD inaccessible : {e}")
        raise

    try:
        soup = BeautifulSoup(r.content, "lxml")
    except Exception:
        soup = BeautifulSoup(r.content, "html.parser")

    today       = date.today()
    total_pages = _total_pages(soup)
    log.info(f"AFD : {total_pages} page(s) détectée(s)")

    all_notices = _parse_page(soup, today)

    for page_num in range(2, total_pages + 1):
        try:
            time.sleep(0.8)
            params = {
                "d-446978-p":   page_num,
                "selPageNumber": page_num,
                "d-446978-n":   "1",
            }
            r2 = get_with_doh_fallback(session, AFD_LIST, params=params, timeout=40)
            r2.raise_for_status()
            try:
                s2 = BeautifulSoup(r2.content, "lxml")
            except Exception:
                s2 = BeautifulSoup(r2.content, "html.parser")
            page_notices = _parse_page(s2, today)
            all_notices.extend(page_notices)
            log.debug(f"AFD page {page_num}/{total_pages} : {len(page_notices)} notices")
        except Exception as e:
            log.warning(f"AFD page {page_num} échouée : {e} — continué")
            break

    log.info(f"AFD : {len(all_notices)} AO retenus sur {total_pages} pages")
    return all_notices
