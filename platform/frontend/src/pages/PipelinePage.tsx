import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRight,
  ChevronLeft,
  Building2,
  Briefcase,
  AlertCircle,
  Plus,
  Pencil,
  Trash2,
  Check,
  X,
  Users,
  ExternalLink,
} from "lucide-react";
import { Link } from "react-router-dom";
import { Card } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { QueryBoundary } from "@/components/QueryBoundary";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import {
  getPipeline,
  updatePipelineClient,
  getPipelineStages,
  createPipelineStage,
  updatePipelineStage,
  deletePipelineStage,
  // F261 — drill-down: list every application under a pipeline card
  // and let the admin reassign their funnel stage from the side panel.
  getClientApplications,
  updateApplicationStage,
  // Ticket bac45b42 — manual pipeline card creation.
  createManualPipelineCard,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { ManualPipelineCardPayload, PipelineItem } from "@/lib/types";
import { formatCount } from "@/lib/format";

const STAGE_COLORS = [
  "bg-blue-500",
  "bg-purple-500",
  "bg-emerald-500",
  "bg-amber-500",
  "bg-green-500",
  "bg-red-500",
  "bg-cyan-500",
  "bg-pink-500",
  "bg-orange-500",
  "bg-teal-500",
  "bg-indigo-500",
  "bg-rose-500",
];

function getPriorityVariant(priority: number): "danger" | "warning" | "gray" {
  if (priority >= 7) return "danger";
  if (priority >= 4) return "warning";
  return "gray";
}

const velocityStyles: Record<string, { bg: string; text: string; label: string }> = {
  high: { bg: "bg-green-100", text: "text-green-700", label: "High" },
  medium: { bg: "bg-amber-100", text: "text-amber-700", label: "Medium" },
  low: { bg: "bg-gray-100", text: "text-gray-600", label: "Low" },
};

function PipelineCard({
  item,
  onMove,
  isMoving,
  isAdmin,
  stageOrder,
  // F261 — drill-down handler. When set, the card renders a small
  // "applications" button that opens the side panel for this client.
  // Optional so the prop is opt-in for any future caller that doesn't
  // need the affordance.
  onOpenApplications,
}: {
  item: PipelineItem;
  onMove: (id: string, stage: string) => void;
  isMoving: boolean;
  isAdmin: boolean;
  stageOrder: string[];
  onOpenApplications?: (clientId: string) => void;
}) {
  const currentIdx = stageOrder.indexOf(item.stage);
  const canMoveLeft = currentIdx > 0;
  const canMoveRight = currentIdx < stageOrder.length - 1;

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-3 shadow-sm">
      <div className="flex items-start justify-between mb-2">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-gray-100">
            <Building2 className="h-4 w-4 text-gray-500" />
          </div>
          <div>
            {/* Regression finding 56: company name is now a Link so cards
                are right-clickable / middle-clickable to the company page. */}
            {item.company_id ? (
              <Link
                to={`/companies/${item.company_id}`}
                className="text-sm font-semibold text-gray-900 leading-tight hover:text-primary-600 transition-colors"
                onClick={(e) => e.stopPropagation()}
              >
                {item.company_name}
              </Link>
            ) : (
              <p className="text-sm font-semibold text-gray-900 leading-tight">
                {item.company_name}
              </p>
            )}
            {item.total_open_roles > 0 && (
              <p className="text-xs text-gray-500 mt-0.5">
                {formatCount(item.total_open_roles)} open role{item.total_open_roles !== 1 ? "s" : ""}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {item.hiring_velocity && (() => {
            const v = velocityStyles[item.hiring_velocity] || velocityStyles.low;
            return (
              <span className={`inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-medium ${v.bg} ${v.text}`}>
                {v.label}
              </span>
            );
          })()}
          <Badge variant={getPriorityVariant(item.priority)}>
            {item.priority}
          </Badge>
        </div>
      </div>

      <div className="flex items-center gap-3 text-xs text-gray-500 mb-2 flex-wrap">
        <div className="flex items-center gap-1">
          <Briefcase className="h-3 w-3" />
          <span>{formatCount(item.accepted_jobs_count)} accepted</span>
        </div>
        <span>&middot;</span>
        <span>{formatCount(item.total_open_roles)} total</span>
        {/* F266 — applications count badge. Audit found 19/23 cards
            had zero applications behind them. Pre-fix, admins clicked
            "Apps" → empty drill-down → repeated on the next card.
            Surfacing the count here lets the admin see at a glance
            which targets actually have submitted-application
            activity vs which are research-only / accepted-but-not-
            applied. Backend F266 wires up the count via
            ``Application.company_id`` (F261 denormalisation). */}
        {typeof item.applications_count === "number" && (
          <>
            <span>&middot;</span>
            <span
              className={
                item.applications_count > 0
                  ? "font-medium text-emerald-600"
                  : "text-gray-400"
              }
            >
              {formatCount(item.applications_count)} app
              {item.applications_count === 1 ? "" : "s"}
            </span>
          </>
        )}
      </div>

      {item.notes && (
        <p className="text-xs text-gray-600 mb-2 line-clamp-2">{item.notes}</p>
      )}

      {isAdmin && (item.applied_by_name || item.resume_label) && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-500 mb-2">
          {item.applied_by_name && (
            <span>Assigned: <span className="font-medium text-gray-700">{item.applied_by_name}</span></span>
          )}
          {item.resume_label && (
            <span>Resume: <span className="font-medium text-gray-700">{item.resume_label}</span></span>
          )}
        </div>
      )}

      {item.last_job_at && (
        <p className="text-[11px] text-gray-400 mb-2">
          Last job:{" "}
          {new Date(item.last_job_at).toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
            year: "numeric",
          })}
        </p>
      )}

      <div className="flex items-center justify-between border-t border-gray-100 pt-2 mt-1">
        <button
          onClick={() => canMoveLeft && onMove(item.id, stageOrder[currentIdx - 1])}
          disabled={!canMoveLeft || isMoving}
          className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 disabled:opacity-30 disabled:pointer-events-none transition-colors"
          title="Move to previous stage"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <div className="flex items-center gap-2">
          {/* F261 — drill-down trigger. Only renders for admins (the
              parent gates ``onOpenApplications``) so non-admins
              never see a button that would 403 on click.
              F266 — disabled (with a different tooltip) when there
              are 0 applications under the card. Audit showed 19/23
              cards were empty when drilled into; users repeatedly
              clicked → empty panel → close → next card. Now the
              button visibly indicates "nothing to see here" up
              front. ``disabled:opacity-40 cursor-not-allowed`` makes
              the disabled state legible without removing the button
              entirely (so the count badge above still shows + the
              control surface stays consistent). */}
          {onOpenApplications && (() => {
            const hasApps = (item.applications_count ?? 0) > 0;
            return (
              <button
                onClick={() => hasApps && onOpenApplications(item.id)}
                disabled={!hasApps}
                className={
                  "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-medium transition-colors " +
                  (hasApps
                    ? "bg-gray-100 text-gray-700 hover:bg-gray-200"
                    : "bg-gray-50 text-gray-300 cursor-not-allowed")
                }
                title={
                  hasApps
                    ? "View applications under this company"
                    : "No applications under this card yet — accept jobs in /review and submit them via /applications"
                }
              >
                <Users className="h-3 w-3" />
                Apps
              </button>
            );
          })()}
          <span className="text-xs text-gray-400">
            {new Date(item.created_at).toLocaleDateString("en-US", {
              month: "short",
              day: "numeric",
            })}
          </span>
        </div>
        <button
          onClick={() => canMoveRight && onMove(item.id, stageOrder[currentIdx + 1])}
          disabled={!canMoveRight || isMoving}
          className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 disabled:opacity-30 disabled:pointer-events-none transition-colors"
          title="Move to next stage"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

function AddStageForm({ onAdd, onCancel }: { onAdd: (data: { key: string; label: string; color: string }) => void; onCancel: () => void }) {
  const [label, setLabel] = useState("");
  const [color, setColor] = useState(STAGE_COLORS[0]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!label.trim()) return;
    const key = label.trim().toLowerCase().replace(/\s+/g, "_").replace(/[^a-z0-9_]/g, "");
    onAdd({ key, label: label.trim(), color });
  };

  return (
    <form onSubmit={handleSubmit} className="min-w-[280px] max-w-[320px] flex-shrink-0 rounded-xl border-2 border-dashed border-gray-300 p-4 space-y-3">
      <input
        type="text"
        placeholder="Stage name..."
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-gray-900 focus:outline-none focus:ring-1 focus:ring-gray-900"
        autoFocus
      />
      <div className="flex flex-wrap gap-1.5">
        {STAGE_COLORS.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setColor(c)}
            className={`h-5 w-5 rounded-full ${c} ${color === c ? "ring-2 ring-offset-1 ring-gray-900" : ""}`}
          />
        ))}
      </div>
      <div className="flex gap-2">
        <button type="submit" className="flex items-center gap-1 rounded-md bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800">
          <Check className="h-3 w-3" /> Add
        </button>
        <button type="button" onClick={onCancel} className="flex items-center gap-1 rounded-md bg-gray-100 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-200">
          <X className="h-3 w-3" /> Cancel
        </button>
      </div>
    </form>
  );
}

