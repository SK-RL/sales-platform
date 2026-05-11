import { useState } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ExternalLink,
  Clock,
  Building2,
  Globe,
  MapPin,
  Tag,
  CheckCircle2,
  XCircle,
  SkipForward,
  Send,
  Copy,
  FileText,
  Check,
  AlertTriangle,
  Users,
  Mail,
  Linkedin,
  Brain,
  PenTool,
  MessageSquare,
  ChevronDown,
  ChevronUp,
  Loader2,
} from "lucide-react";
import { Card } from "@/components/Card";
import { Button } from "@/components/Button";
import { Badge } from "@/components/Badge";
import { StatusBadge } from "@/components/StatusBadge";
import { ScoreBar } from "@/components/ScoreBar";
import {
  ApiError,
  getJob,
  getJobDescription,
  getJobReviews,
  getJobScoreBreakdown,
  submitReview,
  prepareApplication,
  updateApplication,
  deleteApplication,
  getApplicationByJob,
  getApplyReadiness,
  syncAnswersToBook,
  getRelevantContacts,
  generateCoverLetter,
  generateInterviewPrep,
  getAIUsage,
} from "@/lib/api";
import { ApplicationQuestionsPreview } from "@/components/ApplicationQuestionsPreview";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import { RoutineQueueToggle } from "@/components/RoutineQueueToggle";
import type { ReviewPayload, PreparedAnswer, CoverLetterResult, InterviewPrepResult } from "@/lib/types";

