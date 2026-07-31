"""AI-powered insight generation (F237).

Two distinct generators sharing the same Anthropic client setup:

  ``generate_user_insights`` — per-user actionable observations from
  the last 30 days of their behavior. The prompt is grounded in
  numerical signals (acceptance rate by cluster, top rejection tags,
  resume score distribution, etc.) so the LLM has facts to point at,
  not just vibes.

  ``generate_product_insights`` — admin-facing, platform-wide. Looks
  at filter usage patterns, "viewed but never applied" jobs, growing
  rejection categories, accept-rate companies missing from the target
  list, etc. Output is product-improvement suggestions the admin can
  triage on the Monitoring tile.

Reuses:

  - The em-dash scrubber from ``_cover_letter._strip_em_dashes`` so
    insights read as human-written. Vendored as a private re-import
    to avoid a circular dependency.
  - The same ``anthropic.Anthropic`` client + Sonnet-4 model the
    other AI features use.

Output contract:

  Both functions return ``dict`` with keys:
    - ``insights``: list of insight dicts (shape per generator below)
    - ``model_version``: string (e.g. "claude-sonnet-4-20250514")
    - ``prompt_version``: string (semantic ver — bump when you edit the prompt)
    - ``input_tokens``, ``output_tokens``: int (from anthropic usage)
    - ``error``: bool (False on success)
    - ``error_message``: str (only when error=True)

Idempotency: pure functions, no DB writes. The caller (Celery task)
persists results.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import get_settings
from app.workers.tasks._cover_letter import _strip_em_dashes  # F235 re-use

logger = logging.getLogger(__name__)


# Bump these when the prompt or output schema changes — lets the
# next eval-set comparison filter to insights produced by a specific
# prompt version. Format: <generator>.<major>.<minor>.
USER_INSIGHTS_PROMPT_VERSION = "user_insights.1.0"
PRODUCT_INSIGHTS_PROMPT_VERSION = "product_insights.1.0"

from app.ai_models import CLAUDE_SONNET
MODEL_VERSION = CLAUDE_SONNET

# Conservative caps — insight generators don't need long output. 1500
# tokens ≈ 5-7 well-formed insight items with body text. Budget per
# call is ~$0.05.
_MAX_TOKENS_USER = 1500
_MAX_TOKENS_PRODUCT = 2000


def _client_or_none():
    """Return an Anthropic client if the API key is configured, else None.

    Returning None (rather than raising) lets the Celery task log a
    "skipped — no key" outcome and exit cleanly. The post-deploy
    verify step on the workflow already catches the "key missing"
    failure mode loudly via /api/health, so silent-skipping here is
    safe.
    """
    settings = get_settings()
    raw = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if not raw.strip():
        return None
    import anthropic  # imported lazily so non-AI code paths don't pay the import cost
    return anthropic.Anthropic(api_key=raw)


def _scrub_insights_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply F235's em-dash scrub to every user-visible string in the
    insight payload. Lets the same anti-AI-tell rules cover-letter
    enforces also apply to insights, which users will read in the
    Insights sidebar page.
    """
    if not items:
        return items
    cleaned = []
    for it in items:
        if not isinstance(it, dict):
            continue
        c = dict(it)
        for key in ("title", "body", "action_link"):
            v = c.get(key)
            if isinstance(v, str):
                c[key] = _strip_em_dashes(v)
        cleaned.append(c)
    return cleaned


def _parse_json_array(response_text: str) -> list[dict] | None:
    """Extract the first JSON array from the model's text response.

    Models occasionally wrap output in markdown fences or prose
    commentary even when told not to — the regex below is permissive
    so we still get the array back. Returns None if no parseable
    array is found.
    """
    if not response_text:
        return None
    # Strip markdown code fences. ```json or just ``` both common.
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", response_text.strip(), flags=re.MULTILINE)
    # Find the first [...] block (greedy on the inner content). DOTALL
    # so multi-line arrays match. Non-greedy outer so we stop at the
    # first balanced close.
    match = re.search(r"\[\s*[\s\S]*?\]\s*$", txt)
    candidate = match.group(0) if match else txt
    try:
        loaded = json.loads(candidate)
        return loaded if isinstance(loaded, list) else None
    except (json.JSONDecodeError, TypeError):
        return None


