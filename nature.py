"""Nature du marché : prestation intellectuelle, travaux, fournitures, autres, à déterminer.

Calculée à la volée quand la liste des avis est servie (database.get_notices) :
aucune colonne en base, et une règle corrigée ici s'applique aussitôt à tous les
avis, anciens compris.

Ordre de décision :
  1. indice propre à la source (type d'avis, mode de sélection, convention de titre) ;
  2. à défaut, mots-clés du titre et de la ligne projet ;
  3. sinon « À déterminer » (affiché par défaut : on ne cache que ce qui est sûr).
"""
import re

PI = "Prestation intellectuelle"
TRAVAUX = "Travaux"
FOURNITURES = "Fournitures"
AUTRES = "Autres"            # services courants, emplois, avis de résultats
INDETERMINE = "À déterminer"
NATURES = (PI, INDETERMINE, TRAVAUX, FOURNITURES, AUTRES)


def _rx(*words: str) -> "re.Pattern":
    return re.compile(r"\b(?:" + "|".join(words) + r")", re.IGNORECASE)


# Avis de résultats ou d'attribution : pas une opportunité.
_RESULTS = _rx(r"(?:avis de )?publication (?:de |des |of )(?:bid )?(?:evaluation )?r[ée]sult",
               r"r[ée]sultats? de l.(?:avis|appel)", r"(?:bid )?evaluation results",
               r"announcement of (?:bid )?evaluation", r"contract award", r"avis d.attribution")

# Conception-réalisation : des travaux, même si le mot « design » apparaît.
_DESIGN_BUILD = _rx(r"design[\s/&-]*(?:and\s*)?(?:build|construct)", r"conception[\s-]*réalisation",
                    r"turnkey", r"clé en main", r"EPC\b", r"engineering,\s*procurement")

# Prestation intellectuelle : signaux forts.
_PI_STRONG = _rx(
    r"consult", r"study\b", r"studies", r"étude", r"estudio", r"feasibility", r"faisabilit",
    r"factibilidad", r"viabilidad", r"supervision", r"supervisión", r"surveillance des travaux",
    r"contrôle des travaux", r"maîtrise d.œuvre", r"maitrise d.oeuvre", r"assistance à maîtrise",
    r"AMO\b", r"owner.s engineer", r"technical assistance", r"assistance technique",
    r"asistencia técnica", r"acompañamiento técnico", r"audit\b", r"audits\b", r"evaluation",
    r"évaluation", r"evaluación", r"advisory", r"conseil", r"diagnostic", r"assessment",
    r"expert", r"engineering services", r"ingénierie", r"master ?plan", r"schéma directeur",
    r"detailed design", r"detailed project report", r"preliminary design", r"avant-projet",
    r"terms of reference", r"twinning", r"capacity (?:building|development|works)",
    r"renforcement des capacités", r"implementation consult", r"project management unit",
    r"monitoring and evaluation", r"suivi[- ]évaluation",
)

# Services courants : ni PI, ni travaux, ni fournitures.
_OTHER_SERVICES = _rx(
    r"event", r"événement", r"catering", r"restauration", r"cleaning", r"nettoyage",
    r"security (?:guard|services)", r"gardiennage", r"freight", r"logistics", r"logistique",
    r"customs clearance", r"dédouanement", r"travel", r"voyage", r"hotel", r"accommodation",
    r"insurance", r"assurance\b", r"printing", r"impression", r"rental", r"location de",
    r"leasing", r"lease of", r"office space", r"courier", r"transportation", r"transport services",
    r"cargo", r"trucking", r"car hire", r"vehicle hire", r"maintenance services",
    r"waste collection services", r"job placement", r"advertis", r"publicidad", r"marketing agency",
)

# Fournitures.
_GOODS = _rx(
    r"supply", r"supplies", r"fourniture", r"suministro", r"procurement of", r"purchase",
    r"acquisition", r"adquisición", r"equipment", r"équipement", r"equipo", r"machinery",
    r"vehicle", r"véhicule", r"vehículo", r"computer", r"laptop", r"server", r"ICT equipment",
    r"furniture", r"mobilier", r"medicines?", r"drugs", r"pharmaceutical",
    r"medical (?:devices|consumables)", r"reagents", r"spare parts", r"pièces de rechange",
    r"meters\b", r"compteurs", r"containers", r"tools\b", r"materials\b", r"matériel",
    r"fuel\b", r"carburant", r"uniforms", r"textbooks", r"generators?", r"generatoren",
    r"heat pumps?", r"wärmepumpe", r"anhänger", r"fahrzeug", r"lieferung", r"bandwidth",
)

