"""Emails (SMTP configuré par variables d'environnement) — charte Assemblage ingénierie."""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import config

log = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def send(to, subject: str, text: str, html_body: str, timeout: int = 15) -> None:
    recipients = [to] if isinstance(to, str) else list(to)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=timeout) as server:
        server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.sendmail(config.SMTP_FROM, recipients, msg.as_string())
    log.info(f"Email « {subject} » envoyé à {len(recipients)} destinataire(s)")


def _layout(title: str, inner_html: str) -> str:
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:20px;background:#F2F2F2;font-family:'Open Sans',Arial,sans-serif">
<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)">
  <div style="background:#E30513;padding:16px 24px">
    <p style="color:#fff;margin:0;font-size:18px;font-weight:700">{escape(title)}</p>
    <p style="color:rgba(255,255,255,.85);margin:4px 0 0;font-size:12px">Veille AO — Assemblage ingénierie</p>
  </div>
  <div style="padding:24px">{inner_html}</div>
  <div style="background:#F2F2F2;padding:10px 24px;font-size:11px;color:#4D4D4D;border-top:1px solid #DFE4E8">
    Assemblage ingénierie · Plateforme de veille AO
  </div>
</div></body></html>"""


def _button(url: str, label: str) -> str:
    return (f'<a href="{escape(url)}" style="display:inline-block;background:#30323E;color:#fff;'
            f'padding:10px 22px;text-decoration:none;border-radius:4px;font-weight:600;'
            f'font-size:13px">{escape(label)}</a>')


def assignment_email(assignee_name: str, by_name: str, notice: dict) -> tuple:
    titre = notice.get("titre") or "—"
    bailleur = notice.get("bailleur") or "—"
    deadline = notice.get("date_limite") or "—"
    lien = notice.get("lien") or ""
    by_label = by_name or "Un collègue"
    url = f"{config.APP_URL}/pertinentes"

    subject = f"[Veille AO] Nouvel AO assigné : {titre[:70]}"
    text = "\n".join([
        f"Bonjour {assignee_name},", "",
        f"{by_label} vous a assigné un appel d'offres à analyser :", "",
        f"  Titre    : {titre}", f"  Bailleur : {bailleur}", f"  Deadline : {deadline}", "",
        f"Voir vos assignations : {url}",
        *( [f"Lien source : {lien}"] if lien else [] ),
        "", "—", "Plateforme de veille AO — Assemblage ingénierie"])
    source = (f'&nbsp;&nbsp;<a href="{escape(lien)}" style="color:#E30513;font-size:13px">'
              f"Voir l'avis source →</a>") if lien else ""
    inner = f"""
    <p style="margin:0 0 12px">Bonjour <strong>{escape(assignee_name)}</strong>,</p>
    <p style="margin:0 0 16px;color:#4D4D4D">{escape(by_label)} vous a assigné un appel d'offres à analyser :</p>
    <div style="background:#F9E1E3;border-left:4px solid #E30513;padding:14px 18px;margin-bottom:20px">
      <p style="margin:0 0 6px;font-weight:700;color:#000">{escape(titre)}</p>
      <p style="margin:0;font-size:13px;color:#4D4D4D">{escape(bailleur)} &nbsp;·&nbsp; Deadline : <strong>{escape(str(deadline))}</strong></p>
    </div>
    {_button(url, "Voir sur la plateforme →")}{source}"""
    return subject, text, _layout("Nouvel AO assigné", inner)


def access_requests_email(pending: list) -> tuple:
    """Un seul email listant toutes les demandes d'accès en attente."""
    n = len(pending)
    url = f"{config.APP_URL}/admin"
    subject = (f"[Veille AO] {n} demande{'s' if n > 1 else ''} d'accès en attente")
    lines = [f"  - {p.get('full_name') or p['email']} <{p['email']}> "
             f"(demandé le {(p.get('requested_at') or '')[:16].replace('T', ' ')})"
             for p in pending]
    text = "\n".join(["Bonjour,", "",
                      f"{n} personne{'s ont' if n > 1 else ' a'} demandé l'accès à Veille AO :",
                      *lines, "", f"Valider ou refuser les demandes : {url}", "",
                      "—", "Plateforme de veille AO — Assemblage ingénierie"])
    rows = "".join(
        f'<tr><td style="padding:6px 0;border-bottom:1px solid #DFE4E8">'
        f'<strong>{escape(p.get("full_name") or p["email"])}</strong><br>'
        f'<span style="font-size:12px;color:#4D4D4D">{escape(p["email"])} · demandé le '
        f'{escape((p.get("requested_at") or "")[:16].replace("T", " "))}</span></td></tr>'
        for p in pending)
    inner = f"""
    <p style="margin:0 0 12px">Bonjour,</p>
    <p style="margin:0 0 16px;color:#4D4D4D">{n} personne{"s ont" if n > 1 else " a"} demandé l'accès à Veille AO :</p>
    <table style="width:100%;border-collapse:collapse;margin-bottom:20px">{rows}</table>
    {_button(url, "Valider ou refuser les demandes →")}"""
    return subject, text, _layout("Demandes d'accès", inner)


def test_email() -> tuple:
    subject = "[Veille AO] Test de configuration email"
    text = ("Félicitations ! La configuration email de Veille AO fonctionne.\n\n"
            "— Plateforme de veille AO · Assemblage ingénierie")
    inner = ("<p>Félicitations ! La configuration email de Veille AO fonctionne.</p>"
             '<p style="color:#4D4D4D;font-size:13px">Les notifications (assignations, demandes '
             "d'accès) seront envoyées automatiquement.</p>")
    return subject, text, _layout("Test de configuration email ✓", inner)
