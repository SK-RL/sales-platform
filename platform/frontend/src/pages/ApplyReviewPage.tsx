import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronLeft, ChevronRight, ExternalLink, Lock } from "lucide-react";
import {
  answerGap,
  getApplication,
  getApplications,
  getJobQuestions,
  submitApplication,
  updateApplication,
} from "@/lib/api";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import { OutreachPanel } from "@/components/OutreachPanel";
import type { AnswerDraft, ApplyGateResult, BlockingGap, JobQuestionsPreview, PreparedQuestion } from "@/lib/types";

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
    // F398 — while a run is queued or in flight, poll so the page moves
    // on its own. The tester clicked Dry run and saw nothing for
    // minutes; the worker was busy and the page had no way to say so.
    refetchInterval: (q) => {
      const d = q.state.data as { status?: string; platform_response?: { queued?: unknown } } | undefined;
      const running = d?.status === "in_flight" || Boolean(d?.platform_response?.queued);
      return running ? 3000 : false;
    },
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

  // F396 — answer a gap inline. The backend saves it to the Answer Book
  // and drops the field from the stored gap list; the questions preview
  // is refetched so the field shows the new answer.
  const answerGapM = useMutation({
    mutationFn: (opts: { gap: BlockingGap; answer: string }) =>
      answerGap(current!.id, { field_key: opts.gap.field_key, question: opts.gap.label || opts.gap.field_key, answer: opts.answer }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["application", current?.id] });
      queryClient.invalidateQueries({ queryKey: ["job-questions", current?.job_id] });
      queryClient.invalidateQueries({ queryKey: ["apply-review-queue"] });
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

      {/* F398 — a run is queued or filling the form. Say so, and how long. */}
      {(gate.queued || detailQ.data?.status === "in_flight") && (
        <RunningBanner queuedAt={gate.queued?.at} dryRun={gate.queued?.dry_run ?? true} inFlight={detailQ.data?.status === "in_flight"} />
      )}

      {/* F400 — a pass from before the gate's rules changed proves nothing now. */}
      {gate.gate === "stale" && !gate.queued && (
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <p className="font-medium">An earlier dry run passed, but the rules have changed since — run it again before submitting.</p>
        </div>
      )}

      {/* F373 — a dry run that passed: the form was filled and read back,
          nothing was sent. The one bit of good news this page can carry. */}
      {gate.gate === "passed" && !gate.queued && (
        <div className="mt-4 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-900">
          <p className="font-medium">
            Dry run passed — {gate.placed ?? "all"}
            {typeof gate.field_count === "number" ? ` of ${gate.field_count}` : ""} fields
            placed and read back from the real form. Nothing was sent.
          </p>
          {(gate.unplaceable?.length ?? 0) > 0 && (
            <p className="mt-1 text-green-800">
              Skipped (optional): {gate.unplaceable!.join(", ")}
            </p>
          )}
        </div>
      )}
      {gate.gate === "submitted" && (
        <div className="mt-4 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-900">
          <p className="font-medium">
            Submitted{gate.confirmation ? ` — the ATS said "${gate.confirmation}"` : ""}.
          </p>
        </div>
      )}

      {/* Why it stopped. The single most important thing on the page. */}
      {(blocking.length > 0 || (gate.reason && gate.gate !== "passed") || guessedForm || wall) && (
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
            <ul className="mt-3 space-y-3">
              {blocking.map((gap) => (
                <GapRow
                  key={gap.field_key}
                  gap={gap}
                  question={preview?.questions.find((q) => q.field_key === gap.field_key)}
                  draft={gate.drafts?.[gap.field_key]}
                  onSave={(answer) => answerGapM.mutateAsync({ gap, answer })}
                />
              ))}
            </ul>
          )}
          {blocking.length > 0 && (
            <p className="mt-3 text-xs text-amber-700">
              Answers you save here go into your{" "}
              <Link to="/answer-book" className="underline">
                Answer Book
              </Link>{" "}
              and are reused when another form asks the same question.
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
            disabled={submitM.isPending || Boolean(gate.queued) || detailQ.data?.status === "in_flight"}
            className="rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            title={
              blocking.length > 0
                ? "Re-checks the form and the gate. With open questions it will stop again, but confirms the form still reads correctly."
                : "Fills the real form and stops before the submit click. Nothing is sent."
            }
          >
            {submitM.isPending ? "Queuing…" : gate.queued || detailQ.data?.status === "in_flight" ? "Running…" : "Dry run"}
          </button>
          <button
            onClick={() => submitM.mutate({ dryRun: false })}
            disabled={submitM.isPending || !canSubmit || Boolean(gate.queued)}
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
      {/* F404 — reach the people behind the application. */}
      {current?.id && <OutreachPanel appId={current.id} />}
    </div>
  );
}

