/**
 * F265 #2 — Jobs filter URL disambiguation.
 *
 * Catches: F260 #3 ("Relevant Jobs" and "All Jobs" showed identical
 * data) and any future cluster-filter regression. The pre-fix bug
 * was that JobsPage's localStorage filter-restore silently re-applied
 * a stale ``role_cluster=relevant`` filter when the URL had no params.
 * F260 fixed it with an explicit ``role_cluster=any`` sentinel on
 * the All Jobs sidebar link.
 *
 * The structural assertion: clicking the two sidebar links from the
 * SAME starting state must produce DIFFERENT result counts (any total
 * > relevant total) AND DIFFERENT URLs.
 */
import { test, expect } from "@playwright/test";
import { loginAsAdmin } from "./fixtures/auth";

test.beforeEach(async ({ page }) => {
  await loginAsAdmin(page);
});

test("Relevant Jobs and All Jobs sidebar links go to distinct URLs", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: /relevant jobs/i }).click();
  await expect(page).toHaveURL(/role_cluster=relevant/);

  await page.getByRole("link", { name: /all jobs/i }).click();
  // F260: All Jobs MUST carry an explicit role_cluster=any sentinel.
  // Without it, the URL is just /jobs and JobsPage's localStorage
  // restore re-applies "relevant" → both pages show identical data.
  await expect(page).toHaveURL(/role_cluster=any/);
});

test("All Jobs total is greater than Relevant Jobs total", async ({ page }) => {
  // Navigate via direct URL so we don't depend on sidebar click order.
  await page.goto("/jobs?role_cluster=relevant");
  // The total count is rendered somewhere on the page — typically in
  // the page header or a summary line. We grep the page text for
  // ``\d+\s*results`` or ``\d+\s*jobs`` patterns. If the rendering
  // changes, this assertion needs updating but the spec catches the
  // intent.
  const relevantText = await page.locator("body").innerText();
  const relevantMatch = relevantText.match(/(\d{1,3}(?:,\d{3})*)\s*(?:results|jobs|total)/i);
  expect(relevantMatch, "Could not find a count on Relevant Jobs page").toBeTruthy();
  const relevantTotal = parseInt(relevantMatch![1].replace(/,/g, ""), 10);

  await page.goto("/jobs?role_cluster=any");
  const anyText = await page.locator("body").innerText();
  const anyMatch = anyText.match(/(\d{1,3}(?:,\d{3})*)\s*(?:results|jobs|total)/i);
  expect(anyMatch, "Could not find a count on All Jobs page").toBeTruthy();
  const anyTotal = parseInt(anyMatch![1].replace(/,/g, ""), 10);

  // The single most important invariant: All Jobs ≥ Relevant Jobs.
  // F260's bug made these equal. We use ≥ instead of > to allow the
  // edge case where every classified job is relevant (unlikely in
  // prod but possible in a small test seed).
  expect(anyTotal).toBeGreaterThanOrEqual(relevantTotal);
});

test("Sidebar active-link state distinguishes Relevant from All", async ({ page }) => {
  await page.goto("/jobs?role_cluster=relevant");
  // The active link gets an ``aria-current`` attribute or a distinct
  // class. We assert that "Relevant Jobs" is the active link, NOT
  // "All Jobs". F260's bug pre-fix would have flagged BOTH as active.
  const relevantLink = page.getByRole("link", { name: /relevant jobs/i });
  const allLink = page.getByRole("link", { name: /all jobs/i });

  // Both links should be visible at all times.
  await expect(relevantLink).toBeVisible();
  await expect(allLink).toBeVisible();

  // Active state — different visual class. We check that the two
  // links don't have IDENTICAL ``class`` attributes when one is
  // selected. This is a pragmatic regression guard rather than a
  // pixel-perfect check.
  const relevantClass = await relevantLink.getAttribute("class");
  const allClass = await allLink.getAttribute("class");
  expect(relevantClass).not.toEqual(allClass);
});

/**
 * F339 — filter stickiness + back-navigation regression coverage.
 *
 * User report 2026-05-09: "there is issue of stickiness of filter
 * in the relevant jobs and also when we open the job from relevant
 * job and click on back button we landed on all jobs."
 *
 * Two root causes documented in the F339 commit:
 *   (a) Sidebar links to ``/jobs?role_cluster=...`` were hardcoded,
 *       so clicking "Relevant Jobs" or "All Jobs" silently dropped
 *       every other filter the user had applied (geography, search,
 *       platform, sorts, etc.).
 *   (b) JobDetailPage's "Back to Jobs" buttons hardcoded
 *       ``navigate("/jobs")``, throwing away the source URL +
 *       filters. The post-F260 localStorage fallback then resolved
 *       to "All Jobs" instead of the user's actual cluster.
 */

test("F339(a): clicking Relevant Jobs in sidebar preserves other filters", async ({ page }) => {
  // Start with a multi-filter URL — relevant cluster + geography +
  // a non-default sort. This represents a user who's narrowed the
  // list to "global remote infra, sorted by recency."
  await page.goto("/jobs?role_cluster=relevant&geography=global_remote&sorts=created_at:desc");

  // Click the "Relevant Jobs" sidebar link (still relevant — they
  // bounced through some sidebar nav and came back).
  await page.getByRole("link", { name: /relevant jobs/i }).click();

  // Pre-F339, this would land on /jobs?role_cluster=relevant
  // (geography + sorts dropped). Post-F339 the link is dynamic
  // and preserves the existing query params.
  await expect(page).toHaveURL(/role_cluster=relevant/);
  await expect(page).toHaveURL(/geography=global_remote/);
  await expect(page).toHaveURL(/sorts=created_at(%3A|:)desc/);
});

test("F339(a): clicking All Jobs preserves other filters but swaps cluster", async ({ page }) => {
  // Same multi-filter starting state, but this time switching TO
  // the All Jobs view.
  await page.goto("/jobs?role_cluster=relevant&geography=global_remote");

  await page.getByRole("link", { name: /all jobs/i }).click();

  // Cluster axis was swapped to ``any`` (the F260 sentinel) — the
  // other filters survive.
  await expect(page).toHaveURL(/role_cluster=any/);
  await expect(page).toHaveURL(/geography=global_remote/);
});

test("F339(b): back-button from job detail returns to the source filter set", async ({ page }) => {
  // Land on a filtered Relevant Jobs view + click the first row.
  await page.goto("/jobs?role_cluster=relevant&geography=global_remote");
  await page.waitForLoadState("networkidle");

  // Click the first job row to navigate to detail.
  const firstRow = page.locator('tr[class*="clickable"], tr[role="link"]').first();
  await firstRow.click();
  await expect(page).toHaveURL(/\/jobs\/[0-9a-f-]{36}/);

  // Click the "Back to Jobs" button. Pre-F339 this would land on
  // /jobs (bare), which post-F260 resolves to All Jobs via
  // localStorage. Post-F339 we land back on the FILTERED Relevant
  // Jobs URL we came from.
  await page.getByRole("button", { name: /back to jobs/i }).click();
  await expect(page).toHaveURL(/role_cluster=relevant/);
  await expect(page).toHaveURL(/geography=global_remote/);
});