# Travaux.
_WORKS = _rx(
    r"construction", r"construcción", r"works\b", r"travaux", r"obras", r"rehabilitation",
    r"réhabilitation", r"rehabilitación", r"réfection", r"génie civil", r"building of",
    r"extension of", r"upgrading of", r"renovation", r"rénovation", r"installation", r"bau\b",
    r"substation", r"sous-station", r"retrofitting",
)

# Signaux faibles de PI : en dernier recours seulement.
_PI_WEAK = _rx(r"design", r"conception", r"training", r"formation", r"mapping", r"cartographi",
               r"survey", r"relevés", r"research", r"recherche", r"plan\b", r"strategy",
               r"stratégie", r"review", r"recrutement d.un")


def _by_keywords(text: str) -> str:
    # « Bid Evaluation Results » ne doit pas compter comme le mot-clé PI « evaluation ».
    if _RESULTS.search(text) and not _PI_STRONG.search(_RESULTS.sub(" ", text)):
        return AUTRES
    if _DESIGN_BUILD.search(text):
        return TRAVAUX
    if _PI_STRONG.search(text):
        return PI
    if _OTHER_SERVICES.search(text):
        return AUTRES
    if _GOODS.search(text):
        return FOURNITURES
    if _WORKS.search(text):
        return TRAVAUX
    if _PI_WEAK.search(text):
        return PI
    return INDETERMINE


def _by_text(n: dict) -> str:
    """Le titre décide ; la ligne projet (nom du programme, agence, résumé) ne sert
    que si le titre ne dit rien — « Urban Water Supply Project » ne fait pas d'une
    étude une fourniture."""
    kw = _by_keywords(n.get("titre") or "")
    return kw if kw != INDETERMINE else _by_keywords(n.get("projet") or "")


def _probably_pi(n: dict) -> str:
    """Type d'avis qui suggère de la PI : les mots-clés ne peuvent que le contredire
    (travaux, fournitures, autres) ; sinon c'est de la PI."""
    kw = _by_text(n)
    return kw if kw in (TRAVAUX, FOURNITURES, AUTRES) else PI


# ── Indices propres aux sources ──────────────────────────────────────────────

_KFW_PI = re.compile(r"^\s*(?:consult|implementation consult|engineering services|feasibility|"
                     r"twinning|technical assistance)", re.IGNORECASE)
_KFW_WORKS = re.compile(r"^\s*(?:construction|civil|design\s*(?:/|and|&)\s*construction|bau\b)",
                        re.IGNORECASE)

_BM_CONSULTANT_METHODS = ("quality and cost", "quality based", "consultant qualification",
                          "consultant's qualification", "least cost", "fixed budget",
                          "individual consultant", "single source selection of consult")

_EF_JOBS = {"CDD", "CDDU", "CDI", "Stage", "V.I.A", "VIA", "Alternance"}


def _by_source(n: dict) -> "str | None":
    bailleur = n.get("bailleur") or ""
    type_avis = (n.get("type_avis") or "").strip()
    titre = n.get("titre") or ""

    if bailleur == "KfW":
        # Convention des titres KfW : « Consulting, … », « Construction, … ».
        if _KFW_PI.search(titre):
            return PI
        if _KFW_WORKS.search(titre):
            return TRAVAUX
        return None

    if bailleur == "Banque Mondiale":
        mode = (n.get("mode_selection") or "").lower()
        if "expression of interest" in type_avis.lower() or any(m in mode for m in _BM_CONSULTANT_METHODS):
            return PI
        return None   # appels d'offres : travaux, fournitures ou services → mots-clés

    if bailleur == "GIZ":
        if type_avis.startswith("VOB"):
            return TRAVAUX
        if type_avis.startswith("VgV"):   # VgV : surtout du conseil, parfois des achats
            return _probably_pi(n)
        return None   # UVgO (seuil bas) : fournitures ou services → mots-clés

    if bailleur in ("UNGM", "PNUD"):
        if type_avis in ("EOI", "REOI"):  # AMI : surtout du conseil, parfois fret, location…
            return _probably_pi(n)
        if type_avis in ("ITB", "RFQ"):
            kw = _by_text(n)
            return kw if kw != PI else INDETERMINE
        return None

    if bailleur == "TED":
        return PI   # la requête TED ne retient que des services d'architecture/ingénierie

    if bailleur == "Expertise France":
        if type_avis in _EF_JOBS:
            return AUTRES
        if type_avis == "Contrat de prestation de services":
            return PI
        return None

    return None


def nature_of(n: dict) -> str:
    return _by_source(n) or _by_text(n)
