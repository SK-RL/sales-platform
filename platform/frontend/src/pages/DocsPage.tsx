import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getAtsCoverage } from "@/lib/api";
import type { AtsCoverageRow } from "@/lib/types";
import { useAuth } from "@/lib/auth";
import {
  Shield,
  Users,
  FileText,
  ClipboardCheck,
  GitBranch,
  MessageSquarePlus,
  Send,
  BookOpen,
  Star,
  ChevronDown,
  ChevronRight,
  Lightbulb,
  AlertTriangle,
  Target,
  BarChart3,
  Search,
  Zap,
  Clock,
  CheckCircle2,
  ArrowRight,
  Building2,
  Briefcase,
  RefreshCw,
  Upload,
  Brain,
  Settings,
  Bot,
} from "lucide-react";

// ── Collapsible Section ─────────────────────────────────────────────────────
function Section({
  title,
  icon: Icon,
  defaultOpen = false,
  badge,
  id,
  children,
}: {
  title: string;
  icon: React.ElementType;
  defaultOpen?: boolean;
  badge?: string;
  id?: string;
  children: React.ReactNode;
}) {
  const location = useLocation();
  const targeted = Boolean(id) && location.hash === `#${id}`;
  const [open, setOpen] = useState(defaultOpen || targeted);
  // A deep link (/docs#auto-apply from the Auto-apply page) opens and
  // scrolls to its section.
  useEffect(() => {
    if (!targeted) return;
    setOpen(true);
    document.getElementById(id as string)?.scrollIntoView?.({ block: "start" });
  }, [targeted, id]);
  return (
    <div id={id} className="rounded-xl border border-gray-200 bg-white overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-3 p-5 text-left hover:bg-gray-50 transition-colors"
      >
        <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-primary-100">
          <Icon className="h-5 w-5 text-primary-700" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold text-gray-900">{title}</h2>
            {badge && (
              <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-semibold text-blue-700 uppercase tracking-wide">
                {badge}
              </span>
            )}
          </div>
        </div>
        {open ? (
          <ChevronDown className="h-5 w-5 text-gray-400" />
        ) : (
          <ChevronRight className="h-5 w-5 text-gray-400" />
        )}
      </button>
      {open && <div className="border-t border-gray-100 px-5 pb-5 pt-4">{children}</div>}
    </div>
  );
}

// ── Tip / Warning Callouts ──────────────────────────────────────────────────
function Tip({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex gap-2.5 rounded-lg bg-blue-50 border border-blue-100 px-4 py-3 text-sm text-blue-800">
      <Lightbulb className="h-4 w-4 mt-0.5 flex-shrink-0 text-blue-600" />
      <div>{children}</div>
    </div>
  );
}

function Warning({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex gap-2.5 rounded-lg bg-amber-50 border border-amber-100 px-4 py-3 text-sm text-amber-800">
      <AlertTriangle className="h-4 w-4 mt-0.5 flex-shrink-0 text-amber-600" />
      <div>{children}</div>
    </div>
  );
}

// ── Auto-apply per-ATS guide (F393) ─────────────────────────────────────────
// Rows come from GET /applications/ats-coverage, which derives each site's
// level from the live submitter / extractor registries — the guide can't
// promise more than the code does.
const LEVEL_STYLE: Record<AtsCoverageRow["level"], string> = {
  automatic: "bg-green-100 text-green-800",
  review: "bg-amber-100 text-amber-800",
  link: "bg-gray-100 text-gray-700",
  closed: "bg-red-100 text-red-800",
};

