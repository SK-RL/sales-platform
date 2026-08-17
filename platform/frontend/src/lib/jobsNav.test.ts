import { describe, it, expect } from "vitest";
import { isRelevantJobsView, currentRoleCluster } from "./jobsNav";

/**
 * Regression test for the recurring "Relevant Jobs → All Jobs" switch.
 *
 * Symptom the user kept hitting: on the Relevant Jobs view, applying ANY
 * role (via the role dropdown or a role-cluster badge on a job row) set
 * `role_cluster` to a concrete cluster name (infra/security/qa), and the
 * sidebar highlight jumped from "Relevant Jobs" to "All Jobs".
 *
 * The sidebar highlights "Relevant Jobs" iff `isRelevantJobsView(...)`
 * returns true, and "All Jobs" iff it returns false. So the guarantee we
 * lock in here is: filtering Relevant Jobs down to any relevant cluster
 * must KEEP `isRelevantJobsView` true.
 */

// The admin-configured relevant clusters (mirrors getRoleClusters() with
// is_active && is_relevant). qa is included — see the QA-cluster work.
const RELEVANT = ["infra", "security", "qa"];

describe("currentRoleCluster", () => {
  it("reads role_cluster out of the search string", () => {
    expect(currentRoleCluster("?role_cluster=infra")).toBe("infra");
    expect(currentRoleCluster("?geography=uae_only&role_cluster=relevant")).toBe(
      "relevant",
    );
  });

  it("returns '' when role_cluster is absent", () => {
    expect(currentRoleCluster("")).toBe("");
    expect(currentRoleCluster("?geography=uae_only")).toBe("");
  });
});

describe("isRelevantJobsView", () => {
  it("is true for the explicit relevant pseudo-value", () => {
    expect(isRelevantJobsView("?role_cluster=relevant", RELEVANT)).toBe(true);
    // ...even alongside other filters (the UAE-restricted case from the
    // original bug report: /jobs?geography=uae_only&role_cluster=relevant).
    expect(
      isRelevantJobsView("?geography=uae_only&role_cluster=relevant", RELEVANT),
    ).toBe(true);
  });

  // The core regression guard: picking any single relevant cluster while
  // on Relevant Jobs must NOT drop out of the relevant view.
  it.each(RELEVANT)(
    "stays true when narrowed to the relevant cluster %s",
    (cluster) => {
      expect(isRelevantJobsView(`?role_cluster=${cluster}`, RELEVANT)).toBe(true);
    },
  );

  it("stays true for a relevant cluster combined with a geography filter", () => {
    // This is exactly the screenshotted repro: Relevant Jobs + UAE, then
    // click the infra role — pre-fix this rendered as "All Jobs".
    expect(
      isRelevantJobsView("?geography=uae_only&role_cluster=infra", RELEVANT),
    ).toBe(true);
  });

  it("is false for the All Jobs sentinel (any)", () => {
    expect(isRelevantJobsView("?role_cluster=any", RELEVANT)).toBe(false);
  });

  it("is false when no role_cluster is present (plain /jobs)", () => {
    expect(isRelevantJobsView("", RELEVANT)).toBe(false);
    expect(isRelevantJobsView("?geography=global_remote", RELEVANT)).toBe(false);
  });

  it("is false for a cluster that is not in the relevant set", () => {
    // A cluster the admin has NOT marked relevant should read as All Jobs.
    expect(isRelevantJobsView("?role_cluster=sales", RELEVANT)).toBe(false);
  });

  it("tracks the admin config — an empty relevant set means only the pseudo-value counts", () => {
    expect(isRelevantJobsView("?role_cluster=infra", [])).toBe(false);
    expect(isRelevantJobsView("?role_cluster=relevant", [])).toBe(true);
  });
});
