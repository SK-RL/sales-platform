import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronLeft, ChevronRight, ExternalLink, Lock } from "lucide-react";
import {
  getApplication,
  getApplications,
  getJobQuestions,
  submitApplication,
  updateApplication,
} from "@/lib/api";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import type { ApplyGateResult, JobQuestionsPreview, PreparedQuestion } from "@/lib/types";

/**
 * The "Needs you" review queue.
 *
 * One job at a time, Prev/Next across the queue, and two actions. That's
 * the whole page — deliberately. The value is in reading what we filled
 * and why something is blocked, so anything else on screen is noise.
 *
 * `needs_user` does not mean "broken". It means the apply gate
 * understood the form well enough to know it shouldn't answer something
 * unattended — a legal or protected-class question with no saved answer,
 * a form we couldn't read, or a page that wants a human. Retrying
 * changes nothing until a person acts, which is why these are a queue
 * and not a retry list.
 */
export function ApplyReviewPage() {
  const queryClient = useQueryClient();
  const [index, setIndex] = useState(0);
  // ?app=<id> reviews one specific application — the entry point from
  // the Applications table. Without it this is the needs_user queue.
  // Both modes render identically; only the source of the list differs.
  const [params] = useSearchParams();
  const focusId = params.get("app");

  const queueQ = useQuery({
    queryKey: ["apply-review-queue", focusId],
    queryFn: async () => {
      if (focusId) {
        const one = await getApplication(focusId);
        // A bad id must read as "not found", not as a row with an empty
        // title and live buttons wired to `undefined`.
        if (!one?.id) return { items: [] };
        return {
          items: [
            {
              id: one.id,
              job_id: one.job?.id,
              job_title: one.job?.title,
              company_name: one.job?.company_name,
              platform: one.job?.platform,
              job_url: one.job?.url,
            },
          ],
        };
      }
      return getApplications({ status: "needs_user", page: 1, page_size: 100 });
    },
  });

  const queue = queueQ.data?.items ?? [];
  const total = queue.length;
  const current = queue[Math.min(index, Math.max(total - 1, 0))];

  // Keep the cursor in range when the queue shrinks under us (an
  // application resolved in another tab, or we just cleared one).
  useEffect(() => {
    if (index > 0 && index >= total) setIndex(Math.max(total - 1, 0));
  }, [total, index]);

  const detailQ = useQuery({
    queryKey: ["application", current?.id],
    queryFn: () => getApplication(current!.id),
    enabled: Boolean(current?.id),
  });

  const questionsQ = useQuery({
    queryKey: ["job-questions", current?.job_id],
    queryFn: () => getJobQuestions(current!.job_id),
    enabled: Boolean(current?.job_id),
    retry: false,
  });

  const submitM = useMutation({
    mutationFn: (opts: { dryRun: boolean }) =>
      submitApplication(current!.id, { dryRun: opts.dryRun }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["apply-review-queue"] });
      queryClient.invalidateQueries({ queryKey: ["application", current?.id] });
    },
  });

  const markAppliedM = useMutation({
    mutationFn: () => updateApplication(current!.id, { status: "applied" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["apply-review-queue"] });
    },
  });

  const gate: ApplyGateResult = detailQ.data?.platform_response ?? {};
  const preview: JobQuestionsPreview | undefined = questionsQ.data;

  // Prefer the gate's own record — it's what the worker actually decided
  // at submit time. The live preview is a second opinion, useful when
  // the gate never ran (a manually prepared application).
  const blocking = gate.blocking?.length ? gate.blocking : preview?.blocking ?? [];
  const guessedForm = preview?.schema?.extraction_mode === "fallback";
  // A wall is the more useful explanation than "couldn't read the form"
  // when both are true (SmartRecruiters): it says what to do next.
  const wall = preview?.schema?.wall ?? null;

  // Until the gate record and the form have both loaded we don't know
  // whether anything blocks — so Submit must not be offered yet. Without
  // this the button is briefly live on first paint and a fast click asks
  // the API to do something the gate will refuse. The backend re-runs
  // every check so nothing wrong reaches an employer, but offering an
  // action we know may be invalid is its own bug.
  // Derived from DATA, not loading flags: a TanStack query that is
  // disabled (ours are, until `current` exists) reports isLoading=false
  // while having no data at all, so a loading-flag guard silently never
  // engages — which is exactly how the button ended up live on first
  // paint.
  const checksReady = Boolean(detailQ.data) && Boolean(preview);
  // A wall (F368) is a platform-level blocker: the worker would refuse,
  // so the button must not offer it.
  const canSubmit = checksReady && blocking.length === 0 && !guessedForm && !wall;

  const go = (delta: number) => {
    setIndex((i) => Math.min(Math.max(i + delta, 0), Math.max(total - 1, 0)));
  };

  // Arrow keys page the queue, but only when focus isn't in a field —
  // otherwise they'd just move the text cursor. (Tsenta advertises arrow
  // navigation and gets this wrong; focus lands in an input immediately
  // and the shortcut silently does nothing.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement;
      const typing =
        el instanceof HTMLInputElement ||
        el instanceof HTMLTextAreaElement ||
        el instanceof HTMLSelectElement ||
        (el as HTMLElement | null)?.isContentEditable;
      if (typing) return;
      if (e.key === "ArrowRight") go(1);
      if (e.key === "ArrowLeft") go(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [total]);

  if (queueQ.isError) return <BackendErrorBanner queries={[queueQ]} />;

  // Render nothing actionable until we know what we're reviewing.
  // Previously the page fell through to the full shell while the queue
  // was still loading — title "—", and a live Dry run / I applied
  // manually button that called `current.id` on undefined.
  if (queueQ.isLoading) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16 text-center text-sm text-gray-500">
        Loading your review queue…
      </div>
    );
  }

  // Empty is checked BEFORE the no-current guard below: an empty queue
  // legitimately has no current application, and testing for that first
  // left the page stuck on "Loading…" forever.
  if (total === 0) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16 text-center">
        <p className="text-lg font-medium text-gray-900">
          {focusId ? "Application not found" : "Nothing needs you"}
        </p>
        <p className="mt-2 text-sm text-gray-500">
          Applications appear here when the apply gate won't answer something on
          your behalf.
        </p>
        <Link
          to="/applications"
          className="mt-6 inline-flex items-center gap-1.5 text-sm text-gray-600 hover:text-gray-900"
        >
          <ArrowLeft className="h-4 w-4" /> All applications
        </Link>
      </div>
    );
  }

  if (!current) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16 text-center text-sm text-gray-500">
        Loading your review queue…
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-6">
      {/* Header: what this is, and where you are in the queue. */}
      <div className="flex items-start justify-between gap-4 border-b border-gray-200 pb-4">
        <div className="min-w-0">
          <h1 className="truncate text-lg font-semibold text-gray-900">
            {current?.job_title ?? "—"}
          </h1>
          <p className="mt-0.5 truncate text-sm text-gray-500">
            {current?.company_name}
            {current?.platform ? ` · ${current.platform}` : ""}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1 text-sm text-gray-500">
          <button
            onClick={() => go(-1)}
            disabled={index === 0}
            className="rounded p-1 hover:bg-gray-100 disabled:opacity-30"
            aria-label="Previous application"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <span className="tabular-nums">
            {Math.min(index + 1, total)} of {total}
          </span>
          <button
            onClick={() => go(1)}
            disabled={index >= total - 1}
            className="rounded p-1 hover:bg-gray-100 disabled:opacity-30"
            aria-label="Next application"
          >
            <ChevronRight className="h-4 w-4" />
          </button>
          {current?.job_url && (
            <a
              href={current.job_url}
              target="_blank"
              rel="noreferrer"
              className="ml-2 inline-flex items-center gap-1 text-gray-500 hover:text-gray-900"
            >
              <ExternalLink className="h-3.5 w-3.5" /> Posting
            </a>
          )}
        </div>
      </div>

      {/* Why it stopped. The single most important thing on the page. */}
      {(blocking.length > 0 || gate.reason || guessedForm || wall) && (
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4">
          <p className="text-sm font-medium text-amber-900">
            {gate.reason ?? wall?.reason ?? "This application needs your input before it can be sent."}
          </p>
          {wall && (
            <p className="mt-1.5 text-sm text-amber-800">
              {gate.reason && gate.reason !== wall.reason ? `${wall.reason} ` : ""}
              Apply on the posting page, then mark it{" "}
              <span className="font-medium">I applied manually</span> here — the
              answers below are ready to copy.
            </p>
          )}
          {guessedForm && !wall && (
            <p className="mt-1.5 text-sm text-amber-800">
              We couldn't read this posting's real application form, so we won't
              submit it automatically — a form we guessed can't be a form we
              filled correctly.
            </p>
          )}
          {blocking.length > 0 && (
            <ul className="mt-3 space-y-2">
              {blocking.map((gap) => (
                <li key={gap.field_key} className="text-sm">
                  <span className="font-medium text-amber-900">
                    {gap.label || gap.field_key}
                  </span>
                  <span className="block text-amber-800">{gap.reason}</span>
                </li>
              ))}
            </ul>
          )}
          {blocking.some((g) => g.reason.includes("legal or protected-class")) && (
            <p className="mt-3 text-sm text-amber-800">
              Save an answer in your{" "}
              <Link to="/answer-book" className="underline">
                Answer Book
              </Link>{" "}
              and this will clear.
            </p>
          )}
        </div>
      )}

      {/* What we filled. */}
      <div className="mt-6">
        {questionsQ.isLoading && (
          <p className="py-8 text-center text-sm text-gray-500">Loading the form…</p>
        )}
        {questionsQ.isError && (
          <p className="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-600">
            Couldn't load this posting's questions — it may have been removed, or
            the ATS is temporarily unavailable.
          </p>
        )}
        {preview?.questions?.map((q) => (
          <FieldRow key={q.field_key} q={q} />
        ))}
      </div>

      {/* Two actions, and an escape hatch. */}
      <div className="sticky bottom-0 mt-8 flex items-center justify-between gap-3 border-t border-gray-200 bg-white py-4">
        <button
          onClick={() => markAppliedM.mutate()}
          disabled={markAppliedM.isPending}
          className="text-sm text-gray-600 hover:text-gray-900 disabled:opacity-50"
        >
          I applied manually
        </button>
        <div className="flex items-center gap-2">
          <button
            onClick={() => submitM.mutate({ dryRun: true })}
            disabled={submitM.isPending}
            className="rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            title="Fills the real form and stops before the submit click. Nothing is sent."
          >
            Dry run
          </button>
          <button
            onClick={() => submitM.mutate({ dryRun: false })}
            disabled={submitM.isPending || !canSubmit}
            className="rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-40"
            title={
              !checksReady
                ? "Checking this application…"
                : blocking.length > 0
                  ? "Resolve the blocked fields first"
                  : "Sends this application to the employer"
            }
          >
            Submit application
          </button>
        </div>
      </div>
      {submitM.isError && (
        <p className="pb-4 text-right text-sm text-red-600">
          {(submitM.error as Error)?.message}
        </p>
      )}
    </div>
  );
}

