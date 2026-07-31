"""AI-powered cover letter generation using Claude API."""

from app.ai_models import CLAUDE_OPUS
import logging
import re
from app.config import get_settings

logger = logging.getLogger(__name__)

# Regression finding 235: cover letters were leaking obvious AI tells —
# specifically em-dashes (— and the en-dash variant –), which Claude
# uses heavily by default and which most human writers don't. The
# strongest defense is a hard prompt instruction, but models still
# slip the character through ~5-10% of the time, so we also scrub
# the output as belt-and-suspenders. Replacement: a comma + space,
# which is the closest grammatical equivalent in 95% of em-dash uses
# (the parenthetical-aside case). Standalone em-dash patterns at
# sentence starts get a softer treatment to avoid double-comma noise.
_EMDASH_CHARS = ("\u2014", "\u2013")  # em-dash (—), en-dash (–)


def _strip_em_dashes(text: str) -> str:
    """Replace em-dashes with comma+space — they're the strongest single
    AI tell in cover-letter output, and the user explicitly asked
    they never appear. Pre-fix users would refuse to send the
    generated letter because "it looks AI-written"; the dashes were
    the largest tell of that, even more than vocabulary choices.

    Strategy:
    - " — " (spaced em-dash, the most common form Claude emits) → ", "
    - "—word" (no leading space, e.g. mid-sentence appositive) → ", word"
    - bare "—" (rare, usually after parsing artifact) → ", "
    Same handling for en-dash (\u2013).

    Doesn't touch double-hyphens (--) — those don't appear in
    Claude's natural output and would be a deliberate user choice.
    """
    if not text:
        return text
    for ch in _EMDASH_CHARS:
        # Line-leading dash: strip the dash + immediate spaces only.
        # Use `[ \t]*` (not `\s*`) so adjacent newlines — which carry
        # paragraph structure — are preserved.
        text = re.sub(
            rf"(^|\n)[ \t]*{re.escape(ch)}[ \t]*",
            r"\1",
            text,
        )
        # " — " → ", " (most common: spaced parenthetical-aside)
        text = text.replace(f" {ch} ", ", ")
        # "—" with no surrounding spaces → ", " (rarer mid-token use)
        text = text.replace(ch, ", ")
    # Collapse double-comma artifacts and intra-line double-spaces.
    # Only horizontal spacing — `[ \t]+`, not `\s+` — so paragraph
    # breaks survive.
    text = re.sub(r",[ \t]*,", ",", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Strip a stray comma at the start of any line (artifact of a
    # different leading-em-dash variant like `,word` after substitution).
    text = re.sub(r"(^|\n)[ \t]*,[ \t]*", r"\1", text)
    return text


def generate_cover_letter(
    resume_text: str,
    job_title: str,
    company_name: str,
    job_description: str,
    tone: str = "professional",
) -> dict:
    """Generate a tailored cover letter using Claude.

    Returns dict with cover_letter, key_points, and customization_notes.
    """
    settings = get_settings()
    if not settings.anthropic_api_key.get_secret_value():
        return {
            "cover_letter": "",
            "key_points": [],
            "customization_notes": "AI cover letter generation requires an Anthropic API key.",
            "error": True,
        }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

        tone_instructions = {
            "professional": "Use a polished, confident, professional tone. Be concise and results-oriented.",
            "enthusiastic": "Show genuine excitement about the role and company. Be warm but not over-the-top.",
            "technical": "Lead with technical expertise. Emphasize specific tools, frameworks, and measurable outcomes.",
            "conversational": "Use a friendly, approachable tone while still being professional. Show personality.",
        }

        # Regression finding 235: two prompt-level constraints landed
        # together because they have the same root cause — users were
        # rejecting the generated letters as "obviously AI-written":
        #   (a) NO em-dashes (Claude's strongest tell — humans use
        #       commas / parentheses / period+capital for the same
        #       grammatical work). The output scrubber `_strip_em_
        #       dashes()` is belt-and-suspenders for the cases where
        #       Claude slips one through anyway.
        #   (b) Resume is the source of truth for facts; job description
        #       is the source of truth for emphasis. Pre-fix the prompt
        #       said "Do NOT fabricate experience" but didn't make the
        #       relationship between the two inputs explicit, so Claude
        #       would occasionally over-pivot toward JD keywords and
        #       imply experience the candidate didn't have. New phrasing
        #       inverts the framing: every claim must be grounded in
        #       the resume; the JD only chooses WHICH resume claims to
        #       surface and HOW to phrase them.
        # Note on the fallback string for missing JD: replaced the
        # previous "Not available — write based on the job title and
        # company." with a comma form (no em-dash) so the prompt itself
        # doesn't model the behavior we're trying to suppress.
        prompt = f"""You are an expert career coach who writes compelling, personalized cover letters that get interviews.

CANDIDATE RESUME (this is the only source of factual claims):
{resume_text[:3000]}

TARGET JOB:
Title: {job_title}
Company: {company_name}

JOB DESCRIPTION (use this to choose which resume claims to surface and how to phrase them, not as a source of new claims):
{job_description[:3000] if job_description else "Not available. Write based on the job title and company alone."}

TONE: {tone_instructions.get(tone, tone_instructions["professional"])}

INSTRUCTIONS:
1. Write a cover letter (250-350 words) tailored specifically to this job.
2. Open with a compelling hook. Do NOT start with "I am writing to apply for..." or any variant.
3. Every factual claim, metric, project, technology, or experience referenced in the cover letter MUST come from the resume above. If the job description mentions a skill or responsibility that isn't backed by the resume, do NOT mention it. If the resume has nothing relevant, write a shorter letter rather than padding with invented claims.
4. Use the job description to decide which resume points to lead with and how to frame them in the company's language. Mirror the job description's vocabulary only when the underlying experience exists in the resume.
5. Show knowledge of the company and why this role is a fit, but only using information available in the job description or company name. Do NOT invent company facts.
6. Include 2-3 concrete achievements or metrics drawn directly from the resume.
7. Close with a confident, specific call to action.
8. Writing style constraints (these are what make the letter read as human-written, not AI-generated):
   - Do NOT use em-dashes (—) or en-dashes (–) anywhere in the letter. Use commas, parentheses, or two short sentences instead. This is non-negotiable.
   - Do NOT use generic filler phrases like "I am excited to apply", "passionate self-starter", "results-driven professional", "dynamic environment", "leverage my skills", "proven track record".
   - Avoid words that pattern-match as AI output: "delve", "leverage" (as a verb), "tapestry", "robust", "elevate", "navigate", "embark", "realm", "myriad", "showcase".
   - Vary sentence length. Mix short punchy sentences with longer ones.
   - Use specific tools, numbers, and project names from the resume rather than generic descriptions.

Return your response in this exact format:

===COVER LETTER===
[The full cover letter text. No em-dashes. Every claim grounded in the resume.]

===KEY POINTS===
- [Key selling point 1 that makes this candidate stand out for this specific role]
- [Key selling point 2]
- [Key selling point 3]

===NOTES===
[Brief notes on customization choices and what to adjust before sending. Em-dashes are allowed in this NOTES section since it's metadata for the user, not the letter itself.]"""

        # Cover letters benefit disproportionately from a stronger model:
        # they're short (one shot, ~2k tokens out), voice-sensitive (the
        # letter must sound like the candidate, not a template), and
        # user-visible verbatim (unlike resume scoring or interview prep
        # where the output feeds a downstream workflow). Opus 4.7 produces
        # materially fewer "AI-ish" cover letters than Sonnet 4 on the
        # same prompt — in user testing Sonnet would drift into platitudes
        # ("I am excited to leverage my experience…") where Opus picks up
        # the specific job-description cues and the candidate's résumé
        # phrasing. Cost delta is ~3× per call, but cover letters are
        # one-per-application and the same ai_daily_limit_per_user=10 cap
        # applies — ceiling is single-digit dollars per user per month.
        #
        # Only cover-letter gets Opus. Resume customization (_ai_resume.py)
        # and interview prep (_interview_prep.py) still use Sonnet 4 —
        # they're called per-job too but at higher volume and their
        # outputs aren't shipped verbatim to a recruiter's inbox.
        message = client.messages.create(
            model=CLAUDE_OPUS,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text

        # Parse sections
        cover_letter = ""
        key_points = []
        notes = ""

        if "===COVER LETTER===" in response_text:
            parts = response_text.split("===COVER LETTER===", 1)
            rest = parts[1] if len(parts) > 1 else ""

            if "===KEY POINTS===" in rest:
                letter_part, rest2 = rest.split("===KEY POINTS===", 1)
                cover_letter = letter_part.strip()

                if "===NOTES===" in rest2:
                    points_part, notes_part = rest2.split("===NOTES===", 1)
                    key_points = [p.strip().lstrip("- ") for p in points_part.strip().split("\n") if p.strip().startswith("-")]
                    notes = notes_part.strip()
                else:
                    key_points = [p.strip().lstrip("- ") for p in rest2.strip().split("\n") if p.strip().startswith("-")]
            else:
                cover_letter = rest.strip()
        else:
            cover_letter = response_text

        # F235 belt-and-suspenders: scrub em-dashes from the user-
        # facing text even after the prompt forbids them. The NOTES
        # section is left untouched because that's internal metadata
        # for the user (review notes about why X was emphasized) and
        # em-dashes there don't end up in the cover letter the user
        # actually sends.
        cover_letter = _strip_em_dashes(cover_letter)
        key_points = [_strip_em_dashes(p) for p in key_points]

        return {
            "cover_letter": cover_letter,
            "key_points": key_points,
            "customization_notes": notes,
            "tone": tone,
            "error": False,
        }

    except Exception as e:
        logger.error("Cover letter generation failed: %s", e)
        return {
            "cover_letter": "",
            "key_points": [],
            "customization_notes": f"Generation failed: {str(e)}",
            "error": True,
        }