function StageHeader({
  stage,
  count,
  isAdmin,
  onRename,
  onDelete,
}: {
  stage: { key: string; label: string; color: string };
  count: number;
  isAdmin: boolean;
  onRename: (label: string) => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(stage.label);

  const handleSave = () => {
    if (label.trim() && label.trim() !== stage.label) {
      onRename(label.trim());
    }
    setEditing(false);
  };

  return (
    <div className="mb-3 flex items-center gap-2">
      <div className={`h-2.5 w-2.5 rounded-full ${stage.color}`} />
      {editing ? (
        <div className="flex items-center gap-1 flex-1">
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") handleSave(); if (e.key === "Escape") setEditing(false); }}
            className="flex-1 rounded border border-gray-300 px-2 py-0.5 text-sm font-semibold focus:border-gray-900 focus:outline-none"
            autoFocus
          />
          <button onClick={handleSave} className="p-0.5 text-green-600 hover:text-green-700"><Check className="h-3.5 w-3.5" /></button>
          <button onClick={() => setEditing(false)} className="p-0.5 text-gray-400 hover:text-gray-600"><X className="h-3.5 w-3.5" /></button>
        </div>
      ) : (
        <>
          <h3 className="text-sm font-semibold text-gray-700">{stage.label}</h3>
          {isAdmin && (
            <div className="flex items-center gap-0.5 ml-1">
              <button onClick={() => setEditing(true)} className="p-0.5 text-gray-300 hover:text-gray-600 transition-colors" title="Rename stage">
                <Pencil className="h-3 w-3" />
              </button>
              <button onClick={onDelete} className="p-0.5 text-gray-300 hover:text-red-500 transition-colors" title="Remove stage">
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          )}
        </>
      )}
      <span className="ml-auto rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
        {count}
      </span>
    </div>
  );
}

export function PipelinePage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin" || user?.role === "super_admin";
  // Ticket bac45b42 — card creation is admin + reviewer (matches the
  // backend require_role("admin", "reviewer") on POST /pipeline/manual).
  const canCreateCard =
    isAdmin || user?.role === "reviewer";
  const queryClient = useQueryClient();
  const [showAddStage, setShowAddStage] = useState(false);
  const [showAddCard, setShowAddCard] = useState(false);
  // F261 — id of the pipeline client whose applications side-panel is
  // currently open. null = panel closed. Admin-only; the
  // ``onOpenApplications`` prop on PipelineCard is gated on isAdmin
  // below so non-admins can never trigger this state.
  const [drillClientId, setDrillClientId] = useState<string | null>(null);

  // F222: previously only destructured `data` — a 500/network error on
  // /pipeline silently rendered blank kanban columns with no error state.
  const pipelineQ = useQuery({
    queryKey: ["pipeline"],
    queryFn: getPipeline,
  });
  const { data: pipeline } = pipelineQ;

  const stagesQ = useQuery({
    queryKey: ["pipeline-stages"],
    queryFn: getPipelineStages,
    enabled: isAdmin,
  });
  const stagesData = stagesQ.data;

  const moveMutation = useMutation({
    mutationFn: ({ id, stage }: { id: string; stage: string }) =>
      updatePipelineClient(id, stage),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pipeline"] });
    },
  });

  const addStageMutation = useMutation({
    mutationFn: createPipelineStage,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pipeline"] });
      queryClient.invalidateQueries({ queryKey: ["pipeline-stages"] });
      setShowAddStage(false);
    },
  });

  const renameStageMutation = useMutation({
    mutationFn: ({ id, label }: { id: string; label: string }) =>
      updatePipelineStage(id, { label }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pipeline"] });
      queryClient.invalidateQueries({ queryKey: ["pipeline-stages"] });
    },
  });

  const deleteStageMutation = useMutation({
    mutationFn: deletePipelineStage,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pipeline"] });
      queryClient.invalidateQueries({ queryKey: ["pipeline-stages"] });
    },
  });

  const handleMove = (id: string, stage: string) => {
    moveMutation.mutate({ id, stage });
  };

  // F222: loading OR error routed through the shared boundary so a 500
  // on /pipeline surfaces "Try again" instead of an empty kanban board.
  if (pipelineQ.isLoading || pipelineQ.isError) {
    return (
      <div className="mx-auto max-w-2xl pt-10">
        <QueryBoundary query={pipelineQ}>
          <></>
        </QueryBoundary>
      </div>
    );
  }

  // Use stages_config from API response (dynamic from database)
  const stagesConfig = pipeline?.stages_config || [];
  const stageOrder = stagesConfig.map((s) => s.key);
  // F215: backend renamed `items` (dict-of-lists) → `by_stage` to reclaim
  // `items` for the canonical flat list. Kanban columns still render from
  // the per-stage dict.
  const stages = pipeline?.by_stage || {};

  // Build a lookup from key to stage config ID (for rename/delete)
  const stageIdMap: Record<string, string> = {};
  if (stagesData?.items) {
    for (const s of stagesData.items) {
      stageIdMap[s.key] = s.id;
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Pipeline</h1>
          <p className="mt-1 text-sm text-gray-500">
            {pipeline?.total ?? 0} companies in pipeline
          </p>
        </div>
        <div className="flex items-center gap-2">
          {canCreateCard && (
            <button
              onClick={() => setShowAddCard(true)}
              className="flex items-center gap-1.5 rounded-lg bg-primary-600 px-3 py-2 text-sm font-medium text-white hover:bg-primary-700 transition-colors"
            >
              <Plus className="h-4 w-4" />
              Add Card
            </button>
          )}
          {isAdmin && (
            <button
              onClick={() => setShowAddStage(true)}
              className="flex items-center gap-1.5 rounded-lg bg-gray-900 px-3 py-2 text-sm font-medium text-white hover:bg-gray-800 transition-colors"
            >
              <Plus className="h-4 w-4" />
              Add Stage
            </button>
          )}
        </div>
      </div>

      {/* F222: stages query is admin-only and non-critical (kanban still
          renders without it) — surface via banner rather than blocking. */}
      {isAdmin && <BackendErrorBanner queries={[stagesQ]} />}

      <div className="flex gap-4 overflow-x-auto pb-4">
        {stagesConfig.map((stage) => {
          const items = stages[stage.key] || [];
          return (
            <div
              key={stage.key}
              className="flex min-w-[280px] max-w-[320px] flex-shrink-0 flex-col"
            >
              <StageHeader
                stage={stage}
                count={items.length}
                isAdmin={isAdmin}
                onRename={(label) => {
                  const id = stageIdMap[stage.key];
                  if (id) renameStageMutation.mutate({ id, label });
                }}
                onDelete={() => {
                  const id = stageIdMap[stage.key];
                  if (id && confirm(`Remove "${stage.label}" stage? Items in this stage will be preserved but hidden.`)) {
                    deleteStageMutation.mutate(id);
                  }
                }}
              />

              <div className="flex-1 space-y-2 rounded-xl bg-gray-50 p-2 min-h-[200px]">
                {items.length > 0 ? (
                  items.map((item) => (
                    <PipelineCard
                      key={item.id}
                      item={item}
                      onMove={handleMove}
                      isMoving={moveMutation.isPending}
                      isAdmin={isAdmin}
                      stageOrder={stageOrder}
                      onOpenApplications={
                        isAdmin ? (id) => setDrillClientId(id) : undefined
                      }
                    />
                  ))
                ) : (
                  <div className="flex h-full items-center justify-center py-8">
                    <p className="text-xs text-gray-400">No companies</p>
                  </div>
                )}
              </div>
            </div>
          );
        })}

        {showAddStage && isAdmin && (
          <AddStageForm
            onAdd={(data) => {
              addStageMutation.mutate({ ...data, sort_order: stagesConfig.length });
            }}
            onCancel={() => setShowAddStage(false)}
          />
        )}
      </div>

      {pipeline && pipeline.total === 0 && stagesConfig.length > 0 && (
        <Card>
          <div className="py-10 text-center">
            <AlertCircle className="mx-auto h-10 w-10 text-gray-300" />
            <p className="mt-3 text-sm font-medium text-gray-900">
              Pipeline is empty
            </p>
            <p className="mt-1 text-sm text-gray-500">
              Accept some jobs to start building your company pipeline.
            </p>
          </div>
        </Card>
      )}

      {/* F261 — applications drill-down panel. Slides in from the right
          when an admin clicks "Apps" on a card. Lists every application
          across the team for the card's company, with the same inline
          stage selector used in the Team Pipeline tab. */}
      {drillClientId && (
        <ClientApplicationsPanel
          clientId={drillClientId}
          stagesConfig={stagesConfig}
          onClose={() => setDrillClientId(null)}
        />
      )}

      {/* Ticket bac45b42 — manual card creation modal. */}
      {showAddCard && canCreateCard && (
        <ManualCardModal
          stagesConfig={stagesConfig}
          onClose={() => setShowAddCard(false)}
          onCreated={() => {
            queryClient.invalidateQueries({ queryKey: ["pipeline"] });
            setShowAddCard(false);
          }}
        />
      )}
    </div>
  );
}


