"""
Fetchers de page de détail par bailleur.
Appelé par /api/ao-detail/{notice_id} pour enrichir les fiches pré-tri.

Stratégie par bailleur
──────────────────────
BM   : API JSON (notice_text HTML déjà en base → parsing BeautifulSoup)
PNUD : scraping HTML view_notice.cfm (rendu serveur)
GIZ  : page AO → onglet Teilnahmeunterlagen → fichier TOR (PDF/Word) → extraction
BERD : scraping ecepp.ebrd.com/delta/viewNotice.html
AFD  : page afd.dgmarket.com/tender/{id} → téléchargement document (PDF/Word) → extraction
"""
import io
import logging
import re
import urllib.request
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from sources.dns_utils import get_with_doh_fallback as _doh_get

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Proxy système (WinINET / variables d'environnement) — détecté une seule fois
_SYS_PROXIES: dict = urllib.request.getproxies()


# ── Utilities réseau ──────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    if _SYS_PROXIES:
        sess.proxies.update(_SYS_PROXIES)
    return sess


def _get(url: str, timeout: int = 30, params: dict = None) -> requests.Response:
    sess = _make_session()
    return sess.get(url, params=params, timeout=timeout, allow_redirects=True)


def _soup(url: str, timeout: int = 30) -> BeautifulSoup:
    r = _get(url, timeout=timeout)
    r.raise_for_status()
    try:
        return BeautifulSoup(r.content, "lxml")
    except Exception:
        return BeautifulSoup(r.content, "html.parser")


def _html_to_text(html: str) -> str:
    """Balises HTML → texte brut, sauts de ligne préservés pour les blocs."""
    if not html:
        return ""
    s = BeautifulSoup(html, "html.parser")
    # Insère un saut de ligne avant chaque élément bloc
    for tag in s.find_all(["p", "li", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"]):
        tag.insert_before("\n")
    text = s.get_text(separator="")
    # Remplace les &nbsp; (\xa0) par des espaces ordinaires
    text = text.replace("\xa0", " ")
    # Normalise : strip par ligne, max 1 ligne vide consécutive
    lines = [l.strip() for l in text.splitlines()]
    cleaned, prev_blank = [], False
    for line in lines:
        if not line:
            if not prev_blank:
                cleaned.append("")
            prev_blank = True
        else:
            cleaned.append(line)
            prev_blank = False
    return "\n".join(cleaned).strip()


# ── Parseurs de documents (PDF / DOCX) ───────────────────────────────────────

def _parse_pdf_bytes(data: bytes) -> str:
    """Extrait le texte d'un PDF via pdfplumber (page par page)."""
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages.append(t)
        return "\n".join(pages)
    except ImportError:
        log.warning("pdfplumber non installé — pip install pdfplumber")
        return ""
    except Exception as e:
        log.debug(f"PDF parsing error: {e}")
        return ""


def _parse_docx_bytes(data: bytes) -> str:
    """Extrait le texte d'un fichier Word DOCX."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(data))
        parts = []
        for para in doc.paragraphs:
            t = para.text.strip()
            if t:
                parts.append(t)
        return "\n".join(parts)
    except ImportError:
        log.warning("python-docx non installé — pip install python-docx")
        return ""
    except Exception as e:
        log.debug(f"DOCX parsing error: {e}")
        return ""


def _bytes_to_text(data: bytes, content_type: str = "", url: str = "") -> str:
    """
    Extrait le texte depuis des bytes en détectant le format automatiquement.
    Priorité : content-type → extension URL → essai PDF puis DOCX.
    """
    ct = content_type.lower()
    url_l = url.lower()

    if "pdf" in ct or url_l.endswith(".pdf"):
        return _parse_pdf_bytes(data)

    if ("word" in ct or "openxmlformats" in ct or "msword" in ct
            or url_l.endswith((".docx", ".doc"))):
        text = _parse_docx_bytes(data)
        return text if text else _parse_pdf_bytes(data)  # docx parfois servi comme PDF

    # Format inconnu : essai PDF d'abord, puis DOCX
    text = _parse_pdf_bytes(data)
    return text if text else _parse_docx_bytes(data)


# ── Extraction de sections (contexte / critères) ──────────────────────────────

def _extract_section(text: str, start_patterns: list[str],
                     stop_patterns: list[str], max_chars: int = 3000) -> str:
    """
    Trouve dans `text` une section dont le titre correspond à l'un des
    `start_patterns` et extrait son contenu jusqu'au premier titre correspondant
    à l'un des `stop_patterns`.

    Les titres de section sont heuristiquement repérés comme des lignes courtes
    (< 120 car.) qui commencent par le pattern (optionnellement précédé d'un
    numéro de section romain/arabe).
    """
    lines = text.splitlines()

    # Numérotation optionnelle en début de titre : "I.", "1.", "A.", "1.2", etc.
    _NUM = r"(?:(?:[IVX]+|[A-Za-z]|\d+(?:\.\d+)*)[\.\)\-]\s+)?"

    start_re = re.compile(
        r"^\s*" + _NUM + r"(?:" + "|".join(start_patterns) + r")\b",
        re.IGNORECASE,
    )
    stop_re = re.compile(
        r"^\s*" + _NUM + r"(?:" + "|".join(stop_patterns) + r")\b",
        re.IGNORECASE,
    )

    # Recherche du début de section
    start_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and len(stripped) < 120 and start_re.match(stripped):
            start_idx = i + 1  # on saute la ligne de titre elle-même
            break

    if start_idx is None:
        return ""

    # Collecte du contenu jusqu'au stop
    parts = []
    total = 0
    for line in lines[start_idx:]:
        stripped = line.strip()
        if stripped and len(stripped) < 120 and stop_re.match(stripped):
            break
        parts.append(line)
        total += len(line) + 1
        if total > max_chars:
            break

    return "\n".join(parts).strip()[:max_chars]


def _extract_context_criteria(text: str) -> tuple[str, str]:
    """
    Extrait le contexte/présentation et les critères de sélection
    d'un document (AO AFD) via correspondance de titres de section.
    """
    CONTEXT = [
        r"contexte",
        r"background",
        r"pr[eé]sentation(?:\s+(?:du|de|des|d')[\w\s]+)?",
        r"context(?:e)?",
        r"objet\s+(?:du|de|des|d[e'])",
        r"introduction",
        r"description\s+(?:du|de|des|d[e']|of)",
        r"justification",
        r"mission\s+(?:du|de|des)",
        r"cadre\s+(?:de|du|g[eé]n[eé]ral)",
    ]
    CRITERIA = [
        r"crit[eè]res?\s+de\s+s[eé]lection",
        r"crit[eè]res?\s+d[e'][\s\w]*[eé]valuation",
        r"selection\s+criteria",
        r"evaluation\s+criteria",
        r"qualifications?\s+requises?",
        r"profil\s+(?:du|de|requis|recherch[eé])",
        r"requirements?",
        r"[eé]ligib",
        r"comp[eé]tences?\s+requises?",
        r"exp[eé]rience\s+requises?",
    ]
    STOP = [
        r"d[eé]lai",
        r"deadline",
        r"soumission",
        r"submission",
        r"budget",
        r"calendrier",
        r"calendar",
        r"annexe",
        r"appendix",
        r"documents?\s+(?:requis|[àa]\s+fournir)",
        r"required\s+documents?",
        r"offre",
        r"proposition",
        r"r[eé]mun[eé]ration",
        r"payment",
        r"paiement",
        r"modalit[eé]s",
    ]

    context  = _extract_section(text, CONTEXT,  CRITERIA + STOP)
    criteria = _extract_section(text, CRITERIA, STOP)
    return context, criteria


# ── Parser notice_text BM ─────────────────────────────────────────────────────

def _parse_bm_notice_text(html: str) -> dict:
    """
    Extrait sections structurées du champ notice_text BM (HTML riche).
    Retourne : description, criteria, submission_email
    """
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    sections = {}
    current_label = None
    current_parts = []

    for elem in soup.find_all(["p", "ol", "ul", "table"]):
        strong = elem.find("strong")
        if strong and strong.get_text(strip=True).replace("\xa0", "").strip():
            label_raw = strong.get_text(strip=True).replace("\xa0", " ").strip()
            if len(label_raw) > 120:
                # <strong> trop long → c'est du contenu, pas un titre
                if current_label is not None:
                    t = elem.get_text(separator="\n", strip=True).replace("\xa0", " ")
                    if t:
                        current_parts.append(t)
                continue
            # Vérifier si le <strong> est isolé dans le <p> (= titre)
            # ou embedded dans un paragraphe plus long (= mise en gras inline)
            elem_text_full = elem.get_text(separator=" ", strip=True).replace("\xa0", " ")
            remaining_after_strong = elem_text_full[len(label_raw):].strip().lstrip(":").strip()
            if len(remaining_after_strong) > 60:
                # Texte substantiel après le strong → mise en gras inline, pas un titre
                if current_label is not None:
                    t = elem.get_text(separator="\n", strip=True).replace("\xa0", " ")
                    if t:
                        current_parts.append(t)
                continue
            # C'est bien un titre de section
            if current_label and current_parts:
                sections[current_label] = "\n\n".join(current_parts).strip()
            current_label = label_raw.rstrip(":").strip().lower()
            current_parts = [remaining_after_strong] if remaining_after_strong else []
        elif current_label is not None:
            t = elem.get_text(separator="\n", strip=True).replace("\xa0", " ")
            if t:
                current_parts.append(t)

    if current_label and current_parts:
        sections[current_label] = "\n\n".join(current_parts).strip()

    desc_keys = [
        # Anglais — background / contexte / portée
        "background", "context", "project description", "project background",
        "description", "introduction", "overview", "assignment background",
        "scope of work", "scope of services", "scope", "tasks", "deliverables",
        "objective", "objectives", "assignment objective", "project objective",
        "situation", "justification", "terms of reference",
        # Français
        "contexte", "description du projet", "présentation du projet",
        "objet de la mission", "objet du marché",
        # Espagnol
        "contexto", "antecedentes", "descripcion", "objetivo del proyecto",
        "alcance", "actividades",
        # Portugais
        "descrição", "contexto do projeto",
        # Sections numérotées — formes précises seulement
        "i. context", "i. background", "i. contexte", "i. contexto",
        "1. background", "1. context", "2. objective", "2. scope",
        "ii. objetivo", "ii. objective",
    ]
    crit_keys = [
        "selection criteria", "criteria", "qualification", "qualifications",
        "requirements", "required qualifications", "eligibility",
        "critères de sélection", "critères", "profil", "profil du consultant",
        "perfil", "requisitos", "critérios",
        "iv. profil", "iii. profil", "iv. perfil", "iv. perfil do",
        "consultant profile", "team composition",
    ]

    # Collecter TOUTES les sections matching (pas seulement la première)
    desc_parts, crit_parts = [], []
    for k, v in sections.items():
        if any(dk in k for dk in desc_keys):
            desc_parts.append(v)
        elif any(ck in k for ck in crit_keys):
            crit_parts.append(v)

    if desc_parts:
        result["description_section"] = "\n\n".join(desc_parts)
    if crit_parts:
        result["criteria_section"] = "\n\n".join(crit_parts)

    # Fallback : si aucune section reconnue, prendre la section la plus longue
    if not result.get("description_section") and sections:
        longest_key = max(sections, key=lambda k: len(sections[k]))
        if len(sections[longest_key]) > 100:
            result["description_section"] = sections[longest_key]

    emails = re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', html)
    if emails:
        result["submission_email"] = emails[-1]

    result["full_text"] = _html_to_text(html)
    return result


# ── Banque Mondiale ────────────────────────────────────────────────────────────

def _fetch_bm(notice_id: str, texte_complet: str) -> dict:
    op_id = notice_id.removeprefix("BM-")
    result = {}

    if texte_complet:
        parsed = _parse_bm_notice_text(texte_complet)
        result.update(parsed)

    try:
        r = _get(
            "https://search.worldbank.org/api/v2/procnotices",
            params={"format": "json", "rows": 1, "id": op_id, "apilang": "en"},
        )
        r.raise_for_status()
        items = r.json().get("procnotices", [])
        if items:
            it = items[0]
            result["contact_name"]    = it.get("contact_name", "")
            result["contact_org"]     = it.get("contact_organization", "")
            result["contact_email"]   = it.get("contact_email", "")
            result["contact_phone"]   = it.get("contact_phone_no", "")
            result["contact_address"] = it.get("contact_address", "")
            result["ref"]             = it.get("bid_reference_no", "")
            result["deadline_time"]   = it.get("submission_deadline_time", "")
            result["project_id"]      = it.get("project_id", "")
            api_text = it.get("notice_text", "")
            if api_text:
                # L'API BM retourne le HTML complet (sans troncature)
                # → toujours re-parser depuis l'API pour avoir la description complète
                parsed2 = _parse_bm_notice_text(api_text)
                for k in ["description_section", "criteria_section", "full_text"]:
                    if parsed2.get(k):
                        result[k] = parsed2[k]
    except Exception as e:
        log.debug(f"BM API detail call failed for {notice_id}: {e}")

    return result


# ── PNUD ──────────────────────────────────────────────────────────────────────

def _fetch_pnud(lien: str) -> dict:
    if not lien:
        return {}

    result = {}
    try:
        soup = _soup(lien, timeout=35)

        for h6 in soup.find_all("h6"):
            label = h6.get_text(strip=True).lower()
            p = h6.find_next_sibling("p")
            val = p.get_text(strip=True) if p else ""
            if "procurement process" in label:
                result["type_detail"] = val
            elif "office" in label and "back" not in label:
                result["bureau"] = val
            elif "deadline" in label:
                result["deadline_detail"] = val
            elif "reference" in label:
                result["reference"] = val
            elif "published" in label:
                result["published_detail"] = val
            elif "overview" in label:
                result["description"] = (
                    p.get_text(separator="\n", strip=True) if p else ""
                )

        if not result.get("description"):
            for tag in soup.find_all(["h2", "h3"]):
                if "overview" in tag.get_text(strip=True).lower():
                    parts = []
                    for sib in tag.find_next_siblings():
                        if sib.name in ["h2", "h3", "h4"]:
                            break
                        t = sib.get_text(separator="\n", strip=True)
                        if t:
                            parts.append(t)
                    if parts:
                        result["description"] = "\n\n".join(parts)[:3000]
                    break

        docs = []
        for tag in soup.find_all(["h2", "h3", "h4"]):
            if "document" in tag.get_text(strip=True).lower():
                p = tag.find_next("p")
                if p:
                    for a in p.find_all("a"):
                        href = a.get("href", "")
                        if href and not href.startswith("http"):
                            href = "https://procurement-notices.undp.org/" + href.lstrip("/")
                        if href:
                            docs.append({"name": a.get_text(strip=True), "url": href})
                break
        result["documents"] = docs

        for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
            result["contact_email"] = a["href"].removeprefix("mailto:").strip()
            break

    except Exception as e:
        log.warning(f"PNUD detail scrape failed ({lien}): {e}")

    return result


# ── GIZ ───────────────────────────────────────────────────────────────────────

def _extract_giz_sections(text: str) -> tuple[str, str]:
    """
    Extrait les sections Tasks et Qualifications d'un ToR GIZ
    (documents généralement en anglais).
    """
    TASKS = [
        r"tasks?",
        r"scope\s+of\s+(?:work|services?)",
        r"services?\s+required",
        r"deliverables?",
        r"work\s+programme",
        r"responsibilities",
        r"activities",
        r"services\s+to\s+be\s+(?:provided|rendered)",
        r"description\s+of\s+(?:services?|tasks?|work)",
        r"background(?:/objectives?)?",
        r"objectives?(?:/background)?",
        r"specific\s+service\s+categories",
        r"categories?\s+and\s+related\s+tasks?",
        r"contexte?",
        r"introduction",
        r"pr[eé]sentation\b",
        r"objet\s+(?:du|de)\s+la\s+mission",
    ]
    QUALIF = [
        r"qualifications?",
        r"required\s+qualifications?",
        r"profil\s+(?:du|des?|requis|recherch[eé])",
        r"expert\s+profile",
        r"profil\s+de\s+l.expert",
        r"eligib",
        r"expertise\s+required",
        r"minimum\s+requirements?",
        r"selection\s+criteria",
        r"crit[eè]res?\s+de\s+s[eé]lection",
        r"crit[eè]res?\s+d[e'][\s\w]+[eé]valuation",
        r"key\s+experts?",
        r"team\s+(?:composition|requirements?)",
        r"professional\s+(?:experience|background)",
        r"exp[eé]riences?\s+et\s+comp[eé]tences?",
        r"comp[eé]tences?\s+requises?",
    ]
    STOP = [
        r"reporting",
        r"duration",
        r"payment",
        r"budget",
        r"timeline",
        r"schedule",
        r"submission",
        r"deadline",
        r"annex",
        r"appendix",
        r"confidential",
        r"tender\s+document",
        r"instructions?\s+(?:to|for)\s+(?:bidder|tenderer|consultant)",
        r"contract\s+(?:value|amount|type)",
    ]
    tasks = _extract_section(text, TASKS, QUALIF + STOP)
    qualif = _extract_section(text, QUALIF, STOP)
    return tasks, qualif


def _fetch_giz(lien: str) -> dict:
    """
    GIZ (ausschreibungen.giz.de / Cosinex) — accès public, sans login.

    Flux :
    1. GET page de l'avis (lien) → suit les redirects, récupère base_url + jsessionid
    2. Trouve l'onglet Teilnahmeunterlagen (href="./documents;jsessionid=...")
    3. GET la page des documents de participation (jsessionid CONSERVÉ dans l'URL)
    4. Cherche le fichier dont le nom contient "TOR" (Terms of Reference)
    5. Télécharge le PDF/Word → extrait Tasks + Qualifications
    """
    if not lien:
        return {}

    sess = _make_session()
    result: dict = {}

    try:
        # ── 1. Page principale de l'avis ──────────────────────────────────
        r = sess.get(lien, timeout=35, allow_redirects=True)
        r.raise_for_status()
        base_url = r.url  # URL finale après redirects (contient peut-être jsessionid)

        try:
            soup = BeautifulSoup(r.content, "lxml")
        except Exception:
            soup = BeautifulSoup(r.content, "html.parser")

        # ── 2. Métadonnées publiques (page Übersicht) ─────────────────────
        content = soup.find(id="projectRoomContent") or soup.body
        text = content.get_text(separator="\n", strip=True) if content else ""
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        _LABELS = {
            "ausschreibungs-id": "ausschreibungs_id",
            "vergabeart":        "procedure_type",
            "verfahrensart":     "procedure_type",
            "regelwerk":         "legal_framework",
            "status":            "status",
            "auftraggeber":      "contracting_auth",
        }
        for i, line in enumerate(lines):
            ll = line.lower()
            for label, key in _LABELS.items():
                if label in ll and i + 1 < len(lines):
                    result[key] = lines[i + 1]
                    break

        cpv_m = re.search(r'(\d{8}-\d)\s*[–-]\s*(.+)', text)
        if cpv_m:
            result["cpv_code"]  = cpv_m.group(1)
            result["cpv_label"] = cpv_m.group(2).strip()

        # ── 3. Onglet Teilnahmeunterlagen ─────────────────────────────────
        # Le jsessionid fait partie du path (Java EE URL rewriting) — ne pas le supprimer.
        tab_url: str | None = None

        for a in soup.find_all("a", href=True):
            href = a["href"]
            txt  = a.get_text(strip=True).lower()
            if (
                "teilnahmeunterlagen" in txt
                or "tender document" in txt
                or "participation document" in txt
                or "bid document" in txt
                or re.search(r'[/.]documents?(?:;|$)', href, re.I)
                or href.startswith("./documents")
            ):
                tab_url = urljoin(base_url, href) if not href.startswith("http") else href
                log.debug(f"GIZ: onglet documents → {tab_url}")
                break

        # Fallback : convention de l'URL de l'onglet
        if not tab_url:
            # Remplacer le dernier segment (/overview, /index, etc.) par /documents
            tab_url = re.sub(r'/[^/;?]*(?:;jsessionid=[^/?#]*)?$', '', base_url)
            tab_url = tab_url.rstrip("/") + "/documents"
            # Récupérer le jsessionid de base_url si présent
            jsid_m = re.search(r';jsessionid=([^/?#]+)', base_url)
            if jsid_m:
                tab_url += f";jsessionid={jsid_m.group(1)}"
            log.debug(f"GIZ: onglet fallback → {tab_url}")

        # ── 4. Page des documents de participation ─────────────────────────
        r2 = sess.get(tab_url, timeout=35, allow_redirects=True)
        r2.raise_for_status()

        try:
            soup2 = BeautifulSoup(r2.content, "lxml")
        except Exception:
            soup2 = BeautifulSoup(r2.content, "html.parser")

        # ── 5. Trouver le fichier TOR ──────────────────────────────────────
        # Priorité 1 : fichier dont le nom contient "TOR" / "terms of reference"
        # Priorité 2 : section "Leistungsbeschreibungen" (descriptions de services)
        # Priorité 3 : premier fichier PDF/Word disponible
        tor_url:      str | None = None
        fallback_url: str | None = None

        _DOC_EXTS = (".pdf", ".docx", ".doc")

        def _is_tor(name: str, path: str) -> bool:
            return (
                re.search(r'\btor\b', name, re.IGNORECASE)
                or re.search(r'\btor\b', path, re.IGNORECASE)
                or "terms of reference" in name.lower()
                or "terms_of_reference" in path.lower()
                or "term of reference" in name.lower()
            )

        def _name_from_row(elem) -> str:
            """Récupère le nom du fichier depuis la 1ère <td> du <tr> parent."""
            row = elem.find_parent("tr")
            if row:
                td = row.find("td")
                if td:
                    span = td.find("span", title=True)
                    if span and span.get("title"):
                        return span["title"]
                    return td.get_text(strip=True)
            return ""

        # Pattern 1 : <a href> avec extension de document (cas sans jsessionid)
        for a in soup2.find_all("a", href=True):
            href  = a["href"]
            name  = a.get_text(strip=True)
            hpath = href.split(";")[0].split("?")[0].lower()
            if not any(hpath.endswith(e) for e in _DOC_EXTS):
                continue
            abs_href = urljoin(r2.url, href) if not href.startswith("http") else href
            if _is_tor(name, hpath):
                tor_url = abs_href
                log.debug(f"GIZ: TOR trouvé (href) → {abs_href} ({name})")
                break
            elif fallback_url is None:
                fallback_url = abs_href

        # Pattern 2 : <span onclick> ou <button onclick> avec location.href='...'
        # (GIZ utilise des <span> cliquables, pas des <a>)
        if not tor_url:
            _ONCLICK_URL = re.compile(
                r"(?:window\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]",
                re.IGNORECASE,
            )
            # Conserver le contexte de la section courante pour détecter
            # la section "Leistungsbeschreibungen" (descriptions de services = TOR)
            leistung_section = False
            for node in soup2.find_all(True):
                # Détection des titres de section
                if node.name in ("div", "h2", "h3", "h4"):
                    txt_n = node.get_text(strip=True).lower()
                    if ("leistungsbeschreibung" in txt_n
                            or "service description" in txt_n
                            or "specification" in txt_n):
                        leistung_section = True
                    elif any(kw in txt_n for kw in (
                        "auszuf", "vertrag", "bekanntmachung", "anschreiben",
                        "cover letter", "correction",
                    )):
                        leistung_section = False

                onclick = node.get("onclick", "")
                if not onclick:
                    continue
                m = _ONCLICK_URL.search(onclick)
                if not m:
                    continue
                href  = m.group(1)
                hpath = href.split(";")[0].split("?")[0].lower()
                if not any(hpath.endswith(e) for e in _DOC_EXTS):
                    continue
                abs_href = urljoin(r2.url, href) if not href.startswith("http") else href
                # Nom : depuis le <tr> parent, ou depuis l'URL
                name = _name_from_row(node) or href.rsplit("/", 1)[-1].replace("+", " ")
                if _is_tor(name, hpath) or leistung_section:
                    tor_url = abs_href
                    log.debug(f"GIZ: TOR trouvé (onclick/section) → {abs_href} ({name})")
                    break
                elif fallback_url is None:
                    fallback_url = abs_href

        download_url = tor_url or fallback_url

        if not download_url:
            log.debug(f"GIZ: aucun document téléchargeable dans l'onglet ({tab_url})")
            return result

        # ── 6. Téléchargement ─────────────────────────────────────────────
        log.info(f"GIZ: téléchargement TOR → {download_url}")
        r3 = sess.get(download_url, timeout=90, allow_redirects=True)
        r3.raise_for_status()

        raw_text = _bytes_to_text(
            r3.content,
            r3.headers.get("content-type", ""),
            download_url,
        )

        if not raw_text:
            log.warning(f"GIZ: extraction texte impossible ({download_url})")
            return result

        log.debug(f"GIZ TOR: {len(raw_text)} car. extraits")

        # ── 7. Extraction Tasks + Qualifications ──────────────────────────
        tasks, qualif = _extract_giz_sections(raw_text)
        if tasks:
            result["description"] = tasks
        if qualif:
            result["criteria_section"] = qualif
        if not tasks and not qualif and raw_text:
            result["description"] = raw_text[:2500]

    except Exception as e:
        log.warning(f"GIZ detail scrape failed ({lien}): {e}")

    return result


# ── BERD ──────────────────────────────────────────────────────────────────────

def _fetch_berd(notice_id: str, lien: str) -> dict:
    """
    BERD : scrape ecepp.ebrd.com/delta/viewNotice.html

    Structure réelle de la page :
    • <table id="oppoverviewtable"> → <tr><td><strong>Label:</strong></td><td>Value</td></tr>
    • <div class="completeentry">   → <p> avec <b> comme titres de sections
    """
    display_id = notice_id.removeprefix("BERD-")
    url = f"https://ecepp.ebrd.com/delta/viewNotice.html?displayNoticeId={display_id}"
    if lien and "viewNotice" in lien:
        url = lien

    result = {}
    try:
        soup = _soup(url, timeout=40)

        # ── Table #oppoverviewtable : label (1ère <td>) / valeur (2ème <td>) ──
        _MAP = {
            "project name":            "project_name",
            "ebrd project id":         "ebrd_project_id",
            "client name":             "client_name",
            "procurement exercise":    "exercise_name",
            "type of procurement":     "procurement_type",
            "procurement method":      "procurement_method",
            "business sector":         "sector_detail",
            "notice type":             "notice_type_detail",
            "closing date":            "closing_date_detail",
            "publication date":        "publication_date_detail",
            "issue date":              "issue_date",
            "estimated value":         "estimated_value",
            "signed contract value":   "contract_value",
            "country":                 "country_detail",
            "name":                    "contact_name",
            "organisation":            "contact_org",
            "organization":            "contact_org",
            "address":                 "contact_address",
            "telephone":               "contact_phone",
            "phone":                   "contact_phone",
            "email":                   "contact_email",
            "e-mail":                  "contact_email",
        }

        ov_table = soup.find("table", id="oppoverviewtable")
        if ov_table:
            for row in ov_table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                # Le label est dans la 1ère <td> (souvent via <strong>)
                strong = cells[0].find("strong")
                label = (
                    strong.get_text(strip=True) if strong
                    else cells[0].get_text(strip=True)
                ).rstrip(":").strip().lower()
                # La valeur est dans la 2ème <td> (pas next_sibling de <strong> !)
                val = cells[1].get_text(separator=" ", strip=True)
                if not label or not val:
                    continue
                for key, field in _MAP.items():
                    if key in label:
                        result[field] = val
                        break
        else:
            # Fallback : chercher dans n'importe quelle table
            for row in soup.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                strong = cells[0].find("strong")
                label = (
                    strong.get_text(strip=True) if strong
                    else cells[0].get_text(strip=True)
                ).rstrip(":").strip().lower()
                val = cells[1].get_text(separator=" ", strip=True)
                if not label or not val:
                    continue
                for key, field in _MAP.items():
                    if key in label:
                        result.setdefault(field, val)
                        break

        # ── div.completeentry : contenu narratif (sections <b>) ───────────
        completeentry = soup.find("div", class_="completeentry")
        if completeentry:
            p = completeentry.find("p")
            if p:
                # Itérer sur les enfants DIRECTS de <p> — .descendants duplique
                # les nœuds texte internes des <b>, causant du texte manquant.
                sections: dict[str, str] = {}
                current_key: str | None = None
                current_parts: list[str] = []

                for child in p.children:
                    if hasattr(child, "name"):
                        if child.name == "b":
                            # Nouvelle section → sauvegarder la précédente
                            if current_key is not None:
                                sections[current_key] = "".join(current_parts).strip()
                            current_key = child.get_text(strip=True)
                            current_parts = []
                        elif child.name == "br":
                            current_parts.append("\n")
                        elif child.name == "a":
                            href = child.get("href", "")
                            txt  = child.get_text(strip=True)
                            if href and href.startswith("http"):
                                current_parts.append(f"{txt} ({href})" if txt else href)
                            elif txt:
                                current_parts.append(txt)
                        else:
                            t = child.get_text(separator=" ", strip=True)
                            if t:
                                current_parts.append(t)
                    else:
                        # NavigableString — texte brut entre les tags
                        t = str(child)
                        if t.strip():
                            current_parts.append(t)

                # Sauvegarder la dernière section
                if current_key is not None:
                    sections[current_key] = "".join(current_parts).strip()

                # ── Contact : section "4. Client Address" ─────────────────
                for k, v in sections.items():
                    if "client address" in k.lower() or (
                        "contact" in k.lower() and "@" in v
                    ):
                        if not v:
                            continue
                        lines_addr = [l.strip() for l in v.splitlines() if l.strip()]
                        # Téléphone
                        ph_m = re.search(r'[Tt]el\.?\s*([+\d\s\-().]+)', v)
                        if ph_m:
                            result.setdefault("contact_phone", ph_m.group(1).strip())
                        # Email
                        em_m = re.search(
                            r'[Ee]-?mail:\s*([\w.+-]+@[\w.-]+\.[a-zA-Z]{2,})', v
                        )
                        if em_m:
                            result.setdefault("contact_email", em_m.group(1).strip())

                        # Nom : 1ère ligne courte non-disclaimer (< 80 chars,
                        # n'est pas un avertissement "for information only")
                        _DISCLAIMER = re.compile(
                            r'information only|register interest|message centre|'
                            r'for information|documentation using|communication with',
                            re.I,
                        )
                        name_idx = None
                        for idx_l, l in enumerate(lines_addr):
                            if len(l) < 80 and not _DISCLAIMER.search(l):
                                name_idx = idx_l
                                break
                        if name_idx is not None:
                            result.setdefault("contact_name", lines_addr[name_idx])
                            # Organisation : ligne suivante si pas numérique
                            if (name_idx + 1 < len(lines_addr)
                                    and not lines_addr[name_idx + 1][:1].isdigit()):
                                result.setdefault("contact_org",
                                                  lines_addr[name_idx + 1])
                            # Adresse physique : lignes suivantes jusqu'à Tel./Email
                            addr_lines = []
                            for l in lines_addr[name_idx + 2:]:
                                if re.match(r'Tel\.?|Phone:|E?-?mail:', l, re.I):
                                    break
                                addr_lines.append(l)
                            if addr_lines:
                                result.setdefault("contact_address",
                                                  ", ".join(addr_lines))
                        break

                # ── Description : sections narratives (hors contact) ───────
                desc_parts = []
                for k, v in sections.items():
                    k_low = k.lower()
                    if "client address" in k_low or "contact" in k_low:
                        continue
                    v_clean = re.sub(r'\n{3,}', '\n\n', v).strip()
                    if v_clean:
                        desc_parts.append(f"{k}\n{v_clean}")
                if desc_parts:
                    full_desc = "\n\n".join(desc_parts)[:3000]
                    result.setdefault("description", full_desc)

    except Exception as e:
        log.warning(f"BERD detail scrape failed for {notice_id}: {e}")

    return result


# ── AFD ───────────────────────────────────────────────────────────────────────

def _fetch_afd(notice_id: str, lien: str) -> dict:
    """
    AFD : visite la page de l'AO (afd.dgmarket.com/tender/{id}) avec la session
    qui bénéficie du patch DNS hardcodé (52.202.8.86), télécharge le document
    attaché (PDF ou Word) et en extrait contexte et critères de sélection.
    """
    if not lien:
        return {}

    sess = _make_session()
    result: dict = {}

    try:
        # ── 1. Visiter la page de l'AO ────────────────────────────────────
        log.info(f"AFD detail: fetch page {lien}")
        r = _doh_get(sess, lien, timeout=40, allow_redirects=True)
        r.raise_for_status()

        try:
            soup = BeautifulSoup(r.content, "lxml")
        except Exception:
            soup = BeautifulSoup(r.content, "html.parser")

        # ── 2. Métadonnées de la page (tableau label/valeur) ──────────────
        _META_MAP = {
            "pays":             "country_detail",
            "country":          "country_detail",
            "date limite":      "deadline_detail",
            "deadline":         "deadline_detail",
            "financement":      "funding_agencies",
            "funding":          "funding_agencies",
            "acheteur":         "buyer",
            "buyer":            "buyer",
            "méthode":          "procurement_method",
            "method":           "procurement_method",
            "type":             "notice_type_detail",
            "valeur":           "estimated_value",
            "value":            "estimated_value",
            "référence":        "ref",
            "reference":        "ref",
            "email":            "contact_email",
            "contact":          "contact_name",
        }
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower()
                val   = cells[1].get_text(separator=" ", strip=True)
                if label and val:
                    for key, field in _META_MAP.items():
                        if key in label:
                            result.setdefault(field, val)
                            break

        # ── 3. Trouver le lien / bouton de téléchargement ────────────────
        download_url: str | None = None
        doc_name = "Document"

        def _abs(href: str) -> str:
            return urljoin(lien, href) if not href.startswith("http") else href

        # 3a. <a href="..."> avec texte ou extension de document
        for a in soup.find_all("a"):
            href    = a.get("href", "")
            txt     = a.get_text(strip=True).lower()
            onclick = a.get("onclick", "")
            if href and href not in ("#", "") and not href.startswith("javascript"):
                h_low = href.lower().split("?")[0].split(";")[0]
                if (
                    any(h_low.endswith(ext) for ext in (".pdf", ".docx", ".doc"))
                    or "download" in h_low
                    or "getdocument" in h_low
                    or "noticepdf" in h_low
                    or "télécharger" in txt
                    or "download" in txt
                ):
                    download_url = _abs(href)
                    doc_name = a.get_text(strip=True) or "Document"
                    break
            # onclick="window.open('/path/to/doc')" ou "location.href='...'"
            if onclick and not download_url:
                m = re.search(
                    r"(?:window\.open|location(?:\.href)?)\s*[=(]\s*['\"]([^'\"]+)['\"]",
                    onclick,
                )
                if m:
                    candidate = _abs(m.group(1))
                    c_low = candidate.lower().split("?")[0]
                    if any(c_low.endswith(ext) for ext in (".pdf", ".docx", ".doc")) \
                            or "download" in c_low or "document" in c_low:
                        download_url = candidate
                        doc_name = a.get_text(strip=True) or "Document"
                        break

        # 3b. <button> ou <input type="button|submit"> avec texte download
        if not download_url:
            for btn in soup.find_all(["button", "input"]):
                txt = (btn.get_text(strip=True) + " " + btn.get("value", "")).lower()
                if "télécharger" in txt or "download" in txt:
                    form = btn.find_parent("form")
                    if form:
                        action = form.get("action", "")
                        if action:
                            download_url = _abs(action)
                            doc_name = btn.get_text(strip=True) or btn.get("value", "Document")
                            break

        # 3c. <form> POST directement (ex. dgmarket downloadNotice.do)
        if not download_url:
            for form in soup.find_all("form"):
                form_txt = form.get_text(strip=True).lower()
                if "télécharger" in form_txt or "download" in form_txt:
                    action = form.get("action", "")
                    if action:
                        download_url = _abs(action)
                        doc_name = "Notice AFD"
                        break

        if not download_url:
            log.debug(f"AFD: aucun lien document trouvé sur {lien}")
            return result

        # ── 4. Télécharger le document ─────────────────────────────────────
        log.info(f"AFD: téléchargement → {download_url}")

        # POST seulement pour les endpoints de téléchargement direct connus
        # (évite de tagger biddingDocumentsList.do comme un form POST)
        is_form_post = any(
            kw in download_url.lower()
            for kw in ("downloadnotice", "getdocument", "noticepdf")
        )

        raw_text = ""
        try:
            if is_form_post:
                # Récupérer les champs cachés du formulaire parent
                form = soup.find("form", action=re.compile(
                    re.escape(
                        download_url.split("//", 1)[-1].split("/", 1)[-1].split("?")[0]
                    ),
                    re.IGNORECASE,
                ))
                post_data: dict = {}
                if form:
                    for inp in form.find_all("input", {"type": "hidden"}):
                        if inp.get("name"):
                            post_data[inp["name"]] = inp.get("value", "")
                r2 = sess.post(download_url, data=post_data,
                               timeout=90, allow_redirects=True)
            else:
                r2 = _doh_get(sess, download_url, timeout=90, allow_redirects=True)

            r2.raise_for_status()
            content_type = r2.headers.get("content-type", "")

            # Si la réponse est HTML (page de login, liste de docs, etc.)
            # → pas de texte extractable : traiter comme lien cliquable
            if "text/html" in content_type.lower():
                log.info(f"AFD: réponse HTML (login ou liste) — ajouté comme lien")
                result["documents"] = [{"name": doc_name or "Notice AFD",
                                        "url": download_url}]
                return result

            raw_text = _bytes_to_text(r2.content, content_type, download_url)

        except Exception as dl_err:
            log.warning(f"AFD: échec téléchargement {download_url} — {dl_err}")
            # Fallback : toujours fournir le lien cliquable
            result["documents"] = [{"name": doc_name or "Notice AFD",
                                    "url": download_url}]
            return result

        # ── 5. PDF image (non lisible) → afficher comme lien cliquable ────
        if not raw_text:
            log.info(f"AFD: document non extractible (image PDF?) — ajouté comme lien")
            result["documents"] = [{"name": doc_name or "Notice AFD",
                                    "url": download_url}]
            return result

        log.debug(f"AFD: texte extrait ({len(raw_text)} car.)")

        # ── 6. Extraire mission et critères ───────────────────────────────
        context, criteria = _extract_context_criteria(raw_text)
        if context:
            result["description"] = context
        if criteria:
            result["criteria_section"] = criteria
        if not context and not criteria:
            result["description"] = raw_text[:2500]

    except Exception as e:
        log.warning(f"AFD detail fetch failed for {notice_id} ({lien}): {e}")

    return result


# ── Dispatcher public ──────────────────────────────────────────────────────────

def fetch_detail(notice_id: str, lien: str, bailleur: str,
                 texte_complet: str = "") -> dict:
    """
    Dispatch vers le parser du bailleur approprié.
    Retourne un dict de champs enrichis (pas les champs déjà en base).
    """
    try:
        if bailleur == "Banque Mondiale":
            return _fetch_bm(notice_id, texte_complet)
        elif bailleur == "PNUD":
            return _fetch_pnud(lien)
        elif bailleur == "GIZ":
            return _fetch_giz(lien)
        elif bailleur == "BERD":
            return _fetch_berd(notice_id, lien)
        elif bailleur == "AFD":
            return _fetch_afd(notice_id, lien)
        else:
            return {"error": f"Bailleur inconnu : {bailleur}"}
    except Exception as e:
        log.error(f"fetch_detail exception for {notice_id}: {e}")
        return {"error": str(e)}
