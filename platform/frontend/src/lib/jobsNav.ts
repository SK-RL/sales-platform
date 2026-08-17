/**
 * Sidebar active-state helpers for the two `/jobs` navigation links
 * ("Relevant Jobs" and "All Jobs").
 *
 * Both links carry a `role_cluster` query param — "Relevant Jobs" is
 * `/jobs?role_cluster=relevant`, "All Jobs" is `/jobs?role_cluster=any`
 * (the explicit `any` sentinel defeats the localStorage filter-restore;
 * see feedback fc0a750b / the F260 note in Sidebar.tsx). The sidebar has
 * to decide which of the two to highlight from the *current* URL.
 *
 * Regression fix (Relevant → All Jobs switch): drilling into a specific
 * relevant cluster from the Relevant Jobs view — selecting a role in the
 * role dropdown, or clicking a role-cluster badge on a job row — sets
 * `role_cluster` to a concrete cluster name (`infra` / `security` / `qa`).
 * The pre-fix check recognised *only* the literal `role_cluster=relevant`
 * pseudo-value as "relevant", so the moment any real role was applied the
 * highlight jumped to "All Jobs" — the exact "it keeps switching to All
 * Jobs" the user kept hitting. But the relevant clusters ARE the Relevant
 * view: filtering to one of them narrows *within* Relevant Jobs, it does
 * not leave it. So we treat the current view as relevant when
 * `role_cluster` is the `relevant` pseudo-value OR one of the
 * admin-configured relevant cluster names.
 *
 * "All Jobs" stays highlighted only for `role_cluster=any` / absent, or a
 * cluster that is not in the relevant set.
 */

export const RELEVANT_PSEUDO = "relevant";

/** Read the `role_cluster` value out of a URL search string. */
export function currentRoleCluster(search: string): string {
  return new URLSearchParams(search).get("role_cluster") || "";
}

/**
 * True when the given `/jobs` URL should count as the "Relevant Jobs"
 * view for sidebar-highlight purposes.
 *
 * @param search               `location.search` (e.g. "?role_cluster=infra")
 * @param relevantClusterNames names of the admin-configured clusters that
 *                             are marked relevant (e.g. ["infra","security","qa"])
 */
export function isRelevantJobsView(
  search: string,
  relevantClusterNames: readonly string[],
): boolean {
  const cluster = currentRoleCluster(search);
  if (cluster === RELEVANT_PSEUDO) return true;
  return relevantClusterNames.includes(cluster);
}
