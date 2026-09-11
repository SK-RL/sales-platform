import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getRoutineTopToApply,
  getKillSwitch,
  setKillSwitch,
  getApplications,
  listRoutineRuns,
  getRoutinePreferences,
  putRoutinePreferences,
  getRoutineQueue,
  deleteRoutineTarget,
  getRoleClusters,
  getExcludedCompanies,
  removeExcludedCompany,
} from "@/lib/api";
import type { RoutineRun, RoutinePreferences } from "@/lib/types";
// ``Link`` already imported from react-router-dom at the top of the file.
import {
  ShieldAlert,
  ShieldCheck,
  Bot,
  CheckCircle2,
  XCircle,
  Clock,
  ExternalLink,
  AlertTriangle,
  Sliders,
  Ban,
  Save,
  X,
} from "lucide-react";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import { RoutineQueueToggle } from "@/components/RoutineQueueToggle";

/**
 * Claude Routine Apply — operator control panel.
 *
 * Three panels on one page:
 *   1. Pre-flight card (kill-switch + daily-cap + answer-book readiness)
 *   2. Top-to-apply — the jobs the routine would target next
 *   3. Recent runs — history with drill-down to run detail
 *
 * No "Run now" button: the routine runs as an MCP-Chrome browser
 * session driven by Claude, not as a backend task. This page just
 * tells the operator what state the routine will see when it polls.
 */

