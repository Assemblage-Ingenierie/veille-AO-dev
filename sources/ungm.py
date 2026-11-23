"""UNGM (United Nations Global Marketplace) — recherche publique des avis.

Fonctionnement du site (relevé le 23/11/2026) :
  1. GET  https://www.ungm.org/Public/Notice  → page de recherche, porte un jeton
     anti-CSRF (<input name="__RequestVerificationToken">) et pose le cookie associé.
  2. POST https://www.ungm.org/Public/Notice/Search  (JSON, en-tête
     RequestVerificationToken) → fragment HTML de 15 lignes <div class="tableRow dataRow">.
     PageSize est imposé à 15 (toute autre valeur → 400) : on pagine par PageIndex
     jusqu'à une page vide. NoticeTypes filtre côté serveur, IsActive=True exclut
     les avis clos.
     PublishedFrom (format « 20-Nov-2026 », borne incluse) limite la recherche aux
     avis publiés depuis une date : on ne relit que ce qui est paru depuis la
     dernière collecte réussie (voir collector.INCREMENTAL).
  3. Le site limite le débit (429 après ~20 requêtes rapprochées) : pause entre
     les pages et nouvel essai après attente en cas de 429.

On ne retient que les demandes de propositions (RFP) et les appels à manifestation
d'intérêt (EOI). Les avis du PNUD sont écartés : la source « PNUD » les collecte déjà
depuis procurement-notices.undp.org.
"""
import logging
import re
import time
from datetime import date, datetime, timedelta
from html import unescape

import requests
from dateutil import parser as dateparser

from filters import classify
from sources.undp import _detect_lang

log = logging.getLogger(__name__)

BASE = "https://www.ungm.org"
SEARCH_PAGE = f"{BASE}/Public/Notice"
SEARCH_API = f"{BASE}/Public/Notice/Search"

# Identifiants des cases « Type of opportunity » du site → abréviation stockée.
NOTICE_TYPES = {"RequestForProposal": "RFP", "RequestForEoi": "EOI"}
TYPE_LABELS = {"Request for proposal": "RFP", "Request for EOI": "EOI"}
EXCLUDED_AGENCIES = {"UNDP"}   # déjà couvert par la source PNUD

PAGE_SIZE = 15        # seule valeur acceptée par le site
MAX_PAGES = 60        # garde-fou (~900 avis ; ~20 pages en pratique)
PAUSE = 0.8           # secondes entre deux pages
RETRY_WAIT = 20       # secondes d'attente après un 429
TIMEOUT = 30
# Marge sur la date de dernière lecture : les dates de publication UNGM n'ont pas
# d'heure ni de fuseau ; relire un jour de plus coûte une page, les doublons sont
# ignorés à l'enregistrement.
SINCE_MARGIN = timedelta(days=1)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-GB,en;q=0.9,fr;q=0.8",
}

_TOKEN_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')
_ROW_RE = re.compile(r'<div role="row"[^>]*data-noticeid="(\d+)"[^>]*class="tableRow dataRow')
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", fragment))).strip()


def _date(s: str) -> str:
    """« 23-Dec-2026 15:00 (GMT 3.00) » ou « 23-Nov-2026 » → 2026-12-23."""
    m = re.search(r"\d{1,2}-[A-Za-z]{3}-\d{4}", s or "")
    if not m:
        return ""
    try:
        return dateparser.parse(m.group(0), dayfirst=True).date().isoformat()
    except Exception:
        return ""


def _cell(row: str, pattern: str) -> str:
    m = re.search(pattern, row, re.S)
    return _text(m.group(1)) if m else ""


def parse_rows(fragment: str) -> list:
    """Découpe le fragment HTML renvoyé par /Public/Notice/Search en lignes brutes."""
    starts = [m.start() for m in _ROW_RE.finditer(fragment)]
    rows = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(fragment)
        chunk = fragment[start:end]
        notice_id = _ROW_RE.match(chunk).group(1)
        rows.append({
            "id": notice_id,
            "titre": _cell(chunk, r'<span class="ungm-title[^"]*">(.*?)</span>'),
            "deadline": _cell(chunk, r'class="tableCell resultInfo1 deadline"[^>]*>\s*<span>(.*?)</span>'),
            "agence": _cell(chunk, r'class="tableCell resultAgency">\s*<span>(.*?)</span>'),
            "type": _cell(chunk, r"<label for='[^']*'>(.*?)</label>"),
            "reference": _cell(chunk, r'data-description="Reference">\s*<span>(.*?)</span>'),
            # Les deux cellules sans classe : date de publication puis pays.
            "plain": [_text(c) for c in re.findall(
                r'<div role="cell" class="tableCell">\s*<span>(.*?)</span>', chunk, re.S)],
        })
    return rows


