"""Scoring de pertinence — aucun filtrage, tous les AO sont retenus."""
from config import STRONG_KEYWORDS, INFRA_KEYWORDS, TARGET_SECTORS


def classify(titre: str, texte: str = "", secteur_hint: str = "") -> tuple[str, str]:
    """
    Toujours retourne (secteur, pertinence) — aucun AO n'est écarté ici.
    Le filtrage métier est entièrement délégué à l'interface web.

    Pertinence :
      "Haute"      → STRONG_KEYWORD + secteur cible identifié
      "Moyenne"    → STRONG_KEYWORD ou secteur cible (pas les deux)
      "À vérifier" → aucun signal fort détecté
    """
    combined = f"{titre} {texte} {secteur_hint}".lower()

    secteur = "Autre"
    for sec, kws in TARGET_SECTORS.items():
        if any(k.lower() in combined for k in kws):
            secteur = sec
            break

    has_strong = any(k.lower() in combined for k in STRONG_KEYWORDS)

    if has_strong and secteur != "Autre":
        pertinence = "Haute"
    elif has_strong or secteur != "Autre":
        pertinence = "Moyenne"
    else:
        pertinence = "À vérifier"

    return secteur, pertinence