// Ticket bac45b42 — manual pipeline card creation. Four mandatory
// fields up top (company name, website, JD link, applied identity);
// everything else lives in a collapsible "More details" section so
// the fast path stays fast.
function ManualCardModal({
  stagesConfig,
  onClose,
  onCreated,
}: {
  stagesConfig: { key: string; label: string; color: string }[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [companyName, setCompanyName] = useState("");
  const [website, setWebsite] = useState("");
  const [jdLink, setJdLink] = useState("");
  const [appliedId, setAppliedId] = useState("");
  const [stage, setStage] = useState(stagesConfig[0]?.key ?? "");
  const [showMore, setShowMore] = useState(false);
  const [linkedinUrl, setLinkedinUrl] = useState("");
  const [fundingStatus, setFundingStatus] = useState("");
  const [designation, setDesignation] = useState("");
  const [salaryCurrent, setSalaryCurrent] = useState("");
  const [salaryExpected, setSalaryExpected] = useState("");
  const [interviewerName, setInterviewerName] = useState("");
  const [interviewerEmail, setInterviewerEmail] = useState("");
  const [intervieweeName, setIntervieweeName] = useState("");
  const [intervieweeType, setIntervieweeType] = useState("");
  const [jdDescription, setJdDescription] = useState("");
  const [details, setDetails] = useState("");
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async () => {
      const opt = (v: string) => (v.trim() ? v.trim() : undefined);
      const payload: ManualPipelineCardPayload = {
        company_name: companyName.trim(),
        company_website: website.trim(),
        jd_link: jdLink.trim(),
        applied_id: appliedId.trim(),
        stage: stage || undefined,
        linkedin_url: opt(linkedinUrl),
        funding_status: opt(fundingStatus),
        designation: opt(designation),
        salary_current: opt(salaryCurrent),
        salary_expected: opt(salaryExpected),
        interviewer_name: opt(interviewerName),
        interviewer_email: opt(interviewerEmail),
        interviewee_name: opt(intervieweeName),
        interviewee_type: opt(intervieweeType),
        jd_description: opt(jdDescription),
        details: opt(details),
      };
      return createManualPipelineCard(payload);
    },
    onSuccess: onCreated,
    onError: (e: unknown) =>
      setError(e instanceof Error ? e.message : "Failed to create card"),
  });

  const valid =
    companyName.trim() && website.trim() && jdLink.trim() && appliedId.trim();

  const field = (
    label: string,
    value: string,
    set: (v: string) => void,
    props: { placeholder?: string; required?: boolean; type?: string } = {},
  ) => (
    <div>
      <label className="block text-xs font-medium text-gray-600 mb-1">
        {label}{" "}
        {props.required ? (
          <span className="text-red-500">*</span>
        ) : (
          <span className="text-gray-400">(optional)</span>
        )}
      </label>
      <input
        type={props.type ?? "text"}
        value={value}
        onChange={(e) => set(e.target.value)}
        placeholder={props.placeholder}
        className="input w-full text-sm"
        required={props.required}
      />
    </div>
  );

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
          <h2 className="text-lg font-semibold text-gray-900">Add Pipeline Card</h2>
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
            {field("Company Name", companyName, setCompanyName, { required: true })}
            {field("Company Website", website, setWebsite, {
              required: true, placeholder: "https://…", type: "url",
            })}
            {field("JD Link", jdLink, setJdLink, {
              required: true, placeholder: "Link to the job description", type: "url",
            })}
            {field("Applied ID (Email/Name)", appliedId, setAppliedId, {
              required: true, placeholder: "Identity used to apply",
            })}
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Stage</label>
            <select
              value={stage}
              onChange={(e) => setStage(e.target.value)}
              className="input w-full text-sm"
            >
              {stagesConfig.map((s) => (
                <option key={s.key} value={s.key}>{s.label}</option>
              ))}
            </select>
          </div>

          <button
            type="button"
            onClick={() => setShowMore(!showMore)}
            className="flex items-center gap-1 text-sm text-primary-600 hover:text-primary-700"
          >
            {showMore ? "Hide" : "Show"} more details
            <ChevronRight className={`h-4 w-4 transition-transform ${showMore ? "rotate-90" : ""}`} />
          </button>

          {showMore && (
            <div className="space-y-4 rounded-lg border border-gray-100 bg-gray-50 p-4">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                {field("LinkedIn", linkedinUrl, setLinkedinUrl, { placeholder: "Company LinkedIn URL" })}
                {field("Funding Status", fundingStatus, setFundingStatus, { placeholder: "e.g. Series B" })}
                {field("Designation", designation, setDesignation, { placeholder: "Role being pursued" })}
                {field("Salary — Current", salaryCurrent, setSalaryCurrent)}
                {field("Salary — Expectation", salaryExpected, setSalaryExpected)}
                {field("Interviewer / Contact Name", interviewerName, setInterviewerName)}
                {field("Interviewer Email", interviewerEmail, setInterviewerEmail)}
                {field("Interviewee Name", intervieweeName, setIntervieweeName)}
                {field("Interviewee Type", intervieweeType, setIntervieweeType, {
                  placeholder: "Description type",
                })}
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  JD Description <span className="text-gray-400">(optional)</span>
                </label>
                <textarea
                  value={jdDescription}
                  onChange={(e) => setJdDescription(e.target.value)}
                  rows={3}
                  className="input w-full text-sm"
                  maxLength={8000}
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Details <span className="text-gray-400">(optional)</span>
                </label>
                <textarea
                  value={details}
                  onChange={(e) => setDetails(e.target.value)}
                  rows={3}
                  placeholder="Anything else worth tracking on this card…"
                  className="input w-full text-sm"
                  maxLength={8000}
                />
              </div>
            </div>
          )}

          {error && (
            <p className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>
          )}

          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg px-3 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!valid || mutation.isPending}
              className="flex items-center gap-1.5 rounded-lg bg-primary-600 px-3 py-2 text-sm font-medium text-white hover:bg-primary-700 disabled:opacity-50"
            >
              {mutation.isPending && <span className="spinner h-4 w-4" />}
              Create Card
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}


