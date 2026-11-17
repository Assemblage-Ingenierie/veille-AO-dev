"""Configuration Veille AO.

Paramètres d'environnement et secrets : variables d'environnement (Vercel) ou, en
développement local, fichier `.env` à la racine (non versionné, voir .env.example).
Aucun effet de bord à l'import (pas d'écriture disque) : le système de fichiers
Vercel est en lecture seule.
"""
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str = os.path.join(_ROOT, ".env")) -> None:
    """Charge un .env local s'il existe, sans écraser les variables déjà définies."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    """Variable d'environnement nettoyée (espaces, retours ligne, BOM invisible
    qu'ajoutent certains outils lors de la saisie)."""
    return os.environ.get(name, default).strip().lstrip("﻿").strip()


# ── Supabase (projet partagé INTERNAL, tables veilleao_*) ────────────────────
SUPABASE_URL = _env("SUPABASE_URL", "https://hhkofvbptnrtwbazftlm.supabase.co")
# Clé publique (« publishable ») : conçue pour être exposée au navigateur ; avec le
# jeton de l'utilisateur, la sécurité de la base (RLS) s'applique.
SUPABASE_PUBLISHABLE_KEY = _env(
    "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_dKYZOMgAeeBsN7HTOpReOw_yeS--rED")
# Clé secrète : contourne la RLS. Backend uniquement (collecte, cache, emails système).
SUPABASE_SERVICE_ROLE_KEY = _env("SUPABASE_SERVICE_ROLE_KEY")

# Secret attendu (en-tête « Authorization: Bearer … ») sur les routes /api/cron/*.
CRON_SECRET = _env("CRON_SECRET")

# URL publique de l'application (liens dans les emails).
APP_URL = _env("APP_URL", "http://localhost:8000").rstrip("/")

# ── Emails (SMTP) ─────────────────────────────────────────────────────────────
SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_env("SMTP_PORT", "587"))
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_FROM = _env("SMTP_FROM") or SMTP_USER
# Plafond d'emails « demande d'accès » envoyés aux admins par heure ; au-delà, les
# demandes restent en file et partent groupées au passage suivant.
ACCESS_MAILS_PER_HOUR = int(_env("VEILLEAO_ACCESS_MAILS_PER_HOUR", "2"))

STRONG_KEYWORDS = [
    "feasibility", "feasibility study", "étude de faisabilité",
    "estudio de factibilidad", "estudo de viabilidade",
    "energy efficiency", "efficacité énergétique", "eficiencia energética",
    "green building", "bâtiment durable", "sustainable infrastructure",
    "low carbon", "climate resilient", "energy audit", "audit énergétique",
    "owner's representative", "project management consultant", "PMC",
    "maîtrise d'ouvrage", "AMO", "project preparation",
    "advisory services", "technical assistance", "asistencia técnica",
    "assistência técnica", "project development", "pre-feasibility",
]

INFRA_KEYWORDS = [
    "construction", "rehabilitation", "renovation", "rénovation",
    "building", "bâtiment", "infrastructure", "facility", "facilities",
    "school", "école", "escuela", "hospital", "clinic", "clinique",
    "health center", "health facility", "museum", "musée", "library",
    "bibliothèque", "community center", "university", "université",
    "solar", "renewable energy", "energy retrofit", "retrofit",
    "heating", "cooling", "hvac", "insulation",
]

TARGET_SECTORS = {
    "Éducation": ["education", "school", "université", "university", "training",
                  "vocational", "escuela", "école", "enseignement", "académique"],
    "Santé":     ["health", "hospital", "clinic", "medical", "santé", "clinique",
                  "sanitation", "hôpital", "healthcare"],
    "Culture":   ["culture", "museum", "library", "heritage", "community center",
                  "musée", "bibliothèque", "patrimoine", "cultural"],
}

EXCLUDE_KEYWORDS = [
    "digital", "digitalisation", "e-government", "software", "database",
    "finance", "financial", "fiscal", "taxation", "accounting", "banking",
    "amendment", "addendum", "corrigendum", "erratum",
]

ACCEPTED_LANGUAGES = {"EN": "Anglais", "FR": "Français", "ES": "Espagnol", "PT": "Portugais"}

UNDP_EXCLUDE_TYPES = ["RFI", "IC", "RFQ"]

# Keyword groups shown in the UI filter panel
KEYWORD_DISPLAY_GROUPS = [
    ["feasibility", "pre-feasibility", "energy efficiency", "AMO", "technical assistance"],
    ["school", "hospital", "rehabilitation", "green building", "construction"],
    ["energy audit", "solar", "renovation", "university", "community center"],
]

# ── TED (Tenders Electronic Daily) — Search API v3 ────────────────────────────
# Source publique sans authentification. La query "expert" ci-dessous est
# volontairement isolée pour être ajustée facilement.

TED_ENDPOINT = "https://api.ted.europa.eu/v3/notices/search"

