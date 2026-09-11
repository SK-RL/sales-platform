import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Bot, ChevronRight, Settings2 } from "lucide-react";
import { getApplications, getRoutinePreferences, putRoutinePreferences } from "@/lib/api";
import { AddJobLink } from "@/components/AddJobLink";
import type { Application } from "@/lib/types";

/**
 * F384 — the auto-apply home. Modelled on the one thing Tsenta gets
 * right: a screen that shows only what changes what you do next.
 *
 *   1. paste a link                       → it's prepared and reviewed
 *   2. "Needs you"                        → the queue, with WHY, one click to fix
 *   3. today: on/off and the cap          → the two numbers that matter
 *   4. what happened recently             → one line per application
 *
 * No table, no filters, no columns. The full table still lives at
 * /applications for people who want it.
 */

const TERMINAL_NOTE: Record<string, (a: Application) => string> = {
  submitted: () => "Submitted — the ATS confirmed it.",
  applied: () => "Applied.",
  in_flight: () => "Filling the form now…",
  failed: (a) => a.gate_error ?? a.gate_reason ?? "Failed — open to see why.",
  needs_user: (a) => a.gate_reason ?? "Needs your answer.",
  prepared: (a) => (a.gate === "passed" ? "Dry run passed — ready to submit." : "Prepared, not sent."),
};

function isToday(iso: string | null | undefined): boolean {
  if (!iso) return false;
  const d = new Date(iso);
  const now = new Date();
  return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
}

export function AutoApplyPage() {
  const qc = useQueryClient();
  const needsQ = useQuery({
    queryKey: ["auto-apply", "needs_user"],
    queryFn: () => getApplications({ status: "needs_user", page: 1, page_size: 50 }),
  });
  const recentQ = useQuery({
    queryKey: ["auto-apply", "recent"],
    queryFn: () => getApplications({ page: 1, page_size: 100 }),
    refetchInterval: 15_000,
  });
  const prefsQ = useQuery({ queryKey: ["routine-preferences"], queryFn: getRoutinePreferences });

  const toggleM = useMutation({
    mutationFn: async (enabled: boolean) => {
      if (!prefsQ.data) return;
      // Turning it on with no cap would do nothing; give it a sane one.
      const cap = enabled && !prefsQ.data.auto_apply_daily_cap ? 5 : prefsQ.data.auto_apply_daily_cap;
      return putRoutinePreferences({ ...prefsQ.data, auto_apply_enabled: enabled, auto_apply_daily_cap: cap });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["routine-preferences"] }),
  });

  const needs = needsQ.data?.items ?? [];
  const recent = useMemo(
    () => (recentQ.data?.items ?? []).filter((a) => a.status !== "needs_user").slice(0, 12),
    [recentQ.data]
  );
  const sentToday = (recentQ.data?.items ?? []).filter((a) => a.status === "submitted" && isToday(a.submitted_at)).length;
  const prefs = prefsQ.data;
  const on = Boolean(prefs?.auto_apply_enabled && (prefs?.auto_apply_daily_cap ?? 0) > 0);

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold text-gray-900">
            <Bot className="h-6 w-6" /> Auto-apply
          </h1>
          <p className="mt-1 text-sm text-gray-500">
            We fill and submit the real application form. Anything we shouldn&apos;t answer for you lands in
            &ldquo;Needs you&rdquo;.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-4">
          <Link to="/docs#auto-apply" className="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-900">
            <BookOpen className="h-4 w-4" /> Which sites are automatic?
          </Link>
          <Link to="/routine" className="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-900">
            <Settings2 className="h-4 w-4" /> Settings
          </Link>
        </div>
      </div>

      {/* 1. Paste a link */}
      <section>
        <h2 className="mb-2 text-sm font-medium text-gray-700">Apply to a specific posting</h2>
        <AddJobLink />
      </section>

      {/* 3. Today */}
      <section className="flex items-center justify-between rounded-xl border border-gray-200 bg-white px-4 py-3">
        <div className="text-sm">
          <span className="font-medium text-gray-900">{on ? "Auto-apply is on" : "Auto-apply is off"}</span>
          {prefs && (
            <span className="ml-2 text-gray-500">
              {sentToday} of {prefs.auto_apply_daily_cap || 0} sent today · min score {prefs.auto_apply_min_score}
            </span>
          )}
        </div>
        <button
          onClick={() => toggleM.mutate(!on)}
          disabled={!prefs || toggleM.isPending}
          className={`rounded-lg px-3 py-1.5 text-sm font-medium ${
            on ? "border border-gray-300 text-gray-700 hover:bg-gray-50" : "bg-gray-900 text-white hover:bg-gray-700"
          } disabled:opacity-50`}
        >
          {!prefs ? "…" : on ? "Turn off" : "Turn on"}
        </button>
      </section>

      {/* 2. Needs you */}
      <section>
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-sm font-medium text-gray-700">
            Needs you{needs.length > 0 ? ` · ${needs.length}` : ""}
          </h2>
          {needs.length > 0 && (
            <Link to="/applications/review" className="text-xs text-gray-500 hover:text-gray-900">
              Review all →
            </Link>
          )}
        </div>
        {needsQ.isLoading ? (
          <p className="text-sm text-gray-500">Loading…</p>
        ) : needs.length === 0 ? (
          <p className="rounded-xl border border-dashed border-gray-200 px-4 py-6 text-center text-sm text-gray-500">
            Nothing needs you right now.
          </p>
        ) : (
          <ul className="divide-y divide-gray-100 rounded-xl border border-gray-200 bg-white">
            {needs.map((a) => (
              <li key={a.id}>
                <Link
                  to={`/applications/review?app=${a.id}`}
                  className="flex items-center gap-3 px-4 py-3 hover:bg-gray-50"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-gray-900">
                      {a.job_title} <span className="font-normal text-gray-500">· {a.company_name}</span>
                    </p>
                    <p className="truncate text-xs text-amber-800">{a.gate_reason ?? "Needs your answer."}</p>
                  </div>
                  <ChevronRight className="h-4 w-4 shrink-0 text-gray-400" />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* 4. Recent */}
      <section>
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-sm font-medium text-gray-700">Recent</h2>
          <Link to="/applications" className="text-xs text-gray-500 hover:text-gray-900">
            All applications →
          </Link>
        </div>
        {recent.length === 0 ? (
          <p className="text-sm text-gray-500">Nothing yet — paste a link above, or turn auto-apply on.</p>
        ) : (
          <ul className="divide-y divide-gray-100 rounded-xl border border-gray-200 bg-white">
            {recent.map((a) => (
              <li key={a.id}>
                <Link to={`/applications/review?app=${a.id}`} className="flex items-center gap-3 px-4 py-3 hover:bg-gray-50">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-gray-900">
                      {a.job_title} <span className="font-normal text-gray-500">· {a.company_name}</span>
                    </p>
                    <p className="truncate text-xs text-gray-500">{(TERMINAL_NOTE[a.status] ?? (() => a.status))(a)}</p>
                  </div>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      a.status === "submitted" || a.status === "applied"
                        ? "bg-green-100 text-green-800"
                        : a.status === "failed"
                          ? "bg-red-100 text-red-800"
                          : a.status === "in_flight"
                            ? "bg-blue-100 text-blue-800"
                            : "bg-gray-100 text-gray-700"
                    }`}
                  >
                    {a.status === "in_flight" ? "in progress" : a.status.replace("_", " ")}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
