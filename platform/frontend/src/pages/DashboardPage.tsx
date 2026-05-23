import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, Link } from "react-router-dom";
import {
  Briefcase,
  Building2,
  GitBranch,
  CheckCircle2,
  XCircle,
  TrendingUp,
  Shield,
  Server,
  Globe,
  TestTube2,
  Star,
  List,
  UserCheck,
  Flame,
  Sparkles,
  RefreshCw,
  BarChart2,
} from "lucide-react";
import {
  BarChart,
  Bar,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { Card } from "@/components/Card";
import { StatusBadge } from "@/components/StatusBadge";
import { ScoreBar } from "@/components/ScoreBar";
import { Badge } from "@/components/Badge";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import {
  getAnalyticsOverview,
  getAnalyticsSources,
  getAnalyticsTrends,
  getAiInsights,
  getJobs,
  getActiveResume,
  getWarmLeads,
} from "@/lib/api";
import { formatCount } from "@/lib/format";

function StatCard({
  label,
  value,
  icon: Icon,
  color,
}: {
  label: string;
  value: string | number;
  icon: React.ElementType;
  color: string;
}) {
  return (
    <Card>
      <div className="flex items-center gap-4">
        <div className={`flex h-12 w-12 items-center justify-center rounded-xl ${color}`}>
          <Icon className="h-6 w-6 text-white" />
        </div>
        <div>
          <p className="text-sm text-gray-500">{label}</p>
          {/* Regression finding 36: Dashboard showed `Total Jobs 47776` and
              `Companies 6639` — raw integers without thousand separators.
              Numeric values route through `formatCount` so they match the
              already-formatted counts on /platforms, /monitoring, etc.
              String values (used when callers pre-format their own display)
              pass through unchanged. */}
          <p className="text-2xl font-bold text-gray-900">
            {typeof value === "number" ? formatCount(value) : value}
          </p>
        </div>
      </div>
    </Card>
  );
}

export function DashboardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // F222: these 11 queries previously only destructured `data`, so any
  // 500/502/504/network-abort from the backend silently zeroed the tile
  // and left the user staring at blank UI. Destructure the FULL query
  // object into an array below, feed it into `<BackendErrorBanner>` at
  // the top of the page. Individual render paths keep their existing
  // `?? 0` / `items.length > 0` guards — the banner only surfaces the
  // "why is this blank" signal, it doesn't hide legit zero-data states.
  const overviewQ = useQuery({
    queryKey: ["analytics", "overview"],
    queryFn: getAnalyticsOverview,
  });
  const overview = overviewQ.data;
  const overviewLoading = overviewQ.isLoading;

  const sourcesQ = useQuery({
    queryKey: ["analytics", "sources"],
    queryFn: getAnalyticsSources,
  });
  const sources = sourcesQ.data;

  const trendsQ = useQuery({
    queryKey: ["analytics", "trends", 30],
    queryFn: () => getAnalyticsTrends(30),
  });
  const trends = trendsQ.data;

  const aiInsightsQ = useQuery({
    queryKey: ["analytics", "ai-insights"],
    queryFn: getAiInsights,
    staleTime: 1000 * 60 * 60, // 1 hour
  });
  const aiInsights = aiInsightsQ.data;
  const insightsLoading = aiInsightsQ.isLoading;
  const insightsFetching = aiInsightsQ.isFetching;

  // Relevant jobs: infra/security roles sorted by score
  const infraJobsQ = useQuery({
    queryKey: ["jobs", "dashboard-infra"],
    queryFn: () => getJobs({ role_cluster: "infra", sort_by: "relevance_score", sort_dir: "desc", page_size: 5 }),
  });
  const infraJobs = infraJobsQ.data;

  const securityJobsQ = useQuery({
    queryKey: ["jobs", "dashboard-security"],
    queryFn: () => getJobs({ role_cluster: "security", sort_by: "relevance_score", sort_dir: "desc", page_size: 5 }),
  });
  const securityJobs = securityJobsQ.data;

  const qaJobsQ = useQuery({
    queryKey: ["jobs", "dashboard-qa"],
    queryFn: () => getJobs({ role_cluster: "qa", sort_by: "relevance_score", sort_dir: "desc", page_size: 5 }),
  });
  const qaJobs = qaJobsQ.data;

  // Global remote jobs
  const globalRemoteJobsQ = useQuery({
    queryKey: ["jobs", "dashboard-global-remote"],
    queryFn: () => getJobs({ geography: "global_remote", sort_by: "relevance_score", sort_dir: "desc", page_size: 5 }),
  });
  const globalRemoteJobs = globalRemoteJobsQ.data;

  // Relevant jobs (infra + security combined, sorted by score)
  const relevantJobsQ = useQuery({
    queryKey: ["jobs", "dashboard-relevant"],
    queryFn: () => getJobs({ role_cluster: "relevant", sort_by: "relevance_score", sort_dir: "desc", page_size: 10 }),
  });
  const relevantJobs = relevantJobsQ.data;

  // Recent jobs (all)
  const recentJobsQ = useQuery({
    queryKey: ["jobs", "dashboard-recent"],
    queryFn: () => getJobs({ sort_by: "first_seen_at", sort_dir: "desc", page_size: 10 }),
  });
  const recentJobs = recentJobsQ.data;

  // Warm leads: companies with active hiring + verified contacts
  const warmLeadsQ = useQuery({
    queryKey: ["analytics", "warm-leads"],
    queryFn: getWarmLeads,
  });
  const warmLeads = warmLeadsQ.data;

  // Active resume for ATS scores
  const activeResumeQ = useQuery({
    queryKey: ["active-resume"],
    queryFn: getActiveResume,
  });
  const activeResumeData = activeResumeQ.data;

  const hasActiveResume = !!activeResumeData?.active_resume;

  // F222: full list of query handles for the error banner. Order only
  // matters insofar as the banner surfaces the first error it finds;
  // keep the dashboard-critical overview query first.
  const dashboardQueries = [
    overviewQ, sourcesQ, trendsQ, aiInsightsQ,
    infraJobsQ, securityJobsQ, qaJobsQ, globalRemoteJobsQ,
    relevantJobsQ, recentJobsQ, warmLeadsQ, activeResumeQ,
  ];

  if (overviewLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  {/* Regression finding 35: job rows were <button onClick={navigate(…)}>
      which blocks right-click → "Open in new tab" and doesn't show the
      target URL in the browser status bar.  <Link> renders a real <a>. */}
  const JobRow = ({ job, showAtsScore }: { job: any; showAtsScore?: boolean }) => (
    <Link
      to={`/jobs/${job.id}`}
      className="flex w-full items-center justify-between px-5 py-3 hover:bg-gray-50 transition-colors text-left"
    >
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-gray-900">
          {job.title}
        </p>
        <p className="text-xs text-gray-500">
          {job.company_name} &middot; {job.source_platform}
          {job.location_restriction && ` · ${job.location_restriction}`}
        </p>
      </div>
      <div className="ml-4 flex items-center gap-3">
        {showAtsScore && job.resume_score != null && (
          <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
            job.resume_score >= 70 ? "bg-green-100 text-green-700" :
            job.resume_score >= 50 ? "bg-yellow-100 text-yellow-700" :
            "bg-gray-100 text-gray-600"
          }`}>
            {job.resume_score}% match
          </span>
        )}
        <ScoreBar score={job.relevance_score} />
        <StatusBadge status={job.status} />
      </div>
    </Link>
  );

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
        <p className="mt-1 text-sm text-gray-500">
          Overview of your job search pipeline
        </p>
      </div>

      {/* F222: surfaces any failed query out of the 12 on this page. */}
      <BackendErrorBanner queries={dashboardQueries} />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {/* "Total Jobs" sits alongside lifetime cardinality tiles
            (Companies, Pipeline Active) so it must render lifetime,
            not F214's windowed flow metric. The lifetime field is
            populated server-side; legacy `total_jobs` is the windowed
            value reserved for a future "Last N days" switcher. */}
        <StatCard
          label="Total Jobs"
          value={overview?.total_jobs_lifetime ?? 0}
          icon={Briefcase}
          color="bg-primary-600"
        />
        <StatCard
          label="Companies"
          value={overview?.total_companies ?? 0}
          icon={Building2}
          color="bg-blue-600"
        />
        <StatCard
          label="Accepted"
          value={overview?.accepted_count ?? 0}
          icon={CheckCircle2}
          color="bg-green-600"
        />
        <StatCard
          label="Pipeline Active"
          value={overview?.pipeline_active ?? 0}
          icon={GitBranch}
          color="bg-purple-600"
        />
      </div>

      {/* Three-column: Infra + Security + QA top roles */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Infra/Cloud/DevOps/SRE */}
        <Card padding="none">
          <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4">
            <div className="flex items-center gap-2">
              <Server className="h-4 w-4 text-blue-500" />
              <h3 className="text-base font-semibold text-gray-900">
                Infra / Cloud / DevOps / SRE
              </h3>
            </div>
            <Badge variant="primary">{formatCount(infraJobs?.total)} jobs</Badge>
          </div>
          {infraJobs && infraJobs.items.length > 0 ? (
            <div className="divide-y divide-gray-50">
              {infraJobs.items.map((job) => (
                <JobRow key={job.id} job={job} showAtsScore={hasActiveResume} />
              ))}
            </div>
          ) : (
            <div className="py-8 text-center text-sm text-gray-400">No infra jobs found</div>
          )}
          <div className="border-t border-gray-100 px-5 py-2">
            <button
              onClick={() => navigate("/jobs?role_cluster=infra")}
              className="text-xs font-medium text-primary-600 hover:text-primary-700"
            >
              View all infra jobs →
            </button>
          </div>
        </Card>

        {/* Security/Compliance/DevSecOps */}
        <Card padding="none">
          <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4">
            <div className="flex items-center gap-2">
              <Shield className="h-4 w-4 text-red-500" />
              <h3 className="text-base font-semibold text-gray-900">
                Security / Compliance / DevSecOps
              </h3>
            </div>
            <Badge variant="danger">{formatCount(securityJobs?.total)} jobs</Badge>
          </div>
          {securityJobs && securityJobs.items.length > 0 ? (
            <div className="divide-y divide-gray-50">
              {securityJobs.items.map((job) => (
                <JobRow key={job.id} job={job} showAtsScore={hasActiveResume} />
              ))}
            </div>
          ) : (
            <div className="py-8 text-center text-sm text-gray-400">No security jobs found</div>
          )}
          <div className="border-t border-gray-100 px-5 py-2">
            <button
              onClick={() => navigate("/jobs?role_cluster=security")}
              className="text-xs font-medium text-primary-600 hover:text-primary-700"
            >
              View all security jobs →
            </button>
          </div>
        </Card>

        {/* QA/Testing/SDET */}
        <Card padding="none">
          <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4">
            <div className="flex items-center gap-2">
              <TestTube2 className="h-4 w-4 text-purple-500" />
              <h3 className="text-base font-semibold text-gray-900">
                QA / Testing / SDET
              </h3>
            </div>
            <Badge variant="gray">{formatCount(qaJobs?.total)} jobs</Badge>
          </div>
          {qaJobs && qaJobs.items.length > 0 ? (
            <div className="divide-y divide-gray-50">
              {qaJobs.items.map((job) => (
                <JobRow key={job.id} job={job} showAtsScore={hasActiveResume} />
              ))}
            </div>
          ) : (
            <div className="py-8 text-center text-sm text-gray-400">No QA jobs found</div>
          )}
          <div className="border-t border-gray-100 px-5 py-2">
            <button
              onClick={() => navigate("/jobs?role_cluster=qa")}
              className="text-xs font-medium text-primary-600 hover:text-primary-700"
            >
              View all QA jobs →
            </button>
          </div>
        </Card>
      </div>

      {/* Worldwide remote section (formerly "Global Remote") */}
      <Card padding="none">
        <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4">
          <div className="flex items-center gap-2">
            <Globe className="h-4 w-4 text-green-500" />
            <h3 className="text-base font-semibold text-gray-900">
              Worldwide remote openings
            </h3>
          </div>
          <Badge variant="success">{formatCount(globalRemoteJobs?.total)} jobs</Badge>
        </div>
        {globalRemoteJobs && globalRemoteJobs.items.length > 0 ? (
          <div className="divide-y divide-gray-50">
            {globalRemoteJobs.items.map((job) => (
              <JobRow key={job.id} job={job} showAtsScore={hasActiveResume} />
            ))}
          </div>
        ) : (
          <div className="py-8 text-center text-sm text-gray-400">
            No worldwide-remote jobs classified yet. Jobs are being scanned and classified continuously.
          </div>
        )}
        <div className="border-t border-gray-100 px-5 py-2">
          <button
            onClick={() => navigate("/jobs?geography=global_remote")}
            className="text-xs font-medium text-primary-600 hover:text-primary-700"
          >
            View all worldwide-remote jobs →
          </button>
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <Card padding="none" className="lg:col-span-2">
          <div className="border-b border-gray-100 px-6 py-4">
            <h3 className="text-base font-semibold text-gray-900">
              Jobs by Source
            </h3>
          </div>
          <div className="p-6">
            {sources && sources.length > 0 ? (
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={sources} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis
                    dataKey="platform"
                    tick={{ fontSize: 12, fill: "#6b7280" }}
                    axisLine={{ stroke: "#e5e7eb" }}
                  />
                  <YAxis
                    tick={{ fontSize: 12, fill: "#6b7280" }}
                    axisLine={{ stroke: "#e5e7eb" }}
                  />
                  <Tooltip
                    contentStyle={{
                      borderRadius: "8px",
                      border: "1px solid #e5e7eb",
                      fontSize: "13px",
                    }}
                  />
                  <Bar
                    dataKey="count"
                    fill="#6366f1"
                    radius={[4, 4, 0, 0]}
                    maxBarSize={48}
                  />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="py-10 text-center text-sm text-gray-500">
                No source data available yet
              </div>
            )}
          </div>
        </Card>

        <Card padding="none">
          <div className="border-b border-gray-100 px-6 py-4">
            <h3 className="text-base font-semibold text-gray-900">
              Key Metrics
            </h3>
          </div>
          <div className="divide-y divide-gray-100">
            <div className="flex items-center justify-between px-6 py-4">
              <span className="text-sm text-gray-600">Acceptance Rate</span>
              <span className="text-sm font-semibold text-green-600">
                {overview?.acceptance_rate
                  ? `${(overview.acceptance_rate * 100).toFixed(1)}%`
                  : "N/A"}
              </span>
            </div>
            <div className="flex items-center justify-between px-6 py-4">
              <span className="text-sm text-gray-600">Avg Relevance</span>
              <ScoreBar score={overview?.avg_relevance_score ?? 0} />
            </div>
            <div className="flex items-center justify-between px-6 py-4">
              <span className="text-sm text-gray-600">Reviewed</span>
              <span className="text-sm font-semibold text-gray-900">
                {overview?.reviewed_count ?? 0}
              </span>
            </div>
            <div className="flex items-center justify-between px-6 py-4">
              <span className="text-sm text-gray-600">Rejected</span>
              <div className="flex items-center gap-1.5">
                <XCircle className="h-4 w-4 text-red-400" />
                <span className="text-sm font-semibold text-red-600">
                  {overview?.rejected_count ?? 0}
                </span>
              </div>
            </div>
            <div className="flex items-center justify-between px-6 py-4">
              <span className="text-sm text-gray-600">Trend</span>
              <div className="flex items-center gap-1.5 text-green-600">
                <TrendingUp className="h-4 w-4" />
                <span className="text-sm font-semibold">Active</span>
              </div>
            </div>
          </div>
        </Card>
      </div>

      {/* Relevant Jobs: Cloud, DevOps, SRE, Compliance, Security */}
      <Card padding="none" className="ring-2 ring-primary-200">
        <div className="flex items-center justify-between border-b border-primary-100 bg-primary-50 px-5 py-4">
          <div>
            <div className="flex items-center gap-2">
              <Star className="h-4 w-4 text-amber-500" />
              <h3 className="text-base font-semibold text-gray-900">
                Relevant Jobs
              </h3>
            </div>
            <p className="text-xs text-gray-500 mt-0.5">
              Cloud, DevOps, SRE, Compliance &amp; Security — scored by relevance
              {hasActiveResume && (
                <span className="ml-1 text-primary-600 font-medium">
                  · ATS match from {activeResumeData!.active_resume!.label || activeResumeData!.active_resume!.filename}
                </span>
              )}
            </p>
          </div>
          <Badge variant="primary">{formatCount(relevantJobs?.total)} jobs</Badge>
        </div>
        {relevantJobs && relevantJobs.items.length > 0 ? (
          <div className="divide-y divide-gray-50">
            {relevantJobs.items.map((job) => (
              <JobRow key={job.id} job={job} showAtsScore={hasActiveResume} />
            ))}
          </div>
        ) : (
          <div className="py-10 text-center text-sm text-gray-500">
            No relevant jobs found yet. Jobs are scored as they are scraped.
          </div>
        )}
        <div className="border-t border-gray-100 px-5 py-2 flex items-center justify-between">
          <button
            onClick={() => navigate(hasActiveResume ? "/jobs?role_cluster=relevant&sort_by=resume_score&sort_dir=desc" : "/jobs?role_cluster=relevant&sort_by=relevance_score&sort_dir=desc")}
            className="text-xs font-medium text-primary-600 hover:text-primary-700"
          >
            View all relevant jobs →
          </button>
          {!hasActiveResume && (
            <span className="text-xs text-gray-400">Upload a resume to see ATS match scores</span>
          )}
        </div>
      </Card>

      {/* Warm Leads */}
      <Card padding="none" className="ring-2 ring-orange-200">
        <div className="flex items-center justify-between border-b border-orange-100 bg-orange-50 px-5 py-4">
          <div>
            <div className="flex items-center gap-2">
              <Flame className="h-4 w-4 text-orange-500" />
              <h3 className="text-base font-semibold text-gray-900">Warm Leads</h3>
            </div>
            <p className="text-xs text-gray-500 mt-0.5">
              Companies actively hiring infra/security roles with verified contacts
            </p>
          </div>
          <Badge variant="warning">{warmLeads?.items.length ?? 0} companies</Badge>
        </div>
        {warmLeads && warmLeads.items.length > 0 ? (
          <div className="divide-y divide-gray-50">
            {warmLeads.items.map((lead) => (
              <button
                key={lead.company_id}
                onClick={() => navigate(`/companies/${lead.company_id}`)}
                className="flex w-full items-center justify-between px-5 py-3 hover:bg-gray-50 transition-colors text-left"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium text-gray-900">{lead.company_name}</p>
                    {lead.is_target && <Star className="h-3 w-3 text-amber-500" />}
                    {lead.new_jobs_7d > 0 && (
                      <span className="rounded-full bg-green-100 px-1.5 py-0.5 text-xs font-medium text-green-700">
                        {lead.new_jobs_7d} new this week
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {lead.industry && <span>{lead.industry} · </span>}
                    {lead.funding_stage && <span>{lead.funding_stage}</span>}
                    {lead.total_funding && <span className="text-emerald-600 font-medium"> · {lead.total_funding}</span>}
                  </p>
                </div>
                <div className="ml-4 flex items-center gap-3 text-xs text-gray-500 shrink-0">
                  {lead.decision_makers > 0 && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-purple-100 px-2 py-0.5 text-purple-700 font-medium">
                      <UserCheck className="h-3 w-3" />
                      {lead.decision_makers} DM
                    </span>
                  )}
                  <span className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 text-blue-600">
                    <Building2 className="h-3 w-3" />
                    {lead.total_contacts} contacts
                  </span>
                  <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-gray-600">
                    <Briefcase className="h-3 w-3" />
                    {lead.new_jobs_30d} jobs/mo
                  </span>
                </div>
              </button>
            ))}
          </div>
        ) : (
          <div className="py-8 text-center text-sm text-gray-400">
            Warm leads appear once companies have verified contacts and active hiring
          </div>
        )}
        <div className="border-t border-gray-100 px-5 py-2">
          <button
            onClick={() => navigate("/companies?has_contacts=true&actively_hiring=true")}
            className="text-xs font-medium text-orange-600 hover:text-orange-700"
          >
            View all companies with contacts →
          </button>
        </div>
      </Card>

      {/* ─── Trends + AI Insights ─── */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Trends chart */}
        <Card padding="none" className="lg:col-span-2">
          <div className="flex items-center justify-between border-b border-gray-100 px-6 py-4">
            <div className="flex items-center gap-2">
              <BarChart2 className="h-4 w-4 text-indigo-500" />
              <h3 className="text-base font-semibold text-gray-900">Hiring Trends — Last 30 Days</h3>
            </div>
            <div className="flex items-center gap-3 text-xs text-gray-400">
              <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-indigo-400" />Total</span>
              <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-blue-400" />Infra</span>
              <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-red-400" />Security</span>
              <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-green-400" />Accepted</span>
            </div>
          </div>
          <div className="p-6">
            {trends && trends.length > 0 ? (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={trends} margin={{ top: 4, right: 16, bottom: 0, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis
                    dataKey="day"
                    tick={{ fontSize: 10, fill: "#9ca3af" }}
                    axisLine={{ stroke: "#e5e7eb" }}
                    tickFormatter={(v: string) => v.slice(5)}
                    interval="preserveStartEnd"
                  />
                  <YAxis
                    tick={{ fontSize: 10, fill: "#9ca3af" }}
                    axisLine={{ stroke: "#e5e7eb" }}
                    width={28}
                  />
                  <Tooltip
                    contentStyle={{ borderRadius: "8px", border: "1px solid #e5e7eb", fontSize: "12px" }}
                    labelFormatter={(v: string) => `Date: ${v}`}
                  />
                  <Line type="monotone" dataKey="total" stroke="#818cf8" strokeWidth={2} dot={false} name="Total" />
                  <Line type="monotone" dataKey="infra" stroke="#60a5fa" strokeWidth={1.5} dot={false} name="Infra" />
                  <Line type="monotone" dataKey="security" stroke="#f87171" strokeWidth={1.5} dot={false} name="Security" />
                  <Line type="monotone" dataKey="accepted" stroke="#34d399" strokeWidth={1.5} dot={false} name="Accepted" />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-[220px] items-center justify-center text-sm text-gray-400">
                No trend data yet — run a scan to populate
              </div>
            )}
          </div>
        </Card>

        {/* AI Insights */}
        <Card padding="none" className="ring-1 ring-violet-200">
          <div className="flex items-center justify-between border-b border-violet-100 bg-violet-50 px-5 py-4">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-violet-500" />
              <h3 className="text-sm font-semibold text-gray-900">AI Insights</h3>
            </div>
            <button
              onClick={() => queryClient.invalidateQueries({ queryKey: ["analytics", "ai-insights"] })}
              disabled={insightsFetching}
              className="rounded p-1 text-violet-400 hover:text-violet-600 disabled:opacity-40"
              title="Refresh insights"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${insightsFetching ? "animate-spin" : ""}`} />
            </button>
          </div>
          <div className="p-4">
            {insightsLoading ? (
              <div className="flex items-center gap-2 py-8 justify-center text-sm text-gray-400">
                <div className="spinner h-4 w-4" />
                Analyzing trends…
              </div>
            ) : aiInsights?.insights && aiInsights.insights.length > 0 ? (
              <ul className="space-y-3">
                {aiInsights.insights.map((insight, i) => (
                  <li key={i} className="flex gap-2 text-xs text-gray-700">
                    <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-violet-100 text-violet-600 font-semibold text-[10px]">
                      {i + 1}
                    </span>
                    <span>{insight}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="py-4 text-center text-xs text-gray-400">No insights available</p>
            )}
            {aiInsights && (
              <p className="mt-4 text-right text-[10px] text-gray-300">
                {aiInsights.ai_generated ? "✦ Claude" : "Rule-based"} · {aiInsights.generated_at?.slice(0, 10)}
              </p>
            )}
          </div>
        </Card>
      </div>

      {/* All Jobs */}
      <Card padding="none">
        <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4">
          <div>
            <div className="flex items-center gap-2">
              <List className="h-4 w-4 text-gray-500" />
              <h3 className="text-base font-semibold text-gray-900">
                All Recent Jobs
              </h3>
            </div>
            <p className="text-xs text-gray-400 mt-0.5">All profiles including non-scored positions</p>
          </div>
          <Badge variant="default">{formatCount(recentJobs?.total)} jobs</Badge>
        </div>
        {recentJobs && recentJobs.items.length > 0 ? (
          <div className="divide-y divide-gray-50">
            {recentJobs.items.map((job) => (
              <JobRow key={job.id} job={job} showAtsScore={false} />
            ))}
          </div>
        ) : (
          <div className="py-10 text-center text-sm text-gray-500">
            No jobs found yet. Run a scraping task to get started.
          </div>
        )}
        <div className="border-t border-gray-100 px-5 py-2">
          <button
            onClick={() => navigate("/jobs?sort_by=first_seen_at&sort_dir=desc")}
            className="text-xs font-medium text-primary-600 hover:text-primary-700"
          >
            View all jobs →
          </button>
        </div>
      </Card>
    </div>
  );
}