function AtsRow({ row }: { row: AtsCoverageRow }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-lg border border-gray-200 bg-white" id={`ats-${row.platform}`}>
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-gray-50">
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold ${LEVEL_STYLE[row.level]}`}>{row.level_label}</span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-gray-900">{row.name}</span>
        <code className="hidden truncate text-[11px] text-gray-400 sm:block">{row.link_example}</code>
        {open ? <ChevronDown className="h-4 w-4 shrink-0 text-gray-400" /> : <ChevronRight className="h-4 w-4 shrink-0 text-gray-400" />}
      </button>
      {open && (
        <div className="grid gap-4 border-t border-gray-100 px-4 py-3 text-sm sm:grid-cols-2">
          <div>
            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500">Automatic</p>
            {row.automatic.length === 0 ? (
              <p className="text-gray-500">Nothing — we can't drive this site.</p>
            ) : (
              <ul className="list-disc space-y-1 pl-4 text-gray-700">{row.automatic.map((t) => <li key={t}>{t}</li>)}</ul>
            )}
          </div>
          <div>
            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500">You</p>
            <ul className="list-disc space-y-1 pl-4 text-gray-700">{row.you.map((t) => <li key={t}>{t}</li>)}</ul>
            {row.notes.length > 0 && (
              <p className="mt-2 text-xs text-gray-500">{row.notes.join(" · ")}</p>
            )}
          </div>
          <p className="text-xs text-gray-500 sm:col-span-2">Paste links that look like <code>{row.link_example}</code></p>
        </div>
      )}
    </li>
  );
}

function AtsCoverageGuide() {
  const q = useQuery({ queryKey: ["ats-coverage"], queryFn: getAtsCoverage, staleTime: 5 * 60_000 });
  if (q.isLoading) return <p className="text-sm text-gray-500">Loading the site list…</p>;
  if (q.isError || !q.data) return <p className="text-sm text-red-700">Couldn't load the site list — try again later.</p>;
  const c = q.data.counts;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2 text-xs">
        <span className={`rounded-full px-2 py-0.5 font-semibold ${LEVEL_STYLE.automatic}`}>{c.automatic} automatic</span>
        <span className={`rounded-full px-2 py-0.5 font-semibold ${LEVEL_STYLE.review}`}>{c.review} we fill, you submit</span>
        <span className={`rounded-full px-2 py-0.5 font-semibold ${LEVEL_STYLE.link}`}>{c.link} link only</span>
        <span className={`rounded-full px-2 py-0.5 font-semibold ${LEVEL_STYLE.closed}`}>{c.closed} not supported</span>
      </div>
      <ul className="space-y-2">{q.data.items.map((r) => <AtsRow key={r.platform} row={r} />)}</ul>
    </div>
  );
}

// ── Step Card ───────────────────────────────────────────────────────────────
function StepCard({
  number,
  title,
  children,
}: {
  number: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex gap-3">
      <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-primary-600 text-xs font-bold text-white">
        {number}
      </div>
      <div className="flex-1 min-w-0">
        <h4 className="text-sm font-semibold text-gray-900">{title}</h4>
        <div className="mt-1 text-sm text-gray-600 space-y-1.5">{children}</div>
      </div>
    </div>
  );
}

// ── Flow Diagram ────────────────────────────────────────────────────────────
function FlowStep({ label, color }: { label: string; color: string }) {
  return (
    <span className={`inline-flex items-center rounded-md px-2.5 py-1 text-xs font-semibold ${color}`}>
      {label}
    </span>
  );
}

function FlowArrow() {
  return <ArrowRight className="h-3.5 w-3.5 text-gray-400 flex-shrink-0" />;
}

// ── Role Permissions Data ───────────────────────────────────────────────────
const rolePermissions = [
  {
    role: "Super Admin",
    color: "bg-red-100 text-red-800",
    permissions: [
      "Full platform control",
      "User management (invite, deactivate, role changes)",
      "All admin permissions",
      "Ticket management",
      "View all resumes across users",
      "View all sales performance",
    ],
  },
  {
    role: "Admin",
    color: "bg-orange-100 text-orange-800",
    permissions: [
      "Monitoring and system health",
      "Role cluster configuration",
      "Ticket management (view, update status)",
      "View all resumes and sales performance",
      "Trigger scans and discovery",
      "Cannot manage users",
    ],
  },
  {
    role: "Reviewer (Sales Team)",
    color: "bg-blue-100 text-blue-800",
    permissions: [
      "Review and accept/reject jobs",
      "Upload and manage own resumes",
      "Score resumes against jobs",
      "Manage own answer book and credentials",
      "Submit and track applications",
      "Manage pipeline entries",
      "Submit tickets (bugs, features, improvements)",
      "View analytics",
    ],
  },
  {
    role: "Viewer",
    color: "bg-gray-100 text-gray-800",
    permissions: [
      "Browse jobs and companies (read-only)",
      "View analytics",
      "Submit tickets",
    ],
  },
];

// ── Main Page ───────────────────────────────────────────────────────────────
export function DocsPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin" || user?.role === "super_admin";

  return (
    <div className="space-y-4 max-w-4xl">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Platform Guide</h1>
        <p className="mt-1 text-sm text-gray-600">
          Everything you need to use the platform effectively. Click any section to expand.
        </p>
      </div>

      {/* ── First-Time Setup ───────────────────────────────────────────── */}
      <Section title="First-Time Setup Checklist" icon={CheckCircle2} defaultOpen={true}>
        <p className="text-sm text-gray-600 mb-4">
          Complete these one-time steps in order to get fully operational. Each step unlocks more platform capabilities.
        </p>
        <div className="space-y-5">
          <StepCard number={1} title="Upload Your Resume">
            <p>
              Go to <Link to="/resume-score" className="font-semibold text-primary-600 hover:text-primary-700 underline">Resume Score</Link> in the sidebar. Upload a PDF or DOCX and give it a label
              (e.g. "Cloud Engineer" or "Security Specialist"). This is required before you can apply to jobs or run ATS scoring.
            </p>
            <Tip>You can upload multiple resumes for different role types. Set one as <strong>Active</strong> -- this is the one used for applications and scoring.</Tip>
          </StepCard>

          <StepCard number={2} title="Build Your Answer Book">
            <p>
              Open <Link to="/answer-book" className="font-semibold text-primary-600 hover:text-primary-700 underline">Answer Book</Link>. Click <strong>"Import from Resume"</strong> to auto-extract answers from your active resume.
              Then manually fill in anything the import missed: work authorization, salary expectations, availability, etc.
            </p>
            <p>
              Categories to fill: <strong>Personal Info</strong>, <strong>Work Authorization</strong>,{" "}
              <strong>Experience</strong>, <strong>Skills</strong>, <strong>Preferences</strong>.
            </p>
          </StepCard>

          <StepCard number={3} title="Add Platform Credentials">
            <p>
              Go to <Link to="/credentials" className="font-semibold text-primary-600 hover:text-primary-700 underline">Credentials</Link>. Add your login details for job platforms you actively apply on
              (Greenhouse, Lever, Workable, etc.). Credentials are encrypted and only visible to you.
            </p>
            <Warning>Never share your credentials with teammates. Each person maintains their own set.</Warning>
          </StepCard>

          <StepCard number={4} title="Score Your Resume">
            <p>
              Back in <Link to="/resume-score" className="font-semibold text-primary-600 hover:text-primary-700 underline">Resume Score</Link>, click <strong>"Score Against All Jobs"</strong>.
              The system scores your resume against every relevant job in the database. This takes a few minutes. When done,
              you'll see which jobs you match best and where your resume has gaps.
            </p>
          </StepCard>

          <StepCard number={5} title="Browse and Accept Jobs">
            <p>
              Go to <Link to="/jobs?role_cluster=relevant" className="font-semibold text-primary-600 hover:text-primary-700 underline">Relevant Jobs</Link> or the <Link to="/review" className="font-semibold text-primary-600 hover:text-primary-700 underline">Review Queue</Link> and start accepting jobs
              that match your target companies and role types. Accepted jobs feed into the pipeline.
            </p>
          </StepCard>
        </div>
      </Section>

      {/* ── Daily Workflow ─────────────────────────────────────────────── */}
      <Section title="Recommended Daily Workflow" icon={Clock} defaultOpen={true}>
        <p className="text-sm text-gray-600 mb-4">
          Follow this routine to stay on top of new opportunities. Scans run automatically every few hours, so new jobs appear throughout the day.
        </p>
        <div className="grid gap-3 md:grid-cols-2">
          {[
            {
              icon: Star,
              time: "Morning",
              title: "Check Dashboard",
              desc: "See overnight scan results, new job counts, warm leads, and AI insights. Note any funding signals.",
            },
            {
              icon: ClipboardCheck,
              time: "Morning",
              title: "Work the Review Queue",
              desc: "Triage new jobs: Accept matches, Reject mismatches (with tags), Skip if unsure. Aim to clear the queue daily.",
            },
            {
              icon: Search,
              time: "Midday",
              title: "Browse Relevant Jobs",
              desc: "Use filters to find specific opportunities. Filter by geography, platform, or role cluster for targeted searches.",
            },
            {
              icon: Building2,
              time: "Midday",
              title: "Research Companies",
              desc: "Check recently funded companies and warm leads. Enrich company profiles, review contacts, draft outreach emails.",
            },
            {
              icon: Send,
              time: "Afternoon",
              title: "Submit Applications",
              desc: "Apply to your top matches. The system checks readiness (resume, credentials, answers) before each application.",
            },
            {
              icon: GitBranch,
              time: "Afternoon",
              title: "Update Pipeline",
              desc: "Move companies through stages as outreach progresses. Update notes and priority scores.",
            },
            {
              icon: BarChart3,
              time: "End of Day",
              title: "Review Analytics",
              desc: "Check your acceptance rate, application funnel, and team performance. Identify patterns in what works.",
            },
            {
              icon: Briefcase,
              time: "Weekly",
              title: "Re-Score Resume",
              desc: "Re-run resume scoring after new jobs are added. Update your answer book with new question patterns.",
            },
          ].map((item) => (
            <div key={item.title} className="flex gap-3 rounded-lg border border-gray-100 bg-gray-50 p-3.5">
              <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-primary-50">
                <item.icon className="h-4 w-4 text-primary-600" />
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h4 className="text-sm font-semibold text-gray-900">{item.title}</h4>
                  <span className="text-[10px] font-medium text-gray-400 uppercase">{item.time}</span>
                </div>
                <p className="mt-0.5 text-xs text-gray-600">{item.desc}</p>
              </div>
            </div>
          ))}
        </div>
      </Section>

      {/* ── How Scoring Works ──────────────────────────────────────────── */}
      <Section title="How Scoring Works" icon={Target}>
        <div className="space-y-5">
          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-2">Job Relevance Score (0-100)</h3>
            <p className="text-sm text-gray-600 mb-3">
              Every job is scored automatically when scraped. Higher scores mean a better fit for your team's target roles.
            </p>
            <div className="grid gap-2">
              {[
                { weight: "40%", signal: "Title Match", desc: "How closely the job title matches your role clusters (Infra, Security, QA)" },
                { weight: "20%", signal: "Company Fit", desc: "Target company bonus, funding stage, employee count, and industry alignment" },
                { weight: "20%", signal: "Geography Clarity", desc: "Higher for clearly stated 'remote worldwide' roles, lower for ambiguous locations" },
                { weight: "10%", signal: "Source Priority", desc: "Tier 1 platforms (Greenhouse, Lever) score higher than Tier 3 (Himalayas)" },
                { weight: "10%", signal: "Freshness", desc: "Newer postings score higher. Score decays over time as jobs age" },
              ].map((s) => (
                <div key={s.signal} className="flex items-start gap-3 rounded-lg bg-gray-50 px-3 py-2.5">
                  <span className="flex-shrink-0 rounded bg-primary-100 px-2 py-0.5 text-xs font-bold text-primary-700">{s.weight}</span>
                  <div>
                    <span className="text-sm font-medium text-gray-900">{s.signal}</span>
                    <p className="text-xs text-gray-500">{s.desc}</p>
                  </div>
                </div>
              ))}
            </div>
            <Tip>
              Jobs scoring <strong>70+</strong> are strong matches. Jobs below 40 are usually outside your target clusters.
              Focus your review time on the 50-80 range where your judgment adds the most value.
            </Tip>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-2">Resume ATS Score (0-100)</h3>
            <p className="text-sm text-gray-600 mb-3">
              Measures how well your resume matches a specific job. Based on what real ATS systems look for.
            </p>
            <div className="grid gap-2">
              {[
                { weight: "50%", signal: "Keyword Overlap", desc: "Technical skills, tools, and certifications that appear in both your resume and the job description" },
                { weight: "30%", signal: "Role Alignment", desc: "How well your experience titles and responsibilities match the target role cluster" },
                { weight: "20%", signal: "Format & Completeness", desc: "Resume length, section structure, and professional completeness" },
              ].map((s) => (
                <div key={s.signal} className="flex items-start gap-3 rounded-lg bg-gray-50 px-3 py-2.5">
                  <span className="flex-shrink-0 rounded bg-green-100 px-2 py-0.5 text-xs font-bold text-green-700">{s.weight}</span>
                  <div>
                    <span className="text-sm font-medium text-gray-900">{s.signal}</span>
                    <p className="text-xs text-gray-500">{s.desc}</p>
                  </div>
                </div>
              ))}
            </div>
            <Tip>
              If a resume scores below 70 for a job you care about, use <strong>AI Customize</strong> to generate
              an improved version targeting that specific job's keywords.
            </Tip>
          </div>
        </div>
      </Section>

      {/* ── Feature Guide: Jobs ────────────────────────────────────────── */}
      <Section title="Finding and Reviewing Jobs" icon={Briefcase}>
        <div className="space-y-4">
          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Relevant Jobs vs All Jobs</h3>
            <p className="text-sm text-gray-600">
              <strong>Relevant Jobs</strong> shows only jobs matching your active role clusters (Infra, Security, QA).
              These are pre-scored and ranked. <strong>All Jobs</strong> includes everything in the database, including
              unscored and irrelevant roles -- useful for ad-hoc searches.
            </p>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Filters That Matter</h3>
            <ul className="text-sm text-gray-600 space-y-1.5 ml-4 list-disc">
              <li><strong>Remote scope: "Worldwide remote"</strong> -- Shows roles open to candidates anywhere. Most valuable for international sales.</li>
              <li><strong>Platform filter</strong> -- Use this to focus on Greenhouse/Lever (higher quality boards) or check LinkedIn results.</li>
              <li><strong>Sort by Relevance</strong> (default) -- Puts best matches first. Switch to "Date" to see the freshest postings.</li>
              <li><strong>Status: "New"</strong> -- Shows only jobs nobody has reviewed yet. Great for daily triage.</li>
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-2">Review Queue Workflow</h3>
            <div className="flex flex-wrap items-center gap-2 mb-3">
              <FlowStep label="New Job" color="bg-blue-100 text-blue-700" />
              <FlowArrow />
              <FlowStep label="Review" color="bg-yellow-100 text-yellow-700" />
              <FlowArrow />
              <FlowStep label="Accept" color="bg-green-100 text-green-700" />
              <span className="text-xs text-gray-400">or</span>
              <FlowStep label="Reject" color="bg-red-100 text-red-700" />
              <span className="text-xs text-gray-400">or</span>
              <FlowStep label="Skip" color="bg-gray-100 text-gray-700" />
            </div>
            <ul className="text-sm text-gray-600 space-y-1.5 ml-4 list-disc">
              <li><strong>Accept</strong> -- Job moves into your pipeline and application tracking. Use for strong matches.</li>
              <li><strong>Reject</strong> -- Always add a <strong>tag</strong> (location mismatch, seniority, etc.). This trains your future filtering.</li>
              <li><strong>Skip</strong> -- Use when unsure. The job stays in queue for later or for a teammate to review.</li>
            </ul>
            <Warning>
              <strong>Tag your rejections.</strong> Rejection tags build data that helps the team understand which job types
              to deprioritize. Untagged rejections are lost insights.
            </Warning>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Bulk Actions</h3>
            <p className="text-sm text-gray-600">
              On the Jobs page, select multiple jobs using checkboxes, then use the bulk action bar to accept, reject,
              or reset their status in one click. Great for clearing batches of obvious matches or mismatches.
            </p>
          </div>
        </div>
      </Section>

      {/* ── Feature Guide: Companies ───────────────────────────────────── */}
      <Section title="Working with Companies" icon={Building2}>
        <div className="space-y-4">
          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Company Intelligence</h3>
            <p className="text-sm text-gray-600">
              Every company in the system is enriched with funding data, employee count, headquarters, tech stack,
              and hiring velocity. Use the <strong>"Recently Funded"</strong> filter to find companies most likely to be
              actively hiring and spending.
            </p>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">High-Value Filters</h3>
            <ul className="text-sm text-gray-600 space-y-1.5 ml-4 list-disc">
              <li><strong>Recently Funded</strong> -- Companies with funding in the last 180 days. These have budget and urgency.</li>
              <li><strong>Actively Hiring</strong> -- Companies with open job postings right now.</li>
              <li><strong>Is Target</strong> -- Companies your team has marked as priority accounts.</li>
              <li><strong>Has Contacts</strong> -- Companies with at least one enriched contact. Ready for outreach.</li>
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Contact Management</h3>
            <p className="text-sm text-gray-600 mb-2">
              On any company detail page, you'll find contacts organized by role: Executives, Engineering Leads,
              Hiring Managers, Talent/People Ops. Each contact has:
            </p>
            <ul className="text-sm text-gray-600 space-y-1 ml-4 list-disc">
              <li><strong>Email status</strong>: Valid (green), Unverified (yellow), Invalid (red), Catch-all (blue)</li>
              <li><strong>Decision maker flag</strong>: Star icon for contacts with budget authority</li>
              <li><strong>Outreach status</strong>: Track where you are -- Not Contacted, Emailed, Replied, Meeting Scheduled</li>
            </ul>
            <Tip>
              Always prioritize contacts marked as <strong>Decision Makers</strong> with <strong>Valid</strong> emails.
              Use the <strong>"Export Contacts"</strong> button to get a filtered CSV for your outreach tool.
            </Tip>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Warm Leads & Funding Signals</h3>
            <p className="text-sm text-gray-600">
              The Dashboard shows <strong>Warm Leads</strong> -- companies with both recent hiring activity AND
              verified contacts. <strong>Funding Signals</strong> highlight companies with fresh capital.
              Cross-reference both to find your best outreach targets.
            </p>
          </div>
        </div>
      </Section>

      {/* ── Feature Guide: Resume & Applications ──────────────────────── */}
      <Section title="Resumes, Scoring & Applications" icon={FileText}>
        <div className="space-y-4">
          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Managing Multiple Resumes</h3>
            <p className="text-sm text-gray-600">
              You can upload multiple resumes for different role types (e.g. one for Cloud/Infra, one for Security).
              The <strong>Active</strong> resume is used for all scoring and applications. Switch your active resume
              when targeting a different role cluster.
            </p>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Reading Your Score Results</h3>
            <p className="text-sm text-gray-600 mb-2">
              After scoring, your results show three key data points:
            </p>
            <ul className="text-sm text-gray-600 space-y-1 ml-4 list-disc">
              <li><strong>Matched Keywords</strong> (green) -- Skills already on your resume that the job requires. Your strengths.</li>
              <li><strong>Missing Keywords</strong> (red) -- Skills the job wants that aren't on your resume. Your gaps.</li>
              <li><strong>Suggestions</strong> -- Specific improvements to boost your score for that job.</li>
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">AI Resume Customization</h3>
            <p className="text-sm text-gray-600">
              For any job where your score is below your target, click <strong>"Customize for This Job"</strong>.
              The AI analyzes the job description and rewrites your resume to include missing keywords, reframe
              your experience, and improve alignment. Always <strong>review the output</strong> before using it -- the AI
              sometimes adds skills you don't have.
            </p>
            <Warning>
              AI customization requires the platform to have an Anthropic API key configured. If the button is grayed out,
              contact your admin.
            </Warning>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-2">Application Workflow</h3>
            <div className="flex flex-wrap items-center gap-2 mb-3">
              <FlowStep label="Prepared" color="bg-gray-100 text-gray-700" />
              <FlowArrow />
              <FlowStep label="Submitted" color="bg-blue-100 text-blue-700" />
              <FlowArrow />
              <FlowStep label="Applied" color="bg-indigo-100 text-indigo-700" />
              <FlowArrow />
              <FlowStep label="Interview" color="bg-purple-100 text-purple-700" />
              <FlowArrow />
              <FlowStep label="Offer" color="bg-green-100 text-green-700" />
            </div>
            <p className="text-sm text-gray-600">
              From a job detail page, the <strong>Apply</strong> button runs a readiness check (resume, credentials, answers).
              Your answer book auto-fills the application form. Review the answers, edit if needed, then submit.
              Track all applications in the <strong>Applications</strong> tab.
            </p>
          </div>
        </div>
      </Section>

      {/* ── Auto-apply: what is automatic on each site (F393) ─────────── */}
      <Section title="Auto-apply: what is automatic on each site" icon={Bot} id="auto-apply">
        <div className="space-y-4">
          <p className="text-sm text-gray-600">
            <Link to="/apply" className="font-semibold text-primary-600 hover:text-primary-700 underline">Auto-apply</Link> fills
            and submits the employer&apos;s real application form for you. What it can do depends on the site the form is
            hosted on. Paste a posting link on the Auto-apply page and the site is recognised automatically.
          </p>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Where the answers come from</h3>
            <ul className="text-sm text-gray-600 space-y-1 ml-4 list-disc">
              <li>Every answer comes from your <Link to="/answer-book" className="font-semibold text-primary-600 hover:text-primary-700 underline">Answer Book</Link> (plus your name, email and phone from your résumé). Nothing is invented.</li>
              <li>A question with <strong>no answer</strong> stops the application in <strong>Needs you</strong> — you answer it there, it&apos;s saved, and the application continues.</li>
              <li>For a role-specific free-text question (&ldquo;why are you a good fit&rdquo;, &ldquo;describe your experience with X&rdquo;) we <strong>draft an answer</strong> from your résumé, your Answer Book and the job description, fact-check every claim against them, and revise once if anything is unsupported. The draft waits in Needs you for you to edit and approve, with any claim that still could not be traced marked in red. Employers who ask for your own words get no draft. Nothing drafted is ever sent on its own.</li>
              <li>Answering a question in Needs you saves it and clears the same question on every other application waiting for it.</li>
              <li>Work authorization, sponsorship, salary and diversity questions are <strong>never guessed</strong>: only an answer you saved under that exact question is used.</li>
              <li>A loose match (same category, different wording) is shown as <strong>&ldquo;Guessed — not sent&rdquo;</strong> when you review. Guesses are never sent: a required one stops in Needs you, an optional one is left blank.</li>
              <li>If your saved answer isn&apos;t one of the form&apos;s options (e.g. &ldquo;5&rdquo; where the form offers 0 / 1 / 2+), it stops in Needs you instead of being forced.</li>
            </ul>
            <Warning>
              A wrong answer that exactly matches a question <em>will</em> be sent — the system cannot tell a wrong answer from a right one. Keep the Answer Book accurate and replace any entry marked &ldquo;[TEST …]&rdquo;.
            </Warning>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Levels</h3>
            <ul className="text-sm text-gray-600 space-y-1 ml-4 list-disc">
              <li><strong>Automatic</strong> — we fill and submit; only an unanswered question stops it.</li>
              <li><strong>We fill, you submit</strong> — the site puts a CAPTCHA checkbox on submit. We prepare every answer; you paste them in your own browser and press &ldquo;Mark applied&rdquo;.</li>
              <li><strong>Link only</strong> — we find the posting but can&apos;t drive the form.</li>
              <li><strong>Not supported</strong> — a pasted link is refused with the reason.</li>
            </ul>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-2">Every site</h3>
            <AtsCoverageGuide />
          </div>
        </div>
      </Section>

      {/* ── Feature Guide: Pipeline ────────────────────────────────────── */}
      <Section title="Sales Pipeline" icon={GitBranch}>
        <div className="space-y-4">
          <p className="text-sm text-gray-600">
            The pipeline is a Kanban board tracking companies through your sales process. Companies enter the pipeline
            when you accept jobs from them or manually add them.
          </p>
          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Working the Board</h3>
            <ul className="text-sm text-gray-600 space-y-1.5 ml-4 list-disc">
              <li>Drag cards or use arrows to move companies between stages</li>
              <li>Set <strong>priority</strong> (1-10) -- higher priority cards sort to the top</li>
              <li>Add <strong>notes</strong> on each card for context (meeting outcomes, blockers, next steps)</li>
              <li>Assign a <strong>resume</strong> to track which persona you're using for that company</li>
              <li>Velocity badge shows if the company is hiring fast (High), normal (Medium), or slow (Low)</li>
            </ul>
          </div>
          <Tip>
            Keep your pipeline clean. Move stale entries to the appropriate stage or archive them.
            A pipeline with 50+ entries in "New Lead" is just a list -- use stages to reflect actual progress.
          </Tip>
        </div>
      </Section>

      {/* ── Feature Guide: Answer Book ─────────────────────────────────── */}
      <Section title="Answer Book Best Practices" icon={BookOpen}>
        <div className="space-y-4">
          <p className="text-sm text-gray-600">
            Your answer book is a library of pre-written answers that auto-fill job application forms. The better your answer book, the faster your applications.
          </p>
          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Categories to Complete</h3>
            <div className="grid gap-2 sm:grid-cols-2">
              {[
                { cat: "Personal Info", examples: "Full name, email, phone, location, LinkedIn URL, portfolio" },
                { cat: "Work Authorization", examples: "Visa status, work permits, country eligibility, sponsorship needs" },
                { cat: "Experience", examples: "Years of experience, current title, previous companies, notice period" },
                { cat: "Skills", examples: "Programming languages, cloud platforms, certifications, tools" },
                { cat: "Preferences", examples: "Salary expectations, remote preference, start date, travel willingness" },
                { cat: "Custom", examples: "Cover letter snippets, 'why this company' templates, diversity responses" },
              ].map((c) => (
                <div key={c.cat} className="rounded-lg border border-gray-100 bg-gray-50 p-3">
                  <h4 className="text-xs font-semibold text-gray-900">{c.cat}</h4>
                  <p className="mt-0.5 text-xs text-gray-500">{c.examples}</p>
                </div>
              ))}
            </div>
          </div>
          <Tip>
            After each application, check if any questions came up that weren't in your answer book.
            Add them so the next application is even faster. The system shows <strong>coverage stats</strong> so
            you can see what percentage of typical application fields you have pre-answered.
          </Tip>
        </div>
      </Section>

      {/* ── Things to Remember ─────────────────────────────────────────── */}
      <Section title="Things to Remember" icon={AlertTriangle} defaultOpen={true}>
        <div className="space-y-3">
          {[
            {
              icon: RefreshCw,
              title: "Scans run automatically",
              desc: "New jobs are scraped every few hours from all connected ATS boards. You don't need to trigger scans manually -- just check for new jobs in the review queue each morning.",
              type: "info" as const,
            },
            {
              icon: FileText,
              title: "Set your active resume before scoring",
              desc: "The system scores whichever resume is marked 'Active'. If you upload a new version, make sure to set it as active and re-run scoring.",
              type: "warning" as const,
            },
            {
              icon: ClipboardCheck,
              title: "Tag every rejection",
              desc: "When rejecting jobs in the review queue, always select a tag (location mismatch, seniority, etc). This data helps the team understand patterns and tune role clusters.",
              type: "warning" as const,
            },
            {
              icon: BookOpen,
              title: "Keep your answer book current",
              desc: "Outdated answers (old salary range, expired visa, wrong availability) can hurt your applications. Review and update monthly, or after any life change.",
              type: "warning" as const,
            },
            {
              icon: Target,
              title: "Relevance score is a starting point, not a verdict",
              desc: "A score of 55 doesn't mean the job is bad -- it means the automated signals are mixed. Always read the job description for roles in the 40-70 range before deciding.",
              type: "info" as const,
            },
            {
              icon: Building2,
              title: "Enrich companies before outreach",
              desc: "Click 'Enrich' on a company profile to fetch the latest funding data, contacts, and office locations. Enrichment takes ~30 seconds.",
              type: "info" as const,
            },
            {
              icon: Brain,
              title: "Review AI-customized resumes carefully",
              desc: "The AI may add skills or reframe experience in ways that don't match reality. Always read through the full customized version before using it for an application.",
              type: "warning" as const,
            },
            {
              icon: Shield,
              title: "Your data is isolated",
              desc: "Resumes, credentials, answer book entries, and applications are all private to you. Teammates cannot see your data. Only admins have cross-user visibility for oversight.",
              type: "info" as const,
            },
            {
              icon: Upload,
              title: "Attachments on tickets",
              desc: "When submitting bug reports, attach screenshots showing the issue. Drag and drop files directly into the ticket form. Max 10 MB per file.",
              type: "info" as const,
            },
            {
              icon: Zap,
              title: "Use bulk actions for efficiency",
              desc: "On the Jobs page, use checkboxes to select multiple jobs, then bulk-accept or bulk-reject. Much faster than reviewing one at a time for obvious matches/mismatches.",
              type: "info" as const,
            },
          ].map((item) => (
            <div
              key={item.title}
              className={`flex gap-3 rounded-lg px-4 py-3 ${
                item.type === "warning"
                  ? "bg-amber-50 border border-amber-100"
                  : "bg-blue-50 border border-blue-100"
              }`}
            >
              <item.icon
                className={`h-4 w-4 mt-0.5 flex-shrink-0 ${
                  item.type === "warning" ? "text-amber-600" : "text-blue-600"
                }`}
              />
              <div>
                <h4
                  className={`text-sm font-semibold ${
                    item.type === "warning" ? "text-amber-900" : "text-blue-900"
                  }`}
                >
                  {item.title}
                </h4>
                <p
                  className={`mt-0.5 text-xs ${
                    item.type === "warning" ? "text-amber-700" : "text-blue-700"
                  }`}
                >
                  {item.desc}
                </p>
              </div>
            </div>
          ))}
        </div>
      </Section>

      {/* ── Platform Glossary ──────────────────────────────────────────── */}
      <Section title="Key Terms" icon={BookOpen}>
        <div className="grid gap-2 sm:grid-cols-2">
          {[
            { term: "Role Cluster", def: "A category of job titles (Infra, Security, QA). Defines which jobs are 'relevant' to your team." },
            { term: "ATS Board", def: "A company's job board on an Applicant Tracking System (Greenhouse, Lever, etc). The platform scrapes these for new jobs." },
            { term: "Remote scope", def: "Where the candidate can work from: Worldwide remote (anywhere), Country-restricted remote (e.g. US-only, UAE-only — companion country list says which), Region-restricted remote (EMEA / APAC), Hybrid (office + remote), On-site (no remote), or Needs classification." },
            { term: "Relevance Score", def: "0-100 score measuring how well a job matches your team's criteria. Higher = better match." },
            { term: "ATS Score", def: "0-100 score measuring how well your resume matches a specific job. Used by actual ATS systems." },
            { term: "Warm Lead", def: "A company that is actively hiring, has verified contacts, and shows recent activity. Best outreach targets." },
            { term: "Funding Signal", def: "A recently funded company. Fresh capital usually means new hires and budget for services." },
            { term: "Enrichment", def: "The process of fetching detailed company data: contacts, funding, offices, tech stack." },
            { term: "Decision Maker", def: "A contact with hiring authority or budget control. Prioritize these for outreach." },
            { term: "Answer Book", def: "Your stored answers for common application questions. Auto-fills forms when you apply." },
            { term: "Discovery Scan", def: "An admin-triggered scan that probes for new companies and ATS boards not yet in the system." },
            { term: "Pipeline", def: "Kanban board tracking companies through your sales process from first contact to close." },
          ].map((item) => (
            <div key={item.term} className="rounded-lg border border-gray-100 bg-gray-50 px-3 py-2.5">
              <h4 className="text-xs font-semibold text-gray-900">{item.term}</h4>
              <p className="mt-0.5 text-xs text-gray-500">{item.def}</p>
            </div>
          ))}
        </div>
      </Section>

      {/* ── Ticket System ──────────────────────────────────────────────── */}
      <Section title="Submitting Tickets" icon={MessageSquarePlus}>
        <div className="space-y-4">
          <p className="text-sm text-gray-600">
            Use the <strong>Tickets</strong> page to report bugs, request features, or suggest improvements.
            All tickets go directly to the admin team -- no approval step needed.
          </p>
          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-2">Ticket Categories</h3>
            <div className="grid gap-2 sm:grid-cols-2">
              {[
                {
                  cat: "Bug Report",
                  color: "bg-red-100 text-red-700",
                  desc: "Something broken or not working as expected. Include steps to reproduce, expected vs actual behavior.",
                },
                {
                  cat: "Feature Request",
                  color: "bg-purple-100 text-purple-700",
                  desc: "A new capability you want. Describe the use case, who benefits, and the expected impact.",
                },
                {
                  cat: "Improvement",
                  color: "bg-blue-100 text-blue-700",
                  desc: "An existing feature that could work better. Describe what's slow or awkward and your ideal workflow.",
                },
                {
                  cat: "Question",
                  color: "bg-gray-100 text-gray-700",
                  desc: "Anything you're confused about. Check this docs page first, then submit a question if needed.",
                },
              ].map((c) => (
                <div key={c.cat} className="rounded-lg border border-gray-100 p-3">
                  <span className={`inline-block rounded px-2 py-0.5 text-xs font-semibold ${c.color}`}>
                    {c.cat}
                  </span>
                  <p className="mt-1.5 text-xs text-gray-600">{c.desc}</p>
                </div>
              ))}
            </div>
          </div>
          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-2">Ticket Status Flow</h3>
            <div className="flex flex-wrap items-center gap-2">
              <FlowStep label="Open" color="bg-blue-100 text-blue-700" />
              <FlowArrow />
              <FlowStep label="In Progress" color="bg-yellow-100 text-yellow-700" />
              <FlowArrow />
              <FlowStep label="Resolved" color="bg-green-100 text-green-700" />
              <FlowArrow />
              <FlowStep label="Closed" color="bg-gray-100 text-gray-700" />
            </div>
          </div>
          <Tip>
            <strong>For bugs:</strong> attach a screenshot showing the problem. Drag files directly into the ticket form.
            The more detail you include upfront, the faster it gets fixed.
          </Tip>
        </div>
      </Section>

      {/* ── Data Privacy ───────────────────────────────────────────────── */}
      <Section title="Data Privacy & Security" icon={Shield}>
        <div className="space-y-2 text-sm text-gray-700">
          {[
            { bold: "Your resumes are private.", text: "Only you can see your uploaded resumes, scores, and AI customizations. Admins can view all resumes for oversight." },
            { bold: "Your credentials are private.", text: "Platform login credentials are encrypted and tied to your resume persona. Nobody else can see them." },
            { bold: "Your answer book is private.", text: "Each salesperson maintains their own answer book. Answers are never shared across users." },
            { bold: "Your applications are private.", text: "Only you can see your submitted applications and their status." },
            { bold: "No data is ever deleted.", text: "When you remove a resume, credential, or application, it is archived -- never permanently deleted." },
          ].map((item) => (
            <div key={item.bold} className="flex items-start gap-2">
              <Shield className="mt-0.5 h-4 w-4 text-green-600 flex-shrink-0" />
              <span>
                <strong>{item.bold}</strong> {item.text}
              </span>
            </div>
          ))}
        </div>
      </Section>

      {/* ── Role Permissions (Admin Only) ──────────────────────────────── */}
      {isAdmin && (
        <Section title="Role Permissions" icon={Users} badge="Admin">
          <div className="grid gap-4 md:grid-cols-2">
            {rolePermissions.map((rp) => (
              <div key={rp.role} className="rounded-lg border border-gray-100 p-4">
                <span className={`inline-block rounded-full px-3 py-1 text-xs font-semibold ${rp.color}`}>
                  {rp.role}
                </span>
                <ul className="mt-3 space-y-1">
                  {rp.permissions.map((p) => (
                    <li key={p} className="flex items-start gap-2 text-sm text-gray-700">
                      <span className="mt-1.5 h-1.5 w-1.5 flex-shrink-0 rounded-full bg-gray-400" />
                      {p}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* ── Admin-Only Guide ───────────────────────────────────────────── */}
      {isAdmin && (
        <Section title="Admin Operations Guide" icon={Settings} badge="Admin">
          <div className="space-y-4">
            <div>
              <h3 className="text-sm font-semibold text-gray-900 mb-1">Scan Management</h3>
              <ul className="text-sm text-gray-600 space-y-1.5 ml-4 list-disc">
                <li><strong>Full Scan</strong>: Scrapes all active ATS boards across all platforms. Takes 5-15 minutes. Run if data seems stale.</li>
                <li><strong>Platform Scan</strong>: Scrapes all boards for one platform (e.g. all Greenhouse boards). Use for targeted refreshes.</li>
                <li><strong>Discovery Scan</strong>: Probes for new companies and boards not yet in the system. Run weekly to grow the database.</li>
                <li>Scans also run automatically via Celery Beat. Check <strong>Monitoring</strong> for last scan times.</li>
              </ul>
            </div>

            <div>
              <h3 className="text-sm font-semibold text-gray-900 mb-1">Role Cluster Configuration</h3>
              <p className="text-sm text-gray-600">
                Edit role clusters in <Link to="/role-clusters" className="font-semibold text-primary-600 hover:text-primary-700 underline">Role Clusters</Link> (Admin sidebar). Changes affect which jobs show as "relevant"
                and how scoring works. After changing clusters, jobs will be re-scored on the next scan.
                Each cluster has: keywords (for matching job titles) and approved roles (exact title matches).
              </p>
            </div>

            <div>
              <h3 className="text-sm font-semibold text-gray-900 mb-1">Monitoring Checklist</h3>
              <ul className="text-sm text-gray-600 space-y-1.5 ml-4 list-disc">
                <li>Check <strong>Monitoring</strong> daily: DB health, Redis status, scan activity</li>
                <li>Watch for scan errors -- failed scans usually mean a company changed their ATS board URL</li>
                <li>Review <strong>Discovered Companies</strong> in Platforms tab weekly -- import promising ones</li>
                <li>Check ticket queue regularly and update status as issues are addressed</li>
              </ul>
            </div>
          </div>
        </Section>
      )}
    </div>
  );
}