// F261 — Slide-out side panel listing applications under a single
// pipeline card. Opened from PipelineCard's "Apps" button. The panel
// is admin-only by virtue of:
//   1. The "Apps" button only renders when ``onOpenApplications`` is
//      passed (we gate that prop on ``isAdmin``).
//   2. The backend route ``GET /pipeline/{id}/applications`` requires
//      ``require_role("admin")``, so even a hand-crafted request from
//      a viewer's browser 403s.
function ClientApplicationsPanel({
  clientId,
  stagesConfig,
  onClose,
}: {
  clientId: string;
  stagesConfig: { key: string; label: string; color: string }[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const drillQ = useQuery({
    queryKey: ["pipeline-applications", clientId],
    queryFn: () => getClientApplications(clientId),
  });
  const stageMut = useMutation({
    mutationFn: ({ id, stageKey }: { id: string; stageKey: string | null }) =>
      updateApplicationStage(id, stageKey),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pipeline-applications", clientId] });
      queryClient.invalidateQueries({ queryKey: ["applications-team"] });
    },
  });
  const items = drillQ.data?.items ?? [];

  return (
    // Backdrop on click closes the panel — common modal pattern.
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/30"
      onClick={onClose}
    >
      <div
        className="h-full w-full max-w-2xl bg-white shadow-xl flex flex-col"
        // Stop click-through so clicking inside the panel doesn't close it.
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">
              Applications under this company
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {drillQ.isLoading
                ? "Loading…"
                : `${items.length} application${items.length === 1 ? "" : "s"}`}
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
            title="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5 space-y-3">
          <BackendErrorBanner queries={[drillQ]} />
          {!drillQ.isLoading && items.length === 0 && (
            <div className="rounded-lg border border-dashed border-gray-200 bg-gray-50 py-10 text-center">
              <Briefcase className="mx-auto h-10 w-10 text-gray-300" />
              <p className="mt-3 text-sm font-medium text-gray-900">
                No applications yet
              </p>
              <p className="mt-1 text-sm text-gray-500">
                Once a teammate applies to a role at this company, the
                row will appear here.
              </p>
            </div>
          )}
          {items.map((row) => (
            <div
              key={row.id}
              className="rounded-lg border border-gray-200 bg-white p-3 shadow-sm"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-semibold text-gray-900 truncate">
                    {row.job_title}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {row.applicant_name} · {row.applicant_email}
                  </p>
                </div>
                {row.job_url && (
                  <a
                    href={row.job_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                    title="Open ATS posting"
                  >
                    <ExternalLink className="h-4 w-4" />
                  </a>
                )}
              </div>
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-500">
                <span>
                  Resume:{" "}
                  <span className="font-medium text-gray-700">
                    {row.resume_label || "—"}
                  </span>
                </span>
                <span>
                  Applied:{" "}
                  <span className="font-medium text-gray-700">
                    {row.applied_at
                      ? new Date(row.applied_at).toLocaleDateString()
                      : new Date(row.created_at).toLocaleDateString()}
                  </span>
                </span>
                <span>
                  Status:{" "}
                  <span className="font-medium text-gray-700">{row.status}</span>
                </span>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <span className="text-[11px] text-gray-500">Stage:</span>
                <select
                  value={row.stage_key ?? ""}
                  onChange={(e) =>
                    stageMut.mutate({
                      id: row.id,
                      stageKey: e.target.value || null,
                    })
                  }
                  disabled={stageMut.isPending}
                  className="rounded-lg border border-gray-200 bg-white px-2 py-1 text-xs"
                >
                  <option value="">— no stage —</option>
                  {stagesConfig.map((s) => (
                    <option key={s.key} value={s.key}>
                      {s.label}
                    </option>
                  ))}
                </select>
              </div>
              {row.notes && (
                <p className="mt-2 text-xs text-gray-600 line-clamp-2">
                  {row.notes}
                </p>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
