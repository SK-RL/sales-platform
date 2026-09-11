"""Application configuration from environment variables."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://jobplatform:jobplatform@localhost:5432/jobplatform"
    database_url_sync: str = "postgresql://jobplatform:jobplatform@localhost:5432/jobplatform"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    google_client_id: str = ""
    google_client_secret: str = ""
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24
    allowed_emails: str = ""  # comma-separated list, empty = allow all

    # App
    app_url: str = "http://localhost:3000"
    api_url: str = "http://localhost:8000"
    debug: bool = False

    # LinkedIn (RapidAPI)
    rapidapi_key: str = ""
    rapidapi_linkedin_host: str = "jsearch.p.rapidapi.com"  # or linkedin-data-api.p.rapidapi.com

    # AI
    # `SecretStr` so `repr(settings)`, `str(settings)`, and `model_dump()`
    # render the value as `**********` instead of the raw key. Prevents
    # accidental leaks through logged exceptions, debug endpoints that
    # might dump settings, or stringified tracebacks. Call sites that
    # need the raw value must use `.get_secret_value()` explicitly —
    # the explicitness is the point: ripgrep finds every place the
    # raw key materializes.
    anthropic_api_key: SecretStr = SecretStr("")
    # F404 — email verification. Outbound port 25 is blocked on the Oracle
    # VM (and most laptops), so the SMTP handshake verifier can never
    # answer there; 2,699 contacts sat "unverified". With a provider key
    # the platform verifies through its API; without one it scores emails
    # heuristically and says so ("likely", never "valid").
    email_verify_provider: str = ""            # "hunter" | "zerobounce" | "" (heuristic only)
    email_verify_api_key: SecretStr = SecretStr("")
    # F236: per-feature AI rate limits — see docs/AI_USAGE.md for the
    # full policy + cost back-of-envelope. All three counters share the
    # same `ai_customization_logs` table (discriminated by the `feature`
    # column added in migration s9n0o1p2q3r4) and the same midnight-UTC
    # reset cadence. Numbers chosen by user 2026-04-17 based on cost vs
    # workflow-velocity tradeoff (10/30/10 = $0.05 × 50 worst-case per
    # user per day = $2.50/user/day worst, ~$30/day platform-wide at 50
    # active users).
    ai_daily_limit_per_user: int = 10           # customize: max AI customizations per user per day
    ai_cover_letter_daily_limit_per_user: int = 30  # cover letter: max generations per user per day
    ai_interview_prep_daily_limit_per_user: int = 10  # interview prep: max generations per user per day

    # Security
    credential_encryption_key: str = ""  # Falls back to jwt_secret if empty

    # Scanning
    scan_interval_hours: int = 6
    scan_rate_limit_per_second: float = 2.0
    job_expiry_days: int = 14

    # Discovery — cap per-run promotion of DiscoveredCompany rows to
    # CompanyATSBoard rows. Without a cap, a fresh Greenhouse sitemap
    # crawl (3k+ slugs, no probe gate) would try to register 3k new
    # boards in a single beat tick. The stale-board auto-deactivator
    # eventually culls dead slugs, but at 30s average per scan that's
    # hours of wasted worker cycles on first ingest. 200/run bounded
    # by the scheduler frequency lets us spread ingestion: aggressive
    # mode (every 30m) fills a 3k backlog in ~7.5h; normal mode (daily)
    # in ~15 days. Both reasonable given the stale-cull backstop.
    discovery_promote_batch_size: int = 200

    # Enrichment
    enrichment_stale_days: int = 30
    contact_verify_stale_days: int = 14
    enrichment_batch_size: int = 50

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def allowed_email_list(self) -> list[str]:
        if not self.allowed_emails:
            return []
        return [e.strip() for e in self.allowed_emails.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