# Query expert TED — modifier librement (voir doc : https://docs.ted.europa.eu).
TED_QUERY = (
    'contract-nature=services AND place-of-performance IN '
    '(DZA BEN BFA BDI CMR CAF TCD COM COG COD CIV DJI GAB GIN MDG MLI MAR MRT '
    'NER RWA SEN TGO TUN BWA ETH GMB GHA KEN LBR MWI NAM NGA SLE ZAF TZA UGA '
    'ZMB ZWE KHM IDN LAO MYS MMR PHL THA TLS VNM BRN ARG BOL BRA CHL COL ECU '
    'GUY PRY PER SUR URY VEN) AND (classification-cpv IN (71241000 71242000 '
    '71248000 71310000 71314300 71315200 71356200 71530000 71540000) '
    'OR FT ~ "programmation architecturale" '
    'OR FT ~ "programación arquitectónica" '
    'OR FT ~ "étude de faisabilité" '
    'OR FT ~ "feasibility study" '
    'OR FT ~ "estudio de factibilidad" '
    'OR FT ~ "estudio de viabilidad" '
    'OR FT ~ "assistance à maîtrise d\'ouvrage" '
    'OR FT ~ "owner\'s engineer" '
    'OR FT ~ "asistencia técnica" '
    'OR FT ~ "supervisión de obras" '
    'OR FT ~ "efficacité énergétique" '
    'OR FT ~ "energy efficiency" '
    'OR FT ~ "eficiencia energética")'
)

# Champs demandés à l'API (noms réels validés par appel de découverte).
TED_FIELDS = [
    "publication-number", "notice-title", "description-lot",
    "deadline-receipt-tender-date-lot", "publication-date", "notice-type",
    "place-of-performance", "classification-cpv", "buyer-name", "links",
]

TED_PAGE_LIMIT = 100      # avis par page (max API : 250)
TED_MAX_TOTAL  = 15000    # plafond API (garde-fou)
TED_TIMEOUT    = 30       # secondes

# Codes pays ISO-3166 alpha-3 → nom FR (pour la colonne `pays`).
TED_COUNTRY_NAMES = {
    "DZA": "Algérie", "BEN": "Bénin", "BFA": "Burkina Faso", "BDI": "Burundi",
    "CMR": "Cameroun", "CAF": "République centrafricaine", "TCD": "Tchad",
    "COM": "Comores", "COG": "Congo", "COD": "République démocratique du Congo",
    "CIV": "Côte d'Ivoire", "DJI": "Djibouti", "GAB": "Gabon", "GIN": "Guinée",
    "MDG": "Madagascar", "MLI": "Mali", "MAR": "Maroc", "MRT": "Mauritanie",
    "NER": "Niger", "RWA": "Rwanda", "SEN": "Sénégal", "TGO": "Togo",
    "TUN": "Tunisie", "BWA": "Botswana", "ETH": "Éthiopie", "GMB": "Gambie",
    "GHA": "Ghana", "KEN": "Kenya", "LBR": "Libéria", "MWI": "Malawi",
    "NAM": "Namibie", "NGA": "Nigéria", "SLE": "Sierra Leone",
    "ZAF": "Afrique du Sud", "TZA": "Tanzanie", "UGA": "Ouganda",
    "ZMB": "Zambie", "ZWE": "Zimbabwe",
    # Asie du Sud-Est
    "KHM": "Cambodge", "IDN": "Indonésie", "LAO": "Laos", "MYS": "Malaisie",
    "MMR": "Birmanie", "PHL": "Philippines", "THA": "Thaïlande",
    "TLS": "Timor oriental", "VNM": "Viêt Nam", "BRN": "Brunei",
    # Amérique latine
    "ARG": "Argentine", "BOL": "Bolivie", "BRA": "Brésil", "CHL": "Chili",
    "COL": "Colombie", "ECU": "Équateur", "GUY": "Guyana", "PRY": "Paraguay",
    "PER": "Pérou", "SUR": "Suriname", "URY": "Uruguay", "VEN": "Venezuela",
}

# ── Expertise France — plateforme gestmax (recrutement / missions) ─────────────
# Collecte de TOUTES les offres (tous domaines). Pour restreindre à un secteur,
# remplacer les deux URLs par la forme filtrée, ex. « Développement durable » :
#   EF_SEARCH_URL = f"{EF_BASE}/search/set-vacsearchfront_secteurs-046/domaines-d-expertises-developpement-durable-8"
#   EF_PAGE_URL   = f"{EF_BASE}/search/faceted/searchAction/set/field/vacsearchfront_secteurs/value/046/page/{{page}}"
EF_BASE = "https://expertise-france.gestmax.fr"
EF_SEARCH_URL = f"{EF_BASE}/search"
EF_PAGE_URL   = f"{EF_BASE}/search/index/page/{{page}}"
EF_TIMEOUT   = 30
EF_MAX_PAGES = 30  # garde-fou pagination (~12 pages pour 113 offres)

# Type de contrat coché par défaut dans l'UI (les autres sont décochés).
EF_DEFAULT_CONTRACT = "Contrat de prestation de services"
