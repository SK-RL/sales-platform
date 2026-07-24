import { useState, useCallback, useEffect, useRef } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Search, Filter, CheckSquare, X, Send, ArrowUp, ArrowDown, ArrowUpDown, Link as LinkIcon, Star, ChevronDown, ChevronUp } from "lucide-react";
import { Card } from "@/components/Card";
import { Button } from "@/components/Button";
import { StatusBadge } from "@/components/StatusBadge";
import { ScoreBar } from "@/components/ScoreBar";
import { Badge } from "@/components/Badge";
import { Pagination } from "@/components/Pagination";
import { RoutineQueueToggle } from "@/components/RoutineQueueToggle";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import { SubmitLinkModal } from "@/components/SubmitLinkModal";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/Table";
import { getJobs, bulkAction, getActiveResume, prepareApplication, getRoleClusters, listSavedFilters, createSavedFilter, deleteSavedFilter } from "@/lib/api";
import type {
  JobFilters,
  JobStatus,
  BulkActionStatus,
  BulkFilterCriteria,
  SortKey,
} from "@/lib/types";
import { formatCount } from "@/lib/format";

// localStorage key for filter stickiness. Versioned so a future shape
// change (e.g. adding a new filter axis) can be migrated by bumping
// the version instead of silently feeding stale state into a refactored
// reducer. Mirrors a feature request from user feedback `e93fabd0`
// "Problem of Filter Stickness — when applying filters, if I want to
// switch to another company, I have to apply them again."
const FILTERS_STORAGE_KEY = "jobspage_filters_v1";

// Default direction per column when first added to the sort chain.
// Numeric / temporal columns get `desc` (highest/newest first), text
// columns get `asc` (A → Z). Toggling a column already in the chain
// flips its direction; this map only seeds the initial direction.
const DEFAULT_SORT_DIR: Record<string, "asc" | "desc"> = {
  relevance_score: "desc",
  resume_score: "desc",
  first_seen_at: "desc",
  last_seen_at: "desc",
  posted_at: "desc",
  title: "asc",
  company_name: "asc",
  platform: "asc",
  status: "asc",
};

function defaultDirFor(column: string): "asc" | "desc" {
  return DEFAULT_SORT_DIR[column] ?? "desc";
}

// Wire format ↔ array conversion. The wire form is a comma-separated
// list of `key:dir` pairs (e.g. `relevance_score:desc,first_seen_at:desc`)
// that the backend `_parse_sort_spec` accepts and that round-trips
// through URL params and localStorage.
function parseSorts(raw: string | null | undefined): SortKey[] {
  if (!raw) return [];
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((seg) => {
      const [k, d] = seg.includes(":") ? seg.split(":", 2) : [seg, defaultDirFor(seg)];
      const dir = (d === "asc" || d === "desc" ? d : defaultDirFor(k)) as "asc" | "desc";
      return { key: k, dir };
    });
}

function serializeSorts(sorts: SortKey[] | undefined): string {
  if (!sorts || sorts.length === 0) return "";
  return sorts.map((s) => `${s.key}:${s.dir}`).join(",");
}

// Build the initial filters from URL params (highest priority — explicit
// user intent), falling back to localStorage (last session's filters,
// the stickiness ask), then to defaults. URL params win because
// shareable links and Sidebar deep-links should override stickiness;
// stickiness is a "no-explicit-state" convenience.
// F339: ``mergeStored`` controls whether un-set URL keys fall back
// to localStorage values. ONLY enable on INITIAL MOUNT (and on
// sidebar-driven full re-mounts). For subsequent URL changes
// triggered by the user clearing a filter via the UI, pass
// ``mergeStored=false`` so the cleared key stays cleared.
function buildInitialFilters(
  searchParams: URLSearchParams,
  mergeStored: boolean = true
): JobFilters {
  const parseIsClassified = (raw: string | null): boolean | undefined => {
    if (raw === "true") return true;
    if (raw === "false") return false;
    return undefined;
  };

  // Detect whether the URL carries any filter axis. We check the keys
  // we know about (not just length > 0) so that incidental params
  // like `page` don't count as "user came in with a filter set."
  const urlHasFilters =
    searchParams.has("search") ||
    searchParams.has("status") ||
    searchParams.has("platform") ||
    searchParams.has("geography") ||
    searchParams.has("role_cluster") ||
    searchParams.has("is_classified") ||
    searchParams.has("sort_by") ||
    searchParams.has("sorts") ||
    searchParams.has("sort_dir");

  // F339 regression fix (filter stickiness):
  //
  // Pre-fix this branch RESET every filter axis the URL didn't
  // explicitly carry. That meant the Sidebar's hardcoded
  // ``/jobs?role_cluster=relevant`` link silently dropped a user's
  // saved geography / platform / search / sorts on click — they had
  // to re-apply them every time they bounced through the sidebar.
  // User report 2026-05-09: "issue of stickiness of filter in the
  // relevant jobs".
  //
  // The fix: URL keys still WIN where present (so a shareable link
  // ``/jobs?geography=usa_only`` overrides any saved geography),
  // but un-set URL keys fall through to localStorage so the user's
  // other filters survive the round-trip. The
  // ``urlHasFilters=true`` branch is the right home for this merge
  // — it's the path that fires on sidebar-link clicks.
  //
  // Page is NOT pulled from localStorage (matches the existing
  // no-URL-filters branch policy: landing on page 7 a day later
  // would surprise the user).
  let storedFilters: Partial<JobFilters> = {};
  if (mergeStored) {
    try {
      const raw = window.localStorage.getItem(FILTERS_STORAGE_KEY);
      if (raw) {
        storedFilters = JSON.parse(raw) as Partial<JobFilters>;
      }
    } catch {
      // Corrupt entry — proceed with empty stored.
    }
  }

  if (urlHasFilters) {
    // Multi-sort URL key takes precedence; fall back to legacy
    // sort_by + sort_dir for shareable links generated before
    // multi-sort shipped; final fallback is localStorage sorts.
    const sortsParam = searchParams.get("sorts");
    const legacySortBy = searchParams.get("sort_by");
    const legacySortDir = searchParams.get("sort_dir");
    let sorts: SortKey[] = [];
    if (sortsParam) {
      sorts = parseSorts(sortsParam);
    } else if (legacySortBy) {
      const dir = (legacySortDir === "asc" ? "asc" : "desc") as "asc" | "desc";
      sorts = [{ key: legacySortBy, dir }];
    } else if (
      storedFilters.sorts &&
      Array.isArray(storedFilters.sorts) &&
      storedFilters.sorts.length > 0
    ) {
      sorts = storedFilters.sorts;
    } else {
      sorts = [{ key: "relevance_score", dir: "desc" }];
    }
    // For each filter axis: URL value wins when present, otherwise
    // fall back to the stored value. ``searchParams.get(...)`` returns
    // ``null`` for absent keys (which we treat as "fall through to
    // stored"), and empty string for explicitly-cleared params (which
    // we also treat as "fall through to stored" — the user clearing
    // a single param shouldn't force the others to defaults).
    const urlOrStored = (key: keyof JobFilters, fallback: string = ""): string => {
      const urlValue = searchParams.get(key as string);
      if (urlValue !== null && urlValue !== "") return urlValue;
      const stored = (storedFilters as any)[key];
      return typeof stored === "string" && stored ? stored : fallback;
    };

    // ``is_classified`` is tri-state (true | false | undefined) so it
    // needs its own URL-vs-stored merge.
    const urlClassified = parseIsClassified(searchParams.get("is_classified"));
    const isClassified =
      urlClassified !== undefined
        ? urlClassified
        : storedFilters.is_classified;

    return {
      search: urlOrStored("search"),
      status: urlOrStored("status") as JobFilters["status"],
      platform: urlOrStored("platform"),
      geography: urlOrStored("geography"),
      // F260: ``role_cluster=any`` is the explicit "All Jobs" sentinel
      // from the Sidebar. We keep it on the filter object so the page
      // header and active-link checks can distinguish "user navigated
      // to All Jobs" from "user landed on /jobs with localStorage
      // restored." Backend treats ``any`` as no-filter (jobs.py).
      role_cluster: urlOrStored("role_cluster"),
      is_classified: isClassified,
      sorts,
      page: Number(searchParams.get("page")) || 1,
      page_size: 25,
    };
  }

  // No URL filters → restore from localStorage (when allowed).
  // We deliberately don't restore `page` from localStorage —
  // landing on page 7 a day later would surprise the user.
  if (mergeStored && storedFilters && Object.keys(storedFilters).length > 0) {
    const sorts = storedFilters.sorts &&
      Array.isArray(storedFilters.sorts) &&
      storedFilters.sorts.length > 0
      ? storedFilters.sorts
      : [{ key: "relevance_score", dir: "desc" as const }];
    return {
      search: storedFilters.search || "",
      status: (storedFilters.status || "") as JobFilters["status"],
      platform: storedFilters.platform || "",
      geography: storedFilters.geography || "",
      role_cluster: storedFilters.role_cluster || "",
      is_classified: storedFilters.is_classified,
      sorts,
      page: 1,
      page_size: 25,
    };
  }
  // mergeStored=false and no URL filters, OR no stored entry
  // present → fall through to defaults below. The
  // ``mergeStored=false`` path fires when the URL→state effect
  // re-runs after the user explicitly cleared the last filter via
  // the UI — we DON'T want to silently re-apply the value they
  // just cleared.

  return {
    search: "",
    status: "",
    platform: "",
    geography: "",
    role_cluster: "",
    is_classified: undefined,
    sorts: [{ key: "relevance_score", dir: "desc" }],
    page: 1,
    page_size: 25,
  };
}

