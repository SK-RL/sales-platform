"""F347 — server-side submission: the adapter contract and the apply gate.

These cover the parts that decide whether we send anything at all. The
Greenhouse DOM selectors can only be proven against a live posting, so
what's pinned here is the logic around them: option coercion, human-wall
detection, the registry, the status machine, and the rule that only a
positive confirmation counts as a submission.
"""

import asyncio

import pytest

from app.api.v1.applications import VALID_TRANSITIONS
from app.services.submitters import (
    BaseSubmitter,
    SubmitOutcome,
    auto_submittable_platforms,
    get_submitter,
    register_submitter,
    submit_platforms,
    unregister_submitter,
)
from app.services.submitters.base import coerce_option, detect_human_wall
from app.services.submitters.greenhouse import GreenhouseSubmitter


class TestOptionCoercion:
    """A select must get one of its own options or nothing."""

    def test_exact_option_is_used(self):
        assert coerce_option("Yes", ["Yes", "No"]) == "Yes"

    def test_case_insensitive_match(self):
        assert coerce_option("yes", ["Yes", "No"]) == "Yes"

    def test_yes_no_synonyms_map(self):
        assert coerce_option("true", ["Yes", "No"]) == "Yes"
        assert coerce_option("n", ["Yes", "No"]) == "No"

    def test_unmappable_answer_returns_none_not_nearest(self):
        """The F346 rule at the DOM layer: no confident mapping means
        unplaceable, not 'pick the closest option'."""
        assert coerce_option("Prefer not to say", ["Yes", "No"]) is None

    def test_empty_answer_is_none(self):
        assert coerce_option("", ["Yes", "No"]) is None

    def test_free_text_passes_through_when_no_options(self):
        assert coerce_option("Bengaluru", []) == "Bengaluru"

    # Greenhouse ships {"value","label"} options; Recruitee ships plain
    # strings. Both must work — the dict shape used to fall through an
    # isinstance(str) filter, so no Greenhouse select ever auto-filled.
    GH_YES_NO = [{"value": "1", "label": "Yes"}, {"value": "0", "label": "No"}]

    def test_dict_options_match_on_label_and_return_value(self):
        assert coerce_option("Yes", self.GH_YES_NO) == "1"

    def test_dict_options_match_on_value(self):
        assert coerce_option("0", self.GH_YES_NO) == "0"

    def test_dict_options_synonym_maps_to_value(self):
        assert coerce_option("true", self.GH_YES_NO) == "1"

    def test_dict_options_reject_unmappable(self):
        assert coerce_option("Prefer not to say", self.GH_YES_NO) is None

    def test_recruitee_style_multi_choice(self):
        opts = ["Market research", "SEO / SEM", "Social Media"]
        assert coerce_option("SEO / SEM", opts) == "SEO / SEM"
        assert coerce_option("Growth hacking", opts) is None


