"""
DNS fallback via DNS-over-HTTPS (DoH) + overrides codés en dur.

Certains réseaux (DNS d'entreprise, FAI) ne résolvent pas certains domaines
alors que le navigateur (Chrome/Edge, qui utilisent DoH par défaut) y accède.

Mécanisme :
1. La requête échoue avec une NameResolutionError  (ou l'hostname est dans
   _HARDCODED_DNS et on court-circuite directement)
2. On interroge https://dns.google/resolve (DoH) pour obtenir l'IP
3. On corrige socket.getaddrinfo (thread-safe via contextvars)
   pour que la 2e tentative réussisse — le hostname reste dans les headers TLS
   (SNI + vérification certificat) : aucun problème de sécurité.
"""
import contextvars
import logging
import socket as _socket

log = logging.getLogger(__name__)

# ── Overrides DNS permanents (domaines dont le DNS public est cassé) ──────────
# Ajouter ici les hostnames pour lesquels le DNS public renvoie NXDOMAIN
# alors que le serveur existe sur une IP connue (ex. : retrouvée via le cache
# Chrome → chrome://net-internals/#dns).
_HARDCODED_DNS: dict[str, str] = {
    "afd.dgmarket.com":  "52.202.8.86",  # AWS – IP récupérée depuis Chrome DNS cache
    "www2.dgmarket.com": "52.202.8.86",  # même serveur (redirect vers téléchargements AFD)
}

# ── Patch socket.getaddrinfo (appliqué une seule fois) ────────────────────────

_ORIG_GETADDRINFO = _socket.getaddrinfo
_DNS_OVERRIDE = contextvars.ContextVar("dns_override", default=None)
_patched = False


def _install_patch():
    global _patched
    if _patched:
        return

    def _getaddrinfo(host, port, *args, **kwargs):
        # 1. Override permanent (codé en dur) — prioritaire
        if host in _HARDCODED_DNS:
            log.debug(f"dns_utils : override permanent {host} → {_HARDCODED_DNS[host]}")
            host = _HARDCODED_DNS[host]
        else:
            # 2. Override temporaire par thread/contexte (DoH fallback)
            override = _DNS_OVERRIDE.get()
            if override and host == override[0]:
                host = override[1]
        return _ORIG_GETADDRINFO(host, port, *args, **kwargs)

    _socket.getaddrinfo = _getaddrinfo
    _patched = True
    log.debug("dns_utils : patch socket.getaddrinfo installé")


# Installer le patch dès l'import du module (nécessaire pour _HARDCODED_DNS)
_install_patch()


# ── Résolution DoH ────────────────────────────────────────────────────────────

def doh_resolve(hostname: str) -> str | None:
    """
    Résout un hostname via Google DNS-over-HTTPS.
    Renvoie la première adresse IPv4 trouvée, ou None si échec.
    """
    import requests
    try:
        r = requests.get(
            "https://dns.google/resolve",
            params={"name": hostname, "type": "A"},
            timeout=8,
            headers={"accept": "application/dns-json"},
        )
        r.raise_for_status()
        for ans in r.json().get("Answer", []):
            if ans.get("type") == 1:  # A record
                return ans["data"]
    except Exception as e:
        log.debug(f"DoH échoué pour {hostname} : {e}")
    return None


# ── Wrapper requête avec fallback DoH ─────────────────────────────────────────

def get_with_doh_fallback(session, url: str, **kwargs):
    """
    session.get(url) avec fallback DoH si le DNS OS échoue.
    Thread-safe : le patch DNS est local au contexte d'exécution via contextvars.
    """
    import requests

    # ── 1. Tentative normale ──────────────────────────────────────────────
    try:
        return session.get(url, **kwargs)
    except requests.exceptions.ConnectionError as exc:
        err = str(exc)
        if "getaddrinfo" not in err and "NameResolution" not in err:
            raise  # autre erreur réseau → propager

    # ── 2. DNS OS échoué → fallback DoH ──────────────────────────────────
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname
    log.info(f"DNS OS échoué pour {hostname} → tentative DoH (dns.google)")

    ip = doh_resolve(hostname)
    if not ip:
        raise requests.exceptions.ConnectionError(
            f"DNS resolution failed for {hostname} via OS DNS et DoH"
        )
    log.info(f"DoH résolu : {hostname} → {ip}")

    # ── 3. Réessai avec IP pré-résolue ───────────────────────────────────
    _install_patch()
    token = _DNS_OVERRIDE.set((hostname, ip))
    try:
        return session.get(url, **kwargs)
    finally:
        _DNS_OVERRIDE.reset(token)
