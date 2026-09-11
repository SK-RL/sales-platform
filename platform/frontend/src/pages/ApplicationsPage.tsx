import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getApplications,
  getApplicationStats,
  updateApplication,
  deleteApplication,
  getApplication,
  getApplicationSubmission,
  promoteAnswer,
  // F261 — Team Pipeline Tracker. Three new helpers feed the admin
  // "Team Pipeline" scope: the cross-user feed, the inline stage
  // editor, and the catalog of pipeline stages used by the dropdown.
  getTeamApplications,
  updateApplicationStage,
  getPipelineStages,
} from "@/lib/api";
import {
  Send,
  Briefcase,
  Clock,
  Trophy,
  Trash2,
  ExternalLink,
  ChevronLeft,
  ChevronRight,
  FileCheck,
  Mail,
  XCircle,
  LogOut,
  FileText,
  Bot,
  BookmarkPlus,
  Check,
  Users,
  User as UserIcon,
} from "lucide-react";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import { AddJobLink } from "@/components/AddJobLink";
import { useAuth } from "@/lib/auth";
import type { ApplicationDetail, SubmissionDetail } from "@/lib/types";

const STATUS_TABS: { label: string; value: string }[] = [
  { label: "All", value: "" },
  { label: "Prepared", value: "prepared" },
  // F347 server-side apply states, ordered as they occur.
  { label: "In flight", value: "in_flight" },
  { label: "Needs you", value: "needs_user" },
  { label: "Failed", value: "failed" },
  { label: "Submitted", value: "submitted" },
  { label: "Applied", value: "applied" },
  { label: "Interview", value: "interview" },
  { label: "Offer", value: "offer" },
  { label: "Rejected", value: "rejected" },
  { label: "Withdrawn", value: "withdrawn" },
];

const STATUS_COLORS: Record<string, string> = {
  prepared: "bg-gray-100 text-gray-700",
  in_flight: "bg-sky-100 text-sky-700",
  // Amber, not red: "needs you" is not a failure. The gate understood
  // the form well enough to know it shouldn't answer something on your
  // behalf. Colouring it like an error trains people to ignore it.
  needs_user: "bg-amber-100 text-amber-800",
  failed: "bg-red-100 text-red-700",
  submitted: "bg-blue-100 text-blue-700",
  applied: "bg-indigo-100 text-indigo-700",
  interview: "bg-yellow-100 text-yellow-700",
  offer: "bg-green-100 text-green-700",
  rejected: "bg-red-100 text-red-700",
  withdrawn: "bg-gray-100 text-gray-500",
};

// Mirrors app/api/v1/applications.py VALID_TRANSITIONS. Kept in sync by
// hand — the two drifted before F355, which let the UI offer a
// transition the API then rejected.
const VALID_TRANSITIONS: Record<string, string[]> = {
  prepared: ["in_flight", "needs_user", "applied", "withdrawn"],
  in_flight: ["submitted", "needs_user", "failed", "withdrawn"],
  // Both allow "applied" so you can finish an application by hand and
  // mark it done — the escape hatch, not a dead end.
  needs_user: ["in_flight", "submitted", "applied", "withdrawn"],
  failed: ["in_flight", "needs_user", "applied", "withdrawn"],
  submitted: ["applied", "withdrawn"],
  applied: ["interview", "rejected", "withdrawn"],
  interview: ["offer", "rejected", "withdrawn"],
  offer: ["rejected", "withdrawn"],
  rejected: [],
  withdrawn: [],
};