class TestHumanWallDetection:
    @pytest.mark.parametrize(
        "html",
        [
            '<div class="g-recaptcha"></div>',
            "<p>Please verify you are human</p>",
            "<div>A verification code has been sent to your email</div>",
            "<button>Sign in to apply</button>",
        ],
    )
    def test_walls_are_detected(self, html):
        assert detect_human_wall(html) is not None

    def test_ordinary_form_is_not_a_wall(self):
        assert detect_human_wall("<form><input id='first_name'></form>") is None

    # F368 — the anchor iframe is mounted by BOTH the v2 checkbox and
    # invisible v3. Only the former needs a person. Srcs are the live
    # ones (Ashby ramp; BambooHR icmarkets), entity-encoded as
    # page.content() returns them.
    ASHBY_V3 = (
        '<iframe src="https://www.recaptcha.net/recaptcha/api2/anchor?ar=1&amp;k=6LeFb'
        '&amp;co=aHR0&amp;hl=en&amp;v=Bnq&amp;size=invisible&amp;anchor-ms=20000"></iframe>'
        '<textarea name="g-recaptcha-response"></textarea>'
    )
    BAMBOO_V2 = (
        '<iframe src="https://www.google.com/recaptcha/api2/anchor?ar=1&amp;k=6LfZ'
        '&amp;co=aHR&amp;hl=en&amp;v=Bnq&amp;size=normal&amp;anchor-ms="></iframe>'
    )

    def test_invisible_recaptcha_v3_is_not_a_wall(self):
        """Ashby. Treating this as a wall made a drivable ATS
        extraction-only — the false-positive failure the docstring
        warns about, and it was live for F366."""
        assert detect_human_wall(self.ASHBY_V3) is None

    def test_recaptcha_checkbox_is_a_wall(self):
        assert detect_human_wall(self.BAMBOO_V2) == "recaptcha/api2/anchor"

    def test_image_challenge_frame_is_a_wall(self):
        assert detect_human_wall('<iframe src="https://www.google.com/recaptcha/api2/bframe?hl=en"></iframe>')

    def test_invisible_widget_is_not_a_wall(self):
        """The v2-invisible pattern: a .g-recaptcha with data-size=invisible."""
        assert detect_human_wall('<div class="g-recaptcha" data-sitekey="k" data-size="invisible"></div>') is None

    def test_one_visible_anchor_among_invisible_still_walls(self):
        assert detect_human_wall(self.ASHBY_V3 + self.BAMBOO_V2) == "recaptcha/api2/anchor"

    def test_datadome_interstitial_is_a_wall(self):
        assert detect_human_wall("<script>var dd={'rt':'c','cid':'x'}</script>") is not None

    def test_none_html_is_safe(self):
        assert detect_human_wall("") is None


class TestRegistry:
    def test_greenhouse_is_registered(self):
        s = get_submitter("greenhouse")
        assert isinstance(s, GreenhouseSubmitter)

    def test_unknown_platform_returns_none(self):
        assert get_submitter("workday") is None

    def test_platform_lookup_is_case_and_space_insensitive(self):
        assert isinstance(get_submitter("  GreenHouse "), GreenhouseSubmitter)

    def test_auto_submittable_is_the_intersection(self):
        """Submitting a form we can't read is meaningless, so the gate's
        set is submitters ∩ extractors."""
        from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS

        assert auto_submittable_platforms() == (
            submit_platforms() & SUPPORTED_QUESTION_PLATFORMS
        )
        assert "greenhouse" in auto_submittable_platforms()
        assert "workday" not in auto_submittable_platforms()

    def test_register_and_unregister_roundtrip(self):
        class FakeSubmitter(BaseSubmitter):
            platform = "fake_ats"

            async def submit(self, **kwargs):
                return SubmitOutcome(status="submitted", confirmation_text="ok")

        register_submitter(FakeSubmitter)
        try:
            assert isinstance(get_submitter("fake_ats"), FakeSubmitter)
            # Registered but not extractable → still not auto-submittable.
            assert "fake_ats" not in auto_submittable_platforms()
        finally:
            unregister_submitter("fake_ats")
        assert get_submitter("fake_ats") is None

    def test_submitter_without_platform_is_rejected(self):
        class Nameless(BaseSubmitter):
            async def submit(self, **kwargs):
                return SubmitOutcome(status="failed")

        with pytest.raises(ValueError):
            register_submitter(Nameless)


class TestSubmitOutcome:
    def test_only_submitted_is_ok(self):
        assert SubmitOutcome(status="submitted").ok
        assert not SubmitOutcome(status="failed").ok
        assert not SubmitOutcome(status="blocked").ok

    def test_defaults_are_independent_lists(self):
        a, b = SubmitOutcome(status="failed"), SubmitOutcome(status="failed")
        a.detected_issues.append("x")
        assert b.detected_issues == []


