import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  RefreshCw,
  ToggleLeft,
  ToggleRight,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  Clock,
  Briefcase,
  CheckCircle2,
  XCircle,
  Plus,
  Trash2,
  Download,
  Ban,
  Search,
} from "lucide-react";
import { Card } from "@/components/Card";
import { Button } from "@/components/Button";
import { Badge } from "@/components/Badge";
import { BackendErrorBanner } from "@/components/BackendErrorBanner";
import { useAuth } from "@/lib/auth";
import {
  getPlatforms,
  getPlatformBoards,
  toggleBoard,
  triggerPlatformScan,
  getScanLogs,
  addBoard,
  deleteBoard,
  getDiscoveredCompanies,
  importDiscoveredCompany,
  bulkImportDiscovered,
  bulkIgnoreDiscovered,
  ignoreDiscoveredCompany,
} from "@/lib/api";
import { Pagination } from "@/components/Pagination";
import type { PlatformBoard, ScanLogEntry } from "@/lib/types";

const PLATFORM_COLORS: Record<string, string> = {
  greenhouse: "bg-green-100 text-green-800",
  lever: "bg-blue-100 text-blue-800",
  ashby: "bg-purple-100 text-purple-800",
  workable: "bg-orange-100 text-orange-800",
  bamboohr: "bg-lime-100 text-lime-800",
  career_page: "bg-gray-100 text-gray-800",
  linkedin: "bg-sky-100 text-sky-800",
  wellfound: "bg-pink-100 text-pink-800",
  recruitee: "bg-teal-100 text-teal-800",
  breezy: "bg-lime-100 text-lime-800",
  personio: "bg-rose-100 text-rose-800",
  rippling: "bg-yellow-100 text-yellow-800",
  jazzhr: "bg-fuchsia-100 text-fuchsia-800",
  teamtailor: "bg-cyan-100 text-cyan-800",
  smartrecruiters: "bg-yellow-100 text-yellow-800",
  jobvite: "bg-cyan-100 text-cyan-800",
  himalayas: "bg-emerald-100 text-emerald-800",
  indeed: "bg-indigo-100 text-indigo-800",
  remoteok: "bg-red-100 text-red-800",
  weworkremotely: "bg-amber-100 text-amber-800",
  remotive: "bg-violet-100 text-violet-800",
};

const VALID_PLATFORMS = ["greenhouse", "lever", "ashby", "workable", "bamboohr", "linkedin", "wellfound", "recruitee", "breezy", "personio", "rippling", "jazzhr", "teamtailor", "smartrecruiters", "jobvite", "himalayas"];

