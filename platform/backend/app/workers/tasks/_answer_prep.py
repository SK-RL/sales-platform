"""Match ATS form questions to user answer-book entries."""

from __future__ import annotations

import re
from typing import Any

# Common aliases: map normalised field keys to likely answer-book question keys.
_FIELD_ALIASES: dict[str, list[str]] = {
    "first_name": ["first_name", "given_name", "whats_your_first_name", "name"],
    "last_name": ["last_name", "surname", "family_name", "whats_your_last_name"],
    "name": ["full_name", "name", "first_name", "your_name", "whats_your_name"],
    "email": ["email", "email_address", "whats_your_email", "whats_your_email_address"],
    "phone": ["phone", "phone_number", "mobile", "whats_your_phone_number", "telephone"],
    "linkedin_url": ["linkedin", "linkedin_url", "linkedin_profile", "linkedincom"],
    "website": ["website", "portfolio", "personal_website", "website_portfolio", "github", "github_url"],
    "cover_letter": ["cover_letter", "why_do_you_want_to_work", "tell_us_about_yourself"],
    "salary": ["salary", "salary_expectations", "expected_salary", "desired_salary", "compensation"],
    "work_authorization": ["work_authorization", "are_you_authorized", "authorized_to_work", "work_auth", "do_you_require_sponsorship"],
    "location": ["location", "current_location", "where_are_you_located", "city"],
    "how_did_you_hear": ["how_did_you_hear", "how_did_you_hear_about_us", "referral_source"],
    "years_experience": ["years_of_experience", "years_experience", "how_many_years"],
    "start_date": ["start_date", "earliest_start_date", "when_can_you_start", "availability"],
    "gender": ["gender", "gender_identity"],
    "race": ["race", "ethnicity", "race_ethnicity"],
    "veteran_status": ["veteran", "veteran_status", "are_you_a_veteran"],
    "disability_status": ["disability", "disability_status"],
    # F394 — the fixed keys the newer adapters emit (Dover/Gem "linkedin",
    # Pinpoint/Hireology address parts) resolve at high confidence to
    # the Answer Book's usual spellings instead of via token matching.
    "linkedin": ["linkedin_url", "linkedin", "linkedin_profile", "linkedin_profile_url"],
    "city": ["city", "town", "current_city"],
    "address": ["address", "street_address", "address1", "address_line_1"],
    "postcode": ["postcode", "zip_code", "zip", "postal_code", "zip_postal_code"],
    "country": ["country", "country_of_residence"],
    "state": ["state", "state_province", "province"],
    "preferred_name": ["preferred_name", "nickname"],
}

# F346 — fields we must never answer by inference.
#
# `_find_best_match` has five strategies, in descending precision:
# exact key, alias, normalised label, substring, category fallback.
# The last two are *fuzzy*: substring matches any key that shares a
# fragment, and the category fallback returns the first non-empty
# answer in a guessed category. Both are fine for "what's your
# LinkedIn" and actively dangerous for "do you have the legal right
# to work in the US".
#
# Concrete failure this prevents: a Greenhouse field
# `do_you_have_a_legal_right_to_work_in_the_us` hits
# `_CATEGORY_HINTS["authorized"] -> work_auth` and, under the old
# code, returned whichever work-auth entry happened to sort first —
# e.g. an India work-authorization answer — marked `confidence="low"`
# and then submitted, because nothing downstream read `confidence`.
#
# These are legal attestations, protected-class disclosures and
# compensation figures. Getting one wrong is not a bad guess, it is a
# false statement on an employment application. For any field matching
# these patterns we allow ONLY the three precise strategies (exact,
# alias, label) and otherwise return unresolved so a human fills it in.
_NEVER_INFER_PATTERNS: tuple[str, ...] = (
    # Work authorization / immigration status
    "work_auth", "authorized", "authorisation", "authorization",
    "legal_right", "right_to_work", "eligible_to_work", "work_permit",
    "sponsor", "visa", "immigration", "citizen", "residency",
    # Protected-class / EEO self-identification
    "veteran", "disability", "disabled", "gender", "race", "ethnicity",
    "hispanic", "latino", "protected", "self_identif",
    # Background attestations
    "criminal", "conviction", "felony", "background_check",
    "security_clearance", "clearance", "export_control",
    "non_compete", "noncompete", "drug_test",
    # Compensation — a wrong number here is quoted back at offer stage.
    # F366: a bare "rate" matched inside elabo-rate / corpo-rate / ope-rate
    # and flagged an ordinary Ashby question ("Please elaborate on your
    # experience…") as legal/protected-class. Only the pay-rate phrasings.
    "salary", "compensation", "expected_pay", "desired_pay",
    "hourly_rate", "day_rate", "pay_rate", "rate_expectation",
)