class TestGreenhouseConfirmation:
    """Only positive evidence counts as a submission.

    Verified live on 2026-09-10 against a real Greenhouse posting
    (Figma 5426468004), with dry_run=False so the submit button was
    genuinely clicked. The form carried a deliberately malformed email
    so the ATS would reject it and no application could be created:

        status             : failed
        confirmation_text  : None
        detected_issues    : ['no_confirmation']
        error              : submitted the form but saw no confirmation

    `no_confirmation` is only set on the post-click branch, so that
    output proves the click executed and the adapter still refused to
    report success. This is the failure this class exists for — a
    Greenhouse form that fails inline validation stays on the page and
    raises nothing, so "no exception" must never read as "submitted".

    Still unproven: the POSITIVE path. A genuinely successful submission
    producing a confirmation string requires applying to a real job.
    """

    @pytest.mark.parametrize(
        "html",
        [
            "<h1>Thank you for applying</h1>",
            "<div>Your application has been submitted</div>",
            "<p>APPLICATION SUBMITTED</p>",
        ],
    )
    def test_confirmation_markers_are_recognised(self, html):
        session = _FakeSession(html=html, url="https://boards.greenhouse.io/x")
        assert _confirm(session) is not None

    def test_silent_failure_is_not_a_confirmation(self):
        """A Greenhouse form that fails inline validation stays on the
        page and raises nothing. That must not read as success."""
        session = _FakeSession(
            html="<form><span class='error'>This field is required</span></form>",
            url="https://boards.greenhouse.io/acme/jobs/123",
        )
        assert _confirm(session) is None

    def test_confirmation_url_counts(self):
        session = _FakeSession(
            html="<div>nothing useful</div>",
            url="https://boards.greenhouse.io/acme/confirmation",
        )
        assert _confirm(session) is not None


class TestStatusMachine:
    def test_new_states_exist(self):
        for state in ("in_flight", "needs_user", "failed"):
            assert state in VALID_TRANSITIONS, state

    def test_in_flight_can_resolve_every_way(self):
        assert set(VALID_TRANSITIONS["in_flight"]) == {
            "submitted", "needs_user", "failed", "withdrawn"
        }

    def test_needs_user_allows_manual_completion(self):
        """The 'I've Applied' escape hatch."""
        assert "applied" in VALID_TRANSITIONS["needs_user"]

    def test_failed_can_be_retried_into_flight(self):
        assert "in_flight" in VALID_TRANSITIONS["failed"]

    def test_terminal_states_stay_terminal(self):
        assert VALID_TRANSITIONS["rejected"] == []
        assert VALID_TRANSITIONS["withdrawn"] == []

    def test_every_target_is_itself_a_known_state(self):
        for src, targets in VALID_TRANSITIONS.items():
            for t in targets:
                assert t in VALID_TRANSITIONS, f"{src} -> {t} is not a known state"


def _confirm(session):
    """Run the async confirmation check. This repo has no pytest-asyncio;
    the convention (see test_playwright_browser) is asyncio.run()."""
    return asyncio.run(GreenhouseSubmitter._await_confirmation(session))


class _FakeSession:
    """Minimal stand-in for BrowserSession for the pure-logic helpers."""

    def __init__(self, html: str, url: str):
        self._html, self._url = html, url

    async def html(self) -> str:
        return self._html

    async def url(self) -> str:
        return self._url