def _to_notice(row: dict) -> "dict | None":
    titre = row["titre"]
    if not titre:
        return None
    agence = row["agence"].strip()
    if agence.upper() in EXCLUDED_AGENCIES:
        return None
    type_avis = TYPE_LABELS.get(row["type"])
    if not type_avis:
        return None
    plain = row["plain"]
    published = _date(plain[0]) if plain else ""
    pays = plain[-1] if len(plain) > 1 else ""
    secteur, pertinence = classify(titre, agence, pays)
    return {
        "id": f"UNGM-{row['id']}",
        "bailleur": "UNGM",
        "titre": titre,
        "projet": agence,
        "secteur": secteur,
        "type_avis": type_avis,
        "mode_selection": "",
        "langue": _detect_lang(titre),
        "date_publication": published,
        "date_limite": _date(row["deadline"]),
        "pays": pays,
        "lien": f"{BASE}/Public/Notice/{row['id']}",
        "pertinence": pertinence,
        "texte_complet": f"{titre} {agence} {row['reference']}",
    }


def published_from(since: "datetime | date | None") -> str:
    """Date de dernière lecture → valeur PublishedFrom (« 20-Nov-2026 »), marge comprise.
    Indépendant de la langue du serveur (pas de strftime %b)."""
    if since is None:
        return ""
    d = (since.date() if isinstance(since, datetime) else since) - SINCE_MARGIN
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _search_body(page: int, published_from_value: str = "") -> dict:
    return {
        "PageIndex": page, "PageSize": PAGE_SIZE,
        "Title": "", "Description": "", "Reference": "",
        "PublishedFrom": published_from_value, "PublishedTo": "", "DeadlineFrom": "", "DeadlineTo": "",
        "Countries": [], "Agencies": [], "UNSPSCs": [],
        "NoticeTypes": list(NOTICE_TYPES),
        "SortField": "DatePublished", "SortAscending": False,
        "isPicker": False, "IsSustainable": False, "IsActive": True,
        "NoticeDisplayType": None, "NoticeSearchTotalLabelId": "noticeSearchTotal",
        "TypeOfCompetitions": [],
    }


def _post(sess: requests.Session, token: str, page: int, frm: str = "") -> str:
    for attempt in range(3):
        r = sess.post(SEARCH_API, json=_search_body(page, frm), timeout=TIMEOUT,
                      headers={"RequestVerificationToken": token,
                               "X-Requested-With": "XMLHttpRequest"})
        if r.status_code == 429 and attempt < 2:
            wait = int(r.headers.get("Retry-After") or RETRY_WAIT)
            log.info(f"UNGM : débit limité (429), nouvel essai dans {wait} s")
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        return r.text
    return ""


def fetch_ungm(since: "datetime | None" = None) -> list:
    """since : début de la dernière collecte réussie ; None → tous les avis ouverts."""
    frm = published_from(since)
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    r = sess.get(SEARCH_PAGE, timeout=TIMEOUT)
    r.raise_for_status()
    m = _TOKEN_RE.search(r.text)
    if not m:
        raise RuntimeError("UNGM : jeton de recherche introuvable (structure du site modifiée ?)")
    token = m.group(1)

    notices, seen = [], set()
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAUSE)
        rows = parse_rows(_post(sess, token, page, frm))
        if not rows:
            break
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            n = _to_notice(row)
            if n:
                notices.append(n)
    else:
        log.warning(f"UNGM : arrêt au garde-fou de {MAX_PAGES} pages")

    scope = f"publiés depuis le {frm}" if frm else "tous les avis ouverts"
    log.info(f"UNGM : {len(seen)} avis lus ({scope}), {len(notices)} retenus (RFP + EOI, hors PNUD)")
    return notices
