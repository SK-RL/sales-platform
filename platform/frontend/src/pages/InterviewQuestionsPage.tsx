import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpenCheck,
  Building2,
  CalendarDays,
  ChevronDown,
  ChevronRight,
  Pencil,
  Plus,
  Search,
  Trash2,
  User as UserIcon,
  X,
} from "lucide-react";

import { Card } from "@/components/Card";
import { Button } from "@/components/Button";
import { Badge } from "@/components/Badge";
import { Pagination } from "@/components/Pagination";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import { useAuth } from "@/lib/auth";
import {
  createInterviewQuestionSet,
  deleteInterviewQuestionSet,
  listInterviewQuestionSets,
  updateInterviewQuestionSet,
} from "@/lib/api";
import type {
  InterviewQuestionSet,
  InterviewQuestionSetPayload,
} from "@/lib/types";

/**
 * Interview Question Repository — ticket 8ef0e9c2.
 *
 * Shared institutional memory: after a candidate finishes an
 * interview round with a client, the questions they were asked are
 * recorded here so future candidates interviewing with the same
 * client can prepare from real data.
 *
 * Access model mirrors the backend: everyone can read + add,
 * author-or-admin can edit/delete.
 */
export function InterviewQuestionsPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin" || user?.role === "super_admin";
  const queryClient = useQueryClient();

  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<InterviewQuestionSet | null>(null);

  const listQ = useQuery({
    queryKey: ["interview-questions", search, page],
    queryFn: () => listInterviewQuestionSets({ q: search || undefined, page, page_size: 25 }),
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["interview-questions"] });

  const deleteMutation = useMutation({
    mutationFn: deleteInterviewQuestionSet,
    onSuccess: invalidate,
  });

  const items = listQ.data?.items ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
            <BookOpenCheck className="h-6 w-6 text-primary-600" />
            Interview Questions
          </h1>
          <p className="mt-1 text-sm text-gray-500">
            Real questions from past interview rounds, shared by candidates.
            Search by company, role, or question text before your next round.
          </p>
        </div>
        <Button onClick={() => { setEditing(null); setShowForm(true); }}>
          <Plus className="h-4 w-4 mr-1" />
          Add Debrief
        </Button>
      </div>

      <BackendErrorBanner queries={[listQ]} />

      {/* Search */}
      <Card>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setSearch(searchInput.trim());
            setPage(1);
          }}
        >
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
            <input
              type="text"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search company, role, round, or question text…"
              className="input w-full pl-9 text-sm"
            />
          </div>
          <Button type="submit" variant="secondary">Search</Button>
          {search && (
            <Button
              type="button"
              variant="ghost"
              onClick={() => { setSearch(""); setSearchInput(""); setPage(1); }}
            >
              Clear
            </Button>
          )}
        </form>
      </Card>

      {/* List */}
      {listQ.isLoading ? (
        <div className="flex items-center justify-center py-16">
          <div className="spinner h-6 w-6" />
        </div>
      ) : items.length === 0 ? (
        <Card>
          <div className="py-12 text-center">
            <BookOpenCheck className="mx-auto h-10 w-10 text-gray-300" />
            <p className="mt-3 text-sm font-medium text-gray-900">
              {search ? "No debriefs match your search" : "No interview debriefs yet"}
            </p>
            <p className="mt-1 text-sm text-gray-500">
              {search
                ? "Try a broader term — company name or role usually works best."
                : "After your next interview round, add the questions you were asked so the next candidate walks in prepared."}
            </p>
          </div>
        </Card>
      ) : (
        <Card padding="none">
          <div className="divide-y divide-gray-50">
            {items.map((item) => (
              <QuestionSetRow
                key={item.id}
                item={item}
                canEdit={isAdmin || item.user_id === user?.id}
                onEdit={() => { setEditing(item); setShowForm(true); }}
                onDelete={() => {
                  if (
                    window.confirm(
                      `Delete the ${item.company_name} · ${item.interview_round} debrief? This cannot be undone.`,
                    )
                  ) {
                    deleteMutation.mutate(item.id);
                  }
                }}
              />
            ))}
          </div>
          {(listQ.data?.total_pages ?? 0) > 1 && (
            <Pagination
              page={page}
              totalPages={listQ.data!.total_pages}
              onPageChange={setPage}
            />
          )}
        </Card>
      )}

      {showForm && (
        <QuestionSetFormModal
          initial={editing}
          onClose={() => { setShowForm(false); setEditing(null); }}
          onSaved={() => {
            invalidate();
            setShowForm(false);
            setEditing(null);
          }}
        />
      )}
    </div>
  );
}

// ─── Row ──────────────────────────────────────────────────────────