// F69: user-facing verb → backend status mapping. The buttons stay
// labelled Accept/Reject/Reset (what the user intends) but the wire
// payload must be one of the JobStatusLiteral values (F99 tightened
// the backend to reject verbs). The previous code sent the verb
// directly and 422'd in prod on every bulk action — silently,
// because the UI still showed a loading spinner and invalidated
// the query. Kept as a const map so a future status addition lands
// in one place.
const VERB_TO_STATUS: Record<"accept" | "reject" | "reset", BulkActionStatus> = {
  accept: "accepted",
  reject: "rejected",
  reset: "new",
};

// Regression finding 87: synthetic dropdown value used for the
// "Unclassified" option. Keeps the `<select>`'s `value` shape
// stable (always a string) while translating on the wire to
// `is_classified=false` + `role_cluster=""`. Picked a `__…__`
// sentinel so it cannot collide with any admin-defined cluster
// name (`/role-clusters` rejects names starting with `_`).
const UNCLASSIFIED_SENTINEL = "__unclassified__";

const STATUS_OPTIONS: { value: JobStatus | ""; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "new", label: "New" },
  { value: "under_review", label: "Under Review" },
  { value: "accepted", label: "Accepted" },
  { value: "rejected", label: "Rejected" },
  { value: "expired", label: "Expired" },
];

// F218 (Round 31 follow-up): backend `PlatformFilter` Literal in
// `platform/backend/app/schemas/job.py` rejects any value outside the set
// of fetcher `PLATFORM` attributes. The previous dropdown listed `indeed`,
// `builtin`, `career_page` — none of which are real fetchers — and was
// missing `bamboohr`, `smartrecruiters`, `jobvite`, `recruitee`. Pre-F218
// the backend silently returned `total:0` for invalid values and missing
// ones were un-filterable; post-F218 invalid values 422. This list is the
// one source of truth for UX and must stay aligned with the backend
// Literal (14 entries; see comment in schemas/job.py).
const PLATFORM_OPTIONS = [
  { value: "", label: "All Platforms" },
  { value: "greenhouse", label: "Greenhouse" },
  { value: "lever", label: "Lever" },
  { value: "ashby", label: "Ashby" },
  { value: "workable", label: "Workable" },
  { value: "bamboohr", label: "BambooHR" },
  { value: "smartrecruiters", label: "SmartRecruiters" },
  { value: "jobvite", label: "Jobvite" },
  { value: "recruitee", label: "Recruitee" },
  { value: "wellfound", label: "Wellfound" },
  { value: "himalayas", label: "Himalayas" },
  { value: "linkedin", label: "LinkedIn" },
  { value: "weworkremotely", label: "WeWorkRemotely" },
  { value: "remoteok", label: "RemoteOK" },
  { value: "remotive", label: "Remotive" },
  // F351 — team-curated Google Sheets ingested as boards.
  { value: "google_sheet", label: "Google Sheet" },
];

// Legacy filter values still sent on the wire as ``?geography=`` so
// the existing URL-share / saved-filter rows keep working through
// the transition window. Labels updated to the new vocabulary so the
// team sees the same words across the app — the value→label mapping
// below is the only place this asymmetry exists.
const GEOGRAPHY_OPTIONS = [
  { value: "", label: "All remote scopes" },
  { value: "global_remote", label: "Worldwide remote" },
  { value: "usa_only", label: "USA-restricted remote" },
  { value: "uae_only", label: "UAE-restricted remote" },
];

