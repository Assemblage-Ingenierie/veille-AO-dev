"""Banque Mondiale — API v2 procnotices."""
import logging
from datetime import date
from dateutil import parser as dateparser
from filters import classify

log = logging.getLogger(__name__)

BM_API = "https://search.worldbank.org/api/v2/procnotices"

_LANG_MAP = {
    "English":             "Anglais",
    "French":              "Français",
    "Spanish; Castilian":  "Espagnol",
    "Spanish":             "Espagnol",
    "Portuguese":          "Portugais",
}


def _parse_date(s: str) -> str:
    if not s:
        return ""
    try:
        return dateparser.parse(s).date().isoformat()
    except Exception:
        return ""


def fetch_bm() -> list:
    import requests

    today = date.today().isoformat()
    params = {
        "format": "json",
        "rows": 500,
        "srt": "submission_deadline_date",
        "order": "asc",
        "deadline_strdate": today,
        "apilang": "en",
        "srce": "both",
    }

    try:
        resp = requests.get(BM_API, params=params, timeout=40)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"BM requête échouée : {e}")
        raise

    raw = data.get("procnotices", [])
    if not raw:
        log.warning("BM : aucun avis reçu")
        return []

    notices = []
    today_date = date.today()

    for item in raw:
        if item.get("notice_status") != "Published":
            continue

        pub_str = item.get("submission_date", "")
        if pub_str:
            try:
                if dateparser.parse(pub_str).date() > today_date:
                    continue
            except Exception:
                pass

        titre = (item.get("bid_description") or item.get("project_name") or "").strip()
        if not titre:
            continue

        lang_raw = item.get("notice_lang_name", "")
        langue = _LANG_MAP.get(lang_raw, "")
        if not langue:
            continue

        # Use notice_text (full HTML) as context — keywords are still findable as substrings
        texte = item.get("notice_text", "") or ""
        projet = (item.get("project_name") or "").strip()

        result = classify(titre, texte, projet)
        secteur, pertinence = result

        notice_id = item.get("id", "")

        notices.append({
            "id": f"BM-{notice_id}",
            "bailleur": "Banque Mondiale",
            "titre": titre,
            "projet": projet,
            "secteur": secteur,
            "type_avis": item.get("notice_type", ""),
            "mode_selection": item.get("procurement_method_name", ""),
            "langue": langue,
            "date_publication": _parse_date(item.get("submission_date", "")),
            "date_limite": _parse_date(item.get("submission_deadline_date", "")),
            "pays": item.get("project_ctry_name", ""),
            "lien": f"https://projects.worldbank.org/procurement/noticeoverview?id={notice_id}&lang=en",
            "pertinence": pertinence,
            "texte_complet": texte,
        })

    log.info(f"BM : {len(notices)} AO retenus sur {len(raw)} reçus")
    return notices
