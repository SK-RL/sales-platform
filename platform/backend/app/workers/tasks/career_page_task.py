"""Career page change-detection task."""

import hashlib
import ipaddress
import logging
import socket
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession
from app.models.scan import CareerPageWatch

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds
USER_AGENT = "JobPlatformBot/1.0 (+https://github.com/your-org/job-platform)"


# Regression finding 151 (CWE-918 SSRF, severity:red): even after F201
# gated career-pages CRUD on admin, any admin can still store a URL
# like `http://127.0.0.1:6379/INFO` or `http://169.254.169.254/...`
# (Oracle IMDS) and the scanner runs from the VPC's network position,
# so the request lands on internal services. Access-control alone
# isn't enough — ops accounts have legitimate reasons to be admin-
# equivalent without SSRF-blast-radius authority. Defense-in-depth
# here blocks the actual network reach: resolve the URL's hostname
# BEFORE issuing the HTTP request, walk every resolved IP (A + AAAA
# records — DNS rebinding can return multiple addresses, some
# public some private), and refuse if any one is in the private-
# or reserved-range set. We also refuse non-http(s) schemes since
# the API validator only guarantees the scheme at create-time and
# legacy rows (F174's 117 non-URL rows) may still trickle through.
_BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        # IPv4 private ranges (RFC 1918) + loopback + link-local + CGNAT
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",      # link-local incl. Oracle/AWS IMDS
        "100.64.0.0/10",       # CGNAT
        "0.0.0.0/8",           # "this network"
        "224.0.0.0/4",         # multicast
        "240.0.0.0/4",         # reserved / broadcast
        # IPv6 equivalents
        "::1/128",             # loopback
        "fc00::/7",            # unique local (RFC 4193)
        "fe80::/10",           # link-local
        "ff00::/8",            # multicast
        # IPv4-mapped / 4-in-6 — block the underlying v4 via dual lookup
    )
]


def _url_resolves_to_blocked_ip(url: str) -> bool:
    """Return True if the URL's scheme is non-http(s) OR any resolved IP
    falls in the private / reserved ranges. Caller should skip the
    fetch and log on True.

    Not using `socket.gethostbyname` because it returns only one
    address and loses AAAA results. `getaddrinfo` covers both. DNS
    rebinding attacks that return mixed public+private IPs are
    caught by refusing if ANY resolved IP is blocked (strictest
    interpretation).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return True  # can't parse → refuse

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return True

    host = parsed.hostname
    if not host:
        return True

    # Literal IPs bypass DNS — check them directly. Also unwrap IPv4-
    # mapped IPv6 (`::ffff:127.0.0.1`) so the caller can't sneak past
    # the v4 blocklist by encoding the address as v6.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if any(literal in n for n in _BLOCKED_IP_NETWORKS):
            return True
        if isinstance(literal, ipaddress.IPv6Address) and literal.ipv4_mapped is not None:
            if any(literal.ipv4_mapped in n for n in _BLOCKED_IP_NETWORKS):
                return True
        return False

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Unresolvable — let httpx handle the error cleanly below so
        # the error path / retry semantics don't change. The fetcher
        # will log and return None.
        return False

    for info in infos:
        sockaddr = info[4]
        raw = sockaddr[0]
        # IPv6 sockaddr may include %scope suffix — strip it.
        if "%" in raw:
            raw = raw.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if any(ip in n for n in _BLOCKED_IP_NETWORKS):
            return True
        # Also catch IPv4-mapped IPv6 like ::ffff:127.0.0.1
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            if any(ip.ipv4_mapped in n for n in _BLOCKED_IP_NETWORKS):
                return True
    return False


_MAX_REDIRECTS = 10


def _fetch_page_hash(url: str) -> str | None:
    """Fetch a URL and return the SHA-256 hash of the response body, or None on error.

    F151: SSRF guard runs on the initial URL AND on every redirect
    hop. We can't trust `httpx.follow_redirects=True` because it
    wouldn't re-validate the redirect target against our
    private-IP blocklist — a public URL that 302s to
    `http://127.0.0.1:6379/INFO` would still land on Redis. Instead
    we drive the redirect loop manually with `follow_redirects=False`
    and re-check each hop.
    """
    if _url_resolves_to_blocked_ip(url):
        logger.warning("Refusing to fetch career page %s: blocked by SSRF guard", url)
        return None

    current = url
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            for _hop in range(_MAX_REDIRECTS):
                resp = client.get(current, headers={"User-Agent": USER_AGENT})
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    if not location:
                        resp.raise_for_status()
                        return hashlib.sha256(resp.content).hexdigest()
                    # Resolve relative Location headers against the
                    # current URL (same semantics httpx would apply).
                    next_url = str(httpx.URL(current).join(location))
                    if _url_resolves_to_blocked_ip(next_url):
                        logger.warning(
                            "Refusing career-page redirect %s → %s: blocked by SSRF guard",
                            current, next_url,
                        )
                        return None
                    current = next_url
                    continue
                resp.raise_for_status()
                return hashlib.sha256(resp.content).hexdigest()
            logger.warning("Career page %s exceeded redirect limit (%d)", url, _MAX_REDIRECTS)
            return None
    except Exception as e:
        logger.warning("Failed to fetch career page %s: %s", url, e)
        return None


@celery_app.task(name="app.workers.tasks.career_page_task.check_career_pages", bind=True, max_retries=1, acks_late=False, soft_time_limit=2400, time_limit=2700)
def check_career_pages(self):
    """Iterate active CareerPageWatch records, fetch pages, and compare hashes.

    If the page content hash has changed since the last check, mark has_changed=True,
    increment change_count, and log the change (notification placeholder).
    """
    logger.info("Starting check_career_pages")
    session = SyncSession()

    changed_count = 0
    checked_count = 0
    error_count = 0

    try:
        watches = session.execute(
            select(CareerPageWatch).where(CareerPageWatch.is_active.is_(True))
        ).scalars().all()

        for watch in watches:
            new_hash = _fetch_page_hash(watch.url)
            now = datetime.now(timezone.utc)

            watch.last_checked_at = now
            watch.check_count += 1

            if new_hash is None:
                error_count += 1
                continue

            checked_count += 1

            if watch.last_hash and new_hash != watch.last_hash:
                watch.has_changed = True
                watch.change_count += 1
                changed_count += 1
                logger.info(
                    "Career page changed: %s (company_id=%s, changes=%d)",
                    watch.url, watch.company_id, watch.change_count,
                )
                # TODO: trigger notification (email, Slack webhook, etc.)
            else:
                watch.has_changed = False

            watch.last_hash = new_hash

        session.commit()

        logger.info(
            "check_career_pages complete: %d checked, %d changed, %d errors",
            checked_count, changed_count, error_count,
        )
        return {
            "checked": checked_count,
            "changed": changed_count,
            "errors": error_count,
        }

    except Exception as e:
        logger.exception("check_career_pages failed: %s", e)
        session.rollback()
        raise self.retry(exc=e, countdown=120)
    finally:
        session.close()
