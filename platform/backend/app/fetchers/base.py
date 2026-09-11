from abc import ABC, abstractmethod
from typing import Optional

import httpx


class BaseFetcher(ABC):
    """Abstract base class for ATS board fetchers."""

    PLATFORM: str = ""

    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client
        self._own_client = client is None

    # Browser-ish UA so bot-detection (Cloudflare, Akamai) doesn't auto-block
    # us on first request. Some platforms (Wellfound) still block this — that
    # is a per-fetcher problem, not a base-client problem.
    _DEFAULT_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def _get_client(self) -> httpx.Client:
        """Return the injected client or create a new one."""
        if self._client:
            return self._client
        self._created_client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": self._DEFAULT_UA},
        )
        return self._created_client

    @abstractmethod
    def fetch(self, slug: str) -> list[dict]:
        """Fetch all jobs from a company's board.

        Returns a list of normalized job dicts with keys:
            external_id, company_slug, title, url, platform,
            location_raw, remote_scope, department, raw_json
        """

    def fetch_one(self, slug: str, external_id: str) -> Optional[dict]:
        """Fetch a single job by its (slug, external_id) pair.

        Used by ``POST /jobs/submit-link`` (Feature A — manual link
        submission) so a pasted URL resolves to exactly one upsertable
        job dict without paging through an entire board.

        Default implementation: call :meth:`fetch` and filter. Cheap
        and safe for small boards (Lever, Ashby, Recruitee), but
        expensive on large Greenhouse boards where a single-job API
        endpoint exists — concrete fetchers with such an endpoint
        override this method to avoid the full-board round-trip.

        Returns the normalized job dict or ``None`` if no posting
        with ``external_id`` is live on the board. The caller
        translates ``None`` into HTTP 404 "job no longer listed".
        """
        for job in self.fetch(slug):
            if str(job.get("external_id", "")) in self._id_forms(external_id):
                return job
        return None

    def _id_forms(self, external_id: str) -> set[str]:
        """The raw and the namespaced form of an id.

        F383 namespaced the ids of the newer fetchers
        (``teamtailor-8237846``); rows created before that carry the raw
        id, and a lookup with either must still find the posting —
        production: a Teamtailor job created pre-namespacing extracted
        as the 8-field fallback because fetch_one matched neither.
        """
        raw = str(external_id or "")
        prefix = f"{self.PLATFORM}-"
        bare = raw[len(prefix):] if raw.startswith(prefix) else raw
        return {bare, f"{prefix}{bare}"}

    def _normalize(self, raw: dict, slug: str) -> dict:
        """Override in subclass to normalize a raw API response to the standard format."""
        raise NotImplementedError

    # Signals that indicate truly global/worldwide remote
    _WORLDWIDE_SIGNALS = (
        "worldwide", "work from anywhere", "anywhere in the world",
        "global remote", "remote - global", "remote global",
        "fully remote", "100% remote", "remote (anywhere)",
        "remote - anywhere", "any country", "any location",
        "open to all", "location independent", "location-independent",
    )

    @staticmethod
    def _detect_remote_scope(*texts: str) -> Optional[str]:
        """Heuristic remote-scope detection from one or more text fields.

        Returns one of: "worldwide", "remote", or None.
        - "worldwide": fully global, anyone can apply from anywhere
        - "remote": remote work allowed but may have country/region restrictions
        - None: no remote signal detected (on-site or unspecified)
        """
        for text in texts:
            if not text:
                continue
            lower = text.lower()
            if any(sig in lower for sig in BaseFetcher._WORLDWIDE_SIGNALS):
                return "worldwide"

        # Second pass: check for plain "remote"
        for text in texts:
            if not text:
                continue
            if "remote" in text.lower():
                return "remote"

        return None

    # Context-manager support for resource cleanup.

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._own_client and hasattr(self, "_created_client"):
            self._created_client.close()
