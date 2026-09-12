/**
 * F406 — "Show your work": a small project to send to the people hiring.
 *
 * Stage 1 only, on purpose: research → three ideas with cited evidence
 * → the user picks. No project code is generated until the idea
 * quality has been judged across many postings. Every quote is checked
 * against the research; an idea is called "company-specific" only when
 * a checked quote comes from a company source, not the model's say-so.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ExternalLink, RefreshCw, Search } from "lucide-react";
import { chooseProjectIdea, draftProjectIdeas, draftProjectIdeasLibrary, getProjectIdeas, getProjectIdeasLibrary } from "@/lib/api";
import type { GenericProjectIdea, ProjectIdea } from "@/lib/types";

const SPEC: Record<string, { text: string; cls: string }> = {
  company: { text: "Company-specific", cls: "bg-green-100 text-green-800" },
  role: { text: "Role-specific", cls: "bg-blue-100 text-blue-800" },
  ungrounded: { text: "Evidence not verified", cls: "bg-red-100 text-red-700" },
};

const ANGLE: Record<string, string> = {
  pain: "Addresses a pain they show",
  initiative: "Builds on something they are doing",
  stack_match: "Matches their stack",
  generic: "General",
};

export function ProjectIdeasPanel({ appId }: { appId: string }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["project-ideas", appId],
    queryFn: () => getProjectIdeas(appId),
    refetchInterval: (query) => (query.state.data?.running ? 3000 : false),
  });
  const lib = useQuery({
    queryKey: ["project-ideas-library"],
    queryFn: getProjectIdeasLibrary,
    refetchInterval: (query) => (query.state.data?.running ? 4000 : false),
  });
  const draftM = useMutation({ mutationFn: () => draftProjectIdeas(appId), onSuccess: () => qc.invalidateQueries({ queryKey: ["project-ideas", appId] }) });
  const libM = useMutation({ mutationFn: draftProjectIdeasLibrary, onSuccess: () => qc.invalidateQueries({ queryKey: ["project-ideas-library"] }) });
  const chooseM = useMutation({
    mutationFn: (ideaId: string | null) => chooseProjectIdea(appId, ideaId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["project-ideas", appId] }),
  });

  const d = q.data;
  const running = Boolean(d?.running);
  const ideas = d?.ideas ?? [];
  const chosen = d?.chosen?.idea_id ?? null;
  const found = d?.research?.found;
  const nothingSpecific = ideas.length > 0 && !ideas.some((i) => i.specificity === "company");
  const [showLib, setShowLib] = useState(false);

  if (q.isLoading) return null;

  return (
    <section className="mt-6 rounded-xl border border-gray-200 bg-white" data-testid="project-ideas-panel">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-100 px-5 py-3">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">Show your work</h2>
          <p className="text-xs text-gray-500">
            A small project to send the hiring manager or DevOps lead, built on what we can find about this company and role.{" "}
            <Link to="/docs#show-your-work" className="underline hover:text-gray-800">How this works</Link>
          </p>
        </div>
        <button
          type="button"
          onClick={() => draftM.mutate()}
          disabled={running || draftM.isPending}
          className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
          {running ? "Researching…" : ideas.length ? "Research again" : "Find a project idea"}
        </button>
      </div>

      {d?.error && <p className="border-b border-red-100 bg-red-50 px-5 py-2 text-xs text-red-700">{d.error}</p>}

      {found && (
        <div className="flex flex-wrap items-center gap-1.5 border-b border-gray-100 px-5 py-2 text-[11px]">
          <span className="text-gray-500">Looked at:</span>
          {(d?.research?.sources ?? []).map((s) => (
            <span key={s.id} className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-700" title={`${s.chars} characters`}>
              {s.url ? <a href={s.url} target="_blank" rel="noreferrer" className="hover:underline">{s.title}</a> : s.title}
            </span>
          ))}
          {!found.github && <span className="rounded border border-dashed border-gray-300 px-1.5 py-0.5 text-gray-400">no public GitHub org</span>}
          {!found.status && <span className="rounded border border-dashed border-gray-300 px-1.5 py-0.5 text-gray-400">no status page</span>}
          {!found.blog && <span className="rounded border border-dashed border-gray-300 px-1.5 py-0.5 text-gray-400">no engineering blog found</span>}
        </div>
      )}
      {d?.research_verdict && <p className="px-5 pt-3 text-xs text-gray-600">{d.research_verdict}</p>}

      {ideas.length === 0 && !running && (
        <p className="px-5 py-4 text-sm text-gray-500">Nothing researched yet. Press “Find a project idea”: we read the posting, the company profile, their other openings, their public GitHub, status page and blog, then propose three ideas with the evidence for each.</p>
      )}

      {ideas.length > 0 && (
        <ul className="divide-y divide-gray-100">
          {ideas.map((i) => (
            <IdeaCard key={i.id} idea={i} chosen={chosen === i.id} onChoose={() => chooseM.mutate(chosen === i.id ? null : i.id)} />
          ))}
        </ul>
      )}

      {(nothingSpecific || showLib || ideas.length === 0) && (
        <div className="border-t border-gray-100 px-5 py-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-semibold text-gray-900">Ideas that fit most openings</h3>
              <p className="text-xs text-gray-500">
                {nothingSpecific ? "Nothing company-specific turned up, so these are the fallback. " : ""}
                Coverage is the share of the {lib.data?.jobs_in_corpus ?? "…"} infra and security postings we track whose description asks for at least two of the idea's skills.
              </p>
            </div>
            <button
              type="button"
              onClick={() => libM.mutate()}
              disabled={Boolean(lib.data?.running) || libM.isPending}
              className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${lib.data?.running ? "animate-spin" : ""}`} />
              {lib.data?.running ? "Computing…" : lib.data?.ideas?.length ? "Recompute" : "Build the list"}
            </button>
          </div>
          {lib.data?.error && <p className="mt-2 text-xs text-red-700">{lib.data.error}</p>}
          {(lib.data?.ideas ?? []).length > 0 && (
            <ul className="mt-3 space-y-2">
              {lib.data!.ideas.map((g) => (
                <GenericCard key={g.id} idea={g} chosen={chosen === g.id} onChoose={() => chooseM.mutate(chosen === g.id ? null : g.id)} />
              ))}
            </ul>
          )}
        </div>
      )}
      {!nothingSpecific && ideas.length > 0 && !showLib && (
        <p className="px-5 pb-3 text-xs">
          <button type="button" className="text-gray-500 underline hover:text-gray-800" onClick={() => setShowLib(true)}>Also show ideas that fit most openings</button>
        </p>
      )}
      <p className="border-t border-gray-100 px-5 py-2 text-[11px] text-gray-400">
        Picking an idea records it. Building the project comes in the next stage, once the ideas have been judged good across many postings.
      </p>
    </section>
  );
}

function IdeaCard({ idea, chosen, onChoose }: { idea: ProjectIdea; chosen: boolean; onChoose: () => void }) {
  const [open, setOpen] = useState(false);
  const spec = SPEC[idea.specificity] ?? SPEC.ungrounded;
  return (
    <li className={`px-5 py-4 ${chosen ? "bg-green-50/40" : ""}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <h3 className="text-sm font-semibold text-gray-900">{idea.title}</h3>
            <span className={`rounded px-1.5 py-0.5 text-[11px] ${spec.cls}`}>{spec.text}</span>
            <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-600">{ANGLE[idea.angle] ?? idea.angle}</span>
            {idea.effort_hours ? <span className="text-[11px] text-gray-500">~{idea.effort_hours} h</span> : null}
          </div>
          <p className="mt-1 text-sm text-gray-700">{idea.summary}</p>
        </div>
        <button
          type="button"
          onClick={onChoose}
          className={`inline-flex shrink-0 items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-medium ${chosen ? "bg-green-700 text-white" : "border border-gray-300 text-gray-700 hover:bg-gray-50"}`}
        >
          {chosen ? <><Check className="h-3.5 w-3.5" /> Picked</> : "Pick this idea"}
        </button>
      </div>
      {idea.evidence.length > 0 && (
        <ul className="mt-2 space-y-1">
          {idea.evidence.map((e, n) => (
            <li key={n} className={`text-xs ${e.grounded ? "text-gray-600" : "text-red-700"}`}>
              <span className="mr-1 rounded bg-gray-100 px-1 py-0.5 text-[10px] uppercase tracking-wide text-gray-500">{e.source}</span>
              “{e.quote}”{" "}
              {e.grounded ? (
                e.url ? <a href={e.url} target="_blank" rel="noreferrer" className="text-gray-400 hover:underline"><ExternalLink className="inline h-3 w-3" /></a> : null
              ) : (
                <span>(not found in the source, treat as unverified)</span>
              )}
            </li>
          ))}
        </ul>
      )}
      <button type="button" onClick={() => setOpen(!open)} className="mt-2 text-xs text-gray-500 underline hover:text-gray-800">
        {open ? "Hide details" : "What would be built, and for whom"}
      </button>
      {open && (
        <div className="mt-2 grid gap-3 text-xs text-gray-700 md:grid-cols-2">
          <div>
            <p className="font-medium text-gray-900">Build</p>
            <ol className="ml-4 list-decimal space-y-0.5">{idea.build.map((b, n) => <li key={n}>{b}</li>)}</ol>
            <p className="mt-2"><span className="font-medium text-gray-900">They open:</span> {idea.deliverable}</p>
          </div>
          <div>
            <p><span className="font-medium text-gray-900">Send to:</span> {idea.recipient_role}{idea.why_them ? ` — ${idea.why_them}` : ""}</p>
            <p className="mt-1"><span className="font-medium text-gray-900">Shows:</span> {idea.skills_shown.join(", ")}</p>
            {idea.risks && <p className="mt-1"><span className="font-medium text-gray-900">Risk:</span> {idea.risks}</p>}
          </div>
        </div>
      )}
    </li>
  );
}

function GenericCard({ idea, chosen, onChoose }: { idea: GenericProjectIdea; chosen: boolean; onChoose: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <li className={`rounded-lg border border-gray-200 px-4 py-3 ${chosen ? "bg-green-50/40" : ""}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <h4 className="text-sm font-semibold text-gray-900">{idea.title}</h4>
            <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[11px] text-blue-800">fits {idea.coverage_pct}% of postings</span>
            {idea.effort_hours ? <span className="text-[11px] text-gray-500">~{idea.effort_hours} h</span> : null}
          </div>
          <p className="mt-1 text-sm text-gray-700">{idea.summary}</p>
          <p className="mt-1 text-[11px] text-gray-500">{idea.term_labels.join(" · ")}</p>
        </div>
        <button
          type="button"
          onClick={onChoose}
          className={`inline-flex shrink-0 items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-medium ${chosen ? "bg-green-700 text-white" : "border border-gray-300 text-gray-700 hover:bg-gray-50"}`}
        >
          {chosen ? <><Check className="h-3.5 w-3.5" /> Picked</> : "Pick this idea"}
        </button>
      </div>
      <button type="button" onClick={() => setOpen(!open)} className="mt-2 text-xs text-gray-500 underline hover:text-gray-800">
        {open ? "Hide details" : "What would be built"}
      </button>
      {open && (
        <div className="mt-2 text-xs text-gray-700">
          <ol className="ml-4 list-decimal space-y-0.5">{idea.build.map((b, n) => <li key={n}>{b}</li>)}</ol>
          <p className="mt-2"><span className="font-medium text-gray-900">They open:</span> {idea.deliverable}</p>
          {idea.recipient_role && <p className="mt-1"><span className="font-medium text-gray-900">Send to:</span> {idea.recipient_role}</p>}
        </div>
      )}
    </li>
  );
}