export function ApplicationsPage() {
  const navigate = useNavigate();
  // F261 — scope toggle. Default is "mine" (the original page).
  // Admin / super_admin see a "Team Pipeline" tab that swaps in the
  // cross-user feed; the toggle is hidden for reviewer/viewer to
  // avoid presenting an option that would 403 on click. The backend
  // is the source of truth (require_role("admin") on the route), so
  // this UI gate is purely UX.
  const { user: authUser } = useAuth();
  const isAdmin =
    authUser?.role === "admin" || authUser?.role === "super_admin";
  const [scope, setScope] = useState<"mine" | "team">("mine");
  const [statusFilter, setStatusFilter] = useState("");
  // F228 — provenance filter. "" = all, "review_queue" | "manual_prepare" |
  // "routine" = narrow to that source. Matches the backend Literal values
  // from applications.py:list_applications. "routine" (v6) was added once
  // the Claude Routine Apply feature started writing rows with a distinct
  // submission_source — it gets its own dedicated detail modal because
  // the snapshot shape is Q/A + cover letter + screenshots, not a
  // resume-text + score tile like the review_queue path.
  const [sourceFilter, setSourceFilter] = useState<"" | "review_queue" | "manual_prepare" | "routine">("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  // Feature C — id of the application whose apply-time snapshot is
  // currently displayed in the modal. null = modal closed.
  const [snapshotOpenFor, setSnapshotOpenFor] = useState<string | null>(null);
  // v6 — id of the application whose routine-submission detail is open.
  // Separate state from snapshotOpenFor because the two modals render
  // completely different payload shapes (SubmissionDetail vs
  // ApplicationDetail.applied_resume_*).
  const [routineOpenFor, setRoutineOpenFor] = useState<string | null>(null);
  const queryClient = useQueryClient();

  // F222: destructure full query objects for banner-backed error surfacing.
  const statsQ = useQuery({
    queryKey: ["application-stats"],
    queryFn: getApplicationStats,
  });
  const stats = statsQ.data;

  const applicationsQ = useQuery({
    queryKey: ["applications", statusFilter, sourceFilter, search, page],
    queryFn: () => getApplications({
      status: statusFilter || undefined,
      submission_source: sourceFilter || undefined,
      search: search || undefined,
      page,
      page_size: 25,
    }),
  });
  const { data, isLoading } = applicationsQ;

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }: { id: string; status?: string; notes?: string }) => updateApplication(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["applications"] });
      queryClient.invalidateQueries({ queryKey: ["application-stats"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteApplication,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["applications"] });
      queryClient.invalidateQueries({ queryKey: ["application-stats"] });
    },
  });

  // Regression finding 55: stat cards now cover all 8 application statuses
  // instead of only Total/Applied/Interview/Offer.
  const statCards = [
    { label: "Total", value: stats?.total ?? 0, icon: Briefcase, color: "text-gray-700 bg-gray-100" },
    { label: "Prepared", value: stats?.prepared ?? 0, icon: FileCheck, color: "text-gray-600 bg-gray-100" },
    { label: "Submitted", value: stats?.submitted ?? 0, icon: Mail, color: "text-blue-700 bg-blue-100" },
    { label: "Applied", value: stats?.applied ?? 0, icon: Send, color: "text-indigo-700 bg-indigo-100" },
    { label: "Interview", value: stats?.interview ?? 0, icon: Clock, color: "text-yellow-700 bg-yellow-100" },
    { label: "Offer", value: stats?.offer ?? 0, icon: Trophy, color: "text-green-700 bg-green-100" },
    { label: "Rejected", value: stats?.rejected ?? 0, icon: XCircle, color: "text-red-700 bg-red-100" },
    { label: "Withdrawn", value: stats?.withdrawn ?? 0, icon: LogOut, color: "text-gray-500 bg-gray-100" },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Applications</h1>
        <p className="text-sm text-gray-500 mt-1">Track your job applications</p>
      </div>

      {/* F222: surfaces stats/list failures. */}
      <BackendErrorBanner queries={[statsQ, applicationsQ]} />

      {/* F371 — bring your own link. Its own row: squeezed into the filter
          bar the placeholder was cut off, and this is the one thing on the
          page that starts something rather than filters it. */}
      <AddJobLink className="max-w-2xl" />

      {/* F261 — scope tabs. Admin/super_admin only. Switches the page
          between "my applications" and "team pipeline". The team view
          is rendered as a separate sub-component below so the per-user
          path stays unchanged for non-admin users. */}
      {isAdmin && (
        <div className="flex gap-1 rounded-lg border border-gray-200 bg-white p-1 w-fit">
          <button
            onClick={() => setScope("mine")}
            className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              scope === "mine"
                ? "bg-primary-100 text-primary-700"
                : "text-gray-600 hover:bg-gray-50"
            }`}
          >
            <UserIcon className="h-3.5 w-3.5" />
            My applications
          </button>
          <button
            onClick={() => setScope("team")}
            className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              scope === "team"
                ? "bg-primary-100 text-primary-700"
                : "text-gray-600 hover:bg-gray-50"
            }`}
          >
            <Users className="h-3.5 w-3.5" />
            Team pipeline
          </button>
        </div>
      )}

      {scope === "team" && isAdmin ? <TeamPipelineView /> : (
      <>
      {/* Stats */}
      <div className="grid grid-cols-4 gap-3 lg:grid-cols-8">
        {statCards.map((s) => (
          <div key={s.label} className="rounded-lg border border-gray-200 bg-white p-3">
            <div className="flex items-center gap-2">
              <div className={`rounded-lg p-1.5 ${s.color}`}>
                <s.icon className="h-4 w-4" />
              </div>
              <div>
                <p className="text-lg font-bold text-gray-900">{s.value}</p>
                <p className="text-xs text-gray-500">{s.label}</p>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div className="flex items-center gap-4">
        {/* F355 — one link into the review queue. Reviewing 10
            applications one at a time is a different job from browsing a
            table, so it gets its own screen rather than a modal here. */}
        <Link
          to="/applications/review"
          className="shrink-0 rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-100"
        >
          Review queue
        </Link>
        <div className="flex gap-1 rounded-lg border border-gray-200 bg-white p-1">
          {STATUS_TABS.map((tab) => (
            <button
              key={tab.value}
              onClick={() => { setStatusFilter(tab.value); setPage(1); }}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                statusFilter === tab.value
                  ? "bg-primary-100 text-primary-700"
                  : "text-gray-600 hover:bg-gray-50"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {/* F228 — Source dropdown. Values match the backend Literal on
            GET /applications. Labels match the badge text in the row
            below for a consistent mental model. */}
        <select
          value={sourceFilter}
          onChange={(e) => {
            setSourceFilter(e.target.value as "" | "review_queue" | "manual_prepare" | "routine");
            setPage(1);
          }}
          className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm"
          aria-label="Filter by source"
        >
          <option value="">All sources</option>
          <option value="review_queue">Review queue</option>
          <option value="manual_prepare">Manual</option>
          <option value="routine">Apply Routine</option>
        </select>
        <input
          type="text"
          placeholder="Search by job title or company..."
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
          className="w-64 rounded-lg border border-gray-200 px-3 py-1.5 text-sm"
        />
      </div>

      {/* Table */}
      <div className="rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-100">
              <th className="px-4 py-3 text-left font-medium text-gray-500">Job</th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">Company</th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">Platform</th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">Resume</th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">Source</th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">Status</th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">Date</th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">Actions</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr><td colSpan={8} className="px-4 py-8 text-center text-gray-400">Loading...</td></tr>
            ) : data?.items.length === 0 ? (
              <tr><td colSpan={8} className="px-4 py-12 text-center">
                {/* Regression finding 54: instructional empty-state with links */}
                <Briefcase className="mx-auto h-10 w-10 text-gray-300" />
                <p className="mt-3 text-sm font-medium text-gray-900">No applications yet</p>
                <p className="mt-1 text-sm text-gray-500">
                  Browse the{" "}
                  <Link to="/review" className="font-medium text-primary-600 hover:text-primary-700 underline">Review Queue</Link>
                  {" "}to accept jobs, or visit{" "}
                  <Link to="/jobs?role_cluster=relevant" className="font-medium text-primary-600 hover:text-primary-700 underline">Relevant Jobs</Link>
                  {" "}and click Apply to get started.
                </p>
              </td></tr>
            ) : (
              data?.items.map((app) => (
                <tr key={app.id} className="border-b border-gray-50 hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <button
                      onClick={() => navigate(`/jobs/${app.job_id}`)}
                      className="font-medium text-gray-900 hover:text-primary-600 text-left"
                    >
                      {app.job_title}
                    </button>
                  </td>
                  <td className="px-4 py-3 text-gray-600">{app.company_name}</td>
                  <td className="px-4 py-3">
                    <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
                      {app.platform}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-600">{app.resume_label || "—"}</td>
                  <td className="px-4 py-3">
                    {/* Feature C + v6 — provenance badge. Three shapes:
                        `review_queue`  = Applied button in Review Queue
                                          (carries resume-text snapshot)
                        `routine`       = Claude Routine Apply submission
                                          (carries Q/A + cover letter)
                        `manual_prepare`= classic /applications/prepare
                        Legacy rows without submission_source render as
                        Manual (DB default). */}
                    {app.submission_source === "review_queue" ? (
                      <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700 ring-1 ring-indigo-200">
                        Review queue
                        {typeof app.applied_resume_score_overall === "number" && (
                          <span className="ml-1 opacity-75">
                            · {app.applied_resume_score_overall}
                          </span>
                        )}
                      </span>
                    ) : app.submission_source === "routine" ? (
                      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 ring-1 ring-emerald-200">
                        <Bot className="h-3 w-3" />
                        Routine
                      </span>
                    ) : (
                      <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-600">
                        Manual
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {(VALID_TRANSITIONS[app.status]?.length ?? 0) > 0 ? (
                      <select
                        value={app.status}
                        onChange={(e) => updateMutation.mutate({ id: app.id, status: e.target.value })}
                        className={`rounded-full px-2.5 py-0.5 text-xs font-medium border-0 cursor-pointer ${STATUS_COLORS[app.status] || ""}`}
                      >
                        <option value={app.status}>
                          {STATUS_TABS.find((t) => t.value === app.status)?.label || app.status}
                        </option>
                        {(VALID_TRANSITIONS[app.status] || []).map((s) => (
                          <option key={s} value={s}>
                            {STATUS_TABS.find((t) => t.value === s)?.label || s}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_COLORS[app.status] || ""}`}>
                        {STATUS_TABS.find((t) => t.value === app.status)?.label || app.status}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">
                    {app.applied_at
                      ? new Date(app.applied_at).toLocaleDateString()
                      : new Date(app.created_at).toLocaleDateString()}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => navigate(`/jobs/${app.job_id}`)}
                        className="rounded p-1 text-gray-400 hover:bg-primary-50 hover:text-primary-600"
                        title="View job details & apply"
                      >
                        <Send className="h-4 w-4" />
                      </button>
                      {app.job_url && (
                        <a href={app.job_url} target="_blank" rel="noopener noreferrer"
                          className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600" title="Open ATS page">
                          <ExternalLink className="h-4 w-4" />
                        </a>
                      )}
                      {/* F361 — the way INTO server-side apply. Without
                          this the feature was unreachable from the UI:
                          the review queue only lists applications already
                          in needs_user, and nothing could get there
                          except the sweep, which is off by default.
                          Deliberately a link to the review screen rather
                          than a one-click send — an application should
                          never leave a table row unseen. */}
                      {["prepared", "needs_user", "failed"].includes(app.status) && (
                        <Link
                          to={`/applications/review?app=${app.id}`}
                          className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                          title="Review & apply automatically"
                        >
                          <Bot className="h-4 w-4" />
                        </Link>
                      )}
                      {/* Feature C — "What we sent" modal trigger. Shown
                          for rows that carry a submit-time snapshot. Two
                          modal shapes:
                            review_queue → ApplySnapshotModal (resume body
                                            + score tiles)
                            routine      → RoutineSubmissionModal (Q/A +
                                            cover letter + screenshots)
                          manual_prepare rows never carry a snapshot so
                          no button is rendered for them. */}
                      {app.submission_source === "review_queue" && (
                        <button
                          onClick={() => setSnapshotOpenFor(app.id)}
                          className="rounded p-1 text-gray-400 hover:bg-primary-50 hover:text-primary-600"
                          title="What we sent"
                        >
                          <FileText className="h-4 w-4" />
                        </button>
                      )}
                      {app.submission_source === "routine" && (
                        <button
                          onClick={() => setRoutineOpenFor(app.id)}
                          className="rounded p-1 text-gray-400 hover:bg-emerald-50 hover:text-emerald-600"
                          title="Routine submission detail"
                        >
                          <FileText className="h-4 w-4" />
                        </button>
                      )}
                      {app.status === "prepared" && (
                        <button
                          onClick={() => deleteMutation.mutate(app.id)}
                          className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600" title="Delete">
                          <Trash2 className="h-4 w-4" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>

        {/* Pagination */}
        {data && data.total_pages > 1 && (
          <div className="flex items-center justify-between border-t border-gray-100 px-4 py-3">
            <span className="text-sm text-gray-500">
              Page {data.page} of {data.total_pages} ({data.total} total)
            </span>
            <div className="flex gap-1">
              <button
                onClick={() => setPage(Math.max(1, page - 1))}
                disabled={page <= 1}
                className="rounded p-1 text-gray-400 hover:bg-gray-100 disabled:opacity-30"
              >
                <ChevronLeft className="h-5 w-5" />
              </button>
              <button
                onClick={() => setPage(Math.min(data.total_pages, page + 1))}
                disabled={page >= data.total_pages}
                className="rounded p-1 text-gray-400 hover:bg-gray-100 disabled:opacity-30"
              >
                <ChevronRight className="h-5 w-5" />
              </button>
            </div>
          </div>
        )}
      </div>
      </>
      )}

      {/* Feature C — apply-time snapshot modal. Fetches the full detail
          payload on demand (the list endpoint doesn't ship the resume
          text). Shows which resume + score + exact resume body were
          submitted, so an edit to the underlying resume later can't
          obscure what the application actually contained. */}
      {snapshotOpenFor && (
        <ApplySnapshotModal
          applicationId={snapshotOpenFor}
          onClose={() => setSnapshotOpenFor(null)}
        />
      )}

      {/* v6 Claude Routine — dedicated detail modal for routine rows.
          Separate component because the payload shape (Q/A list, cover
          letter, screenshots, detected_issues) is fundamentally unlike
          the review_queue resume snapshot, and conflating them would
          make the UI code a pile of optional-chained ternaries. */}
      {routineOpenFor && (
        <RoutineSubmissionModal
          applicationId={routineOpenFor}
          onClose={() => setRoutineOpenFor(null)}
        />
      )}
    </div>
  );
}


// F261 — Team Pipeline view. Shown when an admin/super_admin flips
// the scope toggle to "team". Cross-user feed of every application
// with applicant identity, status, and stage_key. The stage column
// is editable via inline dropdown — admins use this to manually
// move applications through the configurable funnel ("Interview 1",
// "Final round", etc.) without touching the per-user status enum.
//
// Two queries:
//   - getTeamApplications: the rows (admin-gated 403 → falls through
//     to the BackendErrorBanner just like any other failure).
//   - getPipelineStages: the catalog used by the inline dropdown.
//     Cached because stages don't change often; same key as the
//     PipelinePage so the data is shared.
function TeamPipelineView() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [stageFilter, setStageFilter] = useState<string>("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);

  const teamQ = useQuery({
    queryKey: ["applications-team", statusFilter, stageFilter, search, page],
    queryFn: () =>
      getTeamApplications({
        status: statusFilter || undefined,
        stage_key: stageFilter || undefined,
        search: search || undefined,
        page,
        page_size: 25,
      }),
  });
  const stagesQ = useQuery({
    queryKey: ["pipeline-stages"],
    queryFn: getPipelineStages,
  });
  const stages = stagesQ.data?.items.filter((s) => s.is_active) || [];

  const stageMutation = useMutation({
    mutationFn: ({ id, stageKey }: { id: string; stageKey: string | null }) =>
      updateApplicationStage(id, stageKey),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["applications-team"] });
      queryClient.invalidateQueries({ queryKey: ["pipeline"] });
    },
  });

  const data = teamQ.data;
  const items = data?.items ?? [];

  return (
    <div className="space-y-4">
      <BackendErrorBanner queries={[teamQ, stagesQ]} />

      {/* Filters: status (apply-state), stage (funnel), search */}
      <div className="flex flex-wrap items-center gap-3">
        <select
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value);
            setPage(1);
          }}
          className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm"
          aria-label="Filter by status"
        >
          {STATUS_TABS.map((t) => (
            <option key={t.value} value={t.value}>
              {t.value === "" ? "All statuses" : t.label}
            </option>
          ))}
        </select>
        <select
          value={stageFilter}
          onChange={(e) => {
            setStageFilter(e.target.value);
            setPage(1);
          }}
          className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm"
          aria-label="Filter by pipeline stage"
        >
          <option value="">All stages</option>
          {stages.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Search job, company, or applicant…"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(1);
          }}
          className="w-72 rounded-lg border border-gray-200 px-3 py-1.5 text-sm"
        />
      </div>

      <div className="rounded-lg border border-gray-200 bg-white">
        {/* Horizontal scroll wrapper — F260 mac-mini fix style:
            scrollbar-gutter:stable + visible WebKit thumb so admins
            on macOS without a touchpad can still see every column. */}
        <div className="overflow-x-auto [scrollbar-gutter:stable] [&::-webkit-scrollbar]:h-2 [&::-webkit-scrollbar]:bg-gray-100 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-gray-400">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100">
                <th className="px-4 py-3 text-left font-medium text-gray-500">Applied</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Applicant</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Job</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Company</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Resume</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Status</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Stage</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Source</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Actions</th>
              </tr>
            </thead>
            <tbody>
              {teamQ.isLoading ? (
                <tr>
                  <td colSpan={9} className="px-4 py-8 text-center text-gray-400">
                    Loading…
                  </td>
                </tr>
              ) : items.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-4 py-12 text-center">
                    <Users className="mx-auto h-10 w-10 text-gray-300" />
                    <p className="mt-3 text-sm font-medium text-gray-900">
                      No team applications match these filters
                    </p>
                    <p className="mt-1 text-sm text-gray-500">
                      Once a teammate applies to a job, the row appears here.
                    </p>
                  </td>
                </tr>
              ) : (
                items.map((row) => (
                  <tr key={row.id} className="border-b border-gray-50 hover:bg-gray-50">
                    <td className="px-4 py-3 text-gray-600 whitespace-nowrap">
                      {row.applied_at
                        ? new Date(row.applied_at).toLocaleDateString()
                        : new Date(row.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3">
                      {/* Applicant identity is the headline column on
                          the team feed — when HR replies later, this
                          is the row that lets the operator see "Sarthak
                          applied with Resume A from Account X". */}
                      <div className="font-medium text-gray-900">{row.applicant_name}</div>
                      <div className="text-xs text-gray-500">{row.applicant_email}</div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="font-medium text-gray-900">{row.job_title}</div>
                      <div className="text-xs text-gray-500">{row.platform}</div>
                    </td>
                    <td className="px-4 py-3 text-gray-600">{row.company_name || "—"}</td>
                    <td className="px-4 py-3 text-gray-600">{row.resume_label || "—"}</td>
                    <td className="px-4 py-3">
                      <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_COLORS[row.status] || ""}`}>
                        {STATUS_TABS.find((t) => t.value === row.status)?.label || row.status}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {/* Inline stage selector — admins move the
                          application through the configurable funnel
                          here. ``stage_key=null`` clears it (the
                          empty-option below). */}
                      <select
                        value={row.stage_key ?? ""}
                        onChange={(e) =>
                          stageMutation.mutate({
                            id: row.id,
                            stageKey: e.target.value || null,
                          })
                        }
                        disabled={stageMutation.isPending}
                        className="rounded-lg border border-gray-200 bg-white px-2 py-1 text-xs"
                      >
                        <option value="">— no stage —</option>
                        {stages.map((s) => (
                          <option key={s.key} value={s.key}>
                            {s.label}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="px-4 py-3">
                      {row.submission_source === "review_queue" ? (
                        <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700 ring-1 ring-indigo-200">
                          Review queue
                        </span>
                      ) : row.submission_source === "routine" ? (
                        <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 ring-1 ring-emerald-200">
                          <Bot className="h-3 w-3" />
                          Routine
                        </span>
                      ) : (
                        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-600">
                          Manual
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {row.job_url && (
                        <a
                          href={row.job_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                          title="Open ATS posting"
                        >
                          <ExternalLink className="h-4 w-4" />
                        </a>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {data && (data.total_pages ?? 1) > 1 && (
          <div className="flex items-center justify-between border-t border-gray-100 px-4 py-3">
            <span className="text-sm text-gray-500">
              Page {data.page} of {data.total_pages} ({data.total} total)
            </span>
            <div className="flex gap-1">
              <button
                onClick={() => setPage(Math.max(1, page - 1))}
                disabled={page <= 1}
                className="rounded p-1 text-gray-400 hover:bg-gray-100 disabled:opacity-30"
              >
                <ChevronLeft className="h-5 w-5" />
              </button>
              <button
                onClick={() =>
                  setPage(Math.min(data.total_pages ?? 1, page + 1))
                }
                disabled={page >= (data.total_pages ?? 1)}
                className="rounded p-1 text-gray-400 hover:bg-gray-100 disabled:opacity-30"
              >
                <ChevronRight className="h-5 w-5" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}


function ApplySnapshotModal({ applicationId, onClose }: { applicationId: string; onClose: () => void }) {
  // Dedicated query key so a closed-then-reopened modal refetches (users
  // expect "what we sent" to reflect any subsequent status transitions).
  const detailQ = useQuery<ApplicationDetail>({
    queryKey: ["application", applicationId],
    queryFn: () => getApplication(applicationId) as Promise<ApplicationDetail>,
  });

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-xl bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b border-gray-200 px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">What we sent</h2>
            <p className="mt-0.5 text-xs text-gray-500">
              Snapshot of the resume + score used when this application was submitted.
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
            aria-label="Close"
          >
            <XCircle className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {detailQ.isLoading && (
            <p className="text-sm text-gray-500">Loading snapshot…</p>
          )}
          {detailQ.isError && (
            <p className="text-sm text-red-600">Could not load snapshot.</p>
          )}
          {detailQ.data && (() => {
            const d = detailQ.data;
            const snap = d.applied_resume_score_snapshot;
            return (
              <>
                <div className="rounded-md bg-gray-50 px-4 py-3 text-sm">
                  <div className="font-medium text-gray-900">{d.job.title}</div>
                  <div className="text-xs text-gray-500">{d.job.company_name} · {d.job.platform}</div>
                  <div className="mt-2 text-xs text-gray-600">
                    Resume used: <span className="font-medium">{d.resume.label}</span>
                  </div>
                  {d.applied_at && (
                    <div className="text-xs text-gray-600">
                      Submitted: {new Date(d.applied_at).toLocaleString()}
                    </div>
                  )}
                </div>

                {snap && (
                  <div>
                    <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                      Score at submit-time
                    </p>
                    <div className="grid grid-cols-4 gap-2 text-sm">
                      <ScoreTile label="Overall" value={snap.overall} />
                      <ScoreTile label="Keyword" value={snap.keyword} />
                      <ScoreTile label="Role match" value={snap.role_match} />
                      <ScoreTile label="Format" value={snap.format} />
                    </div>
                  </div>
                )}

                <div>
                  <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                    Resume body sent
                  </p>
                  {d.applied_resume_text ? (
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 p-3 text-xs text-gray-700 ring-1 ring-gray-200">
                      {d.applied_resume_text}
                    </pre>
                  ) : (
                    <p className="text-xs italic text-gray-400">
                      No resume text snapshot (legacy row).
                    </p>
                  )}
                </div>

                {d.ai_customization_log_id && (
                  <p className="text-xs text-gray-500">
                    ✨ AI-customized resume (log id {d.ai_customization_log_id.slice(0, 8)}…)
                  </p>
                )}
              </>
            );
          })()}
        </div>
      </div>
    </div>
  );
}


function ScoreTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md bg-gray-50 px-3 py-2 text-center ring-1 ring-gray-200">
      <div className="text-lg font-semibold text-gray-900">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-gray-500">{label}</div>
    </div>
  );
}


/**
 * v6 Claude Routine Apply — submission-detail modal.
 *
 * Fetches the full SubmissionDetail payload for a routine-submitted
 * application and renders:
 *   - Q/A answers, with a "Save to Answer Book" button for each
 *     generated answer (user-supplied manual-required answers are
 *     already canonical, so no promote button there)
 *   - cover letter text (always regenerated per-submit)
 *   - confirmation text captured from the thank-you page
 *   - screenshot keys (download links — the bytes live on object
 *     storage keyed by these)
 *   - detected_issues list (humanizer warnings that didn't block)
 *   - profile_snapshot (resume + contact fields frozen at submit)
 */
function RoutineSubmissionModal({
  applicationId,
  onClose,
}: {
  applicationId: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const detailQ = useQuery<SubmissionDetail>({
    queryKey: ["application-submission", applicationId],
    queryFn: () => getApplicationSubmission(applicationId),
  });
  // Track which answers have been promoted in this modal session so the
  // button flips to a success state without refetching the whole payload.
  // Keyed by question text (the primary key the promote endpoint uses).
  const [promoted, setPromoted] = useState<Record<string, boolean>>({});

  const promoteMutation = useMutation({
    mutationFn: ({ question, answer }: { question: string; answer: string }) =>
      promoteAnswer(applicationId, { question, answer }),
    onSuccess: (_data, vars) => {
      setPromoted((prev) => ({ ...prev, [vars.question]: true }));
      queryClient.invalidateQueries({ queryKey: ["answer-book"] });
    },
  });

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-xl bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b border-gray-200 px-5 py-4">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-100">
              <Bot className="h-4 w-4 text-emerald-700" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-gray-900">
                Routine submission detail
              </h2>
              <p className="mt-0.5 text-xs text-gray-500">
                Everything Claude sent on your behalf — answers, cover
                letter, and screenshots.
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
            aria-label="Close"
          >
            <XCircle className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
          {detailQ.isLoading && (
            <p className="text-sm text-gray-500">Loading submission…</p>
          )}
          {detailQ.isError && (
            <p className="text-sm text-red-600">
              Could not load submission detail.
            </p>
          )}
          {detailQ.data && (() => {
            const d = detailQ.data;
            return (
              <>
                {/* Detected-issues banner — humanizer warnings that
                    passed non-blocking. Show only when non-empty so it
                    doesn't take space on clean runs. */}
                {d.detected_issues && d.detected_issues.length > 0 && (
                  <div className="rounded-md border border-amber-200 bg-amber-50 p-3">
                    <p className="text-xs font-semibold uppercase tracking-wide text-amber-700">
                      Humanizer warnings ({d.detected_issues.length})
                    </p>
                    <ul className="mt-1 list-disc pl-5 text-xs text-amber-700">
                      {d.detected_issues.map((issue, i) => (
                        <li key={i}>{issue}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* Q/A answers — the heart of this modal. Generated
                    answers get a promote button; manual-required ones
                    come from the Answer Book so no promote needed.
                    The backend ships answers_json as a loose
                    Record<string, unknown>[] so the types stay flexible;
                    we narrow at runtime here and render only rows with
                    a string question+answer. */}
                {d.answers_json && d.answers_json.length > 0 && (() => {
                  const narrowed = d.answers_json
                    .map((qa) => {
                      const question =
                        typeof qa.question === "string" ? qa.question : null;
                      const answer =
                        typeof qa.answer === "string" ? qa.answer : null;
                      const source =
                        typeof qa.source === "string" ? qa.source : "unknown";
                      if (!question || !answer) return null;
                      return { question, answer, source };
                    })
                    .filter(
                      (
                        x,
                      ): x is { question: string; answer: string; source: string } =>
                        x !== null,
                    );
                  if (narrowed.length === 0) return null;
                  return (
                    <div>
                      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                        Application answers ({narrowed.length})
                      </p>
                      <ul className="space-y-3">
                        {narrowed.map((qa, i) => {
                          const isPromoted =
                            promoted[qa.question] ||
                            qa.source === "answer_book";
                          const canPromote = qa.source === "generated";
                          return (
                            <li
                              key={i}
                              className="rounded-md border border-gray-200 p-3"
                            >
                              <div className="flex items-start justify-between gap-3">
                                <p className="flex-1 text-sm font-medium text-gray-900">
                                  {qa.question}
                                </p>
                                <span
                                  className={`flex-shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
                                    qa.source === "answer_book"
                                      ? "bg-indigo-50 text-indigo-700"
                                      : qa.source === "generated"
                                        ? "bg-emerald-50 text-emerald-700"
                                        : "bg-gray-100 text-gray-600"
                                  }`}
                                >
                                  {qa.source}
                                </span>
                              </div>
                              <p className="mt-1.5 whitespace-pre-wrap text-sm text-gray-700">
                                {qa.answer}
                              </p>
                              {canPromote && (
                                <div className="mt-2 flex justify-end">
                                  <button
                                    type="button"
                                    onClick={() =>
                                      promoteMutation.mutate({
                                        question: qa.question,
                                        answer: qa.answer,
                                      })
                                    }
                                    disabled={
                                      isPromoted || promoteMutation.isPending
                                    }
                                    className={`inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                                      isPromoted
                                        ? "bg-emerald-50 text-emerald-700"
                                        : "bg-primary-50 text-primary-700 hover:bg-primary-100"
                                    } disabled:cursor-not-allowed`}
                                  >
                                    {isPromoted ? (
                                      <>
                                        <Check className="h-3 w-3" />
                                        Saved
                                      </>
                                    ) : (
                                      <>
                                        <BookmarkPlus className="h-3 w-3" />
                                        Save to Answer Book
                                      </>
                                    )}
                                  </button>
                                </div>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  );
                })()}

                {/* Cover letter — always regenerated per-submit so the
                    text is authoritative for this application only. */}
                {d.cover_letter_text && (
                  <div>
                    <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                      Cover letter sent
                    </p>
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 p-3 text-xs text-gray-700 ring-1 ring-gray-200">
                      {d.cover_letter_text}
                    </pre>
                  </div>
                )}

                {/* Confirmation text captured from the ATS thank-you
                    page — the strongest "it actually went through"
                    signal we have short of an email. */}
                {d.confirmation_text && (
                  <div>
                    <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                      Confirmation captured
                    </p>
                    <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md bg-emerald-50 p-3 text-xs text-emerald-800 ring-1 ring-emerald-200">
                      {d.confirmation_text}
                    </pre>
                  </div>
                )}

                {/* Screenshot keys — just link to the object-store
                    paths. Rendering the images inline would mean an
                    extra signed-URL round trip; keys are enough for
                    the "verify it happened" use case. */}
                {d.screenshot_keys && d.screenshot_keys.length > 0 && (
                  <div>
                    <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                      Screenshots ({d.screenshot_keys.length})
                    </p>
                    <ul className="space-y-1 text-xs">
                      {d.screenshot_keys.map((key, i) => (
                        <li
                          key={i}
                          className="truncate rounded bg-gray-50 px-2 py-1 font-mono text-gray-600"
                        >
                          {key}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* Profile snapshot — name + email + resume used at
                    submit time. Frozen so later profile edits don't
                    retroactively rewrite history. */}
                {d.profile_snapshot && (
                  <div>
                    <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                      Profile snapshot at submit
                    </p>
                    <div className="rounded-md bg-gray-50 p-3 text-xs text-gray-700 ring-1 ring-gray-200">
                      <pre className="whitespace-pre-wrap">
                        {JSON.stringify(d.profile_snapshot, null, 2)}
                      </pre>
                    </div>
                  </div>
                )}
              </>
            );
          })()}
        </div>
      </div>
    </div>
  );
}
