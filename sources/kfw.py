"""KfW Entwicklungsbank — avis publiés sur GTAI (Germany Trade & Invest).

Fonctionnement du site (relevé le 24/11/2026) :
  - Recherche : https://www.gtai.de/en/meta/search/kfw-tenders/795748!search;<état>
    où <état> est le JSON des critères, compressé zlib puis encodé base64url. On le
    construit nous-mêmes : financeur KfW (facette financer=4486), rubrique « Tenders »
    (99662), 100 résultats par page, tri par date décroissante. Pagination : ?page=N.
  - Page HTML rendue côté serveur : <li class="result-item"> avec date, sous-type
    (Prequalification, Expression of Interest…), pays, secteurs, titre, date limite,
    financeur et résumé « Requested Services ».
  - Le critère de date de l'état mélange d'autres contenus du site (événements…) :
    pour ne lire que les nouveautés, on trie par date et on s'arrête à la dernière
    collecte réussie.

On ne garde que les vrais appels d'offres : date limite à venir, sans les avis
d'attribution, d'annulation, de résultats de préqualification ni les rectificatifs
(mise à jour, addendum, correction, report de date).
"""
import base64
import json
import logging
import re
import time
import zlib
from datetime import date, datetime, timedelta
from html import unescape

import requests

from filters import classify
from sources.undp import _detect_lang

log = logging.getLogger(__name__)

BASE = "https://www.gtai.de"
SEARCH = f"{BASE}/en/meta/search/kfw-tenders/795748!search;"
FINANCER_KFW = "4486"
RUBRIC_TENDERS = "99662"
PAGE_SIZE = 100
MAX_PAGES = 20          # ~1 100 avis en tout sur GTAI aujourd'hui (11 pages)
PAUSE = 1.0
TIMEOUT = 60
SINCE_MARGIN = timedelta(days=1)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-GB,en;q=0.9",
}

_FACETS = ("business-area", "rubric", "country", "sectors", "law", "special", "economic",
           "customs", "financer", "phase", "companyFoundation", "eventType", "series",
           "subject", "formats")

# Sous-types écartés : pas des opportunités, ou rectificatifs d'un avis déjà collecté.
_EXCLUDED = re.compile(r"award|cancel|pq-result|result", re.IGNORECASE)
_RECTIF = re.compile(r"update|addendum|correction|extension|amendment", re.IGNORECASE)
_NEW_CALL = re.compile(r"expression of interest|re-tender|tender with", re.IGNORECASE)

_ITEM_RE = re.compile(r'<li class="result-item">(.*?)</li>\s*(?=<li class="result-item">|</ul>)', re.S)
_OVERLINE_RE = re.compile(r'<span class="overline__text[^"]*"\s*>([^<]*)</span>')
_LINK_RE = re.compile(r'<a class="" href="([^"]+)"\s*>\s*<h3[^>]*>(.*?)</h3>', re.S)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_ID_RE = re.compile(r"-(\d+)$")


def _clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", s or "")
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _date(s: str) -> str:
    try:
        return datetime.strptime(_clean(s), "%b %d, %Y").date().isoformat()
    except ValueError:
        return ""


def search_state(page_size: int = PAGE_SIZE) -> str:
    facets = {k: {"cnj": "any", "excl": [], "selected": []} for k in _FACETS}
    facets["business-area"]["selected"] = ["trade"]
    facets["rubric"]["selected"] = [RUBRIC_TENDERS]
    facets["financer"]["selected"] = [FINANCER_KFW]
    state = {"date": {"end": None, "range": None, "start": None}, "facets": facets,
             "pageSize": page_size, "q": None, "sort": "dateDesc"}
    raw = zlib.compress(json.dumps(state, separators=(",", ":")).encode())
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_items(page_html: str) -> list:
    items = []
    for li in _ITEM_RE.findall(page_html):
        link = _LINK_RE.search(li)
        if not link:
            continue
        href = link.group(1).split("?")[0]
        m = _ID_RE.search(href)
        if not m:
            continue
        overline = [_clean(x) for x in _OVERLINE_RE.findall(li)]
        item = {"gtai_id": m.group(1), "href": href, "titre": _clean(link.group(2)),
                "published": _date(overline[0]) if overline else "",
                "pays": overline[2] if len(overline) > 2 else "",
                "secteurs": overline[3] if len(overline) > 3 else "",
                "sous_type": "", "deadline": "", "financement": "", "services": ""}
        for raw in _P_RE.findall(li):
            p = _clean(raw)
            if p.startswith("Deadline:"):
                item["deadline"] = _date(p.partition(":")[2])
            elif p.startswith("Financing:"):
                item["financement"] = p.partition(":")[2].strip()
            elif p.startswith("Requested Service"):
                item["services"] = re.sub(r"^Requested Services?\s*:?\s*", "", p)
            elif not item["sous_type"] and not item["deadline"]:
                item["sous_type"] = p
        items.append(item)
    return items


def is_open_call(item: dict, today: "date | None" = None) -> bool:
    today = (today or date.today()).isoformat()
    if not item["deadline"] or item["deadline"] < today:
        return False
    sub = item["sous_type"]
    if _EXCLUDED.search(sub):
        return False
    if _RECTIF.search(sub) and not _NEW_CALL.search(sub):
        return False
    return True


def to_notice(item: dict) -> dict:
    titre, services, pays = item["titre"], item["services"], item["pays"]
    secteur, pertinence = classify(titre, f"{services} {item['secteurs']}", pays)
    return {
        "id": f"KFW-{item['gtai_id']}",
        "bailleur": "KfW",
        "titre": titre,
        "projet": services[:300],
        "secteur": secteur,
        "type_avis": item["sous_type"] or "Tender Notice",
        "mode_selection": "",
        "langue": _detect_lang(titre),
        "date_publication": item["published"],
        "date_limite": item["deadline"],
        "pays": pays,
        "lien": f"{BASE}{item['href']}",
        "pertinence": pertinence,
        "texte_complet": f"{titre} {services} {item['secteurs']}",
    }


def fetch_kfw(since: "datetime | None" = None) -> list:
    """since : début de la dernière collecte réussie ; None → tous les avis."""
    cutoff = ((since.date() if isinstance(since, datetime) else since) - SINCE_MARGIN).isoformat() \
        if since else ""
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    url = SEARCH + search_state()

    notices, seen, read = [], set(), 0
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAUSE)
        r = sess.get(url + (f"?page={page}" if page else ""), timeout=TIMEOUT)
        r.raise_for_status()
        items = parse_items(r.text)
        if not items:
            if page == 0:
                raise RuntimeError("KfW/GTAI : aucun avis lu (structure du site modifiée ?)")
            break
        reached_cutoff = False
        for item in items:
            if cutoff and item["published"] and item["published"] < cutoff:
                reached_cutoff = True
                continue
            read += 1
            if item["gtai_id"] in seen or not is_open_call(item):
                continue
            if item["financement"] and "kfw" not in item["financement"].lower():
                continue
            seen.add(item["gtai_id"])
            notices.append(to_notice(item))
        if reached_cutoff:
            break

    scope = f"publiés depuis le {cutoff}" if cutoff else "tous les avis"
    log.info(f"KfW : {read} avis lus ({scope}), {len(notices)} appels d'offres ouverts retenus")
    return notices
