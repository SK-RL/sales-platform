"""F369 — no ATS login is needed where the server drives a public form.

Found on production the moment the Ashby adapter (F368) landed: every
Ashby job answered ``POST /applications/prepare`` with "Platform
credentials required for ashby", and the job page's Apply button was
disabled by the same ``readiness`` rule. The ``PlatformCredential`` gate
predates server-side submission — it belonged to a flow that logged in
to the ATS as the candidate. Greenhouse, Recruitee, Workable and Ashby
application forms have no account, so demanding one there only ever
blocked.
"""

import inspect

from app.api.v1 import applications
from app.api.v1.applications import credentials_required
from app.services.submitters import auto_submittable_platforms


class TestRule:
    def test_server_side_platforms_need_no_credentials(self):
        for platform in auto_submittable_platforms():
            assert credentials_required(platform) is False, platform

    def test_ashby_specifically(self):
        """The production symptom."""
        assert credentials_required("ashby") is False

    def test_platforms_without_a_submitter_still_do(self):
        assert credentials_required("workday") is True
        assert credentials_required("linkedin") is True

    def test_case_and_space_insensitive(self):
        assert credentials_required(" Ashby ") is False

    def test_empty_platform_requires(self):
        assert credentials_required("") is True


class TestWiring:
    def test_prepare_gates_on_the_rule(self):
        src = inspect.getsource(applications.prepare_application)
        assert "if credentials_required(job.platform):" in src

    def test_readiness_reports_required(self):
        src = inspect.getsource(applications.get_apply_readiness)
        assert "cred_required = credentials_required(job.platform)" in src
        assert '"required": cred_required' in src
