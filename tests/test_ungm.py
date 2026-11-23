"""Source UNGM : lecture du fragment HTML renvoyé par /Public/Notice/Search."""
from sources import ungm


def _row(notice_id, titre, agence, type_label, pays, ref="REF-1"):
    # Reprend la structure réelle d'une ligne (allégée des boutons et icônes).
    return f'''
<div role="row" tabindex="0" data-noticeid="{notice_id}" class="tableRow dataRow notice-table">
    <div role="cell" class="tableCell editable intendbuttonsHeader resultOptions"><input type="button" value="Express Interest" /></div>
    <div role="cell" class="tableCell resultTitle">
        <span class="ungm-title ungm-title--small">
            {titre}
        </span>
        <a target='_blank' href='/Public/Notice/{notice_id}'></a>
    </div>
    <div role="cell" class="tableCell resultInfo1 deadline" data-description="Deadline">
        <span>
23-Dec-2026 15:00
            (GMT 3.00)
        </span>
        <span class="remainingDaysToDeadline" style="display:none">29.8</span>
    </div>
    <div role="cell" class="tableCell">
        <span>

23-Nov-2026</span>
    </div>
    <div role="cell" class="tableCell resultAgency">
        <span>{agence}</span>
    </div>
    <div role="cell" class="tableCell">
        <span><label for='{type_label}'>{type_label}</label></span>
    </div>
    <div role="cell" class="tableCell resultInfo1" data-description="Reference">
        <span>{ref}</span>
    </div>
    <div role="cell" class="tableCell">
        <span>{pays}</span>
    </div>
</div>
<script>var x = 1;</script>'''


FRAGMENT = (
    _row("101", "Design and supervision of school rehabilitation works", "UNICEF", "Request for proposal", "Senegal")
    + _row("102", "Expression of interest &amp; structural assessment", "UNOPS", "Request for EOI", "Haiti")
    + _row("103", "Consultancy services", "UNDP", "Request for proposal", "Viet Nam")
    + _row("104", "Supply of vehicles", "WFP", "Request for quotation", "Chad")
)


def test_lecture_des_lignes():
    rows = ungm.parse_rows(FRAGMENT)
    assert [r["id"] for r in rows] == ["101", "102", "103", "104"]
    r = rows[0]
    assert r["titre"] == "Design and supervision of school rehabilitation works"
    assert r["agence"] == "UNICEF" and r["type"] == "Request for proposal"
    assert r["reference"] == "REF-1"
    assert r["plain"][0] == "23-Nov-2026" and r["plain"][-1] == "Senegal"


def test_conversion_en_avis():
    notices = [n for n in map(ungm._to_notice, ungm.parse_rows(FRAGMENT)) if n]
    # PNUD écarté (déjà collecté par la source PNUD) et RFQ hors périmètre.
    assert [n["id"] for n in notices] == ["UNGM-101", "UNGM-102"]
    n = notices[0]
    assert n["bailleur"] == "UNGM" and n["projet"] == "UNICEF" and n["type_avis"] == "RFP"
    assert n["date_publication"] == "2026-11-23" and n["date_limite"] == "2026-12-23"
    assert n["pays"] == "Senegal"
    assert n["lien"] == "https://www.ungm.org/Public/Notice/101"
    assert notices[1]["type_avis"] == "EOI"
    assert notices[1]["titre"] == "Expression of interest & structural assessment"


def test_page_vide():
    assert ungm.parse_rows("\n\n") == []


def test_dates():
    assert ungm._date("05-Jan-2027 12:00 (GMT 1.00)") == "2027-01-05"
    assert ungm._date("") == ""


# ── Lecture incrémentale ─────────────────────────────────────────────────────

def test_date_de_depart_avec_marge():
    from datetime import date, datetime, timezone
    assert ungm.published_from(None) == ""
    assert ungm.published_from(datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)) == "22-Nov-2026"
    assert ungm.published_from(date(2027, 1, 1)) == "31-Dec-2026"
    assert ungm._search_body(0, "22-Nov-2026")["PublishedFrom"] == "22-Nov-2026"


def test_collecteur_transmet_la_derniere_lecture(monkeypatch):
    from datetime import datetime, timezone
    import collector, database
    last = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
    seen = {}
    monkeypatch.setattr(database, "last_success", lambda source, sb=None: seen.setdefault("source", source) and last)
    monkeypatch.setattr(database, "start_run", lambda source, by, sb=None: 1)
    monkeypatch.setattr(database, "finish_run", lambda *a, **k: None)
    monkeypatch.setattr(database, "save_notices", lambda notices, refresh=False, sb=None: len(notices))
    monkeypatch.setattr(database, "mark_expired", lambda sb=None: 0)
    monkeypatch.setattr(collector.supa, "service", lambda: None)
    monkeypatch.setattr(ungm, "fetch_ungm", lambda since=None: seen.update(since=since) or [])
    res = collector.run_source("ungm")
    assert res["ok"] and seen == {"source": "ungm", "since": last}


def test_titre_nettoye_des_lignes_decoratives():
    rows = ungm.parse_rows(_row("201", "RFP 12 " + "=" * 80 + " Office renovation", "WHO", "Request for proposal", "Kenya"))
    n = ungm._to_notice(rows[0])
    assert n["titre"] == "RFP 12 Office renovation"