def is_never_infer_field(field_key: str, label: str) -> bool:
    """True when a field is too consequential to answer by inference.

    See :data:`_NEVER_INFER_PATTERNS`. Matches on the field key and the
    human label together, because ATSes vary wildly in which of the two
    carries the meaning: Greenhouse tends to put it in the key
    (``are_you_legally_authorized_to_work``) while Workday uses opaque
    keys (``primaryQuestion--1``) and puts the question in the label.
    """
    # F366: whitespace is folded to "_" so the multi-word patterns
    # ("legal_right", "hourly_rate", "self_identif") match LABEL text,
    # not only snake_case keys. Before this they never fired on a Workday-
    # style label behind an opaque key. Strictly widens coverage in the
    # safe direction — every multi-word pattern is specific enough that
    # the fold introduces no new false positives.
    combined = re.sub(r"\s+", "_", f"{field_key} {label}".lower())
    return any(pattern in combined for pattern in _NEVER_INFER_PATTERNS)


# Category hints: if field_key contains these terms, prefer answer-book entries from these categories.
_CATEGORY_HINTS: dict[str, str] = {
    "first_name": "personal_info",
    "last_name": "personal_info",
    "name": "personal_info",
    "email": "personal_info",
    "phone": "personal_info",
    "linkedin": "personal_info",
    "website": "personal_info",
    "github": "personal_info",
    "salary": "preferences",
    "work_auth": "work_auth",
    "sponsor": "work_auth",
    "authorized": "work_auth",
    "visa": "work_auth",
    "experience": "experience",
    "cover_letter": "experience",
    "tell_us": "experience",
    "skills": "skills",
    "location": "preferences",
    "start_date": "preferences",
    "gender": "custom",
    "race": "custom",
    "veteran": "custom",
    "disability": "custom",
}


def _normalise_key(text: str) -> str:
    key = text.lower().strip()
    key = re.sub(r"[^\w\s]", "", key)
    key = re.sub(r"\s+", "_", key)
    return key[:255]


