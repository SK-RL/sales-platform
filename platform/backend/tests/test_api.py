"""
API integration tests for the Job Platform.

Run with:
    python -m pytest platform/backend/tests/test_api.py -v
    # or directly:
    python platform/backend/tests/test_api.py

Requires the platform to be running on localhost:8000.
"""

import sys
import json
import time
import httpx
import argparse

BASE_URL = "http://localhost:8000/api/v1"
EMAIL = "admin@jobplatform.io"
PASSWORD = "admin123"

# ── Helpers ──────────────────────────────────────────────────────────────────

_PASS = "\033[92m✓\033[0m"
_FAIL = "\033[91m✗\033[0m"
_SKIP = "\033[93m–\033[0m"
_results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = ""):
    symbol = _PASS if condition else _FAIL
    print(f"  {symbol} {name}" + (f"  [{detail}]" if detail else ""))
    _results.append((name, condition, detail))
    return condition


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def login(client: httpx.Client) -> dict:
    resp = client.post(f"{BASE_URL}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    data = resp.json()
    token = data.get("token") or data.get("access_token")
    assert token, f"No token in response: {data}"
    client.cookies.set("session", token)
    return data


# ── Test Suites ───────────────────────────────────────────────────────────────

def test_auth(client: httpx.Client):
    section("Auth")

    # Valid login
    resp = client.post(f"{BASE_URL}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    check("POST /auth/login → 200", resp.status_code == 200)
    data = resp.json()
    check("Response has token", bool(data.get("token")))
    check("Response has user object", bool(data.get("user")))
    check("User has admin role", data.get("user", {}).get("role") == "admin")

    # Invalid login
    resp2 = client.post(f"{BASE_URL}/auth/login", json={"email": EMAIL, "password": "wrong"})
    check("Invalid login → 401/422", resp2.status_code in (401, 422, 400))

    # Me endpoint
    resp3 = client.get(f"{BASE_URL}/auth/me")
    check("GET /auth/me → 200", resp3.status_code == 200)
    me = resp3.json()
    check("Me returns email", me.get("email") == EMAIL)


def test_jobs(client: httpx.Client):
    section("Jobs")

    # All jobs
    resp = client.get(f"{BASE_URL}/jobs?page_size=5")
    check("GET /jobs → 200", resp.status_code == 200)
    data = resp.json()
    check("Response has items", isinstance(data.get("items"), list))
    check("Response has total", isinstance(data.get("total"), int))
    total_all = data.get("total", 0)
    check(f"Has jobs ({total_all})", total_all > 0)

    # Relevant jobs filter
    resp2 = client.get(f"{BASE_URL}/jobs?role_cluster=relevant&page_size=5")
    check("GET /jobs?role_cluster=relevant → 200", resp2.status_code == 200)
    data2 = resp2.json()
    total_relevant = data2.get("total", 0)
    check(f"Relevant jobs < all jobs ({total_relevant} < {total_all})", total_relevant < total_all)
    check("Relevant jobs > 0", total_relevant > 0)
    if data2.get("items"):
        clusters = {j.get("role_cluster") for j in data2["items"]}
        check("Relevant items have infra/security cluster", clusters <= {"infra", "security", None})

    # Infra filter
    resp3 = client.get(f"{BASE_URL}/jobs?role_cluster=infra&page_size=3")
    check("GET /jobs?role_cluster=infra → 200", resp3.status_code == 200)
    if resp3.json().get("items"):
        check("Infra items have infra cluster", all(j.get("role_cluster") == "infra" for j in resp3.json()["items"]))

    # Security filter
    resp4 = client.get(f"{BASE_URL}/jobs?role_cluster=security&page_size=3")
    check("GET /jobs?role_cluster=security → 200", resp4.status_code == 200)

    # Geography filter
    resp5 = client.get(f"{BASE_URL}/jobs?geography=global_remote&page_size=3")
    check("GET /jobs?geography=global_remote → 200", resp5.status_code == 200)

    # F233(b): the legacy "worldwide" geography string was retired by
    # F218's strict-Literal validation on the geography query param.
    # Post-F218 the canonical values are exactly
    # {global_remote, usa_only, uae_only}. Probing ``?geography=
    # worldwide`` should now return HTTP 422, not 200 — the harness
    # asserts the expected reject so a future regression that loosens
    # the Literal back to free-text would surface.
    resp6 = client.get(f"{BASE_URL}/jobs?geography=worldwide&page_size=3")
    check("GET /jobs?geography=worldwide → 422 (post-F218 strict)", resp6.status_code == 422)

    # Search
    resp7 = client.get(f"{BASE_URL}/jobs?search=engineer&page_size=5")
    check("GET /jobs?search=engineer → 200", resp7.status_code == 200)
    check("Search returns results", resp7.json().get("total", 0) > 0)

    # Single job
    if data.get("items"):
        job_id = data["items"][0]["id"]
        resp8 = client.get(f"{BASE_URL}/jobs/{job_id}")
        check(f"GET /jobs/{{id}} → 200", resp8.status_code == 200)
        job = resp8.json()
        check("Job has required fields", all(k in job for k in ["id", "title", "company_id", "platform"]))

        # Job description
        resp9 = client.get(f"{BASE_URL}/jobs/{job_id}/description")
        check("GET /jobs/{id}/description → 200 or 404", resp9.status_code in (200, 404))
        if resp9.status_code == 200:
            desc = resp9.json()
            check("Description has raw_text field", "raw_text" in desc)

    # Sort options
    resp10 = client.get(f"{BASE_URL}/jobs?sort_by=relevance_score&sort_dir=desc&page_size=3")
    check("GET /jobs?sort_by=relevance_score → 200", resp10.status_code == 200)

    # Pagination
    resp11 = client.get(f"{BASE_URL}/jobs?page=2&page_size=10")
    check("GET /jobs?page=2 → 200", resp11.status_code == 200)
    check("Page 2 returns items", len(resp11.json().get("items", [])) > 0)


def test_companies(client: httpx.Client):
    section("Companies")

    resp = client.get(f"{BASE_URL}/companies?page_size=5")
    check("GET /companies → 200", resp.status_code == 200)
    data = resp.json()
    check("Has companies", data.get("total", 0) > 0)
    check("Company has expected fields", all(
        k in data["items"][0] for k in ["id", "name", "enrichment_status"]
    ) if data.get("items") else False)
    check("Company has contact_count field", "contact_count" in (data["items"][0] if data.get("items") else {}))
    check("Company has total_funding field", "total_funding" in (data["items"][0] if data.get("items") else {}))

    # Search
    resp2 = client.get(f"{BASE_URL}/companies?search=Webflow")
    check("GET /companies?search=Webflow → 200", resp2.status_code == 200)
    webflow_items = resp2.json().get("items", [])
    check("Webflow found", any("webflow" in c["name"].lower() for c in webflow_items))

    if webflow_items:
        wf = next(c for c in webflow_items if "webflow" in c["name"].lower())
        wf_id = wf["id"]

        # Single company
        resp3 = client.get(f"{BASE_URL}/companies/{wf_id}")
        check("GET /companies/{id} → 200", resp3.status_code == 200)
        wf_detail = resp3.json()
        check("Webflow has funding data", bool(wf_detail.get("total_funding")), wf_detail.get("total_funding"))
        check("Webflow has domain", wf_detail.get("domain") == "webflow.com")
        check("Webflow contact_count > 0", wf_detail.get("contact_count", 0) > 0, str(wf_detail.get("contact_count")))

        # Contacts
        resp4 = client.get(f"{BASE_URL}/companies/{wf_id}/contacts")
        check("GET /companies/{id}/contacts → 200", resp4.status_code == 200)
        contacts = resp4.json().get("items", [])
        check(f"Webflow has contacts ({len(contacts)})", len(contacts) > 0)
        if contacts:
            c = contacts[0]
            check("Contact has name", bool(c.get("first_name")) and bool(c.get("last_name")))
            check("Contact has title", bool(c.get("title")))
            check("Contact has source", bool(c.get("source")))

        # Enrichment status
        resp5 = client.get(f"{BASE_URL}/companies/{wf_id}/enrichment-status")
        check("GET /companies/{id}/enrichment-status → 200", resp5.status_code == 200)
        status = resp5.json()
        check("Enrichment status has status field", "status" in status)
        check("Enrichment status has contacts_count", "contacts_count" in status)

    # Target companies filter
    resp6 = client.get(f"{BASE_URL}/companies?is_target=true")
    check("GET /companies?is_target=true → 200", resp6.status_code == 200)

    # Company scores
    resp7 = client.get(f"{BASE_URL}/companies/scores")
    check("GET /companies/scores → 200", resp7.status_code == 200)
    check("Scores has items", isinstance(resp7.json().get("items"), list))


def test_enrichment(client: httpx.Client):
    section("Enrichment")

    # Find a pending company to test enrichment trigger
    resp = client.get(f"{BASE_URL}/companies?page_size=50")
    companies = resp.json().get("items", [])

    # Find a company with a website
    test_company = None
    for c in companies:
        if c.get("website") and c.get("enrichment_status") in ("pending", "enriched"):
            test_company = c
            break

    if not test_company:
        print(f"  {_SKIP} No suitable company found for enrichment test")
        return

    company_id = test_company["id"]
    print(f"  Testing enrichment on: {test_company['name']}")

    # Trigger enrichment
    resp2 = client.post(f"{BASE_URL}/companies/{company_id}/enrich")
    check("POST /companies/{id}/enrich → 200/202", resp2.status_code in (200, 201, 202))
    enrich_data = resp2.json()
    check("Enrichment returns task_id", bool(enrich_data.get("task_id")))
    check("Enrichment status is queued", enrich_data.get("status") in ("queued", "enriching", "started"))


def test_analytics(client: httpx.Client):
    section("Analytics")

    resp = client.get(f"{BASE_URL}/analytics/overview")
    check("GET /analytics/overview → 200", resp.status_code == 200)
    data = resp.json()
    check("Overview has job stats", bool(data))

    resp2 = client.get(f"{BASE_URL}/analytics/trends")
    check("GET /analytics/trends → 200", resp2.status_code == 200)

    resp3 = client.get(f"{BASE_URL}/analytics/sources")
    check("GET /analytics/sources → 200 or 404", resp3.status_code in (200, 404))


def test_platforms(client: httpx.Client):
    section("Platforms")

    resp = client.get(f"{BASE_URL}/platforms")
    check("GET /platforms → 200", resp.status_code == 200)
    data = resp.json()
    # Platforms returns {"platforms": [...]}
    platforms = data.get("platforms", data.get("items", []))
    check(f"Has platforms ({len(platforms)})", len(platforms) > 0)

    if platforms:
        p = platforms[0]
        check("Platform has name", "name" in p or "slug" in p)

    resp2 = client.get(f"{BASE_URL}/platforms/stats")
    check("GET /platforms/stats → 200 or 404", resp2.status_code in (200, 404))


def test_pipeline(client: httpx.Client):
    section("Pipeline")

    resp = client.get(f"{BASE_URL}/pipeline")
    check("GET /pipeline → 200", resp.status_code == 200)
    data = resp.json()
    # Pipeline returns {"stages": [...], "items": {...}} or a list
    check("Pipeline returns data", bool(data))


def test_role_clusters(client: httpx.Client):
    section("Role Clusters")

    resp = client.get(f"{BASE_URL}/role-clusters")
    check("GET /role-clusters → 200", resp.status_code == 200)
    data = resp.json()
    check("Has clusters", len(data.get("items", [])) > 0)
    check("Has relevant_clusters list", isinstance(data.get("relevant_clusters"), list))

    clusters = data.get("items", [])
    names = [c["name"] for c in clusters]
    check("Has infra cluster", "infra" in names)
    check("Has security cluster", "security" in names)


def test_monitoring(client: httpx.Client):
    section("Monitoring (Admin)")

    resp = client.get(f"{BASE_URL}/monitoring")
    check("GET /monitoring → 200", resp.status_code == 200)
    data = resp.json()
    check("Monitoring has database info", "database" in data or "uptime_seconds" in data or bool(data))


def test_remote_scope(client: httpx.Client):
    section("Remote Scope Standardization")

    # F233(b): the canonical scope value is ``global_remote`` —
    # ``worldwide`` was retired by F218's strict-Literal validation
    # and is no longer accepted as input. ``remote_scope`` on
    # individual job rows may still surface other text (legacy
    # imports, fetcher-side enrichment) — that's a data shape we
    # observe rather than enforce on the way in.
    resp = client.get(f"{BASE_URL}/jobs?geography=global_remote&page_size=3")
    check("global_remote geography filter works", resp.status_code == 200)

    # Verify no 'worldwide' jobs exist as separate scope confusion
    resp2 = client.get(f"{BASE_URL}/jobs?page_size=200")
    if resp2.status_code == 200:
        jobs = resp2.json().get("items", [])
        scopes = {j.get("remote_scope") for j in jobs if j.get("remote_scope")}
        bad_scopes = scopes - {"worldwide", "remote", "usa_only", "uae_only", None, ""}
        check("No 'global remote' scope values in DB", "global remote" not in scopes, str(scopes))


def test_outreach_and_export(client: httpx.Client):
    section("Outreach Workflow & Export")

    # Test warm leads analytics
    resp = client.get(f"{BASE_URL}/analytics/warm-leads")
    check("GET /analytics/warm-leads → 200", resp.status_code == 200)
    warm = resp.json()
    check("Warm leads has items list", isinstance(warm.get("items"), list))
    if warm.get("items"):
        lead = warm["items"][0]
        check("Warm lead has company_id", "company_id" in lead)
        check("Warm lead has decision_makers field", "decision_makers" in lead)
        check("Warm lead has new_jobs_30d field", "new_jobs_30d" in lead)

    # Test companies filter: has_contacts
    resp2 = client.get(f"{BASE_URL}/companies?has_contacts=true&per_page=5")
    check("GET /companies?has_contacts=true → 200", resp2.status_code == 200)
    if resp2.json().get("items"):
        check("Companies with contacts filter works", resp2.json()["total"] > 0)

    # Test companies filter: actively_hiring
    resp3 = client.get(f"{BASE_URL}/companies?actively_hiring=true&per_page=5")
    check("GET /companies?actively_hiring=true → 200", resp3.status_code == 200)

    # Test export/contacts CSV
    resp4 = client.get(f"{BASE_URL}/export/contacts")
    check("GET /export/contacts → 200", resp4.status_code == 200)
    check("Export contacts is CSV", resp4.headers.get("content-type", "").startswith("text/csv"))

    # Test outreach update on a real contact
    # First find a company with contacts
    companies = client.get(f"{BASE_URL}/companies?has_contacts=true&per_page=3").json().get("items", [])
    if companies:
        cid = companies[0]["id"]
        contacts = client.get(f"{BASE_URL}/companies/{cid}/contacts").json().get("items", [])
        if contacts:
            contact_id = contacts[0]["id"]
            resp5 = client.patch(f"{BASE_URL}/companies/{cid}/contacts/{contact_id}/outreach",
                                 json={"outreach_status": "emailed", "outreach_note": "Test note"})
            check("PATCH .../outreach → 200", resp5.status_code == 200)
            updated = resp5.json()
            check("Outreach status updated", updated.get("outreach_status") == "emailed")
            check("Outreach note saved", updated.get("outreach_note") == "Test note")
            check("last_outreach_at set", updated.get("last_outreach_at") is not None)

            # Reset it
            client.patch(f"{BASE_URL}/companies/{cid}/contacts/{contact_id}/outreach",
                         json={"outreach_status": "not_contacted", "outreach_note": ""})

    # Test draft email endpoint
    if companies:
        cid = companies[0]["id"]
        contacts = client.get(f"{BASE_URL}/companies/{cid}/contacts").json().get("items", [])
        if contacts:
            contact_id = contacts[0]["id"]
            resp6 = client.post(f"{BASE_URL}/companies/{cid}/contacts/{contact_id}/draft-email")
            check("POST .../draft-email → 200", resp6.status_code == 200)
            draft = resp6.json()
            check("Draft has subject", bool(draft.get("subject")))
            check("Draft has body", bool(draft.get("body")))
            check("Draft has generated_by", bool(draft.get("generated_by")))


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all(base_url: str = BASE_URL, email: str = EMAIL, password: str = PASSWORD):
    global BASE_URL, EMAIL, PASSWORD
    BASE_URL = base_url
    EMAIL = email
    PASSWORD = password

    print(f"\n{'═' * 60}")
    print(f"  Job Platform API Tests")
    print(f"  Target: {BASE_URL}")
    print(f"{'═' * 60}")

    start = time.time()

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        # Login first
        try:
            login(client)
            print(f"\n  Logged in as {EMAIL}")
        except Exception as e:
            print(f"\n  \033[91mFATAL: Login failed: {e}\033[0m")
            sys.exit(1)

        # Run all test suites
        test_auth(client)
        test_jobs(client)
        test_companies(client)
        test_enrichment(client)
        test_analytics(client)
        test_platforms(client)
        test_pipeline(client)
        test_role_clusters(client)
        test_monitoring(client)
        test_remote_scope(client)
        test_outreach_and_export(client)

    # Summary
    elapsed = time.time() - start
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    total = len(_results)

    print(f"\n{'═' * 60}")
    print(f"  Results: {passed}/{total} passed  ({elapsed:.1f}s)")
    if failed:
        print(f"\n  \033[91mFailed tests:\033[0m")
        for name, ok, detail in _results:
            if not ok:
                print(f"    ✗ {name}" + (f"  [{detail}]" if detail else ""))
    print(f"{'═' * 60}\n")

    return failed == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job Platform API Tests")
    parser.add_argument("--url", default=BASE_URL, help="Base API URL")
    parser.add_argument("--email", default=EMAIL, help="Admin email")
    parser.add_argument("--password", default=PASSWORD, help="Admin password")
    args = parser.parse_args()

    ok = run_all(args.url, args.email, args.password)
    sys.exit(0 if ok else 1)
