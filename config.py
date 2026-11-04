import os
import shutil

# ── Emplacement de la base de données ─────────────────────────────────────────
# IMPORTANT : la base SQLite ne DOIT PAS vivre sur Google Drive (disque G:).
# Le disque virtuel Google Drive ne gère pas le verrouillage de fichiers requis
# par SQLite -> "Accès refusé", base verrouillée, corruption, erreurs 500.
# On la place donc dans un dossier LOCAL de la machine, hors de toute synchro.
_HERE = os.path.dirname(os.path.abspath(__file__))
_OLD_DB = os.path.join(_HERE, "ao_database.db")  # ancienne base sur le Drive

_LOCAL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "VeilleAO"
)
os.makedirs(_LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(_LOCAL_DIR, "ao_database.db")

# Migration unique : si une base existe encore sur le Drive et qu'aucune base
# locale n'a encore été créée, on la recopie — MAIS seulement si elle est saine.
# (Recopier une base corrompue ne ferait que propager le problème.)
def _is_valid_sqlite(path: str) -> bool:
    import sqlite3
    c = None
    try:
        c = sqlite3.connect(path, timeout=5)
        row = c.execute("PRAGMA quick_check").fetchone()
        c.execute("SELECT name FROM sqlite_master LIMIT 1")
        return bool(row) and row[0] == "ok"
    except Exception:
        return False
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass


if os.path.exists(_OLD_DB) and not os.path.exists(DB_PATH):
    if _is_valid_sqlite(_OLD_DB):
        try:
            shutil.copy2(_OLD_DB, DB_PATH)
        except Exception:
            pass
    # sinon : ancienne base corrompue -> on repart d'une base vierge

# Membres de l'équipe — modifier cette liste pour ajouter / retirer des collègues
TEAM_MEMBERS = [
    {"name": "Chaim",      "email": ""},
    {"name": "Collègue 1", "email": ""},
    {"name": "Collègue 2", "email": ""},
]
SCHEDULER_TIME = "07:00"

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