/**
 * One field. Locked fields are greyed with a padlock and a caption
 * saying where the value came from — so "why can't I edit this here?"
 * is answered on the spot rather than being a mystery.
 */
function FieldRow({ q }: { q: PreparedQuestion }) {
  const locked = q.never_infer || q.match_source === "manual_required";
  const missing = q.needs_user;

  const caption = useMemo(() => {
    if (q.never_infer) {
      return "Legal or protected-class question — only ever answered from a saved answer, never inferred.";
    }
    if (q.match_source === "unmatched") return "No saved answer matched this field.";
    if (q.confidence === "low") return "Loose match — worth checking.";
    if (q.question_key) return `From your Answer Book · ${q.question_key}`;
    return "";
  }, [q]);

  return (
    <div className="border-b border-gray-100 py-4 last:border-0">
      <div className="flex items-baseline gap-1.5">
        {locked && <Lock className="h-3 w-3 shrink-0 text-gray-400" />}
        <span className="text-xs font-medium uppercase tracking-wide text-gray-500">
          {q.label || q.field_key}
        </span>
        {q.required && <span className="text-xs text-red-500">*</span>}
      </div>
      <div
        className={`mt-1.5 rounded-lg border px-3 py-2 text-sm ${
          missing
            ? "border-amber-300 bg-amber-50 text-amber-800"
            : locked
              ? "border-gray-200 bg-gray-50 text-gray-600"
              : "border-gray-200 bg-white text-gray-900"
        }`}
      >
        {q.answer || (missing ? "Needs your answer" : "—")}
      </div>
      {caption && <p className="mt-1 text-xs text-gray-400">{caption}</p>}
    </div>
  );
}