/** F398 — "we heard you": the run is queued or the browser is filling the form. */
function RunningBanner({ queuedAt, dryRun, inFlight }: { queuedAt?: string; dryRun: boolean; inFlight: boolean }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const secs = queuedAt ? Math.max(0, Math.round((now - new Date(queuedAt).getTime()) / 1000)) : 0;
  const mins = Math.floor(secs / 60);
  const what = dryRun ? "Dry run" : "Submission";
  return (
    <div className="mt-4 flex items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-900" role="status">
      <span className="h-2.5 w-2.5 shrink-0 animate-pulse rounded-full bg-blue-500" />
      <div>
        <p className="font-medium">
          {inFlight ? `${what} in progress — filling the real form now.` : `${what} queued — waiting for a free worker.`}
        </p>
        <p className="text-blue-800">
          {mins > 0 ? `${mins} min ${secs % 60}s` : `${secs}s`} so far. This page updates itself
          {secs > 120 && !inFlight ? "; the worker is busy with a scan, which can take a few minutes" : ""}.
        </p>
      </div>
    </div>
  );
}

/**
 * F396 — one Needs-you question with its answer box. Free-text questions
 * open pre-filled with the draft the worker wrote from the résumé and the
 * job description (with its fact-check note); choice questions show the
 * form's options. Nothing is sent until the user saves, and what they
 * save is exactly what the form gets.
 */
function GapRow({
  gap,
  question,
  draft,
  onSave,
}: {
  gap: BlockingGap;
  question?: PreparedQuestion;
  draft?: AnswerDraft;
  onSave: (answer: string) => Promise<unknown>;
}) {
  const options = question?.options ?? [];
  const isChoice = options.length > 0 && (question?.field_type === "select" || question?.field_type === "multi_select");
  const [value, setValue] = useState(draft?.text ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (draft?.text && !value) setValue(draft.text);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft?.text]);
  const save = async () => {
    if (!value.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(value.trim());
    } catch (e) {
      setError((e as Error)?.message || "Couldn't save");
    } finally {
      setSaving(false);
    }
  };
  const optionLabel = (o: unknown) => (typeof o === "string" ? o : ((o as { label?: string; value?: string }).label ?? (o as { value?: string }).value ?? ""));
  return (
    <li className="rounded-lg border border-amber-200 bg-white p-3 text-sm">
      <p className="font-medium text-gray-900">{gap.label || gap.field_key}</p>
      <p className="mt-0.5 text-xs text-amber-800">{gap.reason}</p>
      {question?.description && <p className="mt-1 text-xs text-gray-500">{question.description}</p>}
      {isChoice ? (
        <select
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="mt-2 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          aria-label={gap.label || gap.field_key}
        >
          <option value="">Pick one…</option>
          {options.map((o) => (
            <option key={optionLabel(o)} value={optionLabel(o)}>{optionLabel(o)}</option>
          ))}
        </select>
      ) : (
        <textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          rows={question?.field_type === "textarea" || (draft?.text?.length ?? 0) > 120 ? 5 : 2}
          placeholder={draft?.note && !draft.text ? draft.note : "Your answer"}
          className="mt-2 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm"
          aria-label={gap.label || gap.field_key}
        />
      )}
      {draft?.text && (
        <p className={`mt-1 text-xs ${draft.unsupported_claims?.length ? "text-red-700" : "text-gray-500"}`}>{draft.note}</p>
      )}
      {!draft && !isChoice && (
        <p className="mt-1 text-xs text-gray-400">A draft from your résumé and the job description appears here when it&apos;s ready.</p>
      )}
      <div className="mt-2 flex items-center gap-3">
        <button
          type="button"
          onClick={save}
          disabled={saving || !value.trim()}
          className="rounded-md bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-700 disabled:opacity-50"
        >
          {saving ? "Saving…" : draft?.text && value === draft.text ? "Use this answer" : "Save answer"}
        </button>
        {error && <span className="text-xs text-red-600">{error}</span>}
      </div>
    </li>
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
    if (q.confidence === "low") {
      const g = (q.guess || "").trim();
      return `Guessed${g ? ` "${g.length > 60 ? g.slice(0, 60) + "…" : g}"` : ""} — not sent. Save the real answer in your Answer Book and it will be used from then on.`;
    }
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
