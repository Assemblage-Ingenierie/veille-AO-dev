"""Nature du marché : cas réels relevés dans la base (titres raccourcis)."""
import pytest

from nature import AUTRES, FOURNITURES, INDETERMINE, PI, TRAVAUX, nature_of

CASES = [
    # KfW : convention de titre
    ({"bailleur": "KfW", "titre": "Consulting Services for Integrated Flood Risk Reduction Planning"}, PI),
    ({"bailleur": "KfW", "titre": "Consulting, Construction Supervision (Wastewater Collection Network)"}, PI),
    ({"bailleur": "KfW", "titre": "Construction, Renewable Energy Training Centre in Punjab, Lahore (TVET)"}, TRAVAUX),
    ({"bailleur": "KfW", "titre": "Design/Construction, Replacement of Pumping Equipment at Wastewater Pumping Stations"}, TRAVAUX),
    ({"bailleur": "KfW", "titre": "Equipment for Vocational Training Institutes (VTIs)"}, FOURNITURES),
    ({"bailleur": "KfW", "titre": "Servers and Data Storage Systems"}, FOURNITURES),
    ({"bailleur": "KfW", "titre": "Twinning Services (Health Care)"}, PI),
    ({"bailleur": "KfW", "titre": "Job Placement Services (Social Development)"}, AUTRES),
    ({"bailleur": "KfW", "titre": "Waste Collection Vehicles and Waste Containers"}, FOURNITURES),
    # Le titre l'emporte sur la ligne projet
    ({"bailleur": "KfW", "titre": "Supply of Laboratory Equipment", "projet": "Detailed design and supervision"}, FOURNITURES),
    ({"bailleur": "Banque Mondiale", "type_avis": "Invitation for Bids", "mode_selection": "Request for Bids",
      "titre": "Feasibility study of the sewerage network", "projet": "Urban Water Supply Strengthening Project"}, PI),
    ({"bailleur": "UNGM", "type_avis": "RFP", "titre": "Rapid AI-Evidence Review", "projet": "WHO"}, PI),
    # Banque mondiale
    ({"bailleur": "Banque Mondiale", "type_avis": "Request for Expression of Interest",
      "mode_selection": "Individual Consultant Selection", "titre": "Disbursement Specialist"}, PI),
    ({"bailleur": "Banque Mondiale", "type_avis": "Invitation for Bids", "mode_selection": "Request for Bids",
      "titre": "Construction Works for Batch II Water Pans within Garissa County."}, TRAVAUX),
    ({"bailleur": "Banque Mondiale", "type_avis": "Invitation for Bids", "mode_selection": "Request for Bids",
      "titre": "supply, delivery, installation, test and commissioning of pasteurizer"}, FOURNITURES),
    ({"bailleur": "Banque Mondiale", "type_avis": "Invitation for Bids", "mode_selection": "Request for Bids",
      "titre": "Rehabilitation of five water catchments in Kismayo district"}, TRAVAUX),
    # GIZ
    ({"bailleur": "GIZ", "type_avis": "VgV/Ausschreibung",
      "titre": "10041825 - Supporting GIZ in the development of Green Climate Fund Concept Notes"}, PI),
    ({"bailleur": "GIZ", "type_avis": "VgV/Ausschreibung", "titre": "Heat Pumps 50 KW el and 100 KW el"}, FOURNITURES),
    ({"bailleur": "GIZ", "type_avis": "VgV/Ausschreibung", "titre": "Generatoren 100 kVA"}, FOURNITURES),
    ({"bailleur": "GIZ", "type_avis": "UVgO/Ausschreibung", "titre": "10063054- Capacity Works (CW) Workshop"}, PI),
    ({"bailleur": "GIZ", "type_avis": "VOB/A/Ausschreibung", "titre": "Austausch Feuerwehrschlüsseldepot"}, TRAVAUX),
    # UNGM / PNUD
    ({"bailleur": "UNGM", "type_avis": "EOI", "titre": "FAOMZ - EXPRESSION OF INTEREST - Catering services"}, AUTRES),
    ({"bailleur": "UNGM", "type_avis": "EOI", "titre": "Lease of Office Space in Cali, Colombia"}, AUTRES),
    ({"bailleur": "UNGM", "type_avis": "EOI",
      "titre": "Provision of Air Transportation Services for the Movement of Cargo"}, AUTRES),
    ({"bailleur": "UNGM", "type_avis": "RFP",
      "titre": "LTA_ENGINEERING CONSULTANCY SERVICES FOR IOM-CSSM INFRASTURAL PROJECT ACTIVITIES"}, PI),
    ({"bailleur": "UNGM", "type_avis": "RFP",
      "titre": "UNICEF Moldova RFP 9207192, Construction Works, High School, Briceni."}, TRAVAUX),
    ({"bailleur": "UNGM", "type_avis": "RFP",
      "titre": "Establishment of Long-Term Arrangements for Event Management, Conference Management"}, AUTRES),
    ({"bailleur": "UNGM", "type_avis": "RFP",
      "titre": "LRFP-2026-9207041-Procurement of Grade 1 and Grade 2 Mathematics Textbooks"}, FOURNITURES),
    ({"bailleur": "PNUD", "type_avis": "OTHER", "titre": "Corporate Travel Services - LTA"}, AUTRES),
    ({"bailleur": "PNUD", "type_avis": "OTHER", "titre": "Structural Retrofitting Works for Three Hospitals"}, TRAVAUX),
    ({"bailleur": "PNUD", "type_avis": "OTHER",
      "titre": "Consultancy Services to develop Bankable Power Purchase Agreement Framework"}, PI),
    # AFD (mots-clés seuls)
    ({"bailleur": "AFD", "titre": "Package 1: Construction Design and Build of The Bitung Oceanic Fishing Port"}, TRAVAUX),
    ({"bailleur": "AFD", "titre": "Études socio-économiques et études techniques de 9 aménagements portuaires"}, PI),
    ({"bailleur": "AFD", "titre": "AVIS DE PUBLICATION DE RESULTATS OF 63/2026"}, AUTRES),
    ({"bailleur": "AFD", "titre": "Announcement of Bid Evaluation Results of Protection of Qianjiang River"}, AUTRES),
    ({"bailleur": "AFD", "titre": "The Supply and Delivery of HDPE Pipes and Fittings (Batch 3)"}, FOURNITURES),
    ({"bailleur": "AFD", "titre": "Travaux de finition du réseau d'irrigation"}, TRAVAUX),
    # Autres sources
    ({"bailleur": "TED", "type_avis": "cn-standard", "titre": "Services d'architecture"}, PI),
    ({"bailleur": "Expertise France", "type_avis": "CDD", "titre": "Chargé de projet"}, AUTRES),
    ({"bailleur": "Expertise France", "type_avis": "Contrat de prestation de services", "titre": "Mission"}, PI),
    ({"bailleur": "BERD", "type_avis": "General Procurement Notice", "titre": "Uzbekistan: Horezm Water"}, INDETERMINE),
]


@pytest.mark.parametrize("notice,expected", CASES, ids=[c[0]["titre"][:40] for c in CASES])
def test_nature(notice, expected):
    assert nature_of(notice) == expected