function formatTime(iso: string | null) {
  if (!iso) return "Never";
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function PlatformsPage() {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  // Mirror the backend role hierarchy: ``super_admin > admin > reviewer
  // > viewer`` (see app/api/deps.py::ROLE_HIERARCHY). Pre-fix the
  // strict equality ``user?.role === "admin"`` excluded super_admin
  // users from every admin button on this page (Boards / Scan Logs /
  // trigger-scan / add / delete) — they could see the platform cards
  // but not interact, even though the backend handlers correctly let
  // them through. ``["admin","super_admin"].includes(role)`` brings the
  // frontend gate in line with what the backend actually permits.
  const isAdmin = user?.role === "admin" || user?.role === "super_admin";

  const [activeTab, setActiveTab] = useState<"boards" | "discovered">("boards");
  const [expandedPlatform, setExpandedPlatform] = useState<string | null>(null);
  const [showLogs, setShowLogs] = useState<string | null>(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const [newBoard, setNewBoard] = useState({ company_name: "", platform: "greenhouse", slug: "" });
  const [addError, setAddError] = useState("");
  const [discFilter, setDiscFilter] = useState("");
  const [discPage, setDiscPage] = useState(1);
  const [selectedDisc, setSelectedDisc] = useState<Set<string>>(new Set());
  const [importErrorMsg, setImportErrorMsg] = useState<string | null>(null);

  // F222: 4 silent queries surfaced through `<BackendErrorBanner>`. The
  // boards / logs queries are admin-gated (F223) and only fire when a
  // row is expanded — enabled:false queries never errorIOSo they're
  // safe to include in the banner-queries array.
  const platformsQ = useQuery({
    queryKey: ["platforms"],
    queryFn: getPlatforms,
  });
  const platformData = platformsQ.data;
  const isLoading = platformsQ.isLoading;

  const boardsQ = useQuery({
    queryKey: ["platform-boards", expandedPlatform],
    queryFn: () => getPlatformBoards(expandedPlatform || undefined),
    enabled: !!expandedPlatform,
  });
  const boardsData = boardsQ.data;

  const logsQ = useQuery({
    queryKey: ["scan-logs", showLogs],
    queryFn: () => getScanLogs(showLogs || undefined),
    enabled: !!showLogs,
  });
  const logsData = logsQ.data;

  const discoveredQ = useQuery({
    queryKey: ["discovered-companies", discFilter, discPage],
    queryFn: () => getDiscoveredCompanies({ status: discFilter || undefined, page: discPage, per_page: 50 }),
    enabled: activeTab === "discovered",
  });
  const discovered = discoveredQ.data;
  const discLoading = discoveredQ.isLoading;

  const platformsQueries = [platformsQ, boardsQ, logsQ, discoveredQ];

  const toggleMutation = useMutation({
    mutationFn: (boardId: string) => toggleBoard(boardId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["platforms"] });
      queryClient.invalidateQueries({ queryKey: ["platform-boards"] });
    },
  });

  const scanMutation = useMutation({
    mutationFn: (platform: string) => triggerPlatformScan(platform),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["platforms"] });
    },
  });

  const addMutation = useMutation({
    mutationFn: (data: { company_name: string; platform: string; slug: string }) => addBoard(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["platforms"] });
      queryClient.invalidateQueries({ queryKey: ["platform-boards"] });
      setShowAddForm(false);
      setNewBoard({ company_name: "", platform: "greenhouse", slug: "" });
      setAddError("");
    },
    onError: (err: Error) => {
      setAddError(err.message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (boardId: string) => deleteBoard(boardId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["platforms"] });
      queryClient.invalidateQueries({ queryKey: ["platform-boards"] });
    },
  });

  const importMutation = useMutation({
    mutationFn: (id: string) => importDiscoveredCompany(id),
    onSuccess: () => {
      setImportErrorMsg(null);
      queryClient.invalidateQueries({ queryKey: ["discovered-companies"] });
      queryClient.invalidateQueries({ queryKey: ["platforms"] });
    },
    onError: (err: Error) => {
      setImportErrorMsg(err.message || "Import failed. Company may already exist.");
    },
  });

  const ignoreMutation = useMutation({
    mutationFn: (id: string) => ignoreDiscoveredCompany(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["discovered-companies"] });
    },
  });

  const bulkImportMutation = useMutation({
    mutationFn: (ids: string[]) => bulkImportDiscovered(ids),
    onSuccess: () => {
      setSelectedDisc(new Set());
      queryClient.invalidateQueries({ queryKey: ["discovered-companies"] });
      queryClient.invalidateQueries({ queryKey: ["platforms"] });
    },
  });

  const bulkIgnoreMutation = useMutation({
    mutationFn: (ids: string[]) => bulkIgnoreDiscovered(ids),
    onSuccess: () => {
      setSelectedDisc(new Set());
      queryClient.invalidateQueries({ queryKey: ["discovered-companies"] });
    },
  });

  const handleAddBoard = (e: React.FormEvent) => {
    e.preventDefault();
    setAddError("");
    if (!newBoard.company_name.trim() || !newBoard.slug.trim()) {
      setAddError("Company name and slug are required");
      return;
    }
    addMutation.mutate(newBoard);
  };

  const toggleDiscSelection = (id: string) => {
    setSelectedDisc((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAllDisc = () => {
    if (!discovered?.items) return;
    const allIds = discovered.items.map((d: any) => d.id);
    if (selectedDisc.size === allIds.length) {
      setSelectedDisc(new Set());
    } else {
      setSelectedDisc(new Set(allIds));
    }
  };

  const discStatusBadge = (status: string) => {
    switch (status) {
      case "added":
        return <Badge variant="success">Added</Badge>;
      case "ignored":
        return <Badge variant="gray">Ignored</Badge>;
      default:
        return <Badge variant="info">New</Badge>;
    }
  };

  const discItems = discovered?.items || [];
  const discTotal = discovered?.total || 0;
  const discTotalPages = discovered?.total_pages || Math.ceil(discTotal / 50) || 1;

  const platforms = platformData?.platforms || [];

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Platforms</h1>
          <p className="mt-1 text-sm text-gray-500">
            Monitor and manage ATS platforms being scraped
          </p>
        </div>
        {isAdmin && activeTab === "boards" && (
          <Button variant="primary" size="sm" onClick={() => setShowAddForm(!showAddForm)}>
            <Plus className="h-4 w-4" />
            Add Board
          </Button>
        )}
      </div>

      {/* F222: 4 previously-silent queries surface as a single banner. */}
      <BackendErrorBanner queries={platformsQueries} />

      {/* Tab buttons */}
      <div className="flex gap-1 border-b border-gray-200">
        <button
          onClick={() => setActiveTab("boards")}
          className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "boards"
              ? "border-primary-600 text-primary-600"
              : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
          }`}
        >
          <span className="flex items-center gap-2">
            <Briefcase className="h-4 w-4" />
            ATS Boards
          </span>
        </button>
        <button
          onClick={() => setActiveTab("discovered")}
          className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "discovered"
              ? "border-primary-600 text-primary-600"
              : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
          }`}
        >
          <span className="flex items-center gap-2">
            <Search className="h-4 w-4" />
            Discovered Companies
            {discovered?.total != null && discovered.total > 0 && (
              <span className="ml-1 rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">
                {discovered.total}
              </span>
            )}
          </span>
        </button>
      </div>

      {/* ===== BOARDS TAB ===== */}
      {activeTab === "boards" && (<>

      {/* Add board form (admin only) */}
      {showAddForm && isAdmin && (
        <Card>
          <h3 className="text-base font-semibold text-gray-900 mb-4">Add New ATS Board</h3>
          <form onSubmit={handleAddBoard} className="space-y-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
              <div>
                <label className="label">Company Name</label>
                <input
                  type="text"
                  className="input"
                  placeholder="e.g. Stripe"
                  value={newBoard.company_name}
                  onChange={(e) => setNewBoard({ ...newBoard, company_name: e.target.value })}
                />
              </div>
              <div>
                <label className="label">Platform</label>
                <select
                  className="input"
                  value={newBoard.platform}
                  onChange={(e) => setNewBoard({ ...newBoard, platform: e.target.value })}
                >
                  {VALID_PLATFORMS.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label">Board Slug</label>
                <input
                  type="text"
                  className="input"
                  placeholder="e.g. stripe"
                  value={newBoard.slug}
                  onChange={(e) => setNewBoard({ ...newBoard, slug: e.target.value })}
                />
                <p className="mt-1 text-xs text-gray-400">
                  The company identifier in the ATS URL
                </p>
              </div>
            </div>
            {addError && (
              <p className="text-sm text-red-600">{addError}</p>
            )}
            <div className="flex items-center gap-2">
              <Button type="submit" variant="primary" size="sm" loading={addMutation.isPending}>
                Add Board
              </Button>
              <Button type="button" variant="secondary" size="sm" onClick={() => { setShowAddForm(false); setAddError(""); }}>
                Cancel
              </Button>
            </div>
          </form>
        </Card>
      )}

      {/* Platform overview cards */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {platforms.map((p) => (
          <Card key={p.name} padding="none" className="overflow-hidden">
            <div className="px-5 py-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className={`rounded-lg px-3 py-1.5 text-sm font-semibold ${PLATFORM_COLORS[p.name] || "bg-gray-100 text-gray-800"}`}>
                    {p.name}
                  </div>
                  <Badge variant={p.active_boards > 0 ? "success" : "gray"}>
                    {p.active_boards}/{p.total_boards} active
                  </Badge>
                </div>
                {isAdmin && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => scanMutation.mutate(p.name)}
                    loading={scanMutation.isPending}
                    title="Trigger scan"
                  >
                    <RefreshCw className="h-3.5 w-3.5" />
                  </Button>
                )}
              </div>

              {/* Regression finding 47: stat cards previously rendered blank
                  whitespace for platforms with zero jobs (bamboohr / jobvite /
                  recruitee / wellfound / weworkremotely). Root cause was a
                  `null`/`undefined` slipping through when a platform had
                  boards but no Job rows yet — `null.toLocaleString()` throws,
                  and React then renders nothing in place of the count. Null-
                  coalescing to 0 keeps the card readable ("0") and consistent
                  with the other stat cards on the page. */}
              <div className="mt-4 grid grid-cols-3 gap-3">
                <div className="text-center">
                  <p className="text-2xl font-bold text-gray-900">{(p.total_jobs ?? 0).toLocaleString()}</p>
                  <p className="text-xs text-gray-500">Total Jobs</p>
                </div>
                <div className="text-center">
                  <p className="text-2xl font-bold text-green-600">{(p.accepted_jobs ?? 0).toLocaleString()}</p>
                  <p className="text-xs text-gray-500">Accepted</p>
                </div>
                <div className="text-center">
                  <p className="text-2xl font-bold text-primary-600">{p.avg_score ?? 0}</p>
                  <p className="text-xs text-gray-500">Avg Score</p>
                </div>
              </div>

              {/* F250: per-platform "last run" inline summary. Pre-fix
                  the only signal here was a relative timestamp ("4h ago")
                  which left admins guessing what came back from that
                  run — was it 175 jobs, 0 jobs with an error, a fresh
                  scan that found 0 new because nothing posted?  Now the
                  card shows ``found · new (Xm ago)`` from the actual
                  most-recent ScanLog row. Falls back to the legacy
                  timestamp-only line when the backend doesn't supply
                  ``last_run`` (older API), so old client/new server and
                  vice-versa both render cleanly. */}
              <div className="mt-3 flex items-center justify-between border-t border-gray-100 pt-3">
                <div className="flex items-center gap-1.5 text-xs text-gray-500">
                  <Clock className="h-3 w-3" />
                  {p.last_run ? (
                    <span>
                      Last run:{" "}
                      <span className="font-medium text-gray-700">
                        {p.last_run.jobs_found.toLocaleString()} found
                      </span>
                      {p.last_run.new_jobs > 0 && (
                        <>
                          {" · "}
                          <span className="font-medium text-green-600">
                            {p.last_run.new_jobs.toLocaleString()} new
                          </span>
                        </>
                      )}
                      {" · "}
                      <span>{formatTime(p.last_run.completed_at || p.last_run.started_at)}</span>
                    </span>
                  ) : (
                    <span>Last scan: {formatTime(p.last_scan)}</span>
                  )}
                </div>
                {(p.last_run?.errors ?? 0) > 0 ? (
                  <div
                    className="flex items-center gap-1 text-xs text-red-500"
                    title={p.last_run?.error_message || "Last run had errors"}
                  >
                    <AlertCircle className="h-3 w-3" />
                    {p.last_run?.errors} err
                  </div>
                ) : p.total_errors > 0 ? (
                  <div
                    className="flex items-center gap-1 text-xs text-amber-600"
                    title="Cumulative errors across all scans (last run was clean)"
                  >
                    <AlertCircle className="h-3 w-3" />
                    {p.total_errors} total
                  </div>
                ) : null}
              </div>
            </div>

            <div className="flex border-t border-gray-100">
              {/* F223: Boards listing is now admin-gated on the backend
                  (was: open to all authenticated users) because it
                  reveals the internal scraping registry. Hide the
                  expand button for non-admins so viewers don't click
                  into a 403 — matches the F217 Scan Logs pattern. */}
              {isAdmin && (
                <button
                  onClick={() => setExpandedPlatform(expandedPlatform === p.name ? null : p.name)}
                  className="flex flex-1 items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 transition-colors"
                >
                  <Briefcase className="h-3 w-3" />
                  Boards
                  {expandedPlatform === p.name ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                </button>
              )}
              {/* F217: Scan Logs admin-gated on the backend — keep the
                  button hidden for non-admins so they don't click a button
                  that 403s. */}
              {isAdmin && (
                <button
                  onClick={() => setShowLogs(showLogs === p.name ? null : p.name)}
                  className="flex flex-1 items-center justify-center gap-1.5 border-l border-gray-100 px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 transition-colors"
                >
                  <Clock className="h-3 w-3" />
                  Scan Logs
                  {showLogs === p.name ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                </button>
              )}
            </div>
          </Card>
        ))}
      </div>

      {/* Expanded boards list */}
      {expandedPlatform && boardsData && (
        <Card>
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-base font-semibold text-gray-900">
              {expandedPlatform} Boards ({boardsData.total})
            </h3>
            <button
              onClick={() => setExpandedPlatform(null)}
              className="text-sm text-gray-500 hover:text-gray-700"
            >
              Close
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  <th className="px-3 py-2">Company</th>
                  <th className="px-3 py-2">Slug</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Last Scanned</th>
                  <th className="px-3 py-2">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {boardsData.items.map((board: PlatformBoard) => (
                  <tr key={board.id} className="hover:bg-gray-50">
                    <td className="px-3 py-2 font-medium text-gray-900">{board.company_name}</td>
                    <td className="px-3 py-2 text-gray-600 font-mono text-xs">{board.slug}</td>
                    <td className="px-3 py-2">
                      <Badge variant={board.is_active ? "success" : "gray"}>
                        {board.is_active ? "Active" : "Disabled"}
                      </Badge>
                    </td>
                    <td className="px-3 py-2 text-gray-500">{formatTime(board.last_scanned_at)}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2">
                        {isAdmin && (
                          <>
                            <button
                              onClick={() => toggleMutation.mutate(board.id)}
                              className="text-gray-400 hover:text-gray-600 transition-colors"
                              title={board.is_active ? "Disable" : "Enable"}
                            >
                              {board.is_active ? (
                                <ToggleRight className="h-5 w-5 text-green-500" />
                              ) : (
                                <ToggleLeft className="h-5 w-5" />
                              )}
                            </button>
                            <button
                              onClick={() => {
                                if (confirm(`Delete board ${board.slug}?`)) {
                                  deleteMutation.mutate(board.id);
                                }
                              }}
                              className="text-gray-400 hover:text-red-500 transition-colors"
                              title="Delete board"
                            >
                              <Trash2 className="h-4 w-4" />
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* Scan logs */}
      {showLogs && logsData && (
        <Card>
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-base font-semibold text-gray-900">
              Recent Scan Logs - {showLogs}
            </h3>
            <button
              onClick={() => setShowLogs(null)}
              className="text-sm text-gray-500 hover:text-gray-700"
            >
              Close
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  <th className="px-3 py-2">Source</th>
                  <th className="px-3 py-2">Time</th>
                  <th className="px-3 py-2">Found</th>
                  <th className="px-3 py-2">New</th>
                  <th className="px-3 py-2">Updated</th>
                  <th className="px-3 py-2">Errors</th>
                  <th className="px-3 py-2">Duration</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {logsData.items.slice(0, 30).map((log: ScanLogEntry) => (
                  <tr key={log.id} className="hover:bg-gray-50">
                    <td className="px-3 py-2 font-mono text-xs text-gray-700">{log.source}</td>
                    <td className="px-3 py-2 text-gray-500">{formatTime(log.started_at)}</td>
                    <td className="px-3 py-2">{log.jobs_found}</td>
                    <td className="px-3 py-2">
                      {log.new_jobs > 0 ? (
                        <span className="flex items-center gap-1 text-green-600">
                          <CheckCircle2 className="h-3 w-3" />
                          {log.new_jobs}
                        </span>
                      ) : (
                        <span className="text-gray-400">0</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-600">{log.updated_jobs}</td>
                    <td className="px-3 py-2">
                      {log.errors > 0 ? (
                        <span className="flex items-center gap-1 text-red-500" title={log.error_message}>
                          <XCircle className="h-3 w-3" />
                          {log.errors}
                        </span>
                      ) : (
                        <span className="text-gray-400">0</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-500">
                      {log.duration_ms > 0 ? `${(log.duration_ms / 1000).toFixed(1)}s` : "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* Scoring explanation */}
      <Card>
        <h3 className="text-base font-semibold text-gray-900 mb-3">
          Relevance Scoring
        </h3>
        <p className="text-sm text-gray-600 mb-4">
          Each job is scored 0-100 based on five weighted signals:
        </p>
        <div className="space-y-3">
          {[
            { label: "Title Match", weight: "40%", desc: "How well the title matches approved roles (DevOps, Cloud, Infra, SRE, Security, etc.)" },
            { label: "Company Fit", weight: "20%", desc: "Target companies score higher. Mark companies as targets to boost their jobs." },
            { label: "Geography Clarity", weight: "20%", desc: "Jobs with clear remote scope and geography bucket (global, USA, UAE) score higher." },
            { label: "Source Priority", weight: "10%", desc: "Tier 1 (Greenhouse, Lever, Ashby, Workable, Career Pages) > Tier 2 (LinkedIn, Wellfound) > Tier 3 (Indeed, Himalayas)" },
            { label: "Freshness", weight: "10%", desc: "Recently posted jobs score higher. Jobs older than 30 days score lowest." },
          ].map((item) => (
            <div key={item.label} className="flex items-start gap-3">
              <div className="flex-shrink-0 w-20">
                <span className="inline-block rounded-full bg-primary-100 px-2.5 py-0.5 text-xs font-semibold text-primary-700">
                  {item.weight}
                </span>
              </div>
              <div>
                <p className="text-sm font-medium text-gray-900">{item.label}</p>
                <p className="text-xs text-gray-500">{item.desc}</p>
              </div>
            </div>
          ))}
        </div>
      </Card>

      </>)}

      {/* ===== DISCOVERED COMPANIES TAB ===== */}
      {activeTab === "discovered" && (
        <>
          {/* Filter bar and bulk actions */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-gray-700">Status:</span>
              {[
                { label: "All", value: "" },
                { label: "New", value: "new" },
                { label: "Added", value: "added" },
                { label: "Ignored", value: "ignored" },
              ].map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => { setDiscFilter(opt.value); setDiscPage(1); setSelectedDisc(new Set()); }}
                  className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                    discFilter === opt.value
                      ? "bg-primary-600 text-white"
                      : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>

            {selectedDisc.size > 0 && (
              <div className="flex items-center gap-2">
                <span className="text-sm text-gray-500">{selectedDisc.size} selected</span>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => bulkImportMutation.mutate(Array.from(selectedDisc))}
                  loading={bulkImportMutation.isPending}
                >
                  <Download className="h-3.5 w-3.5" />
                  Import Selected
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => bulkIgnoreMutation.mutate(Array.from(selectedDisc))}
                  loading={bulkIgnoreMutation.isPending}
                >
                  <Ban className="h-3.5 w-3.5" />
                  Ignore Selected
                </Button>
              </div>
            )}
          </div>

          {importErrorMsg && (
            <div className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700 flex items-center justify-between">
              <span>{importErrorMsg}</span>
              <button onClick={() => setImportErrorMsg(null)} className="ml-2 text-red-500 hover:text-red-700">✕</button>
            </div>
          )}

          {/* Discovered companies table */}
          <Card padding="none">
            {discLoading ? (
              <div className="flex items-center justify-center py-12">
                <div className="spinner h-6 w-6" />
              </div>
            ) : discItems.length === 0 ? (
              <div className="py-12 text-center text-sm text-gray-500">
                No discovered companies found{discFilter ? ` with status "${discFilter}"` : ""}.
              </div>
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-gray-100 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        <th className="px-3 py-2 w-10">
                          <input
                            type="checkbox"
                            checked={discItems.length > 0 && selectedDisc.size === discItems.length}
                            onChange={toggleAllDisc}
                            className="h-4 w-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                          />
                        </th>
                        <th className="px-3 py-2">Name</th>
                        <th className="px-3 py-2">Platform</th>
                        <th className="px-3 py-2">Slug</th>
                        <th className="px-3 py-2">Status</th>
                        <th className="px-3 py-2">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-50">
                      {discItems.map((item: any) => (
                        <tr key={item.id} className="hover:bg-gray-50">
                          <td className="px-3 py-2">
                            <input
                              type="checkbox"
                              checked={selectedDisc.has(item.id)}
                              onChange={() => toggleDiscSelection(item.id)}
                              className="h-4 w-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                            />
                          </td>
                          <td className="px-3 py-2 font-medium text-gray-900">{item.name || item.company_name}</td>
                          <td className="px-3 py-2">
                            <span className={`rounded-lg px-2 py-0.5 text-xs font-semibold ${PLATFORM_COLORS[item.platform] || "bg-gray-100 text-gray-800"}`}>
                              {item.platform}
                            </span>
                          </td>
                          <td className="px-3 py-2 text-gray-600 font-mono text-xs">{item.slug}</td>
                          <td className="px-3 py-2">{discStatusBadge(item.status)}</td>
                          <td className="px-3 py-2">
                            <div className="flex items-center gap-2">
                              {item.status !== "added" && (
                                <Button
                                  variant="primary"
                                  size="sm"
                                  onClick={() => importMutation.mutate(item.id)}
                                  loading={importMutation.isPending}
                                  title="Import as board"
                                >
                                  <Download className="h-3.5 w-3.5" />
                                  Import
                                </Button>
                              )}
                              {item.status !== "ignored" && item.status !== "added" && (
                                <Button
                                  variant="secondary"
                                  size="sm"
                                  onClick={() => ignoreMutation.mutate(item.id)}
                                  loading={ignoreMutation.isPending}
                                  title="Ignore"
                                >
                                  <Ban className="h-3.5 w-3.5" />
                                  Ignore
                                </Button>
                              )}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {discTotalPages > 1 && (
                  <Pagination
                    page={discPage}
                    totalPages={discTotalPages}
                    onPageChange={(p) => { setDiscPage(p); setSelectedDisc(new Set()); }}
                  />
                )}
              </>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
