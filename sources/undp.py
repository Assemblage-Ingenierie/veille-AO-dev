"""PNUD — RSS 1.0 (rdf:RDF) procurement-notices."""
import re
import logging
import xml.etree.ElementTree as ET
from datetime import date
from dateutil import parser as dateparser
from config import UNDP_EXCLUDE_TYPES
from filters import classify

log = logging.getLogger(__name__)

UNDP_RSS = "https://procurement-notices.undp.org/rss_feeds/rss.xml"

# RSS 1.0 uses the default namespace http://purl.org/rss/1.0/ for core elements
RSS1   = "http://purl.org/rss/1.0/"
SPEC   = "http://procurement-notices.undp.org/rss_feed/spec/"
DC     = "http://purl.org/dc/elements/1.1/"

_PREFIX_RE = re.compile(r'^(?:UNDP-[A-Z]{2,4}-[\d,.]+\s*[-–]\s*)+', re.IGNORECASE)
_SUFFIX_RE = re.compile(r'\s*[-–]\s*UNDP\s*[-–]\s*[A-Z][A-Z ,]+$')
_TYPE_RE   = re.compile(r'\b(RFP|ITB|EOI|REOI|CFP|CfP|RFI|IC|RFQ|LTA|ANA|CFT|RFPQ)\b', re.IGNORECASE)
_ID_RE     = re.compile(r'notice_id=(\d+)', re.IGNORECASE)

_FR_WORDS = {"de","du","des","le","la","les","un","une","et","pour","avec","dans","sur","par","au","aux"}
_ES_WORDS = {"de","del","los","las","un","una","y","el","para","con","en","por","se","su","al"}
_PT_WORDS = {"de","do","da","dos","das","um","uma","e","para","com","em","por","ao","os","as","no","na"}


def _r(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _parse_date(s: str) -> str:
    if not s:
        return ""
    try:
        return dateparser.parse(s).date().isoformat()
    except Exception:
        return ""


def _clean_title(raw: str) -> str:
    t = _PREFIX_RE.sub("", raw).strip()
    return _SUFFIX_RE.sub("", t).strip()


def _detect_lang(text: str) -> str:
    words = set(text.lower().split())
    fr = len(words & _FR_WORDS)
    es = len(words & _ES_WORDS)
    pt = len(words & _PT_WORDS)
    if fr >= 4 and fr >= es and fr >= pt:
        return "Français"
    if es >= 4 and es > fr:
        return "Espagnol"
    if pt >= 4 and pt > fr and pt > es:
        return "Portugais"
    return "Anglais"


def fetch_undp() -> list:
    import requests

    try:
        resp = requests.get(UNDP_RSS, timeout=40,
                            headers={"User-Agent": "AO-Veille/1.0"})
        resp.raise_for_status()
        content = resp.content
    except Exception as e:
        log.error(f"PNUD requête échouée : {e}")
        raise

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.error(f"PNUD XML invalide : {e}")
        raise

    # RSS 1.0: items are {http://purl.org/rss/1.0/}item at root level
    items = root.findall(_r(RSS1, "item"))
    if not items:
        # Fallback: search without namespace (RSS 2.0 embedded or stripped)
        items = root.findall(".//item")

    log.info(f"PNUD : {len(items)} items dans le flux RSS")

    today = date.today()
    notices = []

    for item in items:
        # ── Titre brut ──────────────────────────────────────────────────────
        raw_title = (
            item.findtext(_r(SPEC, "title"), "")
            or item.findtext("title", "")
            or item.findtext(_r(RSS1, "title"), "")
        ).strip()

        titre = _clean_title(raw_title)
        if not titre:
            continue

        # ── Type AO ─────────────────────────────────────────────────────────
        type_match = _TYPE_RE.search(raw_title)
        notice_type = type_match.group(1).upper() if type_match else "OTHER"
        if notice_type in UNDP_EXCLUDE_TYPES:
            continue

        # ── Deadline ────────────────────────────────────────────────────────
        deadline_str = item.findtext(_r(SPEC, "deadline"), "")
        date_lim = _parse_date(deadline_str)
        if deadline_str:
            try:
                if dateparser.parse(deadline_str).date() < today:
                    continue
            except Exception:
                pass

        # ── Lien & ID ───────────────────────────────────────────────────────
        link = (
            item.findtext(_r(RSS1, "link"), "")
            or item.findtext("link", "")
        ).strip()
        # RSS 1.0 quirk: <link> may carry its content as .tail after a blank text
        if not link:
            el = item.find(_r(RSS1, "link")) or item.find("link")
            if el is not None:
                link = (el.text or el.tail or "").strip()

        id_match = _ID_RE.search(link)
        if id_match:
            notice_id = id_match.group(1)
        else:
            import hashlib
            notice_id = hashlib.md5(link.encode()).hexdigest()[:10]

        # ── Pays / Secteur / Publication ────────────────────────────────────
        country = (
            item.findtext(_r(SPEC, "duty_station_cty"), "")
        ).strip().title()

        area = (
            item.findtext(_r(SPEC, "area_desc"), "")
            or item.findtext(_r(DC, "subject"), "")
        ).strip()

        pub_str = item.findtext(_r(DC, "date"), "")

        # ── Classification ──────────────────────────────────────────────────
        # Use title + area + dc:subject for keyword matching
        # (description in UNDP RSS is just "Application Deadline: …")
        result = classify(titre, area, country)
        secteur, pertinence = result

        langue = _detect_lang(titre)

        notices.append({
            "id": f"UNDP-{notice_id}",
            "bailleur": "PNUD",
            "titre": titre,
            "projet": area,
            "secteur": secteur,
            "type_avis": notice_type,
            "mode_selection": "",
            "langue": langue,
            "date_publication": _parse_date(pub_str),
            "date_limite": date_lim,
            "pays": country,
            "lien": link,
            "pertinence": pertinence,
            "texte_complet": f"{titre} {area}",
        })

    log.info(f"PNUD : {len(notices)} AO retenus")
    return notices