class TestVerifiedAgainstLiveGreenhouseDOM:
    """Facts read off a real posting (job-boards.greenhouse.io, Figma
    5426468004) on 2026-09-10. Each of these was wrong in the first
    draft of the adapter, which is why they're pinned as tests rather
    than left as comments."""

    def test_custom_question_is_addressed_by_id_first(self):
        """The live page had 22 [id^="question_"] and ZERO
        [name^="question_"]. A name-only selector matched nothing."""
        sel = GreenhouseSubmitter._field_selector("question_12497121004")
        assert sel.startswith("#question_12497121004")

    def test_name_attribute_is_kept_as_a_fallback(self):
        """Older embedded boards still render `name`."""
        sel = GreenhouseSubmitter._field_selector("question_12497121004")
        assert '[name="question_12497121004"]' in sel

    def test_selector_escaping_does_not_double_backslashes(self):
        assert GreenhouseSubmitter._field_selector('a"b').count("\\") == 2

    def test_invisible_recaptcha_is_not_a_human_wall(self):
        """THE critical one. Every Greenhouse posting loads reCAPTCHA
        Enterprise v3 — invisible, score-based, no challenge to solve.
        The original bare "recaptcha" marker matched it, so every
        application would have returned blocked -> needs_user and the
        feature would have silently done nothing."""
        live_page = (
            '<script src="https://www.recaptcha.net/recaptcha/enterprise.js'
            '?render=6LfmcbcpAAAAAChNTbhUShz"></script>'
            '<textarea name="g-recaptcha-response" id="g-recaptcha-response-100000">'
            "</textarea>"
        )
        assert detect_human_wall(live_page) is None

    def test_interactive_recaptcha_v2_is_still_a_wall(self):
        v2 = '<iframe src="https://www.google.com/recaptcha/api2/anchor?k=abc"></iframe>'
        assert detect_human_wall(v2) is not None

    def test_explicit_recaptcha_widget_is_a_wall(self):
        assert detect_human_wall('<div class="g-recaptcha"></div>') is not None

    def test_hcaptcha_checkbox_widget_is_a_wall(self):
        """F359 — verified on a live Lever apply page. The old marker was
        the single filename "hcaptcha.com/1/api.js"; Lever loads
        "js.hcaptcha.com/1/secure-api.js", so a form carrying a real
        hCaptcha checkbox came back as NO wall. That's the dangerous
        direction — we'd drive a form we cannot submit and look like
        abuse to the ATS."""
        live = (
            '<script src="https://js.hcaptcha.com/1/secure-api.js?host=jobs.lever.co"></script>'
            '<div id="h-captcha" class="h-captcha" data-sitekey="e33f87f8">'
            '<iframe title="Widget containing checkbox for hCaptcha security challenge">'
            "</iframe></div>"
        )
        assert detect_human_wall(live) is not None

    def test_datadome_challenge_page_is_a_wall(self):
        """F364 — the literal head of the 3 KB interstitial SmartRecruiters
        served headless Chromium (real browser got the full 15-field
        form). Same vendor the scraper docstring notes blocks Wellfound."""
        interstitial = (
            "<html><head><title>smartrecruiters.com</title>"
            '<script src="https://ct.captcha-delivery.com/c.js"></script>'
            "<script>var dd={'rt':'c','cid':'AHrlqAAAAAMAB9lD','hsh':'E5A9F170'}</script>"
            "</head><body></body></html>"
        )
        assert detect_human_wall(interstitial) is not None

    def test_hcaptcha_script_alone_is_not_a_wall(self):
        """Invisible/score-based hCaptcha needs no human, so the markers
        target the widget rather than any script URL — the same
        discrimination the reCAPTCHA markers make."""
        assert detect_human_wall(
            '<script src="https://js.hcaptcha.com/1/secure-api.js"></script>'
        ) is None

    def test_submit_selector_is_scoped_to_the_form(self):
        """F356 — an unscoped button[type=submit] clicks a cookie-banner
        button on any page that has one. Recruitee proved it: a live
        board carried a dozen of them ("Allow all", "Necessary")."""
        from app.services.submitters.greenhouse import _SUBMIT_SELECTOR

        parts = [p.strip() for p in _SUBMIT_SELECTOR.split(",")]
        assert parts and all(p.startswith("form ") for p in parts), _SUBMIT_SELECTOR

    def test_session_exposes_a_press_primitive(self):
        """react-select needs click -> type -> Enter; there is no native
        <select> on a modern Greenhouse board to call select_option on."""
        from app.services.playwright_browser import BrowserSession

        assert callable(getattr(BrowserSession, "press", None))