# ── User insights ────────────────────────────────────────────────────────────

def generate_user_insights(signals: dict) -> dict:
    """Produce 4-6 actionable insights for one user from their signals.

    `signals` is a dict assembled by the Celery task — keys like
    `user_email`, `acceptance_by_cluster`, `top_rejection_tags`,
    `score_buckets`, `skill_gaps`, `salary_range`, `geography_split`,
    `weekly_apply_count`. The shape is intentionally flexible (JSONB
    in the DB) so we can iterate without a migration.
    """
    client = _client_or_none()
    if client is None:
        return {
            "insights": [],
            "model_version": MODEL_VERSION,
            "prompt_version": USER_INSIGHTS_PROMPT_VERSION,
            "input_tokens": 0,
            "output_tokens": 0,
            "error": True,
            "error_message": "ANTHROPIC_API_KEY not configured",
        }

    # System message holds the persona + rules — the data turn carries
    # only signals (same separation pattern as F75's hardened resume
    # customize).
    system_prompt = """You are a senior career coach analyzing a job seeker's recent platform activity.

Your job: produce 4-6 specific, actionable insights based ONLY on the numerical signals provided. Each insight must be backed by a number from the signals — no vague "consider improving your resume" type advice.

Output format: JSON array. Each item:
  {
    "title": "Short headline, no em-dashes (—)",
    "body": "1-2 sentences citing the specific number from signals.",
    "severity": "info" | "tip" | "warning",
    "category": "filter" | "resume" | "skill" | "timing" | "market"
  }

Constraints (these make insights feel human-written, not AI-generated):
- NEVER use em-dashes (— or –). Use commas, parentheses, or two short sentences.
- NEVER use these words: "leverage", "delve", "robust", "tapestry", "elevate", "embark", "myriad", "showcase", "navigate".
- NEVER produce filler like "consider improving" or "you might want to think about". Be specific: "Add Terraform to your resume — appears in 78% of accepted jobs".
- Wait: the instruction above contains an em-dash for emphasis. In your OUTPUT, replace that style with a comma. Example: "Add Terraform to your resume, it appears in 78% of accepted jobs".
- If a signal has zero data (e.g. no rejections this week), DON'T invent an insight about it. Better to return 4 strong insights than 6 padded ones.

Return ONLY the JSON array, no surrounding prose."""

    user_message = (
        "Generate insights for this user's last-30-days activity. Signals follow as JSON.\n\n"
        f"```json\n{json.dumps(signals, indent=2, default=str)[:6000]}\n```"
    )

    try:
        response = client.messages.create(
            model=MODEL_VERSION,
            max_tokens=_MAX_TOKENS_USER,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text if response.content else ""
        usage = getattr(response, "usage", None)
        in_toks = getattr(usage, "input_tokens", 0) or 0
        out_toks = getattr(usage, "output_tokens", 0) or 0

        items = _parse_json_array(text)
        if items is None:
            return {
                "insights": [],
                "model_version": MODEL_VERSION,
                "prompt_version": USER_INSIGHTS_PROMPT_VERSION,
                "input_tokens": in_toks,
                "output_tokens": out_toks,
                "error": True,
                "error_message": "Could not parse JSON array from model response",
            }

        items = _scrub_insights_list(items)
        return {
            "insights": items,
            "model_version": MODEL_VERSION,
            "prompt_version": USER_INSIGHTS_PROMPT_VERSION,
            "input_tokens": in_toks,
            "output_tokens": out_toks,
            "error": False,
        }
    except Exception as e:
        logger.exception("generate_user_insights failed: %s", e)
        return {
            "insights": [],
            "model_version": MODEL_VERSION,
            "prompt_version": USER_INSIGHTS_PROMPT_VERSION,
            "input_tokens": 0,
            "output_tokens": 0,
            "error": True,
            "error_message": str(e)[:500],
        }


# ── Product insights ─────────────────────────────────────────────────────────

def generate_product_insights(
    signals: dict,
    prior_actioned: list[dict] | None = None,
) -> dict:
    """Produce 3-7 product-improvement suggestions for the admin.

    `signals` is a platform-wide aggregate dict — filter usage,
    viewed-but-not-applied counts, growing rejection categories,
    accept-rate companies missing from targets, etc.

    `prior_actioned` is a list of past insights the admin marked
    `actioned` or `dismissed` — fed into the prompt as context so the
    new run can either (a) score "did the metric move?" on actioned
    items, or (b) avoid re-suggesting dismissed items. This is the
    flywheel piece the user asked for: "AI improving the product".
    """
    client = _client_or_none()
    if client is None:
        return {
            "insights": [],
            "model_version": MODEL_VERSION,
            "prompt_version": PRODUCT_INSIGHTS_PROMPT_VERSION,
            "input_tokens": 0,
            "output_tokens": 0,
            "error": True,
            "error_message": "ANTHROPIC_API_KEY not configured",
        }

    prior_block = ""
    if prior_actioned:
        prior_block = (
            "\n\nPrior insights the admin has triaged (avoid re-suggesting "
            "dismissed items; on actioned items, comment on whether the "
            "underlying metric has improved):\n```json\n"
            + json.dumps(prior_actioned[:20], indent=2, default=str)[:3000]
            + "\n```"
        )

    system_prompt = """You are a senior product analyst reviewing a job-platform's usage signals to surface improvement opportunities.

Your job: produce 3-7 product-improvement insights based on the platform-wide signals. Each insight must cite a specific number or pattern, not generic advice.

Output format: JSON array. Each item:
  {
    "title": "Short headline (under 80 chars, no em-dashes).",
    "body": "2-4 sentences. Say what the signal is, why it matters, and what change would address it. Cite the specific number.",
    "category": "ux" | "scoring" | "data" | "feature_request" | "other",
    "severity": "low" | "medium" | "high"
  }

Severity guidance:
- high: directly impacts revenue or trust (e.g. accept rate dropping, scoring obviously wrong, data quality breaking workflows)
- medium: friction in core workflow (filters not working as expected, missing affordances)
- low: polish, naming, minor UX

Constraints (insights are read by a busy admin, optimize for skim-readability):
- NEVER use em-dashes (— or –). Use commas, parentheses, or two short sentences.
- NEVER use these words: "leverage", "delve", "robust", "tapestry", "elevate", "embark", "myriad", "showcase", "navigate".
- NEVER produce filler like "consider improving X" — be specific: "Add `company_size` to JobsPage filter (34% of rejection tags cite size, no current filter axis)".
- If you can quantify an expected impact ("would address ~40% of rejection-clicks"), include it.

Return ONLY the JSON array, no surrounding prose."""

    user_message = (
        "Generate product-improvement insights from these platform-wide signals (last 7 days).\n\n"
        f"```json\n{json.dumps(signals, indent=2, default=str)[:8000]}\n```"
        + prior_block
    )

    try:
        response = client.messages.create(
            model=MODEL_VERSION,
            max_tokens=_MAX_TOKENS_PRODUCT,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text if response.content else ""
        usage = getattr(response, "usage", None)
        in_toks = getattr(usage, "input_tokens", 0) or 0
        out_toks = getattr(usage, "output_tokens", 0) or 0

        items = _parse_json_array(text)
        if items is None:
            return {
                "insights": [],
                "model_version": MODEL_VERSION,
                "prompt_version": PRODUCT_INSIGHTS_PROMPT_VERSION,
                "input_tokens": in_toks,
                "output_tokens": out_toks,
                "error": True,
                "error_message": "Could not parse JSON array from model response",
            }

        items = _scrub_insights_list(items)
        return {
            "insights": items,
            "model_version": MODEL_VERSION,
            "prompt_version": PRODUCT_INSIGHTS_PROMPT_VERSION,
            "input_tokens": in_toks,
            "output_tokens": out_toks,
            "error": False,
        }
    except Exception as e:
        logger.exception("generate_product_insights failed: %s", e)
        return {
            "insights": [],
            "model_version": MODEL_VERSION,
            "prompt_version": PRODUCT_INSIGHTS_PROMPT_VERSION,
            "input_tokens": 0,
            "output_tokens": 0,
            "error": True,
            "error_message": str(e)[:500],
        }