function QuestionSetRow({
  item,
  canEdit,
  onEdit,
  onDelete,
}: {
  item: InterviewQuestionSet;
  canEdit: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const questionLines = item.questions
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

  return (
    <div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between px-5 py-3 hover:bg-gray-50 transition-colors text-left"
      >
        <div className="flex items-center gap-3 min-w-0 flex-1">
          {expanded ? (
            <ChevronDown className="h-4 w-4 text-gray-400 flex-shrink-0" />
          ) : (
            <ChevronRight className="h-4 w-4 text-gray-400 flex-shrink-0" />
          )}
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-gray-900">
              <Building2 className="inline h-3.5 w-3.5 mr-1 text-gray-400" />
              {item.company_name}
              <span className="mx-1.5 text-gray-300">·</span>
              {item.job_role}
            </p>
            <p className="text-xs text-gray-500 mt-0.5">
              <Badge variant="primary">{item.interview_round}</Badge>
              {item.interview_date && (
                <span className="ml-2">
                  <CalendarDays className="inline h-3 w-3 mr-0.5" />
                  {item.interview_date}
                </span>
              )}
              {item.candidate_name && (
                <span className="ml-2">
                  <UserIcon className="inline h-3 w-3 mr-0.5" />
                  {item.candidate_name}
                </span>
              )}
            </p>
          </div>
        </div>
        <span className="ml-3 flex-shrink-0 text-xs text-gray-400">
          {questionLines.length} question{questionLines.length === 1 ? "" : "s"}
        </span>
      </button>

      {expanded && (
        <div className="bg-gray-50 px-5 py-4 space-y-3">
          <ol className="list-decimal space-y-1 pl-8">
            {questionLines.map((q, i) => (
              <li key={i} className="text-sm text-gray-800">{q}</li>
            ))}
          </ol>
          {item.interviewer && (
            <p className="text-xs text-gray-600">
              <span className="font-semibold">Interviewer:</span> {item.interviewer}
            </p>
          )}
          {item.notes && (
            <p className="text-xs text-gray-600 whitespace-pre-line">
              <span className="font-semibold">Notes:</span> {item.notes}
            </p>
          )}
          <div className="flex items-center justify-between border-t border-gray-100 pt-2">
            <span className="text-[11px] text-gray-400">
              Added by {item.author_name || "—"} · {item.created_at.slice(0, 10)}
            </span>
            {canEdit && (
              <div className="flex gap-2">
                <Button size="sm" variant="ghost" onClick={onEdit}>
                  <Pencil className="h-3.5 w-3.5 mr-1" /> Edit
                </Button>
                <Button size="sm" variant="ghost" onClick={onDelete}>
                  <Trash2 className="h-3.5 w-3.5 mr-1 text-red-500" /> Delete
                </Button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Create / edit modal ──────────────────────────────────────────

function QuestionSetFormModal({
  initial,
  onClose,
  onSaved,
}: {
  initial: InterviewQuestionSet | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [companyName, setCompanyName] = useState(initial?.company_name ?? "");
  const [jobRole, setJobRole] = useState(initial?.job_role ?? "");
  const [round, setRound] = useState(initial?.interview_round ?? "");
  const [date, setDate] = useState(initial?.interview_date ?? "");
  const [candidate, setCandidate] = useState(initial?.candidate_name ?? "");
  const [interviewer, setInterviewer] = useState(initial?.interviewer ?? "");
  const [questions, setQuestions] = useState(initial?.questions ?? "");
  const [notes, setNotes] = useState(initial?.notes ?? "");
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async () => {
      const payload: InterviewQuestionSetPayload = {
        company_name: companyName.trim(),
        job_role: jobRole.trim(),
        interview_round: round.trim(),
        interview_date: date || null,
        candidate_name: candidate.trim(),
        interviewer: interviewer.trim(),
        questions: questions.trim(),
        notes: notes.trim(),
      };
      return initial
        ? updateInterviewQuestionSet(initial.id, payload)
        : createInterviewQuestionSet(payload);
    },
    onSuccess: onSaved,
    onError: (e: unknown) =>
      setError(e instanceof Error ? e.message : "Failed to save"),
  });

  const valid =
    companyName.trim() && jobRole.trim() && round.trim() && questions.trim();

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-lg bg-white p-6 shadow-2xl"
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">
            {initial ? "Edit Debrief" : "Add Interview Debrief"}
          </h2>
          <button
            onClick={onClose}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid && !mutation.isPending) mutation.mutate();
          }}
        >
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Company <span className="text-red-500">*</span>
              </label>
              <input
                type="text"
                value={companyName}
                onChange={(e) => setCompanyName(e.target.value)}
                className="input w-full text-sm"
                maxLength={300}
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Job Role <span className="text-red-500">*</span>
              </label>
              <input
                type="text"
                value={jobRole}
                onChange={(e) => setJobRole(e.target.value)}
                placeholder="DevOps Engineer, Backend Engineer, QA…"
                className="input w-full text-sm"
                maxLength={200}
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Interview Round <span className="text-red-500">*</span>
              </label>
              <input
                type="text"
                value={round}
                onChange={(e) => setRound(e.target.value)}
                placeholder="HR, Technical Round 1, System Design…"
                className="input w-full text-sm"
                maxLength={100}
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Interview Date
              </label>
              <input
                type="date"
                value={date ?? ""}
                onChange={(e) => setDate(e.target.value)}
                className="input w-full text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Candidate
              </label>
              <input
                type="text"
                value={candidate}
                onChange={(e) => setCandidate(e.target.value)}
                placeholder="Who attended the interview"
                className="input w-full text-sm"
                maxLength={200}
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Interviewer <span className="text-gray-400">(optional)</span>
              </label>
              <input
                type="text"
                value={interviewer}
                onChange={(e) => setInterviewer(e.target.value)}
                placeholder="Name / designation if known"
                className="input w-full text-sm"
                maxLength={300}
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Questions <span className="text-red-500">*</span>{" "}
              <span className="text-gray-400">(one per line)</span>
            </label>
            <textarea
              value={questions}
              onChange={(e) => setQuestions(e.target.value)}
              rows={8}
              placeholder={"Explain the difference between a Deployment and a StatefulSet\nHow would you debug a CrashLoopBackOff?\nWalk me through your CI/CD pipeline"}
              className="input w-full font-mono text-sm"
              maxLength={16000}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Notes <span className="text-gray-400">(optional)</span>
            </label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              placeholder="Anything else useful — tone, difficulty, format, follow-ups…"
              className="input w-full text-sm"
              maxLength={8000}
            />
          </div>

          {error && (
            <p className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>
          )}

          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!valid} loading={mutation.isPending}>
              {initial ? "Save Changes" : "Add Debrief"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
