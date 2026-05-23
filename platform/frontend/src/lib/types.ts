export interface User {
  id: string;
  email: string;
  name: string;
  picture: string;
  role: "super_admin" | "admin" | "reviewer" | "viewer";
  active_resume_id?: string;
  has_password?: boolean;
  // F247: when a super_admin force-resets the user's password via
  // POST /users/{id}/reset-password, this flag is set to true. The
  // app routes to the change-password screen on the very next page
  // load and locks navigation until the user picks a new password.
  // Optional in the type so older API responses (without the field)
  // default to "no force-change required".
  must_change_password?: boolean;
  created_at: string;
}

// ─── Work-time window (admin-set IST shifts) ──────────────────────
//
// Drives both the per-user lock-out screen and the admin /work-windows
// page. ``server_now_utc`` is included so the lock-out countdown is
// computed against the server's clock, not the (possibly skewed)
// client clock.
export interface WorkWindowState {
  enabled: boolean;
  start_ist: string; // "HH:MM" 24-hour, IST
  end_ist: string;   // "HH:MM"
  override_until: string | null; // ISO UTC, or null
  within_window_now: boolean;
  server_now_utc: string; // ISO UTC
}

export interface WorkTimeExtensionRequest {
  id: string;
  user_id: string;
  user_name: string;
  user_email: string;
  requested_minutes: number;
  reason: string;
  status: "pending" | "approved" | "denied";
  requested_at: string;
  decided_by_user_id: string | null;
  decided_at: string | null;
  decision_note: string;
  approved_until: string | null;
}

