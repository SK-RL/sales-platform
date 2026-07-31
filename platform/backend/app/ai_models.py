"""Central Claude model IDs — single source of truth.

Retiring or renaming a model is now a one-line edit here instead of a
scavenger hunt across resume/cover-letter/interview-prep/insights.

Background (F357): every AI feature hardcoded ``claude-sonnet-4-20250514``
(cover-letter used ``claude-opus-4-7``). Both were retired — the live
Anthropic API returns ``404 not_found_error`` for them — so EVERY AI
feature was silently broken; only AI Insights exercised it often enough
(via the beat task) for anyone to notice.

The IDs below were probed against the production API key on 2026-07-31
and confirmed working. When Anthropic ships a newer model, update here
and redeploy — the probe is a one-liner:

    docker exec sales-platform-backend-1 python -c "import os,anthropic; \
      anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY']).messages.create(\
      model='<id>', max_tokens=8, messages=[{'role':'user','content':'hi'}])"
"""

# Sonnet tier — the default for the volume features (resume
# customization, interview prep, insights). Cost-efficient, current.
CLAUDE_SONNET = "claude-sonnet-4-6"

# Opus tier — reserved for cover-letter generation, where output
# quality justified the higher cost in the original design.
CLAUDE_OPUS = "claude-opus-4-8"
