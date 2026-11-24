"""Source KfW (GTAI) : état de recherche et lecture de la page de résultats."""
import base64
import json
import zlib
from datetime import date

from sources import kfw


def _item(gid, titre, sous_type, deadline, published="Nov 24, 2026", pays="Pakistan",
          services="Detailed design and supervision"):
    sub = f"<p>\n{sous_type} </p>" if sous_type else ""
    dl = f"<p>\nDeadline:\n{deadline}</p>" if deadline else ""
    return f'''<li class="result-item">
<div class="overline"><div class="overline__left">
<span class="overline__text date" >
{published} </span><span class="overline__text">
Tender Notice </span>
<span class="overline__text">
{pays} </span>
<span class="overline__text">
Architecture, Engineering Services </span> </div></div>
<div class="content">
<a class="" href="/en/trade/{pays.lower()}/tenders/some-title-{gid}" > <h3 class=""> {titre}
</h3>
</a>{sub}{dl}<p>
Financing:
KfW Entwicklungsbank</p><p class="excerpt" >
Requested Services: <br><br>{services}
</p></div></li>
'''


PAGE = ('<div class="searchResults"><ul>'
        + _item("101", "Consulting Services, Detailed Project Reports (Sewerage)", "Prequalification", "Jan 14, 2027")
        + _item("102", "Construction, Wastewater Treatment Plant", "", "Dec 11, 2026")
        + _item("103", "Consulting, Land Registration", "Tender Award", "")
        + _item("104", "Consulting, Old Tender", "", "Nov 01, 2026")
        + _item("105", "Consulting, Implementation Consultancy", "Update", "Dec 20, 2026")
        + _item("106", "Consulting, Construction Supervision", "Expression of Interest/ Re-Tender", "Dec 20, 2026")
        + _item("107", "Construction, Substation", "Update/ PQ-Result", "Dec 20, 2026")
        + "</ul></div>")

TODAY = date(2026, 11, 24)


def test_etat_de_recherche():
    raw = kfw.search_state()
    state = json.loads(zlib.decompress(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))))
    assert state["facets"]["financer"]["selected"] == ["4486"]
    assert state["facets"]["rubric"]["selected"] == ["99662"]
    assert state["pageSize"] == 100 and state["sort"] == "dateDesc"


def test_lecture_page():
    items = kfw.parse_items(PAGE)
    assert [i["gtai_id"] for i in items] == ["101", "102", "103", "104", "105", "106", "107"]
    first = items[0]
    assert first["titre"] == "Consulting Services, Detailed Project Reports (Sewerage)"
    assert first["sous_type"] == "Prequalification"
    assert first["deadline"] == "2027-01-14" and first["published"] == "2026-11-24"
    assert first["pays"] == "Pakistan" and first["financement"] == "KfW Entwicklungsbank"
    assert first["services"] == "Detailed design and supervision"
    assert items[1]["sous_type"] == ""


def test_seuls_les_appels_ouverts_sont_retenus():
    kept = [i["gtai_id"] for i in kfw.parse_items(PAGE) if kfw.is_open_call(i, TODAY)]
    # 103 attribution, 104 échu, 105 rectificatif, 107 résultat de préqualification
    assert kept == ["101", "102", "106"]


def test_conversion():
    n = kfw.to_notice(kfw.parse_items(PAGE)[1])
    assert n["id"] == "KFW-102" and n["bailleur"] == "KfW"
    assert n["type_avis"] == "Tender Notice" and n["date_limite"] == "2026-12-11"
    assert n["lien"] == "https://www.gtai.de/en/trade/pakistan/tenders/some-title-102"