export interface WorkTimeExtensionRequestList {
  items: WorkTimeExtensionRequest[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface ManagedUser {
  id: string;
  email: string;
  name: string;
  avatar_url: string;
  role: "super_admin" | "admin" | "reviewer" | "viewer";
  is_active: boolean;
  has_password: boolean;
  has_google: boolean;
  created_at: string | null;
  last_login_at: string | null;
}

export interface RoleInfo {
  name: string;
  description: string;
}

export interface ScanTaskResult {
  task_id: string;
  status: string;
  platform?: string;
  scope?: string;
  boards?: number;
  board?: string;
}

export interface ScanTaskStatus {
  task_id: string;
  status: string;
  result?: any;
}

export type JobStatus =
  | "new"
  | "under_review"
  | "accepted"
  | "rejected"
  | "expired";

export interface Job {
  id: string;
  company_id: string;
  company_name: string;
  title: string;
  url: string;
  source_platform: string;
  remote_scope: string;
  location_restriction: string;
  employment_type: string | null;
  salary_range: string | null;
  status: JobStatus;
  relevance_score: number;
  role_cluster: "infra" | "security";
  geography_bucket: "global_remote" | "usa_only" | "uae_only";
  // d0e1f2g3h4i5: replaces ``geography_bucket`` with a richer
  // vocabulary. Both columns are populated by the classifier during
  // the transition window. Optional because older API responses
  // (before the rolling deploy completes) won't have it.
  remote_policy?:
    | "worldwide"
    | "country_restricted"
    | "region_restricted"
    | "hybrid"
    | "onsite"
    | "unknown";
  remote_policy_countries?: string[];
  tags: string[];
  resume_score?: number | null;
  // The CURRENT reviewer's active-resume fit — set by /jobs/review-queue
  // (per-user score after F248; pre-fix this was MAX across every team
  // resume which surfaced wrong-role jobs to the wrong reviewer). `null`
  // means the reviewer's active resume hasn't been scored against this
  // job yet — the daily ``rescore_active_resumes`` task will populate.
  your_resume_score?: number | null;
  // Backward-compat alias for ``your_resume_score``. Older clients +
  // tabs opened before the F248 deploy may still read this key. Set to
  // the same per-reviewer value, NOT the team-wide max.
  max_resume_score?: number | null;
  first_seen_at?: string;
  // F97/F101: true when the backend has a populated JobDescription
  // row for this job (text_content or html_content). When false, the
  // UI should render a "limited data" badge — resume scoring against
  // an empty description falls back to cluster-baseline keywords and
  // produces low-resolution per-job scores. Optional because list
  // endpoints don't currently return it; only `/jobs/{id}` does.
  has_description?: boolean;
  resume_fit?: {
    overall_score: number;
    keyword_score: number;
    role_match_score: number;
    format_score: number;
    matched_keywords: string[];
    missing_keywords: string[];
    suggestions: string[];
  } | null;
  scraped_at: string;
  created_at: string;
  updated_at: string;
}

export interface JobDescription {
  id: string;
  job_id: string;
  raw_text: string;
  // Regression finding 168: previously declared `parsed_requirements`,
  // `parsed_nice_to_have`, `parsed_tech_stack` — backend shipped them
  // as always-empty arrays (no extraction ever wired up) and the UI
  // rendered them behind `length > 0` guards that were never true.
  // Fields removed from both sides of the contract; add back together
  // with a real parser if that work lands.
}

export interface Review {
  id: string;
  job_id: string;
  reviewer_id: string;
  reviewer_name: string;
  decision: "accept" | "reject" | "skip";
  comment: string;
  tags: string[];
  created_at: string;
}

export interface Company {
  id: string;
  name: string;
  slug: string;
  website: string | null;
  logo_url: string;
  industry: string;
  employee_count: string;
  funding_stage: string;
  headquarters: string;
  description: string;
  is_target: boolean;
  tags: string[];
  domain: string;
  founded_year: number | null;
  total_funding: string;
  linkedin_url: string;
  twitter_url: string;
  tech_stack: string[];
  enrichment_status: "pending" | "enriching" | "enriched" | "failed";
  enriched_at: string | null;
  funded_at: string | null;
  funding_news_url: string;
  ats_boards: ATSBoard[];
  job_count: number;
  accepted_count: number;
  contact_count: number;
  created_at: string;
  updated_at: string;
}

export type OutreachStatus = "not_contacted" | "emailed" | "replied" | "meeting_scheduled" | "not_interested";

export interface CompanyContact {
  id: string;
  company_id: string;
  first_name: string;
  last_name: string;
  title: string;
  role_category: string;
  department: string;
  seniority: string;
  email: string;
  email_status: "unverified" | "valid" | "invalid" | "catch_all";
  email_verified_at: string | null;
  phone: string;
  linkedin_url: string;
  twitter_url: string;
  telegram_id: string;
  source: string;
  confidence_score: number;
  is_decision_maker: boolean;
  outreach_status: OutreachStatus;
  outreach_note: string;
  last_outreach_at: string | null;
  last_verified_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface FundingSignal {
  company_id: string;
  company_name: string;
  domain: string;
  industry: string;
  total_funding: string;
  funding_stage: string;
  funded_at: string | null;
  days_since_funded: number | null;
  funding_news_url: string;
  employee_count: string;
  is_target: boolean;
  total_contacts: number;
  decision_makers: number;
  verified_contacts: number;
  open_roles: number;
}

export interface WarmLead {
  company_id: string;
  company_name: string;
  domain: string;
  industry: string;
  total_funding: string;
  funding_stage: string;
  is_target: boolean;
  new_jobs_30d: number;
  new_jobs_7d: number;
  top_relevance_score: number;
  total_contacts: number;
  verified_contacts: number;
  decision_makers: number;
}

export interface CompanyOffice {
  id: string;
  company_id: string;
  label: string;
  address: string;
  city: string;
  state: string;
  country: string;
  is_headquarters: boolean;
  source: string;
}

export interface CompanyDetail extends Company {
  contacts: CompanyContact[];
  offices: CompanyOffice[];
  actively_hiring: boolean;
  hiring_velocity: string;
  total_open_roles: number;
  enrichment_error: string;
}

export interface JobRelevantContact {
  contact: CompanyContact;
  relevance_reason: string;
  relevance_score: number;
}

export interface ATSBoard {
  id: string;
  company_id: string;
  platform: string;
  board_url: string;
  last_scraped_at: string | null;
}

export type PipelineStage = string;

export interface PipelineStageConfig {
  id: string;
  key: string;
  label: string;
  color: string;
  sort_order: number;
  is_active: boolean;
}

export interface PipelineItem {
  id: string;
  company_id: string | null;
  company_name: string;
  company_website: string;
  stage: PipelineStage;
  priority: number;
  assigned_to: string | null;
  resume_id: string | null;
  applied_by: string | null;
  applied_by_name: string | null;
  resume_label: string | null;
  notes: string;
  created_at: string;
  hiring_velocity: "high" | "medium" | "low";
  total_open_roles: number;
  accepted_jobs_count: number;
  // F266 — count of submitted applications under this card's company
  // (denormalised via Application.company_id, F261). Surfaced as a
  // small badge on the card so admins can see at a glance which
  // targets actually have apply activity. Defaults to 0 if older
  // backends omit the field.
  applications_count?: number;
  last_job_at: string | null;
}

// F215: backend `/pipeline` now returns canonical `items: PipelineItem[]`
// (flat list) + `by_stage: Record<string, PipelineItem[]>` (kanban view).
// PipelinePage renders the kanban columns so it reads `by_stage`. Any
// future consumer that wants a generic pager can use `items`.
export interface PipelineResponse {
  stages: string[];
  stages_config: { key: string; label: string; color: string }[];
  items: PipelineItem[];
  by_stage: Record<string, PipelineItem[]>;
  total: number;
}

export interface AnalyticsOverview {
  // Flow metric — jobs first_seen_at within the last `days` window
  // (default 30, set on the backend by F214). Use this for "Last N
  // days" displays.
  total_jobs: number;
  // Cardinality metric — lifetime count of rows in the jobs table.
  // Use this for tiles that read as "how many jobs exist", matching
  // /monitoring's data_counts.jobs.
  total_jobs_lifetime: number;
  total_companies: number;
  pipeline_active: number;
  reviewed_count: number;
  accepted_count: number;
  rejected_count: number;
  acceptance_rate: number;
  avg_relevance_score: number;
}

export interface SourceDistribution {
  platform: string;
  count: number;
}

export interface TrendDataPoint {
  day: string;
  date?: string;
  total: number;
  infra: number;
  security: number;
  accepted: number;
  // legacy
  count?: number;
  new_jobs?: number;
  rejected?: number;
}

export interface FunnelStep {
  stage: string;
  count: number;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// Review queue: prioritized list + per-date-bucket counts so the page
// can render "N today · N yesterday · N older" chips above the card.
// The three counts reconcile exactly with `total` because the stats
// query uses the same WHERE chain as the items query.
export interface ReviewQueueResponse extends PaginatedResponse<Job> {
  stats: {
    today: number;
    yesterday: number;
    older: number;
    total: number;
  };
}

// Multi-column sort: each entry is a (column key, direction) pair, in
// priority order. Position 0 is the primary sort, position 1 the
// secondary tiebreaker, etc. Serialised on the wire as
// `sort_by=relevance_score:desc,first_seen_at:desc` (comma-separated
// `key:dir` pairs); the backend parser at `jobs.py:_parse_sort_spec`
// validates each segment.
export interface SortKey {
  key: string;
  dir: "asc" | "desc";
}

export interface JobFilters {
  search?: string;
  status?: JobStatus | "";
  platform?: string;
  geography?: string;
  // d0e1f2g3h4i5: new filter vocabulary. Both legacy (`geography`)
  // and new (`remote_policy` + `remote_country`) are accepted by the
  // backend during the transition window. New code should prefer the
  // new fields.
  remote_policy?:
    | "worldwide"
    | "country_restricted"
    | "region_restricted"
    | "hybrid"
    | "onsite"
    | "unknown";
  remote_country?: string; // ISO-3166 alpha-2, e.g. "US"
  role_cluster?: string;
  // Regression finding 87: mirrors the backend `is_classified` query
  // param added alongside the configurable role-cluster catalog.
  // `true` → only jobs with a non-null/non-empty cluster; `false` →
  // only the unclassified pool (~90% of the DB). Used by the JobsPage
  // dropdown's synthetic "Unclassified" option and by the Monitoring
  // dashboard's unclassified count tile (which links here).
  is_classified?: boolean;
  // Authoritative sort state — when present, takes precedence over
  // the legacy `sort_by` / `sort_dir` pair. The API layer in
  // `lib/api.ts` serialises `sorts` into the wire-format `sort_by`
  // string (`key:dir,key:dir,...`) and drops `sort_dir`.
  sorts?: SortKey[];
  // Legacy single-sort fields. Kept so non-JobsPage callers (and any
  // pre-multi-sort code paths) continue to work unchanged. When
  // `sorts` is also set, `sorts` wins.
  sort_by?: string;
  sort_dir?: string;
  page?: number;
  page_size?: number;
}

// Regression finding 69: the bulk endpoint now accepts either a job_ids
// list (existing behavior — preserved for per-page select-all) OR a
// filter criteria object (new — for "Select all N matching"). Callers
// pick one path and the server rejects ambiguous or empty requests.
// Using a discriminated union guarantees the TS layer enforces the
// same XOR at the callsite, so a caller can't accidentally send both
// and rely on the backend to 400 it.
export interface BulkFilterCriteria {
  status?: string;
  platform?: string;
  geography_bucket?: string;
  role_cluster?: string;
  is_classified?: boolean;
  search?: string;
  company_id?: string;
}

// F69 follow-up — `action` must match the backend's `JobStatusLiteral`
// (tightened in F99: `new | under_review | accepted | rejected | hidden
// | archived`). Before this fix, the frontend sent verbs (`accept |
// reject | reset`) that didn't match the Literal, and every bulk action
// 422'd in prod with `literal_error` — silently, because the UI showed
// the loading spinner, logged the error to the console, and invalidated
// the query regardless. Aligning types here; the verb→status mapping
// lives at the JobsPage call site so button labels stay human-friendly.
export type BulkActionStatus =
  | "new"
  | "under_review"
  | "accepted"
  | "rejected"
  | "hidden"
  | "archived";

export type BulkActionPayload =
  | {
      job_ids: string[];
      action: BulkActionStatus;
      filter?: never;
    }
  | {
      filter: BulkFilterCriteria;
      action: BulkActionStatus;
      job_ids?: never;
    };

export interface ReviewPayload {
  decision: "accept" | "reject" | "skip";
  comment: string;
  tags: string[];
}

export interface ScoreBreakdownSignal {
  signal: string;
  weight: number;
  raw: number;
  weighted: number;
  detail: string;
}

export interface ScoreBreakdown {
  total: number;
  breakdown: ScoreBreakdownSignal[];
}

export interface PlatformLastRun {
  source: string;
  started_at: string | null;
  completed_at: string | null;
  jobs_found: number;
  new_jobs: number;
  updated_jobs: number;
  errors: number;
  error_message: string;
}

export interface PlatformStats {
  name: string;
  total_boards: number;
  active_boards: number;
  total_jobs: number;
  new_jobs: number;
  accepted_jobs: number;
  rejected_jobs: number;
  avg_score: number;
  last_scan: string | null;
  total_errors: number;
  // F250: stats from the SINGLE most-recent scan_logs row for this
  // platform. Null for platforms that have never scanned (newly seeded
  // boards). The Platforms UI renders "175 found · 0 new · 12m ago" on
  // each card so admins don't need to expand the Scan Logs panel just
  // to see how the last run went.
  last_run?: PlatformLastRun | null;
}

export interface PlatformBoard {
  id: string;
  company_id: string;
  company_name: string;
  platform: string;
  slug: string;
  is_active: boolean;
  last_scanned_at: string | null;
}

export interface ScanLogEntry {
  id: string;
  source: string;
  platform: string;
  started_at: string;
  completed_at: string | null;
  jobs_found: number;
  new_jobs: number;
  updated_jobs: number;
  errors: number;
  error_message: string;
  duration_ms: number;
}

export interface Resume {
  id: string;
  label: string;
  filename: string;
  file_type: "pdf" | "docx";
  word_count: number;
  status: "processing" | "ready" | "error";
  uploaded_at: string;
  text_preview?: string;
  is_active?: boolean;
  // Whether the original uploaded bytes are stored (b8c9d0e1f2g3+).
  // Drives the Preview button's behaviour: true → render the file via
  // ``GET /resume/{id}/file``; false → fall back to the extracted-text
  // view (legacy resumes). Optional for older API responses that
  // pre-date the migration; treat ``undefined`` as ``false``.
  has_file_data?: boolean;
}

export interface ResumeScore {
  id: string;
  job_id: string;
  job_title: string;
  company_name: string;
  role_cluster: string;
  overall_score: number;
  keyword_score: number;
  role_match_score: number;
  format_score: number;
  matched_keywords: string[];
  missing_keywords: string[];
  suggestions: string[];
  scored_at?: string;
}

export interface ResumeScoreSummary {
  resume_id: string;
  scores: ResumeScore[];
  average_score: number;
  best_score?: number;
  above_70?: number;
  top_missing_keywords?: string[];
  jobs_scored: number;
  total_filtered?: number;
  page?: number;
  page_size?: number;
  total_pages?: number;
}

export interface ResumeScoreTaskResponse {
  task_id: string;
  resume_id: string;
  status: string;
  message: string;
}

export interface ResumeScoreTaskStatus {
  status: "pending" | "progress" | "completed" | "failed";
  current?: number;
  total?: number;
  jobs_scored?: number;
  error?: string;
}

export interface ResumeCustomization {
  resume_id: string;
  job_id: string;
  job_title: string;
  target_score: number;
  customized_text: string;
  changes_made: string[];
  improvement_notes: string;
  error: boolean;
}

// F236: per-feature AI usage snapshot returned by GET /api/v1/ai/usage
// and embedded in the post-call responses on the three AI generation
// endpoints. `reset_at_utc` is the START of today's window (midnight
// UTC); the next reset is +24h. `has_api_key` mirrors the public
// /api/health flag — when false, all three features 503 immediately
// without burning quota.
export interface AIFeatureUsage {
  used: number;
  limit: number;
  remaining: number;
}

export interface AIUsage {
  has_api_key: boolean;
  reset_at_utc: string; // ISO timestamp, start of today's UTC window
  features: {
    customize: AIFeatureUsage;
    cover_letter: AIFeatureUsage;
    interview_prep: AIFeatureUsage;
  };
}

// F237: AI Intelligence — per-user insights + admin product insights.
// Both share the {title, body, severity, category, action_link?} shape
// so the same renderer can show either; only the source endpoint and
// admin-controls differ.
export interface InsightItem {
  title: string;
  body: string;
  severity: "info" | "tip" | "warning" | "low" | "medium" | "high";
  category: string;
  action_link?: string;
}

export interface UserInsightBundle {
  generation_id: string;
  generated_at: string;
  insights: InsightItem[];
  model_version: string;
  prompt_version: string;
}

export interface UserInsightsResponse {
  latest: UserInsightBundle | null;
  history: UserInsightBundle[];
}

export interface ProductInsight {
  id: string;
  generation_id: string;
  title: string;
  body: string;
  category: string;
  severity: "low" | "medium" | "high";
  generated_at: string;
  actioned_at: string | null;
  actioned_status: "actioned" | "dismissed" | "duplicate" | null;
  actioned_note: string | null;
  actioned_by: string | null;
  model_version: string;
  prompt_version: string;
}

export interface ProductInsightsResponse {
  items: ProductInsight[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// F238: training-data capture pipeline. Stats endpoint returns
// per-task counts + class balance. Used by the admin Monitoring tile.
export type TrainingTaskType =
  | "resume_match"
  | "role_classify"
  | "cover_letter_quality"
  | "interview_prep_quality"
  | "customize_quality"
  | "search_intent";

export interface TrainingTaskStats {
  total: number;
  by_class: Record<string, number>;
}

export interface TrainingDataStats {
  by_task_type: Record<TrainingTaskType, TrainingTaskStats>;
  total_rows: number;
  earliest: string | null;
  latest: string | null;
}

// F241: saved filter presets — per-user, persisted to backend.
// `filters` is a free-form JobFilters dict (the same shape JobsPage
// keeps in `useState<JobFilters>`). Backend stores as JSONB so adding
// a new filter axis (e.g. company_size) doesn't need a schema change.
export interface SavedFilter {
  id: string;
  name: string;
  filters: JobFilters;
  created_at: string;
  updated_at: string;
}

export interface SavedFiltersResponse {
  items: SavedFilter[];
  total: number;
}

export interface CompanyScore {
  company_id: string;
  company_name: string;
  is_target: boolean;
  total_jobs: number;
  relevant_jobs: number;
  remote_jobs: number;
  avg_relevance_score: number;
  company_score: number;
}

export interface RoleCluster {
  id: string;
  name: string;
  display_name: string;
  is_relevant: boolean;
  is_active: boolean;
  keywords: string;
  approved_roles: string;
  keywords_list: string[];
  approved_roles_list: string[];
  sort_order: number;
  created_at: string | null;
  updated_at: string | null;
}

// Resume Persona
export interface ActiveResume {
  id: string;
  label: string;
  filename: string;
  file_type: string;
  word_count: number;
  status: string;
  uploaded_at: string;
  has_file_data?: boolean;
  score_summary: {
    jobs_scored: number;
    average_score: number;
    best_score: number;
    above_70: number;
    // F96: ISO timestamp of the most recent ResumeScore row. `null`
    // means no scores yet (fresh upload, scoring task queued or
    // pending). The frontend can render "Scoring…" when null and
    // "Scored N days ago / Rescore now" when the age crosses a
    // staleness threshold (e.g. >7 days) — before this was a
    // response field, the UI had no way to distinguish a healthy
    // rescore cycle from a silent failure.
    last_scored_at?: string | null;
  };
}

// Platform Credentials
export interface PlatformCredential {
  id: string;
  resume_id: string;
  platform: string;
  email: string;
  has_password: boolean;
  profile_url: string;
  is_verified: boolean;
  last_used_at: string | null;
  created_at: string;
}

// Answer Book
export type AnswerCategory = "personal_info" | "work_auth" | "experience" | "skills" | "preferences" | "custom";

export interface AnswerBookEntry {
  id: string;
  user_id: string;
  resume_id: string | null;
  category: AnswerCategory;
  question: string;
  question_key: string;
  answer: string;
  source: string;
  is_override: boolean;
  usage_count: number;
  created_at: string;
  updated_at: string;
  // v6 Claude Routine Apply — routine-required entries are locked so
  // the user can only edit the answer (not the question/category) and
  // the DELETE endpoint rejects. Optional because legacy rows (seeded
  // before is_locked landed) may not have the flag populated in API
  // responses until the backend is fully upgraded.
  is_locked?: boolean;
}

// Applications
export type ApplicationStatus = "prepared" | "submitted" | "applied" | "interview" | "offer" | "rejected" | "withdrawn";
// `claude_routine` added for the v6 MCP-Chrome routine. Still a
// union — ApplicationsPage renders an "Auto" badge for this value.
export type ApplyMethod = "api_submit" | "manual_copy" | "career_page" | "claude_routine";
// `routine` added for v6 — matches Application.submission_source in
// the backend. Applications filed by the routine set this value.
export type ApplicationSubmissionSource = "manual_prepare" | "review_queue" | "routine";

export interface Application {
  id: string;
  job_id: string;
  job_title: string;
  company_name: string;
  platform: string;
  job_url: string;
  resume_id: string;
  resume_label: string;
  status: ApplicationStatus;
  apply_method: ApplyMethod;
  applied_at: string | null;
  submitted_at: string | null;
  created_at: string;
  notes: string;
  // Feature C — provenance + top-level snapshot score. Full snapshot
  // (resume text + components) is only on GET /applications/{id}.
  submission_source?: ApplicationSubmissionSource;
  applied_resume_score_overall?: number | null;
}

// Full application detail — shape of GET /applications/{id}. Adds the
// apply-time snapshot columns introduced for Feature C.
export interface ApplicationDetail {
  id: string;
  job: { id: string; title: string; company_name: string; platform: string; url: string };
  resume: { id: string; label: string };
  status: ApplicationStatus;
  apply_method: ApplyMethod;
  prepared_answers: unknown[];
  submitted_at: string | null;
  applied_at: string | null;
  platform_response: unknown;
  notes: string;
  created_at: string;
  submission_source: ApplicationSubmissionSource;
  applied_resume_text: string | null;
  applied_resume_score_snapshot: {
    overall: number;
    keyword: number;
    role_match: number;
    format: number;
  } | null;
  ai_customization_log_id: string | null;
}

export interface ApplicationStats {
  total: number;
  prepared: number;
  submitted: number;
  applied: number;
  interview: number;
  offer: number;
  rejected: number;
  withdrawn: number;
}

// F261 — Team Pipeline Tracker. Row shape returned by both
// ``GET /applications/team`` (admin team feed) and
// ``GET /pipeline/{client_id}/applications`` (drill-down under a
// pipeline card). Same shape so one row component can render both.
//
// Differs from ``Application`` (per-user list) in two ways:
//   - applicant identity is denormalised into the row so an HR-
//     reply triage doesn't need a second fetch
//   - ``stage_key`` is present (the configurable pipeline stage,
//     e.g. ``"interview_1"``) — separate from ``status`` (the
//     apply-state machine).
export interface TeamApplicationItem {
  id: string;
  job_id: string;
  job_title: string;
  job_url: string;
  platform: string;
  company_id: string | null;
  company_name?: string; // present on /applications/team, omitted on drill-down (caller knows the company)
  user_id: string;
  applicant_name: string;
  applicant_email: string;
  resume_id: string;
  resume_label: string;
  status: ApplicationStatus;
  stage_key: string | null;
  apply_method?: ApplyMethod;
  submission_source?: ApplicationSubmissionSource;
  applied_at: string | null;
  submitted_at: string | null;
  created_at: string;
  notes: string;
}

export interface TeamApplicationsResponse {
  items: TeamApplicationItem[];
  total: number;
  page?: number;
  page_size?: number;
  total_pages?: number;
}

export interface PreparedAnswer {
  field_key: string;
  label: string;
  field_type: "text" | "textarea" | "select" | "multi_select" | "file" | "boolean";
  required: boolean;
  options: { value: string; label: string }[];
  description: string;
  answer: string;
  match_source: "base" | "override" | "unmatched";
  question_key: string;
  confidence: "high" | "medium" | "low";
}

export interface ApplyReadiness {
  resume: { ready: boolean; id?: string; label?: string };
  credentials: { ready: boolean; platform: string; email?: string };
  answer_book: { ready: boolean; count: number };
  resume_score: { available: boolean; score?: number };
  existing_application: { exists: boolean; id?: string | null; status?: string | null };
  can_apply: boolean;
}

export interface PreparedQuestion {
  field_key: string;
  label: string;
  field_type: string;
  required: boolean;
  options: string[];
  description: string;
  answer: string;
  match_source: string;
  question_key: string;
  confidence: "high" | "medium" | "low";
}

export interface JobQuestionsPreview {
  questions: PreparedQuestion[];
  coverage: {
    total: number;
    answered: number;
    high_confidence: number;
    new_entries: number;
  };
}

// Feedback
export type FeedbackCategory = "bug" | "feature_request" | "improvement" | "question";
export type FeedbackPriority = "low" | "medium" | "high" | "critical";
export type FeedbackStatus = "open" | "in_progress" | "resolved" | "closed";

export interface FeedbackAttachment {
  filename: string;
  original_name: string;
  size: number;
  content_type: string;
  uploaded_at: string;
}

export interface FeedbackUser {
  id: string;
  email: string;
  name: string;
  avatar_url: string;
}

export interface Feedback {
  id: string;
  user_id: string;
  category: FeedbackCategory;
  priority: FeedbackPriority;
  status: FeedbackStatus;
  title: string;
  description: string;
  steps_to_reproduce: string | null;
  expected_behavior: string | null;
  actual_behavior: string | null;
  use_case: string | null;
  proposed_solution: string | null;
  impact: string | null;
  screenshot_url?: string;
  attachments: FeedbackAttachment[];
  admin_notes: string | null;
  resolved_by: string | null;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  user: FeedbackUser | null;
  resolver: FeedbackUser | null;
}

export interface FeedbackCreate {
  category: FeedbackCategory;
  priority: FeedbackPriority;
  title: string;
  description: string;
  steps_to_reproduce?: string;
  expected_behavior?: string;
  actual_behavior?: string;
  use_case?: string;
  proposed_solution?: string;
  impact?: string;
  screenshot_url?: string;
}

// ── Alert Configuration ─────────────────────────────────────────────────────
export interface AlertConfig {
  id: string;
  channel: string;
  webhook_url: string;
  min_relevance_score: number;
  role_clusters: string[] | null;
  geography_filter: string | null;
  is_active: boolean;
  last_triggered_at: string | null;
  created_at: string;
}

// ── Cover Letter ────────────────────────────────────────────────────────────
export interface CoverLetterResult {
  cover_letter: string;
  key_points: string[];
  customization_notes: string;
  tone: string;
  job_title: string;
  company_name: string;
}

// ── Interview Prep ──────────────────────────────────────────────────────────
export interface InterviewQuestion {
  category: string;
  question: string;
  why_asked: string;
  suggested_answer: string;
  tips: string;
}

export interface InterviewTalkingPoint {
  topic: string;
  relevance: string;
  how_to_present: string;
}

export interface InterviewResearchItem {
  topic: string;
  why: string;
  question_to_ask: string;
}

export interface InterviewPrepResult {
  job_title: string;
  company_name: string;
  questions: InterviewQuestion[];
  talking_points: InterviewTalkingPoint[];
  company_research: InterviewResearchItem[];
  red_flags: string[];
}

// ── Intelligence ────────────────────────────────────────────────────────────
export interface SkillGapItem {
  skill: string;
  category: string;
  demand_count: number;
  demand_pct: number;
  on_resume: boolean;
  gap: boolean;
}

export interface SkillGapResponse {
  skills: SkillGapItem[];
  summary: {
    jobs_analyzed: number;
    total_skills_tracked: number;
    skills_on_resume: number;
    skills_missing: number;
    coverage_pct: number;
  };
  top_missing: SkillGapItem[];
  category_breakdown: { category: string; total: number; have: number; missing: number }[];
  has_resume: boolean;
}

export interface SalaryInsights {
  overall: { min: number; max: number; avg: number; median: number; count: number };
  by_cluster: Record<string, { min: number; max: number; avg: number; median: number; count: number }>;
  by_geography: Record<string, { min: number; max: number; avg: number; median: number; count: number }>;
  distribution: { range: string; count: number }[];
  top_paying: { min: number; max: number; mid: number; title: string; company: string; role_cluster: string; raw: string }[];
  total_with_salary: number;
  total_jobs: number;
}

export interface TimingIntelligence {
  posting_by_day: { day: string; count: number }[];
  posting_by_hour: { hour: number; count: number }[];
  freshness_distribution: { bucket: string; count: number }[];
  avg_review_hours: number;
  platform_velocity: { platform: string; total_90d: number; last_7d: number; last_30d: number }[];
  recommendations: {
    best_day: string;
    peak_posting_hours: string;
    ideal_apply_window: string;
    // F65: backend-computed metadata accompanying `ideal_apply_window`.
    // `_data_driven=true` means the text was derived from real accepted-
    // review timings and the UI can show it without a disclaimer;
    // `=false` means it's the generic heuristic fallback because sample
    // size was too small. `_sample_size` drives the "low-confidence"
    // badge when < 30 accepted reviews backed the number; the two
    // *_hours fields are surfaced as a subtitle so viewers can see the
    // actual median/p75 that produced the range.
    ideal_apply_window_data_driven?: boolean;
    ideal_apply_window_sample_size?: number;
    ideal_apply_window_median_hours?: number | null;
    ideal_apply_window_p75_hours?: number | null;
    fastest_platforms: string[];
  };
}

export interface NetworkingSuggestion {
  contact_id: string;
  name: string;
  title: string;
  company: string;
  company_id?: string;
  email: string;
  email_status: string;
  linkedin_url: string;
  is_decision_maker: boolean;
  outreach_status: string;
  relevance_reason: string;
  relevance_score?: number;
  suggested_approach: string;
  open_roles?: number;
  top_relevance_score?: number;
}

// ── VM monitoring (from /api/v1/monitoring/vm) ──────────────────────────────

export interface VmGuardrail {
  name: string;
  severity: "critical" | "warn" | "info";
  message: string;
}

export interface VmContainer {
  name: string;
  image: string;
  state: string;   // running|exited|paused|...
  status: string;  // human-readable (e.g. "Up 2 days (healthy)")
  started_at: string;
}

export interface VmContainerStat {
  name: string;
  cpu_percent: number;
  memory_percent: number;
  memory_usage: string;  // raw "123MiB / 1GiB" from docker stats
  net_io: string;        // raw "1.5MB / 2.3MB"
  block_io: string;      // raw "100kB / 0B"
  pids: number;
}

export interface VmTopProcess {
  pid: number;
  user: string;
  cpu_percent: number;
  memory_percent: number;
  command: string;
}

export interface VmMount {
  mount: string;
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  used_percent: number;
}

export interface VmLastDeploy {
  release: string;
  previous: string | null;
  deployed_at: string;
}

export interface VmMetricsUnavailable {
  available: false;
  reason: string;
  free_tier: {
    max_ocpus: number;
    max_memory_gb: number;
    max_disk_gb: number;
    max_egress_tb_month: number;
  };
}

export interface VmMetricsAvailable {
  available: true;
  overall_status: "ok" | "warn" | "critical";
  snapshot_age_seconds: number | null;
  timestamp: string;
  host_uptime_seconds: number;
  cpu: {
    cores: number;
    load_1m: number;
    load_5m: number;
    load_15m: number;
    utilization_percent: number;
  };
  memory: {
    total_bytes: number;
    used_bytes: number;
    available_bytes: number;
    used_percent: number;
    swap_total_bytes: number;
    swap_used_bytes: number;
  };
  network: {
    interfaces: Array<{ name: string; rx_bytes: number; tx_bytes: number }>;
    total_rx_bytes: number;
    total_tx_bytes: number;
    projected_monthly_egress_tb: number | null;
    projected_egress_pct_of_free_tier: number | null;
  };
  disk: {
    mount: string;
    total_bytes: number;
    used_bytes: number;
    available_bytes: number;
    used_percent: number;
    free_tier_used_percent: number;
    inode_total?: number;
    inode_used?: number;
    inode_used_percent?: number;
    mounts?: VmMount[];
    breakdown?: {
      docker_bytes: number | null;
      backups_bytes: number | null;
      logs_bytes: number | null;
    };
  };
  cloudflared: {
    running: boolean;
    pid: number | null;
    uptime_seconds: number | null;
    connections: number | null;
  };
  keepalive: {
    last_run: string | null;
    seconds_since: number | null;
  };
  containers: VmContainer[];
  container_stats: VmContainerStat[];
  top_processes: {
    by_cpu: VmTopProcess[];
    by_memory: VmTopProcess[];
  };
  oom_kills_1h: number;
  last_deploy: VmLastDeploy | null;
  backups: {
    count: number;
    total_size_bytes: number;
    newest: string | null;
    oldest: string | null;
  };
  free_tier: {
    max_ocpus: number;
    max_memory_gb: number;
    max_disk_gb: number;
    max_egress_tb_month: number;
  };
  guardrails: VmGuardrail[];
  guardrail_counts: { critical: number; warn: number };
}

export type VmMetrics = VmMetricsAvailable | VmMetricsUnavailable;

// ── Profile Docs Vault (admin / super_admin only) ────────────────────────────
// Backend canonical DocType — keep in sync with
// backend/app/schemas/profile.py::DocType. The `"other"` branch is
// the escape hatch; the UI treats it specially (shows the free-text
// doc_label instead of the PROFILE_DOC_TYPE_LABELS entry).
export type ProfileDocType =
  | "aadhaar"
  | "pan"
  | "12th_marksheet"
  | "college_marksheet"
  | "cancelled_cheque"
  | "bank_statement"
  | "passbook"
  | "epfo_nominee_proof"
  | "father_aadhaar"
  | "father_pan"
  | "address_proof"
  | "other";

export interface ProfileDocument {
  id: string;
  doc_type: ProfileDocType;
  doc_label: string;
  filename: string;
  file_type: string; // "pdf" | "jpg" | "png" | "heic" | "docx"
  size_bytes: number;
  uploaded_by_user_id: string;
  uploaded_at: string;
  archived_at: string | null;
}

export interface Profile {
  id: string;
  name: string;
  dob: string | null;
  email: string;
  father_name: string | null;
  uan_number: string | null;
  pf_number: string | null;
  notes: string;
  created_by_user_id: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  document_count: number;
}

export interface ProfileDetail extends Profile {
  documents: ProfileDocument[];
}

export interface ProfileListResponse {
  items: Profile[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface ProfileCreatePayload {
  name: string;
  dob?: string | null;
  email: string;
  father_name?: string | null;
  uan_number?: string | null;
  pf_number?: string | null;
  notes?: string;
}

export type ProfileUpdatePayload = Partial<ProfileCreatePayload>;

// ═══════════════════════════════════════════════════════════════════
// Claude Routine Apply (v6) — automated-submission control-plane types.
// Mirrors backend/app/schemas/routine.py. Kept at the bottom of the
// file so the existing types don't churn on a diff review.
// ═══════════════════════════════════════════════════════════════════

export type RoutineMode = "dry_run" | "live" | "single_trial";
export type RoutineStatus = "running" | "complete" | "aborted";
export type AnswerSource = "manual_required" | "learned" | "generated";

// One entry from GET /answer-book/required-coverage. The UI renders
// a 16-row list; each row shows the category header, question, and an
// inline editable answer. `filled=false` rows are the ones blocking
// the pre-flight gate.
export interface RequiredCoverageEntry {
  id: string;
  category: string;
  question: string;
  question_key: string;
  answer: string;
  filled: boolean;
}

export interface RequiredCoverageResponse {
  complete: boolean;
  total_required: number;
  total_filled: number;
  // Unfilled entries only — kept for backward compatibility with the
  // phase-1 UI that rendered "what still needs to be done".
  missing: RequiredCoverageEntry[];
  // All required entries in canonical seed order (filled + unfilled).
  // The phase-2 UI renders this list and lets the operator edit a
  // previously filled answer in place.
  entries: RequiredCoverageEntry[];
}

export interface SeedRequiredResponse {
  created: number;
  already_present: number;
  total: number;
}

export interface TopToApplyJob {
  job_id: string;
  title: string;
  company_id: string | null;
  company_name: string;
  platform: string;
  relevance_score: number;
  geography_bucket: string | null;
  role_cluster: string | null;
  // F257: true when this row is operator-pinned via the manual queue.
  // The UI badges these so the user can confirm the override took
  // effect (queued rows always sort above auto-picked rows).
  is_queued?: boolean;
}

export interface TopToApplyResponse {
  kill_switch_active: boolean;
  daily_cap_remaining: number;
  answer_book_ready: boolean;
  jobs: TopToApplyJob[];
}

// F257 — Apply Routine preferences + manual queue
export type RoutineTargetIntent = "queued" | "excluded";

export interface RoutinePreferences {
  // Convenience toggle: when true, picker keeps only global_remote
  // regardless of allowed_geographies.
  only_global_remote: boolean;
  allowed_geographies: ("global_remote" | "usa_only" | "uae_only")[];
  min_relevance_score: number;  // 0-100; 0 = no floor
  min_resume_score: number;     // 0-100; 0 = no floor
  allowed_role_clusters: string[];
  extra_excluded_platforms: string[];
  // F259: per-user company-level exclude list. Any job whose
  // ``Job.company_id`` is in this list is dropped from auto-picks
  // regardless of cluster/geography/score. Capped at 200 entries
  // server-side.
  excluded_company_ids: string[];
}

export interface ExcludedCompany {
  id: string;
  name: string;
  slug: string;
}

export interface RoutineTargetOut {
  id: string;
  job_id: string;
  intent: RoutineTargetIntent;
  note: string;
  created_at: string;
  updated_at: string;
  job_title: string;
  company_name: string;
  job_url: string;
  relevance_score: number;
  platform: string;
}

export interface RoutineQueueResponse {
  queued: RoutineTargetOut[];
  excluded: RoutineTargetOut[];
}

// F258 — daily relevant-jobs trend by cluster + geography.
// One row per day in the requested window (zero-filled so the chart
// x-axis is continuous). ``by_cluster_geography`` keys are
// ``"<cluster>:<geography>"`` so a heatmap renderer can index them
// directly.
export interface RelevantJobsTrendRow {
  day: string;                                // YYYY-MM-DD
  total_relevant: number;
  by_cluster: Record<string, number>;
  by_geography: Record<string, number>;
  by_cluster_geography: Record<string, number>;
}

export interface RelevantJobsTrendResponse {
  days: number;
  // The dynamic relevant-cluster list at the moment of the query —
  // returned at envelope level so the frontend doesn't have to
  // re-derive its chart legend from the per-row keys.
  clusters: string[];
  geographies: string[];
  rows: RelevantJobsTrendRow[];
}

export interface RoutineRun {
  id: string;
  user_id: string;
  started_at: string;
  ended_at: string | null;
  mode: RoutineMode;
  applications_attempted: number;
  applications_submitted: number;
  applications_skipped: { job_id?: string; reason?: string }[];
  detection_incidents: { at?: string; reason?: string }[];
  status: RoutineStatus;
  kill_switch_triggered: boolean;
}

// Submission detail as returned by GET /applications/{id}/submission
// AND embedded in GET /routine/runs/{id}. Shared shape — the routine
// detail page renders submissions inline, the applications detail
// page renders the same row from the single-application endpoint.
export interface SubmissionDetail {
  id: string;
  application_id: string;
  routine_run_id: string | null;
  submitted_at: string;
  job_url: string;
  ats_platform: string;
  form_fingerprint_hash: string | null;
  // Values that look like PII (email/phone/zip) are stored as
  // `{type, len}` stubs by the backend; everything else is the raw
  // string the ATS form received. Typed as `unknown` so the UI is
  // forced to narrow before rendering.
  payload_json: Record<string, unknown>;
  // List of {question, answer, source, source_ref_id, edit_distance,
  // draft_text}. Stored as a loose dict so the frontend can evolve
  // without a types round-trip — the SubmissionAnswersTab does its
  // own runtime shape check before rendering.
  answers_json: Record<string, unknown>[];
  resume_version_hash: string | null;
  cover_letter_text: string | null;
  screenshot_keys: string[];
  confirmation_text: string | null;
  detected_issues: string[];
  profile_snapshot: Record<string, string>;
  created_at: string;
}

export interface RoutineRunDetail extends RoutineRun {
  submissions: SubmissionDetail[];
}

export interface KillSwitchState {
  disabled: boolean;
  disabled_at: string | null;
  reason: string | null;
}

export interface HumanizeResult {
  text: string;
  passes_applied: string[];
  burstiness_sigma: number;
  banned_phrase_hits: string[];
  style_match_examples_used: number;
}
