"""Notification des admins pour les demandes d'accès, avec plafond horaire.

Appelé par la tâche planifiée (toutes les 10 min) et, au mieux, à la première
connexion d'une personne. Fonctionnement :
  * au plus config.ACCESS_MAILS_PER_HOUR emails envoyés par heure ;
  * chaque email liste TOUTES les demandes pas encore signalées ;
  * si le plafond est atteint, les demandes restent en file (admin_notified_at
    vide) et partent groupées au premier passage suivant où le plafond le permet ;
  * en cas d'échec SMTP, rien n'est marqué : nouvel essai au passage suivant.
"""
import logging
from datetime import datetime, timedelta, timezone

import config
import mailer
import supa
from supa import in_

log = logging.getLogger(__name__)


def notify_access_requests(sb: "supa.Client | None" = None) -> dict:
    if not mailer.smtp_configured():
        return {"sent": False, "reason": "smtp_non_configure"}
    sb = sb or supa.service()

    pending = sb.select("veilleao_profiles",
                        {"select": "id,email,full_name,requested_at", "status": "eq.pending",
                         "admin_notified_at": "is.null", "order": "requested_at.asc"})
    if not pending:
        return {"sent": False, "reason": "aucune_demande"}

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    sent_last_hour = len(sb.select("veilleao_mail_log",
                                   {"select": "id", "kind": "eq.access_request",
                                    "error": "is.null", "sent_at": f"gte.{since}"}))
    if sent_last_hour >= config.ACCESS_MAILS_PER_HOUR:
        return {"sent": False, "reason": "plafond_horaire_atteint", "en_file": len(pending)}

    admins = [a["email"] for a in sb.select(
        "veilleao_profiles", {"select": "email", "role": "eq.admin", "status": "eq.approved"})]
    if not admins:
        return {"sent": False, "reason": "aucun_admin", "en_file": len(pending)}

    ids = [p["id"] for p in pending]
    subject, text, html_body = mailer.access_requests_email(pending)
    try:
        mailer.send(admins, subject, text, html_body)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:500]
        log.warning(f"Email demandes d'accès non envoyé : {msg}")
        sb.insert("veilleao_mail_log", [{"recipients": admins, "profile_ids": ids, "error": msg}])
        return {"sent": False, "reason": "erreur_smtp", "error": msg, "en_file": len(pending)}

    sb.insert("veilleao_mail_log", [{"recipients": admins, "profile_ids": ids}])
    sb.update("veilleao_profiles", {"id": in_(ids)},
              {"admin_notified_at": datetime.now(timezone.utc).isoformat()})
    return {"sent": True, "demandes": len(ids), "destinataires": len(admins)}