def match_questions_to_answers(
    questions: list[dict[str, Any]],
    answer_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Match ATS form questions against user answer-book entries.

    Parameters
    ----------
    questions
        Normalised question dicts from ``fetch_application_questions``.
    answer_entries
        Merged answer-book entries (base + resume overrides).
        Each has ``question_key``, ``answer``, ``category``, ``source``.

    Returns
    -------
    list of matched dicts, one per form question::

        {
            "field_key": str,
            "label": str,
            "field_type": str,
            "required": bool,
            "options": list,
            "description": str,
            "answer": str,
            "match_source": "base" | "override" | "unmatched",
            "question_key": str,
            "confidence": "high" | "medium" | "low",
        }
    """
    # Build lookup indexes from answer-book entries
    by_key: dict[str, dict] = {}
    by_category: dict[str, list[dict]] = {}
    for entry in answer_entries:
        qk = entry.get("question_key", "")
        if qk:
            by_key[qk] = entry
        cat = entry.get("category", "")
        by_category.setdefault(cat, []).append(entry)

    results: list[dict[str, Any]] = []

    for q in questions:
        field_key = q.get("field_key", "")
        label = q.get("label", "")

        never_infer = is_never_infer_field(field_key, label)
        match = _find_best_match(
            field_key, label, by_key, by_category, never_infer=never_infer
        )

        # F346 — `needs_user` is the gate the apply path reads. A field
        # needs a human when it is required and we have no answer we'd
        # stand behind: either nothing matched, or the only match came
        # from a fuzzy strategy on a never-infer field (which
        # `_find_best_match` already refuses to return, so this reduces
        # to "required and not confidently answered").
        answered = bool(match["answer"])
        trusted = match["confidence"] in ("high", "medium")
        results.append({
            "field_key": field_key,
            "label": label,
            "field_type": q.get("field_type", "text"),
            "required": q.get("required", False),
            "options": q.get("options", []),
            "description": q.get("description", ""),
            # Carried through from the schema so callers can tell an
            # extracted form from a guessed one (see fetchers/questions).
            "extraction_mode": q.get("extraction_mode", "extracted"),
            # F350 — non-empty when this field is one of several
            # alternatives satisfying a single ATS question.
            "alternative_group": q.get("alternative_group", ""),
            "answer": match["answer"],
            "match_source": match["source"],
            "question_key": match["question_key"],
            "confidence": match["confidence"],
            "never_infer": never_infer,
            "needs_user": bool(q.get("required", False)) and not (answered and trusted),
        })

    return results


def blocking_gaps(
    matched: list[dict[str, Any]],
    satisfied_field_keys: frozenset[str] | set[str] | None = None,
    unattended: bool = False,
) -> list[dict[str, str]]:
    """Required fields that must not be auto-submitted.

    F346. The apply path calls this and refuses to submit while the
    list is non-empty — the platform equivalent of "we could not
    resolve this, so a human decides". Returning a structured reason
    (rather than a bare count) lets the UI say *which* field and *why*,
    the way a locked field explains itself.

    ``satisfied_field_keys`` names fields answered outside the form
    answers. The resume is the case: ``apply_task`` uploads the stored
    file rather than typing it, so ``resume`` is satisfied even though
    no answer-book entry matches it.

    F350 — fields sharing an ``alternative_group`` are ONE requirement.
    Greenhouse's "Resume/CV" question exposes ``resume`` (file) and
    ``resume_text`` (textarea); either satisfies it. Treating them as
    two separate required fields blocked every application that had a
    resume attached, because nothing ever fills the paste-it-manually
    textarea.
    """
    satisfied = set(satisfied_field_keys or ())

    # Groups that any member has already satisfied.
    satisfied_groups: set[str] = set()
    for m in matched:
        group = m.get("alternative_group") or ""
        if not group:
            continue
        if m.get("answer") or m.get("field_key") in satisfied:
            satisfied_groups.add(group)

    gaps: list[dict[str, str]] = []
    for m in matched:
        if m.get("field_key") in satisfied:
            continue
        if (m.get("alternative_group") or "") in satisfied_groups and m.get("alternative_group"):
            continue
        # F385 — a guess is fine to SHOW a person (the review screen marks
        # it), never to SEND. Seen on production: a Breezy "Summary"
        # textarea auto-filled with an unrelated answer by the category
        # fallback. F394 widened this from the sweep to attended submits
        # too: the review screen has no inline edit, so "check it before
        # sending" was a caption over an answer that went out unchanged
        # (audit: Gem "Website" = the first name). A required field with
        # only a guess is a gap in both modes; the person saves the real
        # answer once and it is high-confidence from then on.
        if m.get("required") and m.get("confidence") == "low" and (m.get("answer") or "").strip():
            gaps.append({
                "field_key": m.get("field_key", ""),
                "label": m.get("label") or m.get("field_key", ""),
                "reason": "We only had a guess for this. Auto-apply never sends guesses — save the real answer in your Answer Book.",
            })
            continue
        # F386 — a saved answer that is not one of the form's options can
        # never be placed; say so here instead of after a browser run
        # (production, Teamtailor "Locations": saved "Bengaluru, India"
        # against [USA, Latin America] failed the adapter, not the gate).
        if m.get("required") and m.get("field_type") in ("select", "multi_select") and (m.get("answer") or "").strip() and m.get("options"):
            from app.services.submitters.base import coerce_option

            answer = m.get("answer") or ""
            parts = [answer] if m.get("field_type") == "select" else [v.strip() for v in re.split(r"\s*[;|]\s*", answer) if v.strip()]
            if any(coerce_option(v, m["options"]) is None for v in parts):
                labels = [o.get("label") or o.get("value") if isinstance(o, dict) else str(o) for o in m["options"]][:6]
                gaps.append({
                    "field_key": m.get("field_key", ""),
                    "label": m.get("label") or m.get("field_key", ""),
                    "reason": f"Your saved answer \"{answer[:40]}\" isn't one of this form's options ({', '.join(labels)}). Pick one.",
                })
                continue
        if not m.get("needs_user"):
            continue
        if m.get("never_infer"):
            reason = (
                "This is a legal or protected-class question. We only answer it "
                "from an exact saved answer, never by inference — please set it."
            )
        elif m.get("answer"):
            reason = "We found only a loose match for this required field."
        else:
            reason = "No saved answer matches this required field."
        gaps.append({
            "field_key": m.get("field_key", ""),
            "label": m.get("label", ""),
            "reason": reason,
        })
    return gaps


def _find_best_match(
    field_key: str,
    label: str,
    by_key: dict[str, dict],
    by_category: dict[str, list[dict]],
    never_infer: bool = False,
) -> dict[str, str]:
    """Find the best answer-book match for a given form field.

    ``never_infer`` (F346) restricts matching to the three precise
    strategies — exact key, alias, normalised label — and skips the two
    fuzzy ones. Used for legal / EEO / compensation fields where a
    plausible-looking wrong answer is worse than no answer at all.
    """
    # F346: unmatched is `confidence="none"`, not `"low"`. Previously
    # both "the category fallback guessed this" and "we found nothing"
    # reported `"low"`, so a caller could not distinguish a weak answer
    # from an absent one. `"low"` now means exactly one thing: a
    # category-fallback guess.
    empty = {"answer": "", "source": "unmatched", "question_key": "", "confidence": "none"}

    # F382 — an entry with no answer must not shadow one that has one.
    # Production: ``auto_populate_answer_book`` writes an empty
    # "ats_discovered" placeholder keyed by the ATS field_key (e.g.
    # section_…_question_0); strategy 1 then returned it, and the answer
    # the user saved under the question's own text ("Why are you a great
    # fit?") was never consulted. The first exact strategy with a
    # non-empty answer wins; an empty exact match is kept only as the
    # fallback so provenance still shows where the field came from.
    placeholder: dict | None = None

    def _exact(entry: dict, key: str) -> dict:
        return {
            "answer": entry.get("answer", ""),
            "source": entry.get("source", "base"),
            "question_key": entry.get("question_key", key),
            "confidence": "high",
        }

    # 1. Exact key match
    if field_key in by_key:
        hit = _exact(by_key[field_key], field_key)
        if (hit["answer"] or "").strip():
            return hit
        placeholder = placeholder or hit

    # 2. Alias match: check if field_key maps to known aliases
    aliases = _FIELD_ALIASES.get(field_key, [])
    for alias in aliases:
        if alias in by_key:
            hit = _exact(by_key[alias], alias)
            if (hit["answer"] or "").strip():
                return hit
            placeholder = placeholder or hit

    # 3. Label-based match: normalise the label and try matching
    label_key = _normalise_key(label)
    if label_key and label_key in by_key:
        hit = _exact(by_key[label_key], label_key)
        if (hit["answer"] or "").strip():
            return hit
        placeholder = placeholder or hit

    if placeholder is not None:
        return placeholder

    # F346 — strategies 4 and 5 below are fuzzy. For never-infer fields
    # we stop here and report unresolved rather than produce a
    # confident-looking answer to a legal question.
    if never_infer:
        return empty

    # 4. Partial match on answer-book keys — by shared TOKENS, not
    # substrings. F394: substring matching sent an ethnicity answer as
    # "City" (city ⊂ ethnicity), a relocation answer as "Location"
    # (location ⊂ relocation) and a language answer as "Age". A match
    # now needs the shorter key's meaningful tokens to all appear in the
    # longer key with only qualifier words left over, so "linkedin_url" ↔
    # "linkedin_profile_url" and "phone" ↔ "phone_number" still match
    # while "country" ↔ "where_are_you_currently_residing_city_and_country"
    # and "years_experience" ↔ "years_of_python_experience" do not (those
    # are asked, then saved under their own label).
    for qk, entry in by_key.items():
        if not qk or not (entry.get("answer") or "").strip():
            continue
        if _tokens_overlap(field_key, qk) or _tokens_overlap(label_key, qk):
            return {
                "answer": entry.get("answer", ""),
                "source": entry.get("source", "base"),
                "question_key": qk,
                "confidence": "medium",
            }

    # 5. Category-based fallback: use category hints to find a plausible match
    hint_cat = _guess_category(field_key, label)
    if hint_cat and hint_cat in by_category:
        # Pick the first entry in the hinted category with a non-empty answer
        for entry in by_category[hint_cat]:
            if entry.get("answer"):
                return {
                    "answer": entry.get("answer", ""),
                    "source": entry.get("source", "base"),
                    "question_key": entry.get("question_key", ""),
                    "confidence": "low",
                }

    return empty


_TOKEN_STOPWORDS = frozenset((
    "a", "an", "and", "any", "are", "as", "at", "be", "been", "by", "can", "currently", "do", "does", "for", "from",
    "have", "how", "if", "in", "is", "it", "me", "my", "no", "not", "now", "of", "on", "or", "our", "please", "so",
    "the", "this", "to", "us", "we", "what", "whats", "which", "who", "will", "with", "would", "you", "your", "yes",
))


def _meaningful_tokens(key: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (key or "").lower()) if len(t) >= 3 and t not in _TOKEN_STOPWORDS}


_QUALIFIER_TOKENS = frozenset((
    # Words that narrow a key without changing what it asks for:
    # "linkedin_profile_url" is still the LinkedIn URL; "years_of_python_
    # experience" is NOT the years-of-experience answer.
    "url", "link", "profile", "number", "address", "page", "full", "current", "contact", "primary",
    "home", "mobile", "cell", "personal", "work", "preferred", "best", "main", "handle",
))


def _tokens_overlap(a: str, b: str) -> bool:
    """True when one key's meaningful tokens are the other's plus only
    qualifier words (see strategy 4)."""
    ta, tb = _meaningful_tokens(a), _meaningful_tokens(b)
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return short <= long_ and (long_ - short) <= _QUALIFIER_TOKENS


def _guess_category(field_key: str, label: str) -> str:
    """Guess the answer-book category from a field key or label."""
    combined = f"{field_key} {label}".lower()
    for term, cat in _CATEGORY_HINTS.items():
        if term in combined:
            return cat
    return ""
