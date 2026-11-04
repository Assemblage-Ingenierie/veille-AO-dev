"""Expertise France — scraping plateforme gestmax (recherche à facettes HTML)."""
import logging
import re
import time

from filters import classify
from config import EF_SEARCH_URL, EF_PAGE_URL, EF_TIMEOUT, EF_MAX_PAGES

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

_ID_RE   = re.compile(r"gestmax\.fr/(\d+)/")
_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _parse_item(item) -> "dict | None":
    a = item.find("a", href=True)
    if not a:
        return None
    href = a["href"]
    m = _ID_RE.search(href)
    if not m:
        return None
    vac_id = m.group(1)

    h2 = item.find("h2")
    if not h2:
        return None
    sr = h2.find("span", class_="sr-only")  # « (Nouvelle fenêtre) »
    if sr:
        sr.extract()
    titre = h2.get_text(" ", strip=True)
    if not titre:
        return None

    # Pays : span.country dont l'icône est ico_pays ; sinon 1re zone géographique.
    pays = ""
    countries = item.find_all("span", class_="country")
    for sp in countries:
        img = sp.find("img")
        if img and "ico_pays" in (img.get("src") or ""):
            st = sp.find("strong")
            pays = (st.get_text(strip=True) if st else sp.get_text(strip=True))
            break
    if not pays and countries:
        st = countries[0].find("strong")
        pays = st.get_text(strip=True) if st else countries[0].get_text(strip=True)

    # Type de contrat (CDI, CDD, Contrat de prestation de services, …)
    ct = item.find("div", class_="text-blue-light")
    type_contrat = ct.get_text(strip=True) if ct else ""

    # Date limite de candidature : "JJ/MM/AAAA HH:MM"
    date_lim = ""
    dl = item.find("div", class_="text-grey")
    if dl:
        md = _DATE_RE.search(dl.get_text())
        if md:
            date_lim = f"{md.group(3)}-{md.group(2)}-{md.group(1)}"

    secteur, pertinence = classify(titre, f"{titre} {pays}", pays)

    return {
        "id":               f"EF-{vac_id}",
        "bailleur":         "Expertise France",
        "titre":            titre,
        "projet":           "",
        "secteur":          secteur,
        "type_avis":        type_contrat,
        "mode_selection":   "",
        "langue":           "Français",
        "date_publication": "",
        "date_limite":      date_lim,
        "pays":             pays,
        "lien":             href.split("?")[0],
        "pertinence":       pertinence,
        "texte_complet":    f"{titre} {pays}",
    }


def _fetch_page(session, url: str) -> list:
    from bs4 import BeautifulSoup
    r = session.get(url, timeout=EF_TIMEOUT)
    r.raise_for_status()
    try:
        soup = BeautifulSoup(r.content, "lxml")
    except Exception:
        soup = BeautifulSoup(r.content, "html.parser")
    return soup.find_all("div", class_="list-group-item")


def fetch_expertise_france() -> list:
    import requests

    session = requests.Session()
    session.headers.update(_HEADERS)

    all_notices: list = []
    seen: set = set()

    for page in range(1, EF_MAX_PAGES + 1):
        url = EF_SEARCH_URL if page == 1 else EF_PAGE_URL.format(page=page)
        try:
            if page > 1:
                time.sleep(0.6)
            items = _fetch_page(session, url)
        except Exception as e:
            log.warning(f"Expertise France page {page} échouée : {e}")
            break

        new = 0
        for it in items:
            n = _parse_item(it)
            if n and n["id"] not in seen:
                seen.add(n["id"])
                all_notices.append(n)
                new += 1

        # Fin : page vide, ou aucune nouvelle offre (paginateur qui reboucle).
        if not items or new == 0:
            break

    log.info(f"Expertise France : {len(all_notices)} AO retenus")
    return all_notices


# ── Test manuel : python -m sources.expertise_france ──────────────────────────
if __name__ == "__main__":
    import json
    from collections import Counter

    logging.basicConfig(level=logging.INFO)
    notices = fetch_expertise_france()
    print(f"\n{len(notices)} offres")
    print("types de contrat :", dict(Counter(n["type_avis"] for n in notices)))
    if notices:
        print("\n=== 1re offre normalisée ===")
        print(json.dumps(notices[0], indent=2, ensure_ascii=False))