export function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();

  // F339 regression fix (back-button preserves source filters):
  //
  // Pre-fix every "Back to Jobs" button hardcoded
  // ``navigate("/jobs")``. A user coming from
  // ``/jobs?role_cluster=relevant&geography=global_remote`` and
  // clicking Back landed on bare ``/jobs`` — which (post-F260)
  // resolves to "All Jobs" via localStorage. User report
  // 2026-05-09: "when we open the job from relevant job and
  // click on back button we landed on all jobs".
  //
  // Fix: prefer the explicit ``location.state.from`` if the
  // caller passed it (cleanest, survives a refresh on the detail
  // page); else use ``navigate(-1)`` which walks browser history
  // back to the exact URL the user came from; final fallback to
  // ``/jobs`` if there's no history (direct bookmark / first
  // page load).
  const handleBackToJobs = () => {
    const stateFrom =
      (location.state as { from?: string } | null)?.from || "";
    if (stateFrom && stateFrom.startsWith("/jobs")) {
      navigate(stateFrom);
      return;
    }
    if (window.history.length > 1) {
      navigate(-1);
      return;
    }
    navigate("/jobs");
  };

  const [comment, setComment] = useState("");
  const [tagInput, setTagInput] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [freshApplyData, setFreshApplyData] = useState<any>(null);
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);
  const [copiedAll, setCopiedAll] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [editingAnswers, setEditingAnswers] = useState<Record<number, string>>({});
  const [syncChecked, setSyncChecked] = useState<Record<number, boolean>>({});
  const [editMode, setEditMode] = useState(false);

  // Regression finding 207: the previous version destructured only
  // `data` and `isLoading`, which meant every non-2xx (401 expired
  // cookie, 500 backend fault, CORS, network) fell through the
  // `if (!job)` branch below and rendered "Job not found" — giving
  // users the false impression the record was deleted. Now we
  // surface `isError` + `error` and render error-class-specific UX.
  // 401 is also handled globally by the api.ts interceptor (full
  // redirect to /login preserving next=<path>) so this branch only
  // needs to cover 404 / 5xx / network.
  const jobQ = useQuery({
    queryKey: ["job", id],
    queryFn: () => getJob(id!),
    enabled: !!id,
    // Don't retry auth failures or missing records — retrying a 401
    // would just burn ticks until the global interceptor fires, and
    // retrying a 404 never helps.
    retry: (failureCount, err) => {
      if (err instanceof ApiError && (err.status === 401 || err.status === 404)) {
        return false;
      }
      return failureCount < 2;
    },
  });
  const { data: job, isLoading: jobLoading, isError: jobIsError, error: jobError } = jobQ;

  const descriptionQ = useQuery({
    queryKey: ["job", id, "description"],
    queryFn: () => getJobDescription(id!),
    enabled: !!id,
  });
  const { data: description, isLoading: descLoading } = descriptionQ;

  const reviewsQ = useQuery({
    queryKey: ["job", id, "reviews"],
    queryFn: () => getJobReviews(id!),
    enabled: !!id,
  });
  const reviews = reviewsQ.data;

  const scoreBreakdownQ = useQuery({
    queryKey: ["job", id, "score-breakdown"],
    queryFn: () => getJobScoreBreakdown(id!),
    enabled: !!id,
  });
  const scoreBreakdown = scoreBreakdownQ.data;

  const relevantContactsQ = useQuery({
    queryKey: ["job", id, "relevant-contacts"],
    queryFn: () => getRelevantContacts(job!.company_id, id!),
    enabled: !!id && !!job?.company_id,
  });
  const relevantContacts = relevantContactsQ.data;

  // Apply readiness check
  const readinessQ = useQuery({
    queryKey: ["apply-readiness", id],
    queryFn: () => getApplyReadiness(id!),
    enabled: !!id,
  });
  const readiness = readinessQ.data;

  // Auto-load existing application for this job
  const existingAppQ = useQuery({
    queryKey: ["application-by-job", id],
    queryFn: () => getApplicationByJob(id!),
    enabled: !!id,
  });
  const existingApp = existingAppQ.data;

  // F222: banner covers the 6 secondary queries. The primary `job` query
  // already has an explicit 404 → "Job not found" screen below (F207),
  // so it's deliberately excluded — otherwise a 404 would show BOTH the
  // dedicated screen and the banner.
  const jobDetailAuxQueries = [
    descriptionQ, reviewsQ, scoreBreakdownQ, relevantContactsQ, readinessQ, existingAppQ,
  ];

  // Merge: freshApplyData (just prepared) takes priority over existingApp (from DB)
  const applyData = freshApplyData || existingApp;

  const reviewMutation = useMutation({
    mutationFn: (payload: ReviewPayload) => submitReview(id!, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["job", id] });
      queryClient.invalidateQueries({ queryKey: ["job", id, "reviews"] });
      setComment("");
      setTags([]);
    },
  });

  const applyMutation = useMutation({
    mutationFn: () => prepareApplication(id!),
    onSuccess: (data) => {
      setFreshApplyData(data);
      setApplyError(null);
      queryClient.invalidateQueries({ queryKey: ["applications"] });
      queryClient.invalidateQueries({ queryKey: ["application-stats"] });
      queryClient.invalidateQueries({ queryKey: ["application-by-job", id] });
    },
    onError: (error: any) => {
      const status = error?.status;
      const msg = error?.message || "";
      if (status === 409 || msg.includes("already")) {
        // Refetch the existing application
        queryClient.invalidateQueries({ queryKey: ["application-by-job", id] });
        setApplyError(null);
      } else {
        setApplyError(msg || "Failed to prepare application");
      }
    },
  });

  const markAppliedMutation = useMutation({
    mutationFn: (appId: string) => updateApplication(appId, { status: "applied" }),
    onSuccess: () => {
      setFreshApplyData((prev: any) => prev ? { ...prev, status: "applied" } : prev);
      queryClient.invalidateQueries({ queryKey: ["applications"] });
      queryClient.invalidateQueries({ queryKey: ["application-stats"] });
      queryClient.invalidateQueries({ queryKey: ["application-by-job", id] });
    },
  });

  const cancelMutation = useMutation({
    mutationFn: (appId: string) => deleteApplication(appId),
    onSuccess: () => {
      setFreshApplyData(null);
      setEditMode(false);
      queryClient.invalidateQueries({ queryKey: ["applications"] });
      queryClient.invalidateQueries({ queryKey: ["application-stats"] });
      queryClient.invalidateQueries({ queryKey: ["application-by-job", id] });
      queryClient.invalidateQueries({ queryKey: ["apply-readiness", id] });
    },
  });

  const withdrawMutation = useMutation({
    mutationFn: (appId: string) => updateApplication(appId, { status: "withdrawn" }),
    onSuccess: () => {
      setFreshApplyData((prev: any) => prev ? { ...prev, status: "withdrawn" } : prev);
      queryClient.invalidateQueries({ queryKey: ["applications"] });
      queryClient.invalidateQueries({ queryKey: ["application-stats"] });
      queryClient.invalidateQueries({ queryKey: ["application-by-job", id] });
    },
  });

  const copyAnswer = (text: string, idx: number) => {
    navigator.clipboard.writeText(text);
    setCopiedIdx(idx);
    setTimeout(() => setCopiedIdx(null), 2000);
  };

  const copyAllAnswers = () => {
    if (!applyData?.prepared_answers) return;
    const text = applyData.prepared_answers
      .filter((pa: PreparedAnswer) => pa.field_type !== "file" && pa.answer)
      .map((pa: PreparedAnswer) => `Q: ${pa.label}\nA: ${pa.answer}`)
      .join("\n\n");
    navigator.clipboard.writeText(text);
    setCopiedAll(true);
    setTimeout(() => setCopiedAll(false), 2000);
  };

  const handleSaveAnswers = async () => {
    if (!applyData) return;
    const updated = applyData.prepared_answers.map((pa: PreparedAnswer, i: number) => ({
      ...pa,
      answer: editingAnswers[i] ?? pa.answer,
    }));
    await updateApplication(applyData.id, { prepared_answers: updated });

    // Sync checked answers back to answer book
    const toSync = updated
      .filter((_: any, i: number) => syncChecked[i] && (editingAnswers[i] ?? "").trim())
      .map((pa: any) => ({ question_key: pa.question_key || pa.field_key, answer: pa.answer }))
      .filter((a: any) => a.question_key);

    if (toSync.length > 0) {
      await syncAnswersToBook(applyData.id, toSync);
    }

    setFreshApplyData((prev: any) => prev ? { ...prev, prepared_answers: updated } : null);
    queryClient.invalidateQueries({ queryKey: ["application-by-job", id] });
    setEditMode(false);
    setSyncChecked({});
  };

  const handleReview = (decision: "accept" | "reject" | "skip") => {
    reviewMutation.mutate({ decision, comment, tags });
  };

  const addTag = () => {
    const trimmed = tagInput.trim();
    if (trimmed && !tags.includes(trimmed)) {
      setTags([...tags, trimmed]);
      setTagInput("");
    }
  };

  const removeTag = (tag: string) => {
    setTags(tags.filter((t) => t !== tag));
  };

  const handleTagKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addTag();
    }
  };

  if (jobLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  // F207: Split the failure state so users can tell the difference
  // between "this job was deleted" and "something went wrong talking to
  // the backend." 401 is handled by the api.ts global interceptor
  // (redirect to /login), but we still guard the branch in case the
  // interceptor didn't fire (e.g., mocked in tests, or the redirect was
  // blocked by a higher-level navigation guard).
  if (jobIsError) {
    const status = jobError instanceof ApiError ? jobError.status : 0;
    const message = (jobError as Error)?.message || "";

    if (status === 404) {
      return (
        <div className="py-20 text-center">
          <p className="text-gray-700 text-lg font-medium">Job not found</p>
          <p className="mt-1 text-sm text-gray-500">
            This listing may have been removed by the source or deleted by an admin.
          </p>
          <Button variant="secondary" className="mt-4" onClick={handleBackToJobs}>
            Back to Jobs
          </Button>
        </div>
      );
    }

    if (status === 401 || status === 403) {
      // Usually pre-empted by the global redirect in api.ts. Fallback UI
      // in case we got here anyway.
      return (
        <div className="py-20 text-center">
          <p className="text-gray-700 text-lg font-medium">Your session has expired</p>
          <p className="mt-1 text-sm text-gray-500">Please sign in again to continue.</p>
          <Button variant="primary" className="mt-4" onClick={() => {
            const next = encodeURIComponent(window.location.pathname + window.location.search);
            window.location.assign(`/login?next=${next}`);
          }}>
            Sign in
          </Button>
        </div>
      );
    }

    // Generic failure (5xx, network, CORS, timeout). Give the user a
    // retry path instead of making them guess whether the job exists.
    return (
      <div className="py-20 text-center">
        <p className="text-gray-700 text-lg font-medium">Couldn't load this job</p>
        <p className="mt-1 text-sm text-gray-500">
          {status >= 500
            ? "The server ran into a problem. Please try again in a moment."
            : message || "Check your connection and try again."}
        </p>
        <div className="mt-4 flex justify-center gap-3">
          <Button variant="primary" onClick={() => queryClient.invalidateQueries({ queryKey: ["job", id] })}>
            Retry
          </Button>
          <Button variant="secondary" onClick={handleBackToJobs}>
            Back to Jobs
          </Button>
        </div>
      </div>
    );
  }

  if (!job) {
    // Defensive: isLoading=false, isError=false, data=undefined is
    // theoretically possible if the query was disabled. With
    // `enabled: !!id` above, this branch means the URL was hit without
    // an id — send them back to the list rather than rendering "not
    // found" (which implies the id resolved to nothing).
    return (
      <div className="py-20 text-center">
        <p className="text-gray-500">No job selected</p>
        <Button variant="secondary" className="mt-4" onClick={handleBackToJobs}>
          Back to Jobs
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <button
          onClick={handleBackToJobs}
          className="rounded-lg p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-600 transition-colors"
        >
          <ArrowLeft className="h-5 w-5" />
        </button>
        <div className="flex-1">
          <h1 className="text-2xl font-bold text-gray-900">{job.title}</h1>
          <p className="mt-1 text-sm text-gray-500">
            <button
              onClick={() => navigate(`/companies/${job.company_id}`)}
              className="font-medium text-primary-600 hover:text-primary-700 hover:underline"
            >
              {job.company_name}
            </button>
            {" "}&middot; {job.source_platform}
          </p>
        </div>
        <StatusBadge status={job.status} />
      </div>

      {/* F222: surfaces description/reviews/score/contacts/readiness/existingApp failures. */}
      <BackendErrorBanner queries={jobDetailAuxQueries} />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2 space-y-6">
          <Card>
            <div className="grid grid-cols-2 gap-4">
              <div className="flex items-start gap-3">
                <Building2 className="mt-0.5 h-4 w-4 text-gray-400" />
                <div>
                  <p className="text-xs text-gray-500">Company</p>
                  <button
                    onClick={() => navigate(`/companies/${job.company_id}`)}
                    className="text-sm font-medium text-primary-600 hover:text-primary-700 hover:underline"
                  >
                    {job.company_name}
                  </button>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <Globe className="mt-0.5 h-4 w-4 text-gray-400" />
                <div>
                  <p className="text-xs text-gray-500">Remote Scope</p>
                  <p className="text-sm font-medium text-gray-900">
                    {job.remote_scope}
                  </p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <MapPin className="mt-0.5 h-4 w-4 text-gray-400" />
                <div>
                  <p className="text-xs text-gray-500">Location Restriction</p>
                  <p className="text-sm font-medium text-gray-900">
                    {job.location_restriction || "None specified"}
                  </p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <Clock className="mt-0.5 h-4 w-4 text-gray-400" />
                <div>
                  <p className="text-xs text-gray-500">Employment Type</p>
                  <p className="text-sm font-medium text-gray-900">
                    {job.employment_type || "Not specified"}
                  </p>
                </div>
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-4 border-t border-gray-100 pt-4">
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-500">Score:</span>
                <ScoreBar score={job.relevance_score} />
              </div>
              {job.salary_range && (
                <Badge variant="success">{job.salary_range}</Badge>
              )}
              {job.role_cluster && <Badge variant="primary">{job.role_cluster}</Badge>}
              {job.geography_bucket && (
                <Badge variant="info">
                  {job.geography_bucket.replace(/_/g, " ")}
                </Badge>
              )}
              {job.tags.map((tag) => (
                <Badge key={tag} variant="gray">{tag}</Badge>
              ))}
            </div>

            <div className="mt-4 flex items-center gap-3 border-t border-gray-100 pt-4">
              <a
                href={job.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-sm font-medium text-primary-600 hover:text-primary-700"
              >
                View Original Listing
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
              <span className="text-xs text-gray-400">
                Scraped{" "}
                {new Date(job.scraped_at).toLocaleDateString("en-US", {
                  month: "short",
                  day: "numeric",
                  year: "numeric",
                })}
              </span>
            </div>
          </Card>

          <Card padding="none">
            <div className="border-b border-gray-100 px-6 py-4">
              <h3 className="text-base font-semibold text-gray-900">
                Job Description
              </h3>
            </div>
            <div className="px-6 py-4">
              {descLoading ? (
                <div className="flex items-center justify-center py-10">
                  <div className="spinner" />
                </div>
              ) : description ? (
                <div className="space-y-6">
                  {description.raw_text && (
                    description.raw_text.includes("<") ? (
                      <div
                        className="prose prose-sm max-w-none text-gray-700"
                        dangerouslySetInnerHTML={{ __html: description.raw_text }}
                      />
                    ) : (
                      <div className="prose prose-sm max-w-none text-gray-700 whitespace-pre-wrap">
                        {description.raw_text}
                      </div>
                    )
                  )}

                  {/* Regression finding 168: Requirements / Tech Stack /
                      Nice to Have sections previously rendered behind
                      `description.parsed_requirements.length > 0` (and
                      similar) guards — but those fields were always the
                      empty array because the backend never populated
                      them. The fields were removed from the API contract;
                      if bullet-point extraction lands later, restore
                      these sections alongside the real parser. */}
                </div>
              ) : (
                <p className="py-6 text-center text-sm text-gray-500">
                  No description available
                </p>
              )}
            </div>
          </Card>
        </div>

        <div className="space-y-6">
          {job.status !== "accepted" && job.status !== "rejected" && (
            <Card>
              <h3 className="text-base font-semibold text-gray-900 mb-4">
                Quick Actions
              </h3>
              <div className="flex gap-2">
                <Button
                  variant="primary"
                  size="sm"
                  className="flex-1"
                  onClick={() => handleReview("accept")}
                  loading={reviewMutation.isPending}
                >
                  <CheckCircle2 className="h-4 w-4" />
                  Accept
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  className="flex-1"
                  onClick={() => handleReview("reject")}
                  loading={reviewMutation.isPending}
                >
                  <XCircle className="h-4 w-4" />
                  Reject
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => handleReview("skip")}
                  loading={reviewMutation.isPending}
                >
                  <SkipForward className="h-4 w-4" />
                </Button>
              </div>
            </Card>
          )}

          {/* AI Tools */}
          <AIToolsPanel jobId={id!} jobTitle={job.title} companyName={job.company_name} />

          {/* Key Contacts at this company */}
          {relevantContacts?.items && relevantContacts.items.length > 0 && (
            <Card>
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-base font-semibold text-gray-900">
                  <Users className="inline h-4 w-4 mr-1.5 text-primary-600" />
                  Key Contacts
                </h3>
                <button
                  onClick={() => navigate(`/companies/${job.company_id}`)}
                  className="text-xs text-primary-600 hover:text-primary-700 hover:underline"
                >
                  View all
                </button>
              </div>
              <div className="space-y-3">
                {relevantContacts.items.slice(0, 5).map((rc) => (
                  <div key={rc.contact.id} className="flex items-start gap-3 rounded-lg border border-gray-100 p-2.5">
                    <div className="flex h-8 w-8 items-center justify-center rounded-full bg-primary-50 text-xs font-semibold text-primary-700 flex-shrink-0">
                      {rc.contact.first_name?.[0]}{rc.contact.last_name?.[0]}
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-gray-900 truncate">
                        {rc.contact.first_name} {rc.contact.last_name}
                        {rc.contact.is_decision_maker && (
                          <span className="ml-1 text-yellow-500" title="Decision Maker">&#9733;</span>
                        )}
                      </p>
                      <p className="text-xs text-gray-500 truncate">{rc.contact.title}</p>
                      <div className="mt-1 flex items-center gap-2">
                        {rc.contact.email && (
                          <a
                            href={`mailto:${rc.contact.email}`}
                            onClick={(e) => e.stopPropagation()}
                            className="inline-flex items-center gap-0.5 text-xs text-gray-500 hover:text-primary-600"
                            title={`${rc.contact.email} (${rc.contact.email_status})`}
                          >
                            <Mail className="h-3 w-3" />
                            <span className={`inline-block h-1.5 w-1.5 rounded-full ${
                              rc.contact.email_status === "valid" ? "bg-green-500" :
                              rc.contact.email_status === "catch_all" ? "bg-amber-400" :
                              rc.contact.email_status === "invalid" ? "bg-red-400" :
                              "bg-gray-300"
                            }`} />
                          </a>
                        )}
                        {rc.contact.linkedin_url && (
                          <a
                            href={rc.contact.linkedin_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            className="text-xs text-gray-500 hover:text-primary-600"
                          >
                            <Linkedin className="h-3 w-3" />
                          </a>
                        )}
                        <span className="text-[10px] text-gray-400">
                          {Math.round(rc.relevance_score * 100)}% match
                        </span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {/* Apply Panel */}
          <Card>
            <h3 className="text-base font-semibold text-gray-900 mb-3">
              <Send className="inline h-4 w-4 mr-1.5 text-primary-600" />
              Apply
            </h3>

            {/* Readiness Checklist */}
            {readiness && !applyData && (
              <div className="space-y-2 mb-3">
                {/* Resume */}
                <div className="flex items-center gap-2 text-xs">
                  {readiness.resume.ready ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-green-500" />
                      <span className="text-gray-700">Resume: <span className="font-medium">{readiness.resume.label}</span></span>
                    </>
                  ) : (
                    <>
                      <XCircle className="h-3.5 w-3.5 text-red-400" />
                      <span className="text-red-600">No active resume — select one in the header</span>
                    </>
                  )}
                </div>
                {/* Credentials */}
                <div className="flex items-center gap-2 text-xs">
                  {readiness.credentials.ready ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-green-500" />
                      <span className="text-gray-700">{readiness.credentials.platform} credentials ({readiness.credentials.email})</span>
                    </>
                  ) : (
                    <>
                      <XCircle className="h-3.5 w-3.5 text-red-400" />
                      <span className="text-red-600">
                        No credentials for {readiness.credentials.platform} —{" "}
                        <a href="/credentials" className="font-medium underline">add credentials</a>
                      </span>
                    </>
                  )}
                </div>
                {/* Answer Book */}
                <div className="flex items-center gap-2 text-xs">
                  {readiness.answer_book.ready ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-green-500" />
                      <span className="text-gray-700">{readiness.answer_book.count} answers in book</span>
                    </>
                  ) : (
                    <>
                      <AlertTriangle className="h-3.5 w-3.5 text-amber-400" />
                      <span className="text-amber-600">
                        No answers yet —{" "}
                        <a href="/answer-book" className="font-medium underline">add answers</a> (recommended)
                      </span>
                    </>
                  )}
                </div>
                {/* Resume Score */}
                {readiness.resume_score.available && (
                  <div className="flex items-center gap-2 text-xs">
                    <FileText className="h-3.5 w-3.5 text-primary-500" />
                    <span className={`rounded-full px-2 py-0.5 font-semibold ${
                      (readiness.resume_score.score ?? 0) >= 70 ? "bg-green-100 text-green-700" :
                      (readiness.resume_score.score ?? 0) >= 50 ? "bg-yellow-100 text-yellow-700" :
                      "bg-gray-100 text-gray-600"
                    }`}>
                      {readiness.resume_score.score}% ATS match
                    </span>
                  </div>
                )}

                {/* Existing application notice */}
                {readiness.existing_application.exists && (
                  <div className="rounded-lg border border-blue-200 bg-blue-50 p-2 text-xs text-blue-700">
                    Application already exists (status: {readiness.existing_application.status})
                  </div>
                )}
              </div>
            )}

            {applyData ? (
              <div className="space-y-3">
                {/* Status badge */}
                <div className="flex items-center justify-between">
                  <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                    applyData.status === "applied" ? "bg-green-100 text-green-700" :
                    applyData.status === "submitted" ? "bg-blue-100 text-blue-700" :
                    applyData.status === "withdrawn" ? "bg-gray-100 text-gray-500" :
                    "bg-gray-100 text-gray-600"
                  }`}>
                    {applyData.status.charAt(0).toUpperCase() + applyData.status.slice(1)}
                  </span>
                  <span className="text-xs text-gray-400">Manual Apply</span>
                </div>

                {/* Form fields / prepared answers */}
                {applyData.prepared_answers && applyData.prepared_answers.length > 0 && (
                  <>
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-medium text-gray-600">
                        {applyData.prepared_answers.length} form fields
                      </span>
                      <div className="flex gap-2">
                        {applyData.status === "prepared" && !editMode && (
                          <button
                            onClick={() => {
                              setEditMode(true);
                              const edits: Record<number, string> = {};
                              applyData.prepared_answers.forEach((pa: PreparedAnswer, i: number) => {
                                edits[i] = pa.answer;
                              });
                              setEditingAnswers(edits);
                              setSyncChecked({});
                            }}
                            className="text-[11px] font-medium text-primary-600 hover:text-primary-700"
                          >
                            Edit
                          </button>
                        )}
                        {!editMode && (
                          <button
                            onClick={copyAllAnswers}
                            className="text-[11px] font-medium text-primary-600 hover:text-primary-700"
                          >
                            {copiedAll ? "Copied!" : "Copy All"}
                          </button>
                        )}
                      </div>
                    </div>

                    {editMode && (
                      <div className="flex gap-2 justify-end">
                        <button
                          onClick={handleSaveAnswers}
                          className="text-[11px] font-medium text-green-600 hover:text-green-700"
                        >
                          Save
                        </button>
                        <button
                          onClick={() => { setEditMode(false); setEditingAnswers({}); setSyncChecked({}); }}
                          className="text-[11px] font-medium text-gray-500 hover:text-gray-700"
                        >
                          Cancel
                        </button>
                      </div>
                    )}

                    <div className="max-h-80 overflow-y-auto space-y-2">
                      {applyData.prepared_answers.map((pa: PreparedAnswer, idx: number) => {
                        const isMatched = pa.match_source !== "unmatched";
                        const isRequired = pa.required;
                        const isFile = pa.field_type === "file";
                        return (
                          <div key={idx} className={`rounded border p-2 ${
                            !isMatched && isRequired ? "border-amber-200 bg-amber-50/50" : "border-gray-100"
                          }`}>
                            <div className="flex items-start justify-between gap-1">
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-1.5">
                                  {isRequired && <span className="text-[9px] font-bold text-red-500">REQ</span>}
                                  <p className="text-xs font-medium text-gray-700">{pa.label}</p>
                                </div>
                                <div className="flex items-center gap-1.5 mt-0.5">
                                  {isFile ? (
                                    <span className="text-[10px] text-primary-500">📎 Attach resume</span>
                                  ) : isMatched ? (
                                    <span className="text-[10px] text-green-500">✓ matched ({pa.confidence})</span>
                                  ) : (
                                    <span className="text-[10px] text-amber-500">⚠ needs input</span>
                                  )}
                                </div>
                              </div>
                              {!isFile && (
                                <button
                                  onClick={() => copyAnswer(editMode ? (editingAnswers[idx] ?? pa.answer) : pa.answer, idx)}
                                  className="flex-shrink-0 rounded p-0.5 text-gray-400 hover:text-gray-600"
                                  title="Copy"
                                >
                                  {copiedIdx === idx ? <Check className="h-3.5 w-3.5 text-green-500" /> : <Copy className="h-3.5 w-3.5" />}
                                </button>
                              )}
                            </div>
                            {!isFile && editMode ? (
                              <div>
                                {pa.field_type === "select" && pa.options?.length > 0 ? (
                                  <select
                                    value={editingAnswers[idx] ?? pa.answer}
                                    onChange={(e) => setEditingAnswers((prev) => ({ ...prev, [idx]: e.target.value }))}
                                    className="mt-1 w-full rounded border border-gray-200 px-2 py-1 text-xs"
                                  >
                                    <option value="">-- Select --</option>
                                    {pa.options.map((opt) => (
                                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                                    ))}
                                  </select>
                                ) : (
                                  <textarea
                                    value={editingAnswers[idx] ?? pa.answer}
                                    onChange={(e) => setEditingAnswers((prev) => ({ ...prev, [idx]: e.target.value }))}
                                    className="mt-1 w-full rounded border border-gray-200 px-2 py-1 text-xs text-gray-700 focus:border-primary-300 focus:ring-1 focus:ring-primary-300 resize-y min-h-[32px]"
                                    rows={pa.field_type === "textarea" ? 3 : 1}
                                  />
                                )}
                                <label className="flex items-center gap-1.5 mt-1 text-[10px] text-gray-500 cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={syncChecked[idx] || false}
                                    onChange={(e) => setSyncChecked((prev) => ({ ...prev, [idx]: e.target.checked }))}
                                    className="rounded border-gray-300 h-3 w-3"
                                  />
                                  Save to Answer Book
                                </label>
                              </div>
                            ) : !isFile ? (
                              <p className="mt-0.5 text-xs text-gray-500">{pa.answer || "—"}</p>
                            ) : null}
                          </div>
                        );
                      })}
                    </div>
                  </>
                )}

                {(!applyData.prepared_answers || applyData.prepared_answers.length === 0) && (
                  <div className="rounded-lg border border-dashed border-gray-200 p-3 text-center">
                    <p className="text-xs text-gray-500">No form fields fetched.</p>
                    <a href="/answer-book" className="text-[11px] font-medium text-primary-600 hover:text-primary-700">
                      Add answers in your Answer Book
                    </a>
                  </div>
                )}

                {/* Action buttons */}
                <div className="flex gap-2">
                  <a
                    href={job.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex-1 flex items-center justify-center gap-1.5 rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
                  >
                    <ExternalLink className="h-4 w-4" />
                    Open ATS
                  </a>
                  {applyData.status === "prepared" && (
                    <button
                      onClick={() => markAppliedMutation.mutate(applyData.id)}
                      disabled={markAppliedMutation.isPending}
                      className="flex-1 flex items-center justify-center gap-1.5 rounded-lg bg-green-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-green-700 disabled:opacity-50"
                    >
                      <Check className="h-4 w-4" />
                      Mark Applied
                    </button>
                  )}
                  {applyData.status === "applied" && (
                    <span className="flex-1 flex items-center justify-center gap-1.5 rounded-lg bg-green-50 px-3 py-1.5 text-sm font-medium text-green-700">
                      <Check className="h-4 w-4" /> Applied
                    </span>
                  )}
                  {applyData.status === "withdrawn" && (
                    <span className="flex-1 flex items-center justify-center gap-1.5 rounded-lg bg-gray-50 px-3 py-1.5 text-sm font-medium text-gray-500">
                      Withdrawn
                    </span>
                  )}
                </div>

                {/* Cancel / Withdraw */}
                <div className="flex justify-center gap-3">
                  {applyData.status === "prepared" && (
                    <button
                      onClick={() => cancelMutation.mutate(applyData.id)}
                      disabled={cancelMutation.isPending}
                      className="text-[11px] text-red-500 hover:text-red-700"
                    >
                      Cancel Application
                    </button>
                  )}
                  {["submitted", "applied", "interview"].includes(applyData.status) && (
                    <button
                      onClick={() => withdrawMutation.mutate(applyData.id)}
                      disabled={withdrawMutation.isPending}
                      className="text-[11px] text-red-500 hover:text-red-700"
                    >
                      Withdraw
                    </button>
                  )}
                </div>
              </div>
            ) : (
              <div>
                {readiness ? (
                  <>
                    <Button
                      variant="primary"
                      size="sm"
                      className="w-full"
                      onClick={() => applyMutation.mutate()}
                      loading={applyMutation.isPending}
                      disabled={!readiness.can_apply}
                    >
                      <Send className="h-4 w-4" />
                      Prepare Application
                    </Button>
                    {!readiness.can_apply && (
                      <p className="text-[11px] text-red-500 text-center mt-1">
                        Complete the checklist above to enable apply
                      </p>
                    )}
                    {readiness.can_apply && (
                      <p className="text-[11px] text-gray-400 text-center mt-1">
                        Fetches form questions from {job.source_platform} and matches your answers
                      </p>
                    )}
                  </>
                ) : (
                  <div className="text-center py-2">
                    <p className="text-sm text-gray-500 mb-2">No active resume selected</p>
                    <p className="text-xs text-gray-400">
                      Use the resume switcher in the header to select a persona
                    </p>
                  </div>
                )}
                {applyError && (
                  <p className="text-xs text-red-600 mt-2">{applyError}</p>
                )}
              </div>
            )}
            {/* F257: Apply Routine queue / exclude controls. Lets the
                operator pin this specific job to the routine's
                top-to-apply list (above auto-picks) OR permanently
                skip it. Independent of the "Prepare Application"
                manual flow above — both can coexist. */}
            <div className="mt-3 border-t border-gray-100 pt-3">
              <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-gray-500">
                Apply Routine
              </p>
              <RoutineQueueToggle jobId={job.id} />
            </div>
          </Card>

          {["greenhouse", "lever", "ashby"].includes(job.source_platform.toLowerCase()) && job.status !== "expired" && (
            <ApplicationQuestionsPreview jobId={job.id} />
          )}

          {scoreBreakdown && (
            <Card>
              <h3 className="text-base font-semibold text-gray-900 mb-3">
                Score Breakdown
              </h3>
              <div className="space-y-2">
                {scoreBreakdown.breakdown.map((s) => (
                  <div key={s.signal}>
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-medium text-gray-700">{s.signal} ({Math.round(s.weight * 100)}%)</span>
                      <span className="font-semibold text-gray-900">{s.weighted.toFixed(1)}</span>
                    </div>
                    <div className="mt-0.5 h-1.5 rounded-full bg-gray-100 overflow-hidden">
                      <div
                        className="h-full rounded-full bg-primary-500 transition-all"
                        style={{ width: `${s.raw * 100}%` }}
                      />
                    </div>
                    <p className="mt-0.5 text-[10px] text-gray-400">{s.detail}</p>
                  </div>
                ))}
                <div className="border-t border-gray-100 pt-2 flex items-center justify-between">
                  <span className="text-sm font-semibold text-gray-900">Total</span>
                  <span className="text-sm font-bold text-primary-600">{scoreBreakdown.total.toFixed(1)}/100</span>
                </div>
              </div>
            </Card>
          )}

          {/* Resume Fit Panel */}
          {job.resume_fit && (
            <Card>
              <h3 className="text-base font-semibold text-gray-900 mb-3">
                <FileText className="inline h-4 w-4 mr-1.5 text-primary-600" />
                Resume Fit
              </h3>
              <div className="space-y-3">
                {/* Score bar */}
                <div>
                  <div className="flex items-center justify-between text-xs mb-1">
                    <span className="text-gray-500">Overall Match</span>
                    <span className={`font-bold ${
                      job.resume_fit.overall_score >= 70 ? "text-green-600" :
                      job.resume_fit.overall_score >= 50 ? "text-yellow-600" :
                      "text-gray-500"
                    }`}>{job.resume_fit.overall_score}%</span>
                  </div>
                  <div className="h-2 rounded-full bg-gray-100 overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${
                        job.resume_fit.overall_score >= 70 ? "bg-green-500" :
                        job.resume_fit.overall_score >= 50 ? "bg-yellow-500" :
                        "bg-gray-400"
                      }`}
                      style={{ width: `${job.resume_fit.overall_score}%` }}
                    />
                  </div>
                </div>

                {/* Sub-scores */}
                <div className="grid grid-cols-3 gap-2 text-center">
                  <div className="rounded-lg bg-gray-50 p-1.5">
                    <p className="text-xs font-bold text-gray-700">{job.resume_fit.keyword_score}%</p>
                    <p className="text-[10px] text-gray-400">Keywords</p>
                  </div>
                  <div className="rounded-lg bg-gray-50 p-1.5">
                    <p className="text-xs font-bold text-gray-700">{job.resume_fit.role_match_score}%</p>
                    <p className="text-[10px] text-gray-400">Role Fit</p>
                  </div>
                  <div className="rounded-lg bg-gray-50 p-1.5">
                    <p className="text-xs font-bold text-gray-700">{job.resume_fit.format_score}%</p>
                    <p className="text-[10px] text-gray-400">Format</p>
                  </div>
                </div>

                {/* Matched keywords */}
                {job.resume_fit.matched_keywords.length > 0 && (
                  <div>
                    <p className="text-[10px] font-medium text-gray-500 uppercase mb-1">Matched Keywords</p>
                    <div className="flex flex-wrap gap-1">
                      {job.resume_fit.matched_keywords.map((kw: string) => (
                        <span key={kw} className="rounded-full bg-green-50 px-2 py-0.5 text-[10px] text-green-700">{kw}</span>
                      ))}
                    </div>
                  </div>
                )}

                {/* Missing keywords */}
                {job.resume_fit.missing_keywords.length > 0 && (
                  <div>
                    <p className="text-[10px] font-medium text-gray-500 uppercase mb-1">Missing Keywords</p>
                    <div className="flex flex-wrap gap-1">
                      {job.resume_fit.missing_keywords.map((kw: string) => (
                        <span key={kw} className="rounded-full bg-red-50 px-2 py-0.5 text-[10px] text-red-600">{kw}</span>
                      ))}
                    </div>
                  </div>
                )}

                {/* Suggestions */}
                {job.resume_fit.suggestions.length > 0 && (
                  <div>
                    <p className="text-[10px] font-medium text-gray-500 uppercase mb-1">Suggestions</p>
                    <ul className="space-y-1">
                      {job.resume_fit.suggestions.map((s: string, i: number) => (
                        <li key={i} className="text-[11px] text-gray-600 flex items-start gap-1">
                          <span className="mt-0.5 text-primary-400">•</span>
                          {s}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <a href="/resume-score" className="block text-center text-[11px] font-medium text-primary-600 hover:text-primary-700 pt-1 border-t border-gray-100">
                  Improve Resume
                </a>
              </div>
            </Card>
          )}

          {job.status !== "accepted" && job.status !== "rejected" && (
            <Card>
              <h3 className="text-base font-semibold text-gray-900 mb-4">
                Review
              </h3>
              <div className="space-y-4">
                <div>
                  <label className="label">Comment</label>
                  <textarea
                    className="input min-h-[80px] resize-y"
                    placeholder="Add a comment..."
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                  />
                </div>
                <div>
                  <label className="label">Tags</label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      className="input flex-1"
                      placeholder="Add tag..."
                      value={tagInput}
                      onChange={(e) => setTagInput(e.target.value)}
                      onKeyDown={handleTagKeyDown}
                    />
                    <Button variant="secondary" size="sm" onClick={addTag}>
                      Add
                    </Button>
                  </div>
                  {tags.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {tags.map((tag) => (
                        <Badge key={tag} variant="primary">
                          {tag}
                          <button
                            onClick={() => removeTag(tag)}
                            className="ml-1 hover:text-primary-900"
                          >
                            &times;
                          </button>
                        </Badge>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </Card>
          )}

          <Card padding="none">
            <div className="border-b border-gray-100 px-6 py-4">
              <h3 className="text-base font-semibold text-gray-900">
                Review History
              </h3>
            </div>
            {reviews && reviews.length > 0 ? (
              <div className="divide-y divide-gray-100">
                {reviews.map((review) => (
                  <div key={review.id} className="px-6 py-3">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-gray-900">
                        {review.reviewer_name}
                      </span>
                      <Badge
                        variant={
                          review.decision === "accept"
                            ? "success"
                            : review.decision === "reject"
                            ? "danger"
                            : "gray"
                        }
                      >
                        {review.decision}
                      </Badge>
                    </div>
                    {review.comment && (
                      <p className="mt-1 text-sm text-gray-600">
                        {review.comment}
                      </p>
                    )}
                    {review.tags.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {review.tags.map((tag) => (
                          <Badge key={tag} variant="gray">
                            <Tag className="mr-1 h-3 w-3" />
                            {tag}
                          </Badge>
                        ))}
                      </div>
                    )}
                    <p className="mt-1 text-xs text-gray-400">
                      {new Date(review.created_at).toLocaleDateString("en-US", {
                        month: "short",
                        day: "numeric",
                        hour: "numeric",
                        minute: "2-digit",
                      })}
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <div className="px-6 py-6 text-center text-sm text-gray-500">
                No reviews yet
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

// ── AI Tools Panel (Cover Letter + Interview Prep) ──────────────────────────

function AIToolsPanel({ jobId }: { jobId: string; jobTitle: string; companyName: string }) {
  const [showCoverLetter, setShowCoverLetter] = useState(false);
  const [showInterviewPrep, setShowInterviewPrep] = useState(false);
  const [coverLetterTone, setCoverLetterTone] = useState("professional");
  const [copiedCL, setCopiedCL] = useState(false);

  // F236: per-feature usage badge so users see "X of Y left today"
  // before they click the button. Pulls from the cross-cutting
  // `/api/v1/ai/usage` endpoint and refetches on every successful
  // mutation so the count stays in sync without a page reload.
  // `staleTime: 0` keeps the badge truthful — caching here would let
  // the user see "5 left" then 429 on the click because the cached
  // value is stale.
  const aiUsageQuery = useQuery({
    queryKey: ["ai-usage"],
    queryFn: getAIUsage,
    staleTime: 0,
  });

  const coverLetterMutation = useMutation<CoverLetterResult>({
    mutationFn: () => generateCoverLetter(jobId, coverLetterTone),
    onSuccess: () => aiUsageQuery.refetch(),
    onError: () => aiUsageQuery.refetch(),  // 429 + 502 also refresh
  });

  const interviewPrepMutation = useMutation<InterviewPrepResult>({
    mutationFn: () => generateInterviewPrep(jobId),
    onSuccess: () => aiUsageQuery.refetch(),
    onError: () => aiUsageQuery.refetch(),
  });

  // F236: small reusable badge that renders "X of Y left today" inline
  // on the feature button. Returns null when the usage hasn't loaded
  // yet (don't flash "loading…" on the button itself; the text would
  // jump as the query resolves).
  const UsageBadge = ({
    used,
    limit,
  }: {
    used: number;
    limit: number;
  }) => {
    const remaining = Math.max(0, limit - used);
    const exhausted = remaining === 0;
    return (
      <span
        className={`ml-2 rounded-full px-2 py-0.5 text-[10px] font-semibold ${
          exhausted
            ? "bg-red-100 text-red-700"
            : remaining <= Math.max(2, Math.floor(limit * 0.2))
              ? "bg-amber-100 text-amber-700"
              : "bg-gray-100 text-gray-600"
        }`}
        title={`Used ${used} of ${limit} today. Resets midnight UTC.`}
      >
        {exhausted ? "0 left today" : `${remaining}/${limit} left`}
      </span>
    );
  };

  const handleCopyCL = () => {
    if (coverLetterMutation.data?.cover_letter) {
      navigator.clipboard.writeText(coverLetterMutation.data.cover_letter);
      setCopiedCL(true);
      setTimeout(() => setCopiedCL(false), 2000);
    }
  };

  return (
    <Card>
      <h3 className="text-base font-semibold text-gray-900 mb-3">
        <Brain className="inline h-4 w-4 mr-1.5 text-primary-600" />
        AI Tools
      </h3>

      <div className="space-y-2">
        {/* Cover Letter */}
        <button
          onClick={() => {
            setShowCoverLetter(!showCoverLetter);
            if (!showCoverLetter && !coverLetterMutation.data && !coverLetterMutation.isPending) {
              coverLetterMutation.mutate();
            }
          }}
          className="flex w-full items-center justify-between rounded-lg border border-gray-200 p-3 text-left hover:bg-gray-50 transition-colors"
        >
          <div className="flex items-center gap-2">
            <PenTool className="h-4 w-4 text-primary-600" />
            <div>
              <div className="text-sm font-medium text-gray-900 flex items-center">
                Cover Letter
                {aiUsageQuery.data && (
                  <UsageBadge
                    used={aiUsageQuery.data.features.cover_letter.used}
                    limit={aiUsageQuery.data.features.cover_letter.limit}
                  />
                )}
              </div>
              <div className="text-xs text-gray-500">AI-tailored for this job</div>
            </div>
          </div>
          {showCoverLetter ? <ChevronUp className="h-4 w-4 text-gray-400" /> : <ChevronDown className="h-4 w-4 text-gray-400" />}
        </button>

        {showCoverLetter && (
          <div className="rounded-lg border border-gray-100 bg-gray-50 p-3 space-y-3">
            <div className="flex gap-2 items-center">
              <label className="text-xs text-gray-500">Tone:</label>
              <select
                value={coverLetterTone}
                onChange={(e) => setCoverLetterTone(e.target.value)}
                className="rounded border border-gray-200 px-2 py-1 text-xs"
              >
                <option value="professional">Professional</option>
                <option value="enthusiastic">Enthusiastic</option>
                <option value="technical">Technical</option>
                <option value="conversational">Conversational</option>
              </select>
              <button
                onClick={() => coverLetterMutation.mutate()}
                disabled={coverLetterMutation.isPending}
                className="rounded bg-primary-600 px-2 py-1 text-xs text-white hover:bg-primary-700 disabled:opacity-50"
              >
                {coverLetterMutation.isPending ? "Generating..." : coverLetterMutation.data ? "Regenerate" : "Generate"}
              </button>
            </div>

            {coverLetterMutation.isPending && (
              <div className="flex items-center gap-2 py-4 justify-center text-sm text-gray-500">
                <Loader2 className="h-4 w-4 animate-spin" /> Generating cover letter...
              </div>
            )}

            {coverLetterMutation.data && (
              <>
                <div className="relative">
                  <div className="max-h-64 overflow-y-auto rounded bg-white border border-gray-200 p-3 text-sm text-gray-800 whitespace-pre-wrap">
                    {coverLetterMutation.data.cover_letter}
                  </div>
                  <button
                    onClick={handleCopyCL}
                    className="absolute top-2 right-2 rounded bg-white border border-gray-200 p-1.5 text-gray-500 hover:text-primary-600"
                    title="Copy to clipboard"
                  >
                    {copiedCL ? <Check className="h-3.5 w-3.5 text-green-500" /> : <Copy className="h-3.5 w-3.5" />}
                  </button>
                </div>
                {coverLetterMutation.data.key_points.length > 0 && (
                  <div>
                    <div className="text-xs font-medium text-gray-600 mb-1">Key Selling Points:</div>
                    <ul className="space-y-0.5">
                      {coverLetterMutation.data.key_points.map((p: string, i: number) => (
                        <li key={i} className="text-xs text-gray-600 flex gap-1">
                          <span className="text-green-500 mt-0.5">&#10003;</span> {p}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {coverLetterMutation.data.customization_notes && (
                  <p className="text-xs text-gray-500 italic">{coverLetterMutation.data.customization_notes}</p>
                )}
              </>
            )}

            {coverLetterMutation.isError && (
              <div className="rounded bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-700">
                <p className="font-medium">Could not generate cover letter</p>
                <p className="mt-0.5 text-red-600">
                  {(coverLetterMutation.error as Error)?.message || "Please try again."}
                </p>
                <p className="mt-1 text-red-500">
                  Tip: make sure you have an active resume and that the admin has configured an Anthropic API key.
                </p>
              </div>
            )}
          </div>
        )}

        {/* Interview Prep */}
        <button
          onClick={() => {
            setShowInterviewPrep(!showInterviewPrep);
            if (!showInterviewPrep && !interviewPrepMutation.data && !interviewPrepMutation.isPending) {
              interviewPrepMutation.mutate();
            }
          }}
          className="flex w-full items-center justify-between rounded-lg border border-gray-200 p-3 text-left hover:bg-gray-50 transition-colors"
        >
          <div className="flex items-center gap-2">
            <MessageSquare className="h-4 w-4 text-purple-600" />
            <div>
              <div className="text-sm font-medium text-gray-900 flex items-center">
                Interview Prep
                {aiUsageQuery.data && (
                  <UsageBadge
                    used={aiUsageQuery.data.features.interview_prep.used}
                    limit={aiUsageQuery.data.features.interview_prep.limit}
                  />
                )}
              </div>
              <div className="text-xs text-gray-500">Questions, talking points, research</div>
            </div>
          </div>
          {showInterviewPrep ? <ChevronUp className="h-4 w-4 text-gray-400" /> : <ChevronDown className="h-4 w-4 text-gray-400" />}
        </button>

        {showInterviewPrep && (
          <div className="rounded-lg border border-gray-100 bg-gray-50 p-3 space-y-3">
            {interviewPrepMutation.isPending && (
              <div className="flex items-center gap-2 py-4 justify-center text-sm text-gray-500">
                <Loader2 className="h-4 w-4 animate-spin" /> Preparing interview guide...
              </div>
            )}

            {interviewPrepMutation.data && (
              <>
                {/* Questions */}
                <div>
                  <div className="text-xs font-semibold text-gray-700 mb-2 uppercase tracking-wide">
                    Likely Questions ({interviewPrepMutation.data.questions?.length || 0})
                  </div>
                  <div className="space-y-2 max-h-72 overflow-y-auto">
                    {interviewPrepMutation.data.questions?.map((q: any, i: number) => (
                      <InterviewQuestionCard key={i} q={q} />
                    ))}
                  </div>
                </div>

                {/* Talking Points */}
                {interviewPrepMutation.data.talking_points?.length > 0 && (
                  <div>
                    <div className="text-xs font-semibold text-gray-700 mb-2 uppercase tracking-wide">
                      Your Talking Points
                    </div>
                    <div className="space-y-1.5">
                      {interviewPrepMutation.data.talking_points.map((tp: any, i: number) => (
                        <div key={i} className="rounded bg-white border border-gray-200 p-2">
                          <div className="text-xs font-medium text-gray-900">{tp.topic}</div>
                          <div className="text-xs text-gray-500 mt-0.5">{tp.how_to_present}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Red Flags */}
                {interviewPrepMutation.data.red_flags?.length > 0 && (
                  <div>
                    <div className="text-xs font-semibold text-amber-700 mb-1 uppercase tracking-wide">
                      Prepare For
                    </div>
                    {interviewPrepMutation.data.red_flags.map((rf: string, i: number) => (
                      <div key={i} className="text-xs text-amber-700 flex gap-1">
                        <AlertTriangle className="h-3 w-3 mt-0.5 flex-shrink-0" /> {rf}
                      </div>
                    ))}
                  </div>
                )}

                {/* Questions to Ask */}
                {interviewPrepMutation.data.company_research?.length > 0 && (
                  <div>
                    <div className="text-xs font-semibold text-gray-700 mb-1 uppercase tracking-wide">
                      Smart Questions to Ask
                    </div>
                    {interviewPrepMutation.data.company_research.map((cr: any, i: number) => (
                      <div key={i} className="text-xs text-gray-700 mb-1">
                        <span className="font-medium text-primary-700">Q:</span> {cr.question_to_ask}
                      </div>
                    ))}
                  </div>
                )}

                <button
                  onClick={() => interviewPrepMutation.mutate()}
                  disabled={interviewPrepMutation.isPending}
                  className="w-full rounded bg-purple-600 px-3 py-1.5 text-xs text-white hover:bg-purple-700 disabled:opacity-50"
                >
                  Regenerate
                </button>
              </>
            )}

            {interviewPrepMutation.isError && (
              <div className="rounded bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-700">
                <p className="font-medium">Could not generate interview prep</p>
                <p className="mt-0.5 text-red-600">
                  {(interviewPrepMutation.error as Error)?.message || "Please try again."}
                </p>
                <p className="mt-1 text-red-500">
                  Tip: make sure you have an active resume and that the admin has configured an Anthropic API key.
                </p>
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function InterviewQuestionCard({ q }: { q: any }) {
  const [expanded, setExpanded] = useState(false);
  const catColors: Record<string, string> = {
    technical: "bg-blue-100 text-blue-700",
    behavioral: "bg-green-100 text-green-700",
    situational: "bg-purple-100 text-purple-700",
    culture_fit: "bg-amber-100 text-amber-700",
  };
  return (
    <div className="rounded bg-white border border-gray-200 p-2">
      <button onClick={() => setExpanded(!expanded)} className="w-full text-left">
        <div className="flex items-start gap-2">
          <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium flex-shrink-0 ${catColors[q.category] || "bg-gray-100 text-gray-700"}`}>
            {q.category?.replace("_", " ")}
          </span>
          <span className="text-xs font-medium text-gray-900">{q.question}</span>
        </div>
      </button>
      {expanded && (
        <div className="mt-2 pl-2 space-y-1.5 border-l-2 border-primary-200">
          <div className="text-xs text-gray-600"><span className="font-medium">Suggested:</span> {q.suggested_answer}</div>
          <div className="text-xs text-gray-500"><span className="font-medium">Tips:</span> {q.tips}</div>
        </div>
      )}
    </div>
  );
}
