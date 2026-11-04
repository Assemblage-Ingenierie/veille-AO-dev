"""TED — Tenders Electronic Daily, Search API v3 (POST paginé, PAGE_NUMBER)."""
import logging
import re
from datetime import date
from dateutil import parser as dateparser

from filters import classify
from config import (TED_ENDPOINT, TED_QUERY, TED_FIELDS, TED_PAGE_LIMIT,
                    TED_MAX_TOTAL, TED_TIMEOUT, TED_COUNTRY_NAMES)

log = logging.getLogger(__name__)

# Codes langue ISO-639-3 utilisés par TED → libellé du schéma `langue`.
_LANG_MAP = {
    "fra": "Français", "eng": "Anglais", "spa": "Espagnol", "por": "Portugais",
}
# Ordre de préférence pour extraire une valeur multilingue.
_LANG_PRIORITY = ("fra", "eng", "spa", "por")


def _pick_lang(value) -> str:
    """Extrait une chaîne d'un champ TED multilingue.

    Les champs texte arrivent sous forme :
      - dict {code_langue: valeur|liste}  → fra, puis eng, sinon 1re clé ;
      - liste  → 1er élément ;
      - scalaire → tel quel.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        if not value:
            return ""
        key = next((k for k in _LANG_PRIORITY if k in value), next(iter(value)))
        return _pick_lang(value[key])
    if isinstance(value, list):
        return _pick_lang(value[0]) if value else ""
    return str(value).strip()


def _detect_langue(*fields) -> str:
    """Déduit la langue de l'avis depuis les clés présentes dans les champs
    multilingues (le 1er code reconnu par ordre de priorité l'emporte)."""
    for f in fields:
        if isinstance(f, dict):
            for code in _LANG_PRIORITY:
                if code in f:
                    return _LANG_MAP[code]
    return ""


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _parse_date(value) -> str:
    """Parse une date TED → ISO 'YYYY-MM-DD'.

    Le format TED est 'YYYY-MM-DD+HH:MM' (date + décalage horaire, sans heure),
    que dateutil ne parse pas ; on extrait donc d'abord la partie date.
    """
    s = _pick_lang(value)
    if not s:
        return ""
    m = _DATE_RE.search(s)
    if not m:
        return ""
    try:
        return dateparser.parse(m.group(1)).date().isoformat()
    except Exception:
        return ""


def _country_names(codes) -> str:
    """['BDI', 'SEN'] → 'Burundi, Sénégal' (codes inconnus conservés bruts)."""
    if not codes:
        return ""
    if isinstance(codes, str):
        codes = [codes]
    seen, names = set(), []
    for c in codes:
        c = (c or "").strip().upper()
        if not c or c in seen:
            continue
        seen.add(c)
        names.append(TED_COUNTRY_NAMES.get(c, c))
    return ", ".join(names)


def _normalize(item: dict) -> "dict | None":
    """Convertit un avis TED brut vers le schéma `notices`. None si inexploitable."""
    pub = (item.get("publication-number") or "").strip()
    if not pub:
        return None

    title_raw = item.get("notice-title")
    desc_raw  = item.get("description-lot")

    titre = _pick_lang(title_raw) or _pick_lang(desc_raw)
    if not titre:
        return None

    description = _pick_lang(desc_raw)
    projet = _pick_lang(item.get("buyer-name"))
    pays = _country_names(item.get("place-of-performance"))
    langue = _detect_langue(title_raw, desc_raw) or ""

    cpv = item.get("classification-cpv") or []
    cpv_txt = " ".join(str(c) for c in cpv) if isinstance(cpv, list) else str(cpv)
    texte = f"{titre} {description} {projet} {pays} {cpv_txt}".strip()

    secteur, pertinence = classify(titre, texte, projet)

    return {
        "id":               f"TED-{pub}",
        "bailleur":         "TED",
        "titre":            titre,
        "projet":           projet,
        "secteur":          secteur,
        "type_avis":        _pick_lang(item.get("notice-type")),
        "mode_selection":   "",
        "langue":           langue,
        "date_publication": _parse_date(item.get("publication-date")),
        "date_limite":      _parse_date(item.get("deadline-receipt-tender-date-lot")),
        "pays":             pays,
        "lien":             f"https://ted.europa.eu/en/notice/-/detail/{pub}",
        "pertinence":       pertinence,
        "texte_complet":    texte,
    }


def _fetch_page(session, page: int) -> "tuple[list, int, bool]":
    """Renvoie (avis_bruts, totalNoticeCount, timedOut) pour une page donnée."""
    body = {
        "query":            TED_QUERY,
        "fields":           TED_FIELDS,
        "page":             page,
        "limit":            TED_PAGE_LIMIT,
        "scope":            "ACTIVE",
        "checkQuerySyntax": False,
        "paginationMode":   "PAGE_NUMBER",
    }
    r = session.post(TED_ENDPOINT, json=body, timeout=TED_TIMEOUT,
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    return (data.get("notices") or [],
            int(data.get("totalNoticeCount") or 0),
            bool(data.get("timedOut")))


def fetch_ted() -> list:
    import requests

    session = requests.Session()
    all_notices: list = []
    seen_ids: set = set()
    page = 1

    while True:
        raw, total, timed_out = _fetch_page(session, page)
        if timed_out:
            log.warning(f"TED : réponse partielle (timedOut) page {page}")

        for item in raw:
            n = _normalize(item)
            if n and n["id"] not in seen_ids:
                seen_ids.add(n["id"])
                all_notices.append(n)

        collected = page * TED_PAGE_LIMIT
        if (len(raw) < TED_PAGE_LIMIT       # dernière page (page non pleine)
                or collected >= total       # tout récupéré
                or collected >= TED_MAX_TOTAL):  # plafond API
            break
        page += 1

    log.info(f"TED : {len(all_notices)} AO retenus (total annoncé : {total})")
    return all_notices


# ── Test manuel : python -m sources.ted  → dump de la forme brute ─────────────
if __name__ == "__main__":
    import json
    import requests

    logging.basicConfig(level=logging.INFO)
    sess = requests.Session()
    raw, total, timed_out = _fetch_page(sess, 1)
    print(f"totalNoticeCount = {total} · timedOut = {timed_out} · page1 = {len(raw)} avis")
    if raw:
        print("\n=== 1er avis brut ===")
        print(json.dumps(raw[0], indent=2, ensure_ascii=False))
        print("\n=== normalisé ===")
        print(json.dumps(_normalize(raw[0]), indent=2, ensure_ascii=False))
