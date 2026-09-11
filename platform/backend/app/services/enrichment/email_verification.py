"""Email verification that works where SMTP cannot (F404).

The existing ``email_verifier.verify_email_smtp`` needs outbound port 25.
Oracle blocks it on the VM and residential ISPs block it on laptops, so
in production it timed out for every contact and the table filled with
"unverified" / "unknown". This module picks the best method available:

1. A provider API when a key is configured (``EMAIL_VERIFY_PROVIDER`` +
   ``EMAIL_VERIFY_API_KEY``): Hunter (``/v2/email-verifier``) or
   ZeroBounce (``/v2/validate``). Results map to valid / invalid /
   catch_all / unknown.
2. SMTP, when port 25 is actually reachable (probed once per process).
3. Otherwise a heuristic that is honest about being one: the domain has
   MX records, it is a company domain (not freemail / disposable), and
   the address follows the pattern of other addresses we have *observed*
   (not guessed) at that domain. That earns ``likely``; a missing MX is
   ``invalid``; anything else stays ``unknown``. ``likely`` is never
   upgraded to ``valid`` without a real check.

Every result carries ``method`` and ``detail`` so the UI can show why.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_FREEMAIL = frozenset({"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com", "proton.me",
                       "protonmail.com", "live.com", "msn.com", "me.com", "mail.com", "yandex.com", "gmx.com"})
_ROLE_LOCAL = frozenset({"info", "jobs", "careers", "hr", "recruiting", "talent", "hello", "contact", "support", "sales",
                         "admin", "team", "press", "noreply", "no-reply", "help", "office"})

_port25_probe: tuple[float, bool] | None = None


@dataclass
class Verification:
    status: str          # valid | invalid | catch_all | likely | unknown
    method: str          # hunter | zerobounce | smtp | heuristic
    detail: str = ""

    def as_dict(self) -> dict:
        return {"status": self.status, "method": self.method, "detail": self.detail}


def port25_reachable(ttl: int = 3600) -> bool:
    """Probe once an hour whether outbound SMTP is possible at all."""
    global _port25_probe
    now = time.time()
    if _port25_probe and now - _port25_probe[0] < ttl:
        return _port25_probe[1]
    ok = False
    try:
        with socket.create_connection(("gmail-smtp-in.l.google.com", 25), timeout=4):
            ok = True
    except OSError:
        ok = False
    _port25_probe = (now, ok)
    return ok


def _mx_exists(domain: str) -> bool | None:
    try:
        import dns.resolver

        return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
    except Exception as exc:  # NoAnswer, NXDOMAIN, Timeout …
        name = type(exc).__name__
        if name in ("NXDOMAIN", "NoAnswer", "NoNameservers"):
            return False
        return None


def _provider(email: str, provider: str, key: str) -> Verification | None:
    import httpx

    try:
        with httpx.Client(timeout=15.0) as c:
            if provider == "hunter":
                r = c.get("https://api.hunter.io/v2/email-verifier", params={"email": email, "api_key": key})
                if r.status_code == 429 or r.status_code >= 500:
                    return None
                d = (r.json().get("data") or {})
                st = (d.get("status") or d.get("result") or "").lower()
                mapped = {"valid": "valid", "invalid": "invalid", "accept_all": "catch_all", "webmail": "valid",
                          "disposable": "invalid", "unknown": "unknown"}.get(st, "unknown")
                return Verification(mapped, "hunter", f"hunter: {st}, score {d.get('score')}")
            if provider == "zerobounce":
                r = c.get("https://api.zerobounce.net/v2/validate", params={"api_key": key, "email": email, "ip_address": ""})
                if r.status_code >= 500:
                    return None
                d = r.json()
                st = (d.get("status") or "").lower()
                mapped = {"valid": "valid", "invalid": "invalid", "catch-all": "catch_all", "spamtrap": "invalid",
                          "abuse": "invalid", "do_not_mail": "invalid", "unknown": "unknown"}.get(st, "unknown")
                return Verification(mapped, "zerobounce", f"zerobounce: {st} {d.get('sub_status') or ''}".strip())
    except Exception as exc:
        logger.info("email_verification: provider %s failed for %s: %s", provider, email, exc)
    return None


def _pattern(local: str) -> str:
    """first.last → 'f.l', flast → 'fl', first → 'f' … a shape, not the name."""
    if "." in local:
        return "f.l" if len(local.split(".")[0]) > 1 else "fi.l"
    if "_" in local:
        return "f_l"
    return "f" if len(local) <= 8 and local.isalpha() else "fl"


def heuristic(email: str, observed_at_domain: list[str] | None = None) -> Verification:
    """No network beyond DNS. ``observed_at_domain`` are addresses at the same
    domain that were seen on a website or in a job description (never
    guessed ones)."""
    if not email or "@" not in email or " " in email:
        return Verification("invalid", "heuristic", "not an address")
    local, domain = email.rsplit("@", 1)
    domain = domain.lower()
    if domain in _FREEMAIL:
        return Verification("unknown", "heuristic", "personal mailbox — cannot be checked without a provider")
    mx = _mx_exists(domain)
    if mx is False:
        return Verification("invalid", "heuristic", "domain has no mail server (no MX record)")
    if mx is None:
        return Verification("unknown", "heuristic", "DNS lookup failed")
    if local.lower() in _ROLE_LOCAL:
        return Verification("likely", "heuristic", "role mailbox at a domain that accepts mail")
    observed = [o for o in (observed_at_domain or []) if o and "@" in o and o.lower() != email.lower()]
    if observed:
        shapes = {_pattern(o.rsplit("@", 1)[0].lower()) for o in observed}
        if _pattern(local.lower()) in shapes:
            return Verification("likely", "heuristic", f"matches the address pattern of {len(observed)} observed address(es) at {domain}")
        return Verification("unknown", "heuristic", f"does not match the pattern of observed addresses at {domain}")
    return Verification("unknown", "heuristic", "domain accepts mail, but no observed address to compare the pattern with")


def available_method() -> str:
    """Which verification the platform can do right now: provider name,
    "smtp", or "heuristic"."""
    from app.config import get_settings

    settings = get_settings()
    provider = (getattr(settings, "email_verify_provider", "") or "").strip().lower()
    key = settings.email_verify_api_key.get_secret_value() if getattr(settings, "email_verify_api_key", None) else ""
    if provider in ("hunter", "zerobounce") and key:
        return provider
    return "smtp" if port25_reachable() else "heuristic"


def verify_email(email: str, observed_at_domain: list[str] | None = None) -> Verification:
    from app.config import get_settings

    settings = get_settings()
    provider = (getattr(settings, "email_verify_provider", "") or "").strip().lower()
    key = settings.email_verify_api_key.get_secret_value() if getattr(settings, "email_verify_api_key", None) else ""
    if provider in ("hunter", "zerobounce") and key:
        v = _provider(email, provider, key)
        if v is not None:
            return v
    if port25_reachable():
        try:
            from app.services.enrichment.email_verifier import verify_email_smtp

            r = verify_email_smtp(email)
            st = r.get("status") or "unknown"
            if st != "unknown":
                return Verification(st, "smtp", f"mail server {r.get('mx_host') or ''} answered".strip())
        except Exception as exc:
            logger.info("email_verification: smtp failed for %s: %s", email, exc)
    return heuristic(email, observed_at_domain)


def observed_emails_for_domain(session, domain: str) -> list[str]:
    """Addresses at ``domain`` that came from a page or a JD, not a guess."""
    from sqlalchemy import select

    from app.models.company_contact import CompanyContact

    if not domain:
        return []
    rows = session.execute(
        select(CompanyContact.email).where(CompanyContact.email.ilike(f"%@{domain}"),
                                           CompanyContact.source.in_(["website_scrape", "job_description", "manual"]))
    ).scalars().all()
    return [r for r in rows if r]