export function JobsPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  // F339: helper that navigates to a job detail and remembers the
  // current /jobs URL (with all filters) under ``location.state.from``.
  // ``JobDetailPage.handleBackToJobs`` consumes this so the back
  // button restores the exact filter set the user was looking at,
  // even after a refresh of the detail page (browser history alone
  // would lose that state).
  const goToJob = (jobId: string) => {
    navigate(`/jobs/${jobId}`, {
      state: { from: location.pathname + location.search },
    });
  };
  // Regression finding 34: ref guards against infinite URL ↔ state loops
  const syncingFromUrl = useRef(false);

  // Feature A — Submit Job Link modal open/closed state.
  const [submitLinkOpen, setSubmitLinkOpen] = useState(false);

  const [filters, setFilters] = useState<JobFilters>(() =>
    buildInitialFilters(searchParams)
  );

  // URL → state: re-sync filters when URL params change externally
  // (e.g. Sidebar navigation between "All Jobs" and "Relevant Jobs",
  // or a deep-link with explicit filters). When the URL has no filter
  // params we leave the current in-memory state alone instead of
  // resetting to defaults — that's how filter stickiness survives a
  // round-trip through a sidebar link that strips the query string.
  useEffect(() => {
    const urlHasFilters =
      searchParams.has("search") ||
      searchParams.has("status") ||
      searchParams.has("platform") ||
      searchParams.has("geography") ||
      searchParams.has("role_cluster") ||
      searchParams.has("is_classified") ||
      searchParams.has("sort_by") ||
      searchParams.has("sorts") ||
      searchParams.has("sort_dir") ||
      searchParams.has("page");
    if (!urlHasFilters) return;
    syncingFromUrl.current = true;
    // F339: after the initial mount we treat the URL as the source
    // of truth. ``mergeStored=false`` so when the user clears a
    // filter via the UI (which triggers a state→URL write, which
    // re-fires this URL→state effect), the cleared value stays
    // cleared instead of being silently re-applied from
    // localStorage.
    setFilters(buildInitialFilters(searchParams, false));
  }, [searchParams]);

  // State → URL: write filter changes back to the URL so the result
  // is shareable. Multi-sort serialises into a single `sorts` param
  // (the legacy `sort_by` + `sort_dir` keys are no longer emitted —
  // shareable links now use the unified format that the
  // `buildInitialFilters` parser also accepts via the `sorts` key).
  // Skip the write when the change originated from the URL→state
  // sync above to avoid a sync loop.
  useEffect(() => {
    if (syncingFromUrl.current) {
      syncingFromUrl.current = false;
      return;
    }
    const params = new URLSearchParams();
    if (filters.search) params.set("search", filters.search);
    if (filters.status) params.set("status", filters.status);
    if (filters.platform) params.set("platform", filters.platform);
    if (filters.geography) params.set("geography", filters.geography);
    if (filters.role_cluster) params.set("role_cluster", filters.role_cluster);
    // F87: only serialise `is_classified` when the user has explicitly
    // narrowed to it — `undefined` = "both", which is the default and
    // doesn't need to show up in the URL.
    if (filters.is_classified === true) params.set("is_classified", "true");
    else if (filters.is_classified === false) params.set("is_classified", "false");
    // Multi-sort: emit only when the chain is non-default (anything
    // other than "relevance_score:desc" alone). Single-sort callers
    // and shareable links generated before multi-sort shipped still
    // work via the legacy `sort_by`/`sort_dir` parser branch in
    // `buildInitialFilters`.
    const sortsStr = serializeSorts(filters.sorts);
    if (sortsStr && sortsStr !== "relevance_score:desc") {
      params.set("sorts", sortsStr);
    }
    if ((filters.page ?? 1) > 1) params.set("page", String(filters.page));
    setSearchParams(params, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters]);

  // Filter stickiness — write to localStorage on every state change so
  // the next visit (incl. after navigating to /companies and back via
  // Sidebar) restores the user's filter set. Page is intentionally
  // omitted here; landing back on page 7 a day later would surprise
  // the user. See user feedback `e93fabd0` "Problem of Filter
  // Stickness" for the originating ask.
  useEffect(() => {
    try {
      const toPersist = {
        search: filters.search,
        status: filters.status,
        platform: filters.platform,
        geography: filters.geography,
        role_cluster: filters.role_cluster,
        is_classified: filters.is_classified,
        sorts: filters.sorts,
      };
      window.localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(toPersist));
    } catch {
      // localStorage can throw in private browsing / quota-full.
      // Stickiness is a UX nicety — a failure here shouldn't break
      // the page render.
    }
  }, [filters]);

  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  // F69: when true, the bulk action targets *every* row matching the
  // current filter (server-side enumeration), not just the ids in
  // `selectedIds`. The banner below the filter Card flips this on,
  // and any filter change or cancel flips it off. Kept separate from
  // `selectedIds` so the existing per-page select-all path is
  // unchanged when the user hasn't opted into the full-matching mode.
  const [selectAllMatching, setSelectAllMatching] = useState(false);
  // F69: remember the last bulk error so the banner above the action
  // Card can show "Filter matches 8,342 jobs which exceeds the cap of
  // 5000" instead of silently failing. `bulkMutation.error` also
  // carries it, but extracting the .detail payload and clearing on
  // success is clearer as its own state.
  const [bulkErrorMsg, setBulkErrorMsg] = useState<string | null>(null);

  // Regression finding 70: clear checkbox selections when filters change —
  // stale selections from a previous filter-set could silently target
  // jobs the user can no longer see in the table. F87: `is_classified`
  // is also a filter axis (Unclassified dropdown option), so toggling
  // between "classified" and "unclassified" views must likewise reset.
  // F69: `selectAllMatching` is explicitly scoped to the filter set
  // active at click-time, so the same "filters changed" event must
  // cancel it too — otherwise a user could narrow the filter AFTER
  // clicking "Select all N matching" and silently shrink the blast
  // radius (surprising) or broaden it (dangerous).
  useEffect(() => {
    setSelectedIds(new Set());
    setSelectAllMatching(false);
    setBulkErrorMsg(null);
  }, [
    filters.search, filters.status, filters.platform, filters.geography,
    filters.role_cluster, filters.is_classified,
    // Multi-sort: serialise the chain so a chain change (column add /
    // remove / reorder / direction toggle) trips the same reset path
    // that the legacy single-sort change did. `useEffect` deps need a
    // primitive, hence the join.
    serializeSorts(filters.sorts),
  ]);

  // F222: destructure full queries so failures surface via the banner.
  const activeResumeQ = useQuery({
    queryKey: ["active-resume"],
    queryFn: getActiveResume,
  });
  const activeResumeData = activeResumeQ.data;
  const hasActiveResume = !!activeResumeData?.active_resume;

  const jobsQ = useQuery({
    queryKey: ["jobs", filters],
    queryFn: () => getJobs(filters),
  });
  const { data, isLoading } = jobsQ;

  // Regression finding 87: the role-cluster dropdown used to hardcode
  // four options (`relevant` + `infra` + `security` + `qa`). That drifts
  // the moment an admin adds or removes a cluster via `/role-clusters`,
  // and it had no affordance for the ~90% of rows with no cluster.
  // Fetch the live catalog here and build options dynamically: keep
  // the synthetic "relevant" pseudo at the top, then one option per
  // active cluster sorted by the admin-defined `sort_order`, then the
  // new "Unclassified" sentinel that maps to `is_classified=false` on
  // the wire. `gcTime: Infinity` / long `staleTime` because the cluster
  // config is low-churn and refetching on every JobsPage mount would
  // be wasteful; invalidation is manual via the admin RoleClusters UI.
  const roleClustersQ = useQuery({
    queryKey: ["role-clusters"],
    queryFn: getRoleClusters,
    staleTime: 10 * 60 * 1000,
  });
  const activeClusters = (roleClustersQ.data?.items ?? [])
    .filter((c) => c.is_active)
    .sort((a, b) => a.sort_order - b.sort_order);
  // The dropdown carries a single string value. Derive it from the
  // current filters state: `__unclassified__` if `is_classified=false`,
  // otherwise the `role_cluster` string (or empty for "All Roles").
  const roleSelectValue =
    filters.is_classified === false ? UNCLASSIFIED_SENTINEL : (filters.role_cluster || "");

  const [applyingJobId, setApplyingJobId] = useState<string | null>(null);
  const [applyFeedback, setApplyFeedback] = useState<{ jobId: string; msg: string; ok: boolean } | null>(null);

  const applyMutation = useMutation({
    mutationFn: (jobId: string) => prepareApplication(jobId),
    onSuccess: (_data, jobId) => {
      setApplyingJobId(null);
      queryClient.invalidateQueries({ queryKey: ["applications"] });
      queryClient.invalidateQueries({ queryKey: ["application-stats"] });
      // Navigate to job detail page where the full apply panel lives
      goToJob(jobId);
    },
    onError: (error: any, jobId) => {
      setApplyingJobId(null);
      if (error?.status === 409 || error?.message?.includes("already")) {
        // Already applied — go to the job detail to see the existing application
        goToJob(jobId);
      } else {
        const msg = error?.message || "Failed to prepare";
        setApplyFeedback({ jobId, msg, ok: false });
        setTimeout(() => setApplyFeedback(null), 3000);
      }
    },
  });

  const bulkMutation = useMutation({
    mutationFn: bulkAction,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      setSelectedIds(new Set());
      // F69: clear full-matching intent after it fires — otherwise
      // the next bulk click would silently re-target the whole
      // (now-invalidated) filter set, which is a foot-gun. The user
      // re-opts in by clicking "Select all N matching" again.
      setSelectAllMatching(false);
      setBulkErrorMsg(null);
    },
    onError: (err: any) => {
      // F69: surface backend 400s from the filter branch (cap exceeded
      // or zero-match) in the UI instead of silently failing. Fall
      // back to a generic message if the error shape is unexpected.
      const detail =
        err?.body?.detail ||
        err?.detail ||
        err?.message ||
        "Bulk action failed. Please try again.";
      setBulkErrorMsg(typeof detail === "string" ? detail : "Bulk action failed.");
    },
  });

  const updateFilter = useCallback(
    (key: keyof JobFilters, value: string | number) => {
      setFilters((prev) => ({ ...prev, [key]: value, page: key === "page" ? Number(value) : 1 }));
    },
    []
  );

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  // Regression finding 68: union/subtract visible page ids instead of
  // replacing the entire Set, so cross-page curation is preserved.
  const toggleSelectAll = () => {
    if (!data) return;
    const pageIds = data.items.map((j) => j.id);
    const allVisible = pageIds.every((id) => selectedIds.has(id));
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (allVisible) {
        pageIds.forEach((id) => next.delete(id));
      } else {
        pageIds.forEach((id) => next.add(id));
      }
      return next;
    });
  };

  // F69: translate the current filters state into the BulkFilterCriteria
  // shape the backend expects. Only non-empty fields survive so the
  // generated WHERE chain matches what `GET /jobs` produced for the
  // page the user is looking at. The frontend uses `geography` in its
  // JobFilters state but the backend field is `geography_bucket`, so
  // we rename here. `sort_by` / `sort_dir` / `page` are intentionally
  // left out — they don't affect the matching id set and sending them
  // would just produce audit-log noise.
  const buildFilterCriteria = (): BulkFilterCriteria => {
    const c: BulkFilterCriteria = {};
    if (filters.search && filters.search.trim()) c.search = filters.search.trim();
    if (filters.status) c.status = filters.status;
    if (filters.platform) c.platform = filters.platform;
    if (filters.geography) c.geography_bucket = filters.geography;
    if (filters.role_cluster) c.role_cluster = filters.role_cluster;
    if (filters.is_classified !== undefined) c.is_classified = filters.is_classified;
    return c;
  };

  // Regression finding 71: gate bulk actions behind window.confirm() so a
  // misclick doesn't silently change the status of dozens of jobs.
  // F69: route between id-list and filter branches based on
  // `selectAllMatching`. The confirm prompt includes the real blast
  // radius — "X selected jobs" for the id path, "all N matching" for
  // the filter path — so the user can't mistake one for the other.
  const handleBulkAction = (verb: "accept" | "reject" | "reset") => {
    const status = VERB_TO_STATUS[verb];
    const verbLabel = verb === "accept" ? "Accept" : verb === "reject" ? "Reject" : "Reset";
    const total = data?.total ?? 0;

    if (selectAllMatching) {
      // Filter branch — target everything matching the current filter.
      // Confirm message calls out the total so the user sees the actual
      // row count they're about to mutate. Backend caps at 5000; if the
      // filter exceeds that, the mutation rejects with a 400 surfaced
      // via `bulkErrorMsg` (no need to pre-check — single round trip).
      if (!window.confirm(
        `${verbLabel} all ${total.toLocaleString()} jobs matching the current filter? This cannot be undone.`
      )) return;
      setBulkErrorMsg(null);
      bulkMutation.mutate({
        filter: buildFilterCriteria(),
        action: status,
      });
      return;
    }

    // Legacy id-list branch — unchanged semantics, correct status value.
    const count = selectedIds.size;
    if (count === 0) return;
    if (!window.confirm(
      `${verbLabel} ${count} selected job${count !== 1 ? "s" : ""}? This cannot be undone.`
    )) return;
    setBulkErrorMsg(null);
    bulkMutation.mutate({
      job_ids: Array.from(selectedIds),
      action: status,
    });
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    });
  };

  // Column-header click handlers. Two modes — plain click resets the
  // chain to a single column (the common case); shift/ctrl/cmd+click
  // adds a column to the chain or toggles its direction in-place
  // (multi-column sort, e.g. "primary by relevance, tiebreaker by
  // first_seen"). Cmd-click on an already-in-chain column REMOVES
  // it from the chain — gives the user an undo without forcing them
  // back through the dropdown.
  const handleColumnSort = (column: string, e: React.MouseEvent) => {
    const additive = e.shiftKey || e.metaKey || e.ctrlKey;
    const removeRequested = (e.metaKey || e.ctrlKey) && !e.shiftKey;
    setFilters((prev) => {
      const chain = prev.sorts ?? [];
      const idx = chain.findIndex((s) => s.key === column);

      // Cmd/Ctrl-click on an already-sorted column → remove it from
      // the chain (unless it's the last remaining sort, in which case
      // we keep it but flip direction — the table always needs at
      // least one ORDER BY to be deterministic).
      if (removeRequested && idx >= 0 && chain.length > 1) {
        const next = chain.filter((_, i) => i !== idx);
        return { ...prev, sorts: next, page: 1 };
      }

      // Shift-click: append to chain, or toggle if already present.
      if (additive) {
        if (idx >= 0) {
          const next = [...chain];
          next[idx] = { ...next[idx], dir: next[idx].dir === "desc" ? "asc" : "desc" };
          return { ...prev, sorts: next, page: 1 };
        }
        return {
          ...prev,
          sorts: [...chain, { key: column, dir: defaultDirFor(column) }],
          page: 1,
        };
      }

      // Plain click: replace the chain with a single column. If the
      // user clicks the SAME column that's currently the sole primary
      // sort, toggle direction (matches the pre-multi-sort behavior).
      if (chain.length === 1 && chain[0].key === column) {
        return {
          ...prev,
          sorts: [{ key: column, dir: chain[0].dir === "desc" ? "asc" : "desc" }],
          page: 1,
        };
      }
      return { ...prev, sorts: [{ key: column, dir: defaultDirFor(column) }], page: 1 };
    });
  };

  // Header sort indicator — combines the direction arrow with a small
  // priority badge when the column is part of a multi-key chain
  // (badge shows `1`/`2`/`3` so the user can see WHICH column is the
  // primary vs tiebreaker). Single-sort users see only the arrow,
  // identical to the pre-multi-sort UI.
  const SortIcon = ({ column }: { column: string }) => {
    const chain = filters.sorts ?? [];
    const idx = chain.findIndex((s) => s.key === column);
    if (idx < 0) {
      return <ArrowUpDown className="ml-1 inline h-3 w-3 text-gray-300" />;
    }
    const arrow = chain[idx].dir === "asc"
      ? <ArrowUp className="ml-1 inline h-3 w-3 text-primary-600" />
      : <ArrowDown className="ml-1 inline h-3 w-3 text-primary-600" />;
    if (chain.length === 1) return arrow;
    return (
      <>
        {arrow}
        <span
          className="ml-0.5 inline-flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-primary-100 px-1 text-[10px] font-bold text-primary-700"
          title={`Sort priority ${idx + 1} of ${chain.length} — Shift-click to add columns, Cmd/Ctrl-click to remove`}
        >
          {idx + 1}
        </span>
      </>
    );
  };

  // Convenience read accessors — the dropdown still drives single-sort
  // by writing a 1-element chain, so we read back the same way.
  const primarySort = (filters.sorts && filters.sorts[0]) ?? { key: "relevance_score", dir: "desc" as const };
  const isMultiSort = (filters.sorts?.length ?? 0) > 1;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">
            {filters.is_classified === false
              ? "Unclassified Jobs"
              : filters.role_cluster === "relevant"
                ? "Relevant Jobs"
                : filters.role_cluster && filters.role_cluster !== "any"
                  ? `${(activeClusters.find((c) => c.name === filters.role_cluster)?.display_name) || (filters.role_cluster.charAt(0).toUpperCase() + filters.role_cluster.slice(1))} Jobs`
                  : "All Jobs"}
          </h1>
          {/* F264 — header subtitle is now generated from the live
              cluster config instead of the pre-fix hardcoded string
              ``"Cloud, DevOps, SRE, Compliance & Security positions"``.
              That string was a lie the moment an admin reconfigured
              ``is_relevant`` on any cluster — and there's no signal
              to non-admins when admin-side reconfig happens. Now the
              subtitle reads "Showing infra, security, qa · 7,942
              jobs" regardless of what the admin has marked relevant
              today. Same applies to the per-cluster header pages
              (``/jobs?role_cluster=infra`` etc.) which now include
              the count + the cluster's keyword preview. */}
          <p className="mt-1 text-sm text-gray-500">
            {filters.is_classified === false ? (
              data ? `${formatCount(data.total)} jobs without a role cluster` : "Loading jobs..."
            ) : filters.role_cluster === "relevant" ? (
              <>
                Showing{" "}
                <span className="font-medium text-gray-700">
                  {activeClusters
                    .filter((c) => c.is_relevant)
                    .map((c) => c.display_name || c.name)
                    .join(" · ") || "infra · security (default)"}
                </span>
                {data ? ` · ${formatCount(data.total)} jobs` : ""}
              </>
            ) : filters.role_cluster && filters.role_cluster !== "any" ? (
              <>
                {(() => {
                  const c = activeClusters.find((x) => x.name === filters.role_cluster);
                  if (!c) return data ? `${formatCount(data.total)} jobs found` : "Loading jobs...";
                  const kwPreview = (c.keywords || "")
                    .split(",")
                    .slice(0, 4)
                    .map((k) => k.trim())
                    .filter(Boolean)
                    .join(", ");
                  return (
                    <>
                      {kwPreview && <span className="text-gray-600">{kwPreview}…</span>}
                      {data ? ` · ${formatCount(data.total)} jobs` : ""}
                    </>
                  );
                })()}
              </>
            ) : (
              data ? `${formatCount(data.total)} jobs found` : "Loading jobs..."
            )}
          </p>
        </div>
        {/* Feature A — submit-link modal trigger. Pasting an ATS URL runs
            it through the normal scoring/classification pipeline and is
            idempotent by external_id. */}
        <Button variant="secondary" size="md" onClick={() => setSubmitLinkOpen(true)}>
          <LinkIcon className="h-4 w-4" />
          Submit link
        </Button>
      </div>

      <SubmitLinkModal
        open={submitLinkOpen}
        onClose={() => setSubmitLinkOpen(false)}
        onOpenJob={(jobId) => goToJob(jobId)}
      />

      {/* F222: surfaces /jobs failures with a Retry button. */}
      <BackendErrorBanner queries={[jobsQ, activeResumeQ]} />

      <Card padding="sm">
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              placeholder='Search by title/company. Boolean: cloud AND remote, "site reliability", -manager'
              className="input pl-9"
              value={filters.search}
              onChange={(e) => updateFilter("search", e.target.value)}
              title='Boolean syntax supported: "exact phrase", AND, OR, NOT, -exclusion, (grouping). Bare queries work as before.'
            />
          </div>

          {/* F241: saved-filter presets dropdown. Lives in the same
              Card as the rest of the filter controls so users see
              "save current" / "load preset" inline with the filters
              themselves. */}
          <SavedFiltersControl
            currentFilters={filters}
            onApply={(f) => setFilters((prev) => ({ ...prev, ...f, page: 1 }))}
          />
          <select
            className="select w-auto"
            value={filters.status}
            onChange={(e) => updateFilter("status", e.target.value)}
          >
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <select
            className="select w-auto"
            value={filters.platform}
            onChange={(e) => updateFilter("platform", e.target.value)}
          >
            {PLATFORM_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <select
            className="select w-auto"
            value={filters.geography}
            onChange={(e) => updateFilter("geography", e.target.value)}
          >
            {GEOGRAPHY_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          {/* F87: role-cluster dropdown is now driven by the admin
              config (`/role-clusters`) with the synthetic "Relevant"
              pseudo at the top and a new "Unclassified" option that
              translates to `is_classified=false` on the wire. */}
          <select
            className="select w-auto"
            value={roleSelectValue}
            onChange={(e) => {
              const v = e.target.value;
              if (v === UNCLASSIFIED_SENTINEL) {
                // Unclassified: clear role_cluster, switch is_classified=false.
                setFilters((prev) => ({
                  ...prev,
                  role_cluster: "",
                  is_classified: false,
                  page: 1,
                }));
              } else {
                // Any cluster name (including "" for All and "relevant"
                // for the pseudo): clear is_classified, set role_cluster.
                setFilters((prev) => ({
                  ...prev,
                  role_cluster: v,
                  is_classified: undefined,
                  page: 1,
                }));
              }
            }}
          >
            <option value="">All Roles</option>
            {/* F264 — pre-fix this option's label was the static
                ``"Relevant (configured clusters)"`` which never named
                the actual clusters. Users had no idea what would
                appear on click. Now the label lists the live cluster
                names so the click is predictable: "Relevant: infra,
                security, qa". When admin reconfigures is_relevant on
                a cluster, this label auto-reflects. */}
            <option value="relevant">
              {(() => {
                const names = activeClusters
                  .filter((c) => c.is_relevant)
                  .map((c) => c.name);
                return names.length > 0
                  ? `Relevant: ${names.join(", ")}`
                  : "Relevant (default: infra, security)";
              })()}
            </option>
            {activeClusters.map((c) => (
              <option key={c.id} value={c.name}>
                {c.display_name || c.name}
              </option>
            ))}
            <option value={UNCLASSIFIED_SENTINEL}>Unclassified</option>
          </select>
          {/* Single-sort dropdown — selecting an option REPLACES the
              entire chain with a 1-element sort, matching the pre-
              multi-sort UX for users who don't shift-click columns.
              The dropdown shows "(+ N more)" when a multi-column
              chain is active so the user understands why the table
              order doesn't match the dropdown's primary key alone. */}
          <select
            className="select w-auto"
            value={`${primarySort.key}:${primarySort.dir}`}
            onChange={(e) => {
              const [key, dir] = e.target.value.split(":");
              setFilters((prev) => ({
                ...prev,
                sorts: [{ key, dir: (dir === "asc" ? "asc" : "desc") as "asc" | "desc" }],
                page: 1,
              }));
            }}
          >
            <option value="relevance_score:desc">
              Relevance (High to Low){isMultiSort ? ` (+${(filters.sorts?.length ?? 1) - 1} more)` : ""}
            </option>
            <option value="relevance_score:asc">Relevance (Low to High)</option>
            {hasActiveResume && <option value="resume_score:desc">Resume Match (High to Low)</option>}
            {hasActiveResume && <option value="resume_score:asc">Resume Match (Low to High)</option>}
            <option value="first_seen_at:desc">Newest First</option>
            <option value="first_seen_at:asc">Oldest First</option>
            <option value="title:asc">Title A-Z</option>
            <option value="title:desc">Title Z-A</option>
            <option value="company_name:asc">Company A-Z</option>
            <option value="company_name:desc">Company Z-A</option>
            <option value="platform:asc">Platform A-Z</option>
            <option value="status:asc">Status A-Z</option>
          </select>
          {(filters.search || filters.status || filters.platform || filters.geography || filters.role_cluster || filters.is_classified !== undefined || isMultiSort) && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                // F87: also reset `is_classified` so Clear truly clears
                // every filter axis (the Unclassified view used to
                // survive a Clear and silently narrow the result set).
                // Multi-sort: collapse the chain back to the default
                // single primary so a "Clear" feels like a true reset.
                setFilters({
                  search: "",
                  status: "",
                  platform: "",
                  geography: "",
                  role_cluster: "",
                  is_classified: undefined,
                  sorts: [{ key: "relevance_score", dir: "desc" }],
                  page: 1,
                  page_size: 25,
                });
                // Also drop the persisted snapshot so a Clear today
                // doesn't get silently overwritten on the next reload.
                try {
                  window.localStorage.removeItem(FILTERS_STORAGE_KEY);
                } catch {
                  // Same swallow as the persistence write.
                }
              }}
            >
              <X className="h-4 w-4" />
              Clear
            </Button>
          )}
        </div>
      </Card>

      {/* F69: "Select all N matching" banner. Appears only when the
          user has checked every row on the current page AND there are
          rows beyond this page (i.e. `total > data.items.length`).
          Clicking the action flips `selectAllMatching` to true so the
          next bulk click POSTs the filter payload instead of the id
          list. We deliberately don't show the banner when the user
          has partially selected — that signals deliberate curation,
          and the banner would nudge them away from it. Hidden once
          already in filter-mode (we show the filter-mode badge
          inside the action Card below instead). */}
      {data &&
        data.items.length > 0 &&
        selectedIds.size > 0 &&
        data.items.every((j) => selectedIds.has(j.id)) &&
        data.total > data.items.length &&
        !selectAllMatching && (
          <Card padding="sm">
            <div className="flex items-center justify-between gap-4">
              <span className="text-sm text-gray-700">
                All <strong>{data.items.length}</strong> jobs on this page are selected.
              </span>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setSelectAllMatching(true)}
              >
                Select all {data.total.toLocaleString()} jobs matching the filter
              </Button>
            </div>
          </Card>
        )}

      {(selectedIds.size > 0 || selectAllMatching) && (
        <Card padding="sm">
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <CheckSquare className="h-4 w-4 text-primary-600" />
              <span className="text-sm font-medium text-gray-700">
                {selectAllMatching
                  ? `All ${(data?.total ?? 0).toLocaleString()} jobs matching the filter selected`
                  : `${selectedIds.size} selected`}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="primary"
                size="sm"
                onClick={() => handleBulkAction("accept")}
                loading={bulkMutation.isPending}
              >
                Accept
              </Button>
              <Button
                variant="danger"
                size="sm"
                onClick={() => handleBulkAction("reject")}
                loading={bulkMutation.isPending}
              >
                Reject
              </Button>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => handleBulkAction("reset")}
                loading={bulkMutation.isPending}
              >
                Reset
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setSelectedIds(new Set());
                  setSelectAllMatching(false);
                  setBulkErrorMsg(null);
                }}
              >
                Cancel
              </Button>
            </div>
            {/* F69: surface the backend 400 message inline. The most
                likely case is "Filter matches X jobs which exceeds the
                bulk cap of 5000. Narrow the filter before retrying." —
                pre-fix that message was swallowed and the user saw
                only a dead spinner. */}
            {bulkErrorMsg && (
              <div className="w-full rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {bulkErrorMsg}
              </div>
            )}
          </div>
        </Card>
      )}

      <Card padding="none">
        {isLoading ? (
          <div className="flex items-center justify-center py-20">
            <div className="spinner h-8 w-8" />
          </div>
        ) : data && data.items.length > 0 ? (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">
                    {/* F71: header checkbox was DOM-unlabeled — screen
                        readers announced it as "checkbox, not checked"
                        with zero context. Row checkboxes below get
                        per-row labels so AT can navigate the table. */}
                    <input
                      type="checkbox"
                      checked={
                        data.items.length > 0 &&
                        data.items.every((j) => selectedIds.has(j.id))
                      }
                      onChange={toggleSelectAll}
                      aria-label="Select all visible jobs"
                      className="rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                    />
                  </TableHead>
                  {/* Sortable column headers — plain click replaces
                      the sort, Shift-click adds a tiebreaker, and
                      Cmd/Ctrl-click removes a column from the chain.
                      The `title` tooltip teaches the multi-sort
                      affordance without cluttering the header row. */}
                  <TableHead
                    className="cursor-pointer select-none"
                    onClick={(e) => handleColumnSort("title", e)}
                    title="Click to sort. Shift-click to add as a tiebreaker, Cmd/Ctrl-click to remove from sort chain."
                  >
                    Title <SortIcon column="title" />
                  </TableHead>
                  <TableHead
                    className="cursor-pointer select-none"
                    onClick={(e) => handleColumnSort("company_name", e)}
                    title="Click to sort. Shift-click to add as a tiebreaker."
                  >
                    Company <SortIcon column="company_name" />
                  </TableHead>
                  {/* `platform` is in the backend sort whitelist (see
                      `_ALLOWED_SORT_KEYS`) but the previous header was
                      not click-sortable — only the dropdown could pick
                      it. Bringing it under the same shift-click chain
                      so users can group infra jobs by platform with
                      relevance as the secondary sort. */}
                  <TableHead
                    className="cursor-pointer select-none"
                    onClick={(e) => handleColumnSort("platform", e)}
                    title="Click to sort. Shift-click to add as a tiebreaker."
                  >
                    Platform <SortIcon column="platform" />
                  </TableHead>
                  <TableHead>Remote Scope</TableHead>
                  <TableHead
                    className="cursor-pointer select-none"
                    onClick={(e) => handleColumnSort("status", e)}
                    title="Click to sort. Shift-click to add as a tiebreaker."
                  >
                    Status <SortIcon column="status" />
                  </TableHead>
                  <TableHead
                    className="cursor-pointer select-none"
                    onClick={(e) => handleColumnSort("relevance_score", e)}
                    title="Click to sort. Shift-click to add as a tiebreaker."
                  >
                    Relevance <SortIcon column="relevance_score" />
                  </TableHead>
                  {hasActiveResume && (
                    <TableHead
                      className="cursor-pointer select-none"
                      onClick={(e) => handleColumnSort("resume_score", e)}
                      title="Click to sort. Shift-click to add as a tiebreaker."
                    >
                      ATS Match <SortIcon column="resume_score" />
                    </TableHead>
                  )}
                  <TableHead
                    className="cursor-pointer select-none"
                    onClick={(e) => handleColumnSort("first_seen_at", e)}
                    title="First discovered date — click to sort, shift-click to add as a tiebreaker."
                  >
                    Discovered <SortIcon column="first_seen_at" />
                  </TableHead>
                  {hasActiveResume && <TableHead className="w-16">Apply</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((job) => (
                  <TableRow
                    key={job.id}
                    clickable
                    onClick={() => goToJob(job.id)}
                  >
                    <TableCell onClick={(e) => e.stopPropagation()}>
                      {/* F71: per-row `aria-label` gives AT users the
                          job context when the Tab lands on a checkbox,
                          so bulk-select isn't a game of blind guesses.
                          `id="job-select-<id>"` + `name="job_ids"` lets
                          native form-enumerating AT treat them as a
                          set (optional; mostly belt-and-suspenders). */}
                      <input
                        id={`job-select-${job.id}`}
                        name="job_ids"
                        type="checkbox"
                        checked={selectedIds.has(job.id)}
                        onChange={() => toggleSelect(job.id)}
                        onClick={(e) => e.stopPropagation()}
                        aria-label={`Select ${job.title}${job.company_name ? ` at ${job.company_name}` : ""}`}
                        className="rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                      />
                    </TableCell>
                    <TableCell>
                      <div className="max-w-[300px]">
                        <p className="truncate font-medium text-gray-900">
                          {job.title}
                        </p>
                        <div className="mt-0.5 flex gap-1">
                          {/* F264 — clickable cluster badge. Pre-fix
                              this was a passive ``<Badge>`` so a
                              reviewer browsing All Jobs and seeing a
                              row tagged "infra" had no fast way to
                              filter to all infra rows. Now it's a
                              button that swaps the role_cluster
                              filter to that single cluster.
                              ``stopPropagation`` so the click doesn't
                              also trigger the row's "open detail"
                              navigation. */}
                          {job.role_cluster && (
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                setFilters((prev) => ({
                                  ...prev,
                                  role_cluster: job.role_cluster,
                                  is_classified: undefined,
                                  page: 1,
                                }));
                              }}
                              title={`Filter to ${job.role_cluster} cluster`}
                              className="cursor-pointer hover:opacity-75 transition-opacity"
                            >
                              <Badge variant="gray">{job.role_cluster}</Badge>
                            </button>
                          )}
                          {/* F263 (feedback 63ed0c32 — "Status under
                              All jobs is not visible after 18+ pages,
                              there is no status (worldwide, remote)"):
                              the geography badge was conditional on a
                              non-empty bucket. With ~60% of jobs
                              unclassified (no detectable geography
                              from location_raw + remote_scope), users
                              hit blank cells on every page and
                              assumed the column was broken. Showing
                              an explicit "Unclassified" badge with
                              muted styling makes the absence of data
                              legible — same width, same row layout,
                              clearly distinct from a real bucket. */}
                          {job.geography_bucket ? (
                            <Badge variant="gray">
                              {job.geography_bucket.replace(/_/g, " ")}
                            </Badge>
                          ) : (
                            <Badge
                              variant="default"
                              className="opacity-60 italic"
                            >
                              unclassified
                            </Badge>
                          )}
                        </div>
                      </div>
                    </TableCell>
                    <TableCell>
                      <span className="font-medium">{job.company_name}</span>
                    </TableCell>
                    <TableCell>{job.source_platform}</TableCell>
                    <TableCell>
                      <span className="max-w-[150px] truncate block text-xs">
                        {job.remote_scope}
                      </span>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={job.status} />
                    </TableCell>
                    <TableCell>
                      <ScoreBar score={job.relevance_score} />
                    </TableCell>
                    {hasActiveResume && (
                      <TableCell>
                        {job.resume_score != null ? (
                          <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-semibold ${
                            job.resume_score >= 70 ? "bg-green-100 text-green-700" :
                            job.resume_score >= 50 ? "bg-yellow-100 text-yellow-700" :
                            "bg-gray-100 text-gray-500"
                          }`}>
                            {job.resume_score}%
                          </span>
                        ) : (
                          <span className="text-xs text-gray-300">—</span>
                        )}
                      </TableCell>
                    )}
                    <TableCell>{formatDate(job.created_at)}</TableCell>
                    {hasActiveResume && (
                      <TableCell onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-1.5">
                          {applyFeedback?.jobId === job.id ? (
                            <span className={`text-xs font-medium ${applyFeedback.ok ? "text-green-600" : "text-red-500"}`}>
                              {applyFeedback.msg}
                            </span>
                          ) : (
                            <button
                              onClick={() => {
                                setApplyingJobId(job.id);
                                applyMutation.mutate(job.id);
                              }}
                              disabled={applyingJobId === job.id}
                              className="rounded p-1.5 text-primary-600 hover:bg-primary-50 hover:text-primary-700 disabled:opacity-50"
                              title="Prepare & apply to this job"
                            >
                              <Send className="h-4 w-4" />
                            </button>
                          )}
                          {/* F257: per-row routine queue toggle in
                              compact mode. One small chip; click
                              cycles through none → queued or shows
                              the current state (queued / skipped). */}
                          <RoutineQueueToggle jobId={job.id} compact />
                        </div>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Pagination
              page={data.page}
              totalPages={data.total_pages}
              onPageChange={(p) => updateFilter("page", p)}
            />
          </>
        ) : (
          <div className="py-20 text-center">
            <Filter className="mx-auto h-10 w-10 text-gray-300" />
            <p className="mt-3 text-sm font-medium text-gray-900">
              No jobs found
            </p>
            <p className="mt-1 text-sm text-gray-500">
              Try adjusting your filters or run a new scraping task.
            </p>
          </div>
        )}
      </Card>
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────────
// F241: SavedFiltersControl
//
// Inline dropdown that lets the user load / save / delete named
// filter presets. Lives next to the search bar in the JobsPage
// filter Card. Pattern:
//
//   - Closed state: a single button labeled "Saved (N)" with a star
//     icon. N is the count of presets the user has.
//   - Open state: panel with three sections:
//       * List of presets (click to apply)
//       * Save current filters as: input + button
//       * Per-row delete (trash icon) with confirm
//
// Uses TanStack Query for the list (so it auto-refreshes after a
// successful save/delete), and useMutation for the writes.
// ──────────────────────────────────────────────────────────────────────────

function SavedFiltersControl({
  currentFilters,
  onApply,
}: {
  currentFilters: JobFilters;
  onApply: (f: JobFilters) => void;
}) {
  const [open, setOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const listQuery = useQuery({
    queryKey: ["saved-filters"],
    queryFn: listSavedFilters,
  });

  const saveMutation = useMutation({
    mutationFn: () => createSavedFilter(newName.trim(), currentFilters),
    onSuccess: () => {
      setNewName("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["saved-filters"] });
    },
    onError: (e: any) => {
      // Backend returns 409 with a descriptive `detail` for name
      // conflicts; surface that inline instead of a generic toast.
      const detail =
        e?.body?.detail ||
        e?.detail ||
        e?.message ||
        "Could not save filter. Try a different name.";
      setError(typeof detail === "string" ? detail : "Could not save filter.");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteSavedFilter(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["saved-filters"] }),
  });

  const presets = listQuery.data?.items ?? [];
  const count = presets.length;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
        title="Saved filter presets"
      >
        <Star className="h-4 w-4 text-amber-500" />
        Saved {count > 0 ? `(${count})` : ""}
        {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>

      {open && (
        <div
          className="absolute right-0 top-full z-20 mt-1 w-72 rounded-lg border border-gray-200 bg-white shadow-lg"
          onMouseLeave={() => setOpen(false)}
        >
          {/* Preset list */}
          <div className="max-h-60 overflow-y-auto p-1">
            {listQuery.isLoading ? (
              <div className="px-3 py-2 text-xs text-gray-400">Loading…</div>
            ) : presets.length === 0 ? (
              <div className="px-3 py-2 text-xs text-gray-400 italic">
                No saved presets yet. Save your current filters below.
              </div>
            ) : (
              presets.map((p) => (
                <div
                  key={p.id}
                  className="group flex items-center justify-between rounded px-2 py-1.5 hover:bg-gray-100"
                >
                  <button
                    type="button"
                    onClick={() => {
                      onApply(p.filters);
                      setOpen(false);
                    }}
                    className="flex-1 truncate text-left text-sm text-gray-700"
                    title="Apply this filter preset"
                  >
                    {p.name}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      if (window.confirm(`Delete saved filter "${p.name}"?`)) {
                        deleteMutation.mutate(p.id);
                      }
                    }}
                    className="ml-2 rounded p-1 text-gray-300 opacity-0 transition group-hover:opacity-100 hover:bg-red-100 hover:text-red-600"
                    title="Delete this preset"
                    aria-label={`Delete ${p.name}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))
            )}
          </div>

          {/* Save current */}
          <div className="border-t border-gray-100 p-2">
            <div className="mb-1 text-[10px] font-semibold uppercase text-gray-400">
              Save current filters
            </div>
            <div className="flex gap-1">
              <input
                type="text"
                placeholder="e.g. Infra, Remote, Series B+"
                value={newName}
                onChange={(e) => setNewName(e.target.value.slice(0, 100))}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && newName.trim()) {
                    saveMutation.mutate();
                  }
                }}
                className="input flex-1 text-sm"
              />
              <button
                type="button"
                disabled={!newName.trim() || saveMutation.isPending}
                onClick={() => saveMutation.mutate()}
                className="rounded bg-primary-600 px-2 py-1 text-xs font-medium text-white hover:bg-primary-700 disabled:opacity-50"
              >
                {saveMutation.isPending ? "Saving…" : "Save"}
              </button>
            </div>
            {error && (
              <p className="mt-1 text-[11px] text-red-600">{error}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