export function RoutinePage() {
  const queryClient = useQueryClient();
  const [killReason, setKillReason] = useState("");

  // Operator panel poll cadence — all three queries refresh on the same
  // 30s tick so the pre-flight card, top-to-apply list, and recent-runs
  // counters stay in sync. Previously only top-to-apply polled, which
  // left the runs list stale for minutes while a live run ticked its
  // counters forward via PATCH /routine/runs/{id}.
  const POLL_MS = 30_000;

  const topToApplyQ = useQuery({
    queryKey: ["routine-top-to-apply"],
    queryFn: () => getRoutineTopToApply(10),
    refetchInterval: POLL_MS,
  });

  const killSwitchQ = useQuery({
    queryKey: ["routine-kill-switch"],
    queryFn: getKillSwitch,
    refetchInterval: POLL_MS,
  });

  const runsQ = useQuery({
    queryKey: ["routine-runs"],
    queryFn: () => listRoutineRuns(10),
    refetchInterval: POLL_MS,
  });
  // F400 (tester round 2, finding 6): applications the routine recorded
  // through /applications/record never create a routine_runs row, so the
  // panel said "No routine runs yet" while the cap counted ten of them.
  const routineAppsQ = useQuery({
    queryKey: ["routine-applications"],
    queryFn: () => getApplications({ submission_source: "routine", page: 1, page_size: 50 }),
    refetchInterval: POLL_MS,
  });
  const routineApps = routineAppsQ.data?.items ?? [];

  const killSwitchMutation = useMutation({
    mutationFn: (payload: { disabled: boolean; reason?: string | null }) =>
      setKillSwitch(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["routine-kill-switch"] });
      queryClient.invalidateQueries({ queryKey: ["routine-top-to-apply"] });
      setKillReason("");
    },
  });

  const topToApply = topToApplyQ.data;
  const killSwitch = killSwitchQ.data;
  const runs = runsQ.data ?? [];

  const anyError =
    topToApplyQ.isError || killSwitchQ.isError || runsQ.isError;

  // Daily-cap is amber at ≤2 remaining — "1 left" is effectively
  // exhausted for operator planning. Green at 3+, amber 1-2, amber
  // (with the "cap hit" copy) at 0.
  const dailyRemaining = topToApply?.daily_cap_remaining ?? 0;
  const dailyGood = dailyRemaining > 2;

  if (topToApplyQ.isLoading || killSwitchQ.isLoading) {
    return (
      <div className="p-8 text-sm text-neutral-500">
        Loading routine state…
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary-100">
          <Bot className="h-5 w-5 text-primary-700" />
        </div>
        <div>
          <h1 className="text-2xl font-bold text-neutral-900">
            Apply Routine
          </h1>
          <p className="text-sm text-neutral-500">
            Two ways to apply live here: the <strong>Claude Desktop routine</strong> (semi-attended, its own
            10-a-day cap, shown above) and <strong>Auto-apply</strong> (fully unattended, its own cap — settings{" "}
            <a href="#auto-apply" className="underline">below</a>, home at{" "}
            <Link to="/apply" className="underline">Auto-apply</Link>).
          </p>
        </div>
      </div>

      {anyError && (
        <BackendErrorBanner queries={[topToApplyQ, killSwitchQ, runsQ]} />
      )}

      {/* ── Pre-flight card ───────────────────────────────────────── */}
      <div className="rounded-lg border border-neutral-200 bg-white shadow-sm">
        <div className="border-b border-neutral-100 px-6 py-4">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-500">
            Pre-flight
          </h2>
        </div>
        <div className="grid gap-4 p-6 md:grid-cols-3">
          <PreflightCell
            label="Kill-switch"
            value={
              killSwitch?.disabled
                ? "Routine disabled"
                : "Routine active"
            }
            good={!killSwitch?.disabled}
            icon={killSwitch?.disabled ? ShieldAlert : ShieldCheck}
            detail={
              killSwitch?.disabled && killSwitch?.reason
                ? `Reason: ${killSwitch.reason}`
                : undefined
            }
          />
          <PreflightCell
            label="Answer book"
            value={
              topToApply?.answer_book_ready
                ? "Ready"
                : "Setup incomplete"
            }
            good={Boolean(topToApply?.answer_book_ready)}
            icon={
              topToApply?.answer_book_ready ? CheckCircle2 : AlertTriangle
            }
            detail={
              !topToApply?.answer_book_ready
                ? undefined
                : "All required entries filled"
            }
          >
            {!topToApply?.answer_book_ready && (
              <Link
                to="/answer-book/required-setup"
                className="text-xs font-medium text-primary-600 hover:text-primary-700 underline"
              >
                Finish setup →
              </Link>
            )}
          </PreflightCell>
          <PreflightCell
            label="Routine cap (Claude Desktop runs)"
            value={`${dailyRemaining} of 10 left`}
            good={dailyGood}
            icon={Clock}
            detail={
              dailyRemaining === 0
                ? "Cap hit — live runs blocked until rollover"
                : dailyRemaining <= 2
                  ? "Almost exhausted (rolling 24h)"
                  : "Rolling 24-hour window"
            }
          />
        </div>

        {/* Kill-switch toggle */}
        <div className="border-t border-neutral-100 bg-neutral-50 px-6 py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex-1">
              <p className="text-sm font-medium text-neutral-900">
                {killSwitch?.disabled
                  ? "Re-enable the routine"
                  : "Stop the routine immediately"}
              </p>
              <p className="text-xs text-neutral-500">
                {killSwitch?.disabled
                  ? "Clears the disabled flag and lets new runs start."
                  : "Locks the routine from starting new runs; in-flight runs abort within ~60 seconds."}
              </p>
            </div>
            <div className="flex gap-2">
              {!killSwitch?.disabled && (
                <input
                  type="text"
                  placeholder="Reason (optional)"
                  value={killReason}
                  onChange={(e) => setKillReason(e.target.value)}
                  className="w-48 rounded-md border border-neutral-300 px-3 py-1.5 text-sm focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
                />
              )}
              <button
                type="button"
                onClick={() => {
                  // Re-enable flow sends reason=null; wipe local input
                  // state too so a stale "deploying new version" string
                  // doesn't sit in the textbox after the toggle flips.
                  if (killSwitch?.disabled) setKillReason("");
                  killSwitchMutation.mutate({
                    disabled: !killSwitch?.disabled,
                    reason: killSwitch?.disabled ? null : killReason || null,
                  });
                }}
                disabled={killSwitchMutation.isPending}
                className={`inline-flex items-center gap-2 rounded-md px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50 ${
                  killSwitch?.disabled
                    ? "bg-emerald-600 hover:bg-emerald-700"
                    : "bg-red-600 hover:bg-red-700"
                }`}
              >
                {killSwitch?.disabled ? (
                  <>
                    <ShieldCheck className="h-4 w-4" /> Re-enable
                  </>
                ) : (
                  <>
                    <ShieldAlert className="h-4 w-4" /> Disable routine
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* ── F259: How-to-run help banner ─────────────────────────── */}
      <HowToRunCard />

      {/* ── F257: Preferences card ───────────────────────────────── */}
      <PreferencesCard />

      {/* ── F257: Manual queue + excluded list ───────────────────── */}
      <ManualQueueCard />

      {/* ── F259: Excluded companies (no-fly list) ───────────────── */}
      <ExcludedCompaniesCard />

      {/* ── Top to apply ──────────────────────────────────────────── */}
      <div className="rounded-lg border border-neutral-200 bg-white shadow-sm">
        <div className="flex items-center justify-between border-b border-neutral-100 px-6 py-4">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-500">
            Next {topToApply?.jobs.length ?? 0} targets
          </h2>
          <p className="text-xs text-neutral-500">
            Ordered by relevance · Excludes LinkedIn ·{" "}
            <span className="text-neutral-700">
              30-day company cooldown applied
            </span>
          </p>
        </div>
        {!topToApply?.jobs.length ? (
          <div className="p-6 text-sm text-neutral-500">
            No jobs currently match the routine's filters. Try lowering
            the relevance cutoff in role clusters, or wait for fresh
            scrapes.
          </div>
        ) : (
          <ul className="divide-y divide-neutral-100">
            {topToApply.jobs.map((job) => (
              <li
                key={job.job_id}
                className="flex items-center justify-between px-6 py-3 hover:bg-neutral-50"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <Link
                      to={`/jobs/${job.job_id}`}
                      className="truncate text-sm font-medium text-neutral-900 hover:text-primary-700"
                    >
                      {job.title}
                    </Link>
                    {/* F257: badge operator-pinned rows so the user
                        can confirm their manual queue is taking effect.
                        Auto-picked rows show no badge. */}
                    {job.is_queued && (
                      <span className="inline-flex items-center gap-0.5 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-800">
                        Queued
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 text-xs text-neutral-500">
                    <span>{job.company_name}</span>
                    <span className="mx-1.5">·</span>
                    <span>{job.platform}</span>
                    {job.geography_bucket && (
                      <>
                        <span className="mx-1.5">·</span>
                        <span>{job.geography_bucket}</span>
                      </>
                    )}
                    {job.role_cluster && (
                      <>
                        <span className="mx-1.5">·</span>
                        <span>{job.role_cluster}</span>
                      </>
                    )}
                  </div>
                </div>
                <div className="ml-4 flex flex-shrink-0 items-center gap-2">
                  <div className="rounded-md bg-primary-50 px-2 py-1 text-xs font-semibold text-primary-700">
                    {Math.round(job.relevance_score)}
                  </div>
                  {/* F257: per-row routine-queue toggle so the user
                      can remove a job from the next-targets list with
                      one click (becomes 'excluded'). Compact mode keeps
                      the row layout tight. */}
                  <RoutineQueueToggle jobId={job.job_id} compact />
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ── Applications recorded by the routine (F400) ─────────── */}
      {routineApps.length > 0 && (
        <div className="rounded-lg border border-neutral-200 bg-white shadow-sm">
          <div className="border-b border-neutral-100 px-6 py-4">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-500">
              Applied by the routine
            </h2>
          </div>
          <ul className="divide-y divide-neutral-100">
            {routineApps.slice(0, 10).map((a) => (
              <li key={a.id} className="flex items-center justify-between px-6 py-3 text-sm">
                <span className="min-w-0 truncate">
                  <span className="font-medium text-neutral-900">{a.job_title}</span>
                  <span className="text-neutral-500"> · {a.company_name}</span>
                </span>
                <span className="ml-3 shrink-0 text-xs text-neutral-500">
                  {a.status}{a.applied_at ? ` · ${new Date(a.applied_at).toLocaleDateString()}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Recent runs ───────────────────────────────────────────── */}
      <div className="rounded-lg border border-neutral-200 bg-white shadow-sm">
        <div className="border-b border-neutral-100 px-6 py-4">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-500">
            Recent runs
          </h2>
        </div>
        {!runs.length ? (
          <div className="p-6 text-sm text-neutral-500">
            {routineApps.length === 0
              ? "No routine runs yet."
              : `No batch runs logged, but the routine has recorded ${routineApps.length} application${routineApps.length === 1 ? "" : "s"} one by one — these are what the cap counts.`}
          </div>
        ) : (
          <ul className="divide-y divide-neutral-100">
            {runs.map((run) => (
              <RunListItem key={run.id} run={run} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function PreflightCell({
  label,
  value,
  good,
  icon: Icon,
  detail,
  children,
}: {
  label: string;
  value: string;
  good: boolean;
  icon: React.ComponentType<{ className?: string }>;
  detail?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-neutral-100 p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-neutral-500">
          {label}
        </span>
        <Icon
          className={`h-4 w-4 ${
            good ? "text-emerald-500" : "text-amber-500"
          }`}
        />
      </div>
      <p
        className={`mt-1 text-lg font-semibold ${
          good ? "text-neutral-900" : "text-amber-700"
        }`}
      >
        {value}
      </p>
      {detail && <p className="mt-1 text-xs text-neutral-500">{detail}</p>}
      {children && <div className="mt-2">{children}</div>}
    </div>
  );
}

function RunListItem({ run }: { run: RoutineRun }) {
  const statusColor =
    run.status === "complete"
      ? "bg-emerald-100 text-emerald-700"
      : run.status === "aborted"
        ? "bg-red-100 text-red-700"
        : "bg-blue-100 text-blue-700";
  return (
    <li className="px-6 py-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span
              className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${statusColor}`}
            >
              {run.status}
            </span>
            <span className="text-xs font-medium uppercase tracking-wide text-neutral-500">
              {run.mode}
            </span>
            {run.kill_switch_triggered && (
              <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-xs text-red-700">
                <XCircle className="h-3 w-3" /> killed
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-neutral-900">
            {run.applications_submitted} submitted ·{" "}
            {run.applications_attempted} attempted ·{" "}
            {run.applications_skipped.length} skipped
          </p>
          <p className="mt-0.5 text-xs text-neutral-500">
            Started {new Date(run.started_at).toLocaleString()}
            {run.ended_at && (
              <>
                <span className="mx-1.5">·</span>
                Ended {new Date(run.ended_at).toLocaleString()}
              </>
            )}
          </p>
        </div>
        <Link
          to={`/routine/runs/${run.id}`}
          className="inline-flex items-center gap-1 text-xs font-medium text-primary-600 hover:text-primary-700"
        >
          <ExternalLink className="h-3.5 w-3.5" />
          Detail
        </Link>
      </div>
    </li>
  );
}


// ─────────────────────────────────────────────────────────────────────
// F257 — Preferences card
// ─────────────────────────────────────────────────────────────────────

/**
 * Per-user filter preferences for the Apply Routine's auto-picker.
 *
 * Loads from ``GET /routine/preferences``, edits in local state until
 * Save, then PUTs the full prefs object (the backend uses replace
 * semantics — sending a field at its zero value resets it).
 *
 * All fields render even when zero/empty so the operator can SEE
 * "yes, I have not set a relevance floor" — silent defaults look
 * like settings that aren't there.
 */
function PreferencesCard() {
  const queryClient = useQueryClient();
  const prefsQ = useQuery({
    queryKey: ["routine-preferences"],
    queryFn: getRoutinePreferences,
  });

  // Local edit state. Initialised once we have the server value;
  // ``draft === null`` until then so the form doesn't render with
  // placeholder zeros that the user might mistake for "real" values.
  const [draft, setDraft] = useState<RoutinePreferences | null>(null);
  const isDirty =
    draft !== null && prefsQ.data !== undefined &&
    JSON.stringify(draft) !== JSON.stringify(prefsQ.data);

  // Hydrate ``draft`` once the query resolves.
  if (prefsQ.data && draft === null) {
    setDraft(prefsQ.data);
  }

  const saveMutation = useMutation({
    mutationFn: (next: RoutinePreferences) => putRoutinePreferences(next),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ["routine-preferences"] });
      queryClient.invalidateQueries({ queryKey: ["routine-top-to-apply"] });
      setDraft(saved);
    },
  });

  // F398 — /routine#auto-apply (the Auto-apply page's settings link)
  // lands on the auto-apply block, not the top of the operator panel.
  const location = useLocation();
  useEffect(() => {
    if (location.hash === "#auto-apply") {
      document.getElementById("auto-apply")?.scrollIntoView?.({ block: "start" });
    }
  }, [location.hash, prefsQ.data]);

  if (prefsQ.isLoading || draft === null) {
    return (
      <div className="rounded-lg border border-neutral-200 bg-white p-6 shadow-sm">
        <p className="text-sm text-neutral-500">Loading preferences…</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-neutral-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-neutral-100 px-6 py-4">
        <h2 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
          <Sliders className="h-4 w-4" />
          Routine preferences
        </h2>
        <button
          onClick={() => saveMutation.mutate(draft)}
          disabled={!isDirty || saveMutation.isPending}
          className="inline-flex items-center gap-1 rounded-md bg-primary-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-primary-700 disabled:opacity-40"
        >
          <Save className="h-3.5 w-3.5" />
          {saveMutation.isPending ? "Saving…" : isDirty ? "Save changes" : "Saved"}
        </button>
      </div>
      <div className="space-y-5 px-6 py-5">
        {/* F360 — auto-apply. Deliberately first and deliberately blunt:
            this is the only control on the page that sends applications
            to employers with nobody watching. */}
        <div id="auto-apply" className="scroll-mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4">
          <div className="flex items-start justify-between gap-4">
            <div>
              <label className="text-sm font-medium text-amber-900">
                Auto-apply: apply automatically
              </label>
              <p className="text-xs text-amber-800">
                Submits applications on your behalf without asking first.
                Anything the gate won&apos;t answer — legal, EEO or salary
                questions with no saved answer, or a form we can&apos;t read —
                goes to the review queue instead.
              </p>
            </div>
            <input
              type="checkbox"
              checked={draft.auto_apply_enabled}
              onChange={(e) =>
                setDraft({ ...draft, auto_apply_enabled: e.target.checked })
              }
              className="mt-1 h-4 w-4 rounded border-amber-300 text-amber-600 focus:ring-amber-500"
            />
          </div>
          {draft.auto_apply_enabled && (
            <div className="mt-4 grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs font-medium text-amber-900">
                  Max per day
                </label>
                <input
                  type="number"
                  min={0}
                  max={50}
                  value={draft.auto_apply_daily_cap}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      auto_apply_daily_cap: Math.max(0, Math.min(50, Number(e.target.value) || 0)),
                    })
                  }
                  className="mt-1 w-full rounded-md border border-amber-300 px-2 py-1 text-sm"
                />
                <p className="mt-1 text-xs text-amber-700">
                  0 means nothing is sent, even with the toggle on.
                </p>
              </div>
              <div>
                <label className="text-xs font-medium text-amber-900">
                  Minimum match score
                </label>
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={draft.auto_apply_min_score}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      auto_apply_min_score: Math.max(0, Math.min(100, Number(e.target.value) || 0)),
                    })
                  }
                  className="mt-1 w-full rounded-md border border-amber-300 px-2 py-1 text-sm"
                />
                <p className="mt-1 text-xs text-amber-700">
                  Keep this above the score you&apos;d browse at.
                </p>
              </div>
              {/* F372 — standing instruction, subtractive only. */}
              <div className="sm:col-span-2">
                <label className="text-xs font-medium text-amber-900" htmlFor="excluded-title-keywords">
                  Never auto-apply when the title contains
                </label>
                <input
                  id="excluded-title-keywords"
                  type="text"
                  value={(draft.excluded_title_keywords ?? []).join(", ")}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      excluded_title_keywords: e.target.value
                        .split(",")
                        .map((k) => k.trim())
                        .filter(Boolean),
                    })
                  }
                  placeholder="clearance, contract, on-site"
                  className="mt-1 w-full rounded-md border border-amber-300 px-2 py-1 text-sm"
                />
                <p className="mt-1 text-xs text-amber-700">
                  Comma-separated. A score can&apos;t know what you won&apos;t do — this can.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Toggle: only_global_remote */}
        <div className="flex items-start justify-between gap-4">
          <div>
            <label className="text-sm font-medium text-neutral-900">
              Only Global Remote
            </label>
            <p className="text-xs text-neutral-500">
              When on, the picker keeps only ``geography_bucket = global_remote``
              jobs — overrides the geography list below.
            </p>
          </div>
          <input
            type="checkbox"
            checked={draft.only_global_remote}
            onChange={(e) => setDraft({ ...draft, only_global_remote: e.target.checked })}
            className="mt-1 h-4 w-4 rounded border-neutral-300 text-primary-600 focus:ring-primary-500"
          />
        </div>

        {/* Multi-select: allowed_geographies */}
        <div>
          <label className="text-sm font-medium text-neutral-900">
            Allowed geographies
          </label>
          <p className="mb-2 text-xs text-neutral-500">
            Empty = all (subject to "only global remote" above).
          </p>
          <div className="flex flex-wrap gap-2">
            {(["global_remote", "usa_only", "uae_only"] as const).map((bucket) => {
              const active = draft.allowed_geographies.includes(bucket);
              return (
                <button
                  key={bucket}
                  type="button"
                  onClick={() => {
                    const next = active
                      ? draft.allowed_geographies.filter((g) => g !== bucket)
                      : [...draft.allowed_geographies, bucket];
                    setDraft({ ...draft, allowed_geographies: next });
                  }}
                  className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                    active
                      ? "bg-primary-100 text-primary-800 ring-1 ring-primary-300"
                      : "bg-neutral-100 text-neutral-600 hover:bg-neutral-200"
                  }`}
                >
                  {bucket.replace(/_/g, " ")}
                </button>
              );
            })}
          </div>
        </div>

        {/* Slider: min_relevance_score */}
        <div>
          <div className="flex items-center justify-between">
            <label className="text-sm font-medium text-neutral-900">
              Minimum relevance score
            </label>
            <span className="rounded bg-neutral-100 px-2 py-0.5 text-xs font-semibold text-neutral-700">
              {draft.min_relevance_score}
            </span>
          </div>
          <input
            type="range"
            min={0}
            max={100}
            step={5}
            value={draft.min_relevance_score}
            onChange={(e) =>
              setDraft({ ...draft, min_relevance_score: Number(e.target.value) })
            }
            className="mt-2 w-full"
          />
          <p className="text-xs text-neutral-500">
            0 = no floor. The picker drops any job below this relevance score.
          </p>
        </div>

        {/* Slider: min_resume_score */}
        <div>
          <div className="flex items-center justify-between">
            <label className="text-sm font-medium text-neutral-900">
              Minimum resume-fit score (your active resume)
            </label>
            <span className="rounded bg-neutral-100 px-2 py-0.5 text-xs font-semibold text-neutral-700">
              {draft.min_resume_score}
            </span>
          </div>
          <input
            type="range"
            min={0}
            max={100}
            step={5}
            value={draft.min_resume_score}
            onChange={(e) =>
              setDraft({ ...draft, min_resume_score: Number(e.target.value) })
            }
            className="mt-2 w-full"
          />
          <p className="text-xs text-neutral-500">
            0 = no floor. Requires your active resume to have been scored
            against the job — unscored jobs are treated as 0 and dropped
            when this floor is set.
          </p>
        </div>

        {/* Free-text list: extra excluded platforms */}
        <div>
          <label className="text-sm font-medium text-neutral-900">
            Extra excluded platforms
          </label>
          <p className="mb-2 text-xs text-neutral-500">
            Comma-separated platform slugs (lowercase). Extends the always-
            excluded LinkedIn. Example: ``wellfound,jobvite``.
          </p>
          <input
            type="text"
            value={draft.extra_excluded_platforms.join(", ")}
            onChange={(e) =>
              setDraft({
                ...draft,
                extra_excluded_platforms: e.target.value
                  .split(",")
                  .map((s) => s.trim().toLowerCase())
                  .filter(Boolean),
              })
            }
            placeholder="wellfound, jobvite"
            className="w-full rounded-md border border-neutral-300 px-3 py-2 text-sm focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
          />
        </div>

        {/* F259 — Allowed role clusters: chip multi-select.
            Pre-fix this was a comma-separated free-text input that
            invited typos (a misspelled cluster name silently filtered
            the picker to zero rows). Reading the canonical cluster
            list from ``GET /role-config`` lets the user pick from
            the same names the platform actually classifies against.
            Empty selection = "all platform-relevant clusters". */}
        <RoleClusterMultiSelect
          selected={draft.allowed_role_clusters}
          onChange={(next) => setDraft({ ...draft, allowed_role_clusters: next })}
        />

        {saveMutation.isError && (
          <p className="text-xs text-red-600">
            {(saveMutation.error as Error)?.message ?? "Failed to save preferences"}
          </p>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// F257 — Manual queue + excluded list
// ─────────────────────────────────────────────────────────────────────

/**
 * Two-column card showing the operator's manually-queued and
 * manually-excluded jobs. Each row has a Remove button that drops
 * the routine_target row entirely (re-enables auto-picker behaviour
 * for that job).
 */
function ManualQueueCard() {
  const queryClient = useQueryClient();
  const queueQ = useQuery({
    queryKey: ["routine-queue"],
    queryFn: getRoutineQueue,
    staleTime: 30_000,
  });

  const removeMutation = useMutation({
    mutationFn: (jobId: string) => deleteRoutineTarget(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["routine-queue"] });
      queryClient.invalidateQueries({ queryKey: ["routine-top-to-apply"] });
    },
  });

  if (queueQ.isLoading) {
    return (
      <div className="rounded-lg border border-neutral-200 bg-white p-6 shadow-sm">
        <p className="text-sm text-neutral-500">Loading manual queue…</p>
      </div>
    );
  }

  const queued = queueQ.data?.queued ?? [];
  const excluded = queueQ.data?.excluded ?? [];

  if (queued.length === 0 && excluded.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-neutral-200 bg-neutral-50 p-6 text-center text-sm text-neutral-500 shadow-sm">
        Use the routine queue toggle on any job (Job Detail or Jobs list) to
        pin it as a manual target or skip it permanently. Pinned jobs surface
        on the next-targets list above auto-picks.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      {/* Queued list */}
      <div className="rounded-lg border border-emerald-200 bg-white shadow-sm">
        <div className="border-b border-emerald-100 bg-emerald-50 px-5 py-3">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-emerald-800">
            <Bot className="h-4 w-4" />
            Manually queued ({queued.length})
          </h3>
        </div>
        {queued.length === 0 ? (
          <p className="p-5 text-xs text-neutral-500">
            No jobs pinned. Use the queue toggle on any job to add it here.
          </p>
        ) : (
          <ul className="divide-y divide-neutral-100">
            {queued.map((target) => (
              <li
                key={target.id}
                className="flex items-center justify-between gap-3 px-5 py-3"
              >
                <div className="min-w-0 flex-1">
                  <Link
                    to={`/jobs/${target.job_id}`}
                    className="block truncate text-sm font-medium text-neutral-900 hover:text-primary-700"
                  >
                    {target.job_title || "(untitled)"}
                  </Link>
                  <p className="mt-0.5 truncate text-xs text-neutral-500">
                    {target.company_name || "—"} · {target.platform}
                  </p>
                </div>
                <button
                  onClick={() => removeMutation.mutate(target.job_id)}
                  className="rounded p-1 text-emerald-700 hover:bg-emerald-100"
                  aria-label="Remove from queue"
                  title="Remove from queue (auto-picker resumes for this job)"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Excluded list */}
      <div className="rounded-lg border border-red-200 bg-white shadow-sm">
        <div className="border-b border-red-100 bg-red-50 px-5 py-3">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-red-800">
            <Ban className="h-4 w-4" />
            Manually excluded ({excluded.length})
          </h3>
        </div>
        {excluded.length === 0 ? (
          <p className="p-5 text-xs text-neutral-500">
            No jobs excluded. Use the queue toggle on any job to skip it
            permanently.
          </p>
        ) : (
          <ul className="divide-y divide-neutral-100">
            {excluded.map((target) => (
              <li
                key={target.id}
                className="flex items-center justify-between gap-3 px-5 py-3"
              >
                <div className="min-w-0 flex-1">
                  <Link
                    to={`/jobs/${target.job_id}`}
                    className="block truncate text-sm font-medium text-neutral-900 hover:text-primary-700"
                  >
                    {target.job_title || "(untitled)"}
                  </Link>
                  <p className="mt-0.5 truncate text-xs text-neutral-500">
                    {target.company_name || "—"} · {target.platform}
                  </p>
                </div>
                <button
                  onClick={() => removeMutation.mutate(target.job_id)}
                  className="rounded p-1 text-red-700 hover:bg-red-100"
                  aria-label="Remove from exclude list"
                  title="Remove from exclude list (auto-picker may surface this job again)"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// F259 — RoleClusterMultiSelect
// ─────────────────────────────────────────────────────────────────────

/**
 * Chip-style multi-select for ``allowed_role_clusters`` populated from
 * the canonical role-config list (``GET /role-config``). Replaces the
 * old comma-separated text input which invited typos — a misspelled
 * cluster name silently filtered the picker to zero rows.
 *
 * Empty selection = "all platform-relevant clusters" (the picker's
 * default). The cluster catalog is admin-curated; ``getRoleClusters``
 * also returns the ``relevant_clusters`` derived list so we can hint
 * which ones the auto-picker would default to.
 */
function RoleClusterMultiSelect({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const clustersQ = useQuery({
    queryKey: ["role-clusters"],
    queryFn: getRoleClusters,
    staleTime: 5 * 60_000,
  });

  const items = clustersQ.data?.items ?? [];
  const relevantSet = new Set(clustersQ.data?.relevant_clusters ?? []);
  const selectedSet = new Set(selected);

  return (
    <div>
      <label className="text-sm font-medium text-neutral-900">
        Allowed role clusters
      </label>
      <p className="mb-2 text-xs text-neutral-500">
        Pick from the platform's classified clusters. Empty = all
        platform-relevant clusters (the auto-picker's default).
      </p>
      {clustersQ.isLoading ? (
        <p className="text-xs text-neutral-400">Loading clusters…</p>
      ) : items.length === 0 ? (
        <p className="text-xs text-neutral-400">
          No clusters configured yet — ask an admin to seed Role Clusters.
        </p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {items.map((cluster) => {
            const active = selectedSet.has(cluster.name);
            const isRelevantDefault = relevantSet.has(cluster.name);
            return (
              <button
                key={cluster.id}
                type="button"
                onClick={() => {
                  const next = active
                    ? selected.filter((c) => c !== cluster.name)
                    : [...selected, cluster.name];
                  onChange(next);
                }}
                className={`inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                  active
                    ? "bg-primary-100 text-primary-800 ring-1 ring-primary-300"
                    : "bg-neutral-100 text-neutral-600 hover:bg-neutral-200"
                }`}
                title={
                  isRelevantDefault
                    ? `${cluster.display_name || cluster.name} — included by default when no clusters are picked`
                    : `${cluster.display_name || cluster.name} — not relevant by default; pick to include`
                }
              >
                {cluster.display_name || cluster.name}
                {isRelevantDefault && !active && (
                  <span className="text-[10px] font-normal text-neutral-400">★</span>
                )}
              </button>
            );
          })}
        </div>
      )}
      {selectedSet.size > 0 && (
        <button
          type="button"
          onClick={() => onChange([])}
          className="mt-2 text-[11px] font-medium text-neutral-500 hover:text-neutral-700"
        >
          Clear ({selectedSet.size})
        </button>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// F259 — Excluded companies (no-fly list)
// ─────────────────────────────────────────────────────────────────────

/**
 * Per-user "never apply to ANY job at this company" list. Rendered
 * as a card with one row per company + an Undo X. Companies are added
 * here from the Company Detail page's "Exclude from Routine" button —
 * the only entry point so the user is looking at the company they're
 * excluding while they make the decision.
 *
 * Empty state explains how to add a company so the user knows the
 * feature exists even before they've used it.
 */
function ExcludedCompaniesCard() {
  const queryClient = useQueryClient();
  const excludedQ = useQuery({
    queryKey: ["routine-excluded-companies"],
    queryFn: getExcludedCompanies,
    staleTime: 60_000,
  });

  const removeMutation = useMutation({
    mutationFn: (companyId: string) => removeExcludedCompany(companyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["routine-excluded-companies"] });
      queryClient.invalidateQueries({ queryKey: ["routine-top-to-apply"] });
      queryClient.invalidateQueries({ queryKey: ["routine-preferences"] });
    },
  });

  if (excludedQ.isLoading) {
    return (
      <div className="rounded-lg border border-neutral-200 bg-white p-6 shadow-sm">
        <p className="text-sm text-neutral-500">Loading excluded companies…</p>
      </div>
    );
  }

  const excluded = excludedQ.data ?? [];

  return (
    <div className="rounded-lg border border-red-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-red-100 bg-red-50 px-5 py-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-red-800">
          <Ban className="h-4 w-4" />
          Excluded companies ({excluded.length})
        </h3>
        <p className="text-xs text-red-700">
          The routine never auto-picks ANY job from these companies.
        </p>
      </div>
      {excluded.length === 0 ? (
        <p className="p-5 text-sm text-neutral-500">
          No companies excluded. Open any{" "}
          <Link to="/companies" className="text-primary-600 hover:underline">
            company
          </Link>{" "}
          page and click &ldquo;Exclude from Apply Routine&rdquo; to add them
          here. Useful for companies you've already applied to off-platform
          or where you don't want repeat suggestions.
        </p>
      ) : (
        <ul className="divide-y divide-neutral-100">
          {excluded.map((co) => (
            <li
              key={co.id}
              className="flex items-center justify-between gap-3 px-5 py-3"
            >
              <Link
                to={`/companies/${co.id}`}
                className="min-w-0 flex-1 truncate text-sm font-medium text-neutral-900 hover:text-primary-700"
              >
                {co.name || "(unnamed)"}
              </Link>
              <button
                onClick={() => removeMutation.mutate(co.id)}
                disabled={removeMutation.isPending}
                className="rounded p-1 text-red-700 hover:bg-red-100 disabled:opacity-50"
                aria-label={`Remove ${co.name} from exclude list`}
                title="Remove from exclude list"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// F259 — How to run the routine
// ─────────────────────────────────────────────────────────────────────

/**
 * Help card explaining how the operator actually KICKS OFF a run.
 *
 * The routine isn't a backend Celery task — it's an MCP-Chrome browser
 * session driven by Claude that polls these endpoints. There's no
 * "Run now" button on this page because pressing one wouldn't start
 * a browser. The user has to open Claude with MCP-Chrome enabled and
 * say "run the apply routine."
 *
 * Pre-F259 this contract was only documented in the file's top
 * comment. Operators reading the page asked "how do I trigger this?"
 * The card surfaces the answer inline + links to the runs history so
 * they can verify a run started after they triggered it externally.
 */
function HowToRunCard() {
  return (
    <div className="rounded-lg border border-blue-200 bg-blue-50 px-5 py-4 shadow-sm">
      <div className="flex items-start gap-3">
        <Bot className="mt-0.5 h-4 w-4 flex-shrink-0 text-blue-700" />
        <div className="flex-1">
          <h3 className="text-sm font-semibold text-blue-900">
            How to start a run
          </h3>
          <p className="mt-1 text-xs leading-relaxed text-blue-900">
            The routine is a Claude-driven browser session, not a backend job —
            there's no &ldquo;Run now&rdquo; button. To trigger it:
          </p>
          <ol className="mt-2 list-inside list-decimal space-y-1 text-xs text-blue-900">
            <li>Make sure preferences below are saved + answer book is complete + kill-switch is OFF.</li>
            <li>
              Open <span className="font-mono text-[11px]">Claude Desktop</span>{" "}
              with the <span className="font-mono text-[11px]">MCP-Chrome</span>{" "}
              extension enabled.
            </li>
            <li>
              Say: <span className="rounded bg-white/60 px-1.5 py-0.5 font-mono text-[11px]">
                &ldquo;Run the Apply Routine on{" "}
                {typeof window !== "undefined" ? window.location.host : "this platform"}&rdquo;
              </span>
            </li>
            <li>
              Claude will poll{" "}
              <span className="font-mono text-[11px]">/api/v1/routine/top-to-apply</span>,
              load each job, fill the form, and call{" "}
              <span className="font-mono text-[11px]">POST /routine/runs</span>{" "}
              for the run row. Every step shows in &ldquo;Recent runs&rdquo;
              below within ~30 seconds.
            </li>
          </ol>
          <p className="mt-2 text-[11px] text-blue-800/80">
            Hit the kill-switch above to abort an in-flight run within ~60 seconds.
          </p>
        </div>
      </div>
    </div>
  );
}
