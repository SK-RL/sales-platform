import { useEffect } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { clsx } from "clsx";
import {
  LayoutDashboard,
  Briefcase,
  Star,
  List,
  ClipboardCheck,
  Building2,
  GitBranch,
  BarChart3,
  Settings,
  LogOut,
  Radio,
  Activity,
  FileText,
  Users,
  Tags,
  Send,
  BookOpen,
  KeyRound,
  MessageSquarePlus,
  HelpCircle,
  Brain,
  Sparkles,
  ShieldCheck,
  Bot,
  Clock,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { logout, getRoleClusters } from "@/lib/api";

const navigation = [
  { name: "Dashboard", to: "/", icon: LayoutDashboard },
  { name: "Relevant Jobs", to: "/jobs?role_cluster=relevant", icon: Star },
  // F260 regression fix: pre-fix this was ``/jobs`` (no query params).
  // JobsPage's filter-restore logic (line 137-159) falls back to
  // localStorage when the URL has no filter params. Users coming
  // from "Relevant Jobs" had ``role_cluster=relevant`` saved in
  // localStorage, so clicking "All Jobs" silently re-applied that
  // filter — both pages showed identical data (feedback fc0a750b
  // "Relevant Jobs and All jobs section has the same URL and
  // functionality"). Adding the explicit ``role_cluster=any`` token
  // forces the URL-precedence branch in JobsPage so the localStorage
  // fallback can't poison the navigation. The backend translates
  // ``any`` → no role-cluster filter (matches the legacy "" empty
  // value), so the wire shape of the request is unchanged.
  { name: "All Jobs", to: "/jobs?role_cluster=any", icon: List },
  { name: "Review Queue", to: "/review", icon: ClipboardCheck },
  { name: "Companies", to: "/companies", icon: Building2 },
  { name: "Platforms", to: "/platforms", icon: Radio },
  { name: "Resume Score", to: "/resume-score", icon: FileText },
  { name: "Answer Book", to: "/answer-book", icon: BookOpen },
  { name: "Credentials", to: "/credentials", icon: KeyRound },
  { name: "Applications", to: "/applications", icon: Send },
  // v6 Claude Routine Apply — operator panel. Kept near Applications
  // since that's its closest sibling (both view-apply state); the
  // routine itself is a superset-feature on top.
  { name: "Apply Routine", to: "/routine", icon: Bot },
  { name: "Pipeline", to: "/pipeline", icon: GitBranch },
  { name: "Analytics", to: "/analytics", icon: BarChart3 },
  { name: "Intelligence", to: "/intelligence", icon: Brain },
  // F237: AI Intelligence — per-user weekly insights + admin product
  // insights queue. Sparkles icon to distinguish from the (data-driven,
  // non-AI) Intelligence page.
  { name: "Insights", to: "/insights", icon: Sparkles },
  { name: "Feedback", to: "/feedback", icon: MessageSquarePlus },
  { name: "Docs", to: "/docs", icon: HelpCircle },
  { name: "Settings", to: "/settings", icon: Settings },
];

const adminNavigation = [
  { name: "Monitoring", to: "/monitoring", icon: Activity },
  { name: "Role Clusters", to: "/role-clusters", icon: Tags },
  // KYC profile docs vault — backend routes are gated on
  // require_role("admin"), so the hierarchy admits both admin and
  // super_admin. Shield icon distinguishes from generic "Docs" help.
  { name: "Profile Vault", to: "/profiles", icon: ShieldCheck },
  // Per-user IST work-time windows + extension-request review queue.
  // Backend gates the same way as Profile Vault — admin role admits
  // both admin and super_admin via the role hierarchy.
  { name: "Work Windows", to: "/work-windows", icon: Clock },
];

const superAdminNavigation = [
  { name: "User Management", to: "/users", icon: Users },
];

export function Sidebar({ mobile, onClose }: { mobile?: boolean; onClose?: () => void }) {
  const { user } = useAuth();
  const location = useLocation();

  // F264 — fetch the role-cluster catalog so the "Relevant Jobs"
  // sidebar entry can render a tooltip naming the currently-relevant
  // clusters. Pre-fix the link said "Relevant Jobs" with zero hint
  // about scope; users had to click and inspect results to figure
  // out what the filter actually contained. Tooltip is set as the
  // ``title`` attribute (no extra rendering cost; native browser
  // tooltip on hover). 10-min staleTime matches JobsPage to avoid
  // duplicate fetches.
  const roleClustersQ = useQuery({
    queryKey: ["role-clusters"],
    queryFn: getRoleClusters,
    staleTime: 10 * 60 * 1000,
  });
  const relevantClusterNames = (roleClustersQ.data?.items ?? [])
    .filter((c) => c.is_active && c.is_relevant)
    .map((c) => c.name);
  const relevantTooltip =
    relevantClusterNames.length > 0
      ? `Filters to: ${relevantClusterNames.join(", ")}`
      : "Filters to: infra, security (default — admin can change at /role-clusters)";

  // Close mobile drawer on route change
  const prevPath = location.pathname + location.search;
  useEffect(() => {
    if (mobile && onClose) onClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prevPath]);

  const handleLogout = async () => {
    try {
      await logout();
    } finally {
      window.location.href = "/login";
    }
  };

  return (
    <div className="flex h-full w-64 flex-col bg-primary-950">
      <div className="flex h-16 items-center gap-2 border-b border-primary-800 px-6">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary-500">
          <Briefcase className="h-4 w-4 text-white" />
        </div>
        <span className="text-lg font-bold text-white">Sales Platform</span>
      </div>

      {/*
        F239 (khushi.jain feedback "Panel — left side not scrollable"):
        the nav was `flex-1` but had no `overflow-y-auto`, so once the
        nav-item count exceeded the viewport (12 main + 2 admin + 1
        super_admin entries — more after F237 added "Insights"),
        items got clipped at the bottom and the user-profile footer
        was pushed off-screen on shorter viewports. Two-token fix:
          - `overflow-y-auto` so the nav scrolls when content
            exceeds available height
          - `min-h-0` to override flex's default `min-height: auto`
            which otherwise prevents flex-1 children from shrinking
            below their content size (and breaks overflow)
      */}
      <nav className="flex-1 min-h-0 overflow-y-auto space-y-1 px-3 py-4">
        {navigation.map((item) => {
          // Custom active check for items with query params.
          //
          // F260 regression fix: pre-fix the "All Jobs" link was
          // ``/jobs`` (no query params), and the active check at the
          // bottom branch (``pathname === item.to && !search.includes(
          // "role_cluster=relevant")``) handled it. After flipping
          // "All Jobs" to ``/jobs?role_cluster=any`` to defeat the
          // localStorage filter-restore (feedback fc0a750b), both
          // Jobs links now have query params. We disambiguate here
          // by walking the role_cluster value: relevant → "Relevant
          // Jobs", any (or absent) → "All Jobs".
          const itemPath = item.to.split("?")[0];
          let isActive: boolean;
          if (itemPath === "/jobs" && location.pathname === "/jobs") {
            const isRelevantLink = item.to.includes("role_cluster=relevant");
            const onRelevantPage = location.search.includes("role_cluster=relevant");
            isActive = isRelevantLink ? onRelevantPage : !onRelevantPage;
          } else if (item.to.includes("?")) {
            isActive = location.pathname + location.search === item.to;
          } else if (item.to === "/") {
            isActive = location.pathname === "/";
          } else {
            isActive = location.pathname === item.to;
          }

          // F339 regression fix (filter stickiness):
          //
          // The two ``/jobs?role_cluster=...`` sidebar links were
          // hardcoded to a static URL with ONLY the role_cluster
          // axis. When a user was on
          // ``/jobs?role_cluster=relevant&geography=usa_only&platform=
          // greenhouse`` and clicked "All Jobs" (or even re-clicked
          // "Relevant Jobs"), the navigation replaced the URL with
          // the static target, silently dropping geography and
          // platform. User report 2026-05-09: "issue of stickiness
          // of filter in the relevant jobs".
          //
          // Fix: dynamically construct the target by copying every
          // existing query param EXCEPT ``role_cluster`` and
          // ``page`` (the latter is intentionally reset so the user
          // doesn't land on page 7 of a different cluster), then
          // overriding ``role_cluster`` to the link's target value.
          // The static ``item.to`` continues to be the source of
          // truth for the link's INTENT (which cluster it points at);
          // the dynamic href is the actual click target.
          let dynamicTo: string = item.to;
          if (itemPath === "/jobs" && item.to.includes("role_cluster=")) {
            const targetParams = new URLSearchParams(item.to.split("?")[1] || "");
            const targetCluster = targetParams.get("role_cluster") || "";
            const currentParams = new URLSearchParams(location.search);
            currentParams.delete("page");
            currentParams.set("role_cluster", targetCluster);
            const qs = currentParams.toString();
            dynamicTo = qs ? `/jobs?${qs}` : "/jobs";
          }

          // F264 — Relevant Jobs link gets a tooltip listing the
          // currently-configured relevant clusters. ``All Jobs`` and
          // the rest stay tooltip-less to avoid noise.
          const tooltip =
            item.name === "Relevant Jobs" ? relevantTooltip : undefined;

          return (
            <NavLink
              key={item.name}
              to={dynamicTo}
              title={tooltip}
              className={clsx(
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-primary-700/50 text-white"
                  : "text-white/90 hover:bg-primary-800 hover:text-white"
              )}
            >
              <item.icon className="h-5 w-5 flex-shrink-0" />
              {item.name}
            </NavLink>
          );
        })}

        {(user?.role === "admin" || user?.role === "super_admin") && (
          <>
            <div className="my-2 border-t border-primary-800" />
            <p className="px-3 py-1 text-xs font-semibold text-white/70 uppercase">Admin</p>
            {adminNavigation.map((item) => (
              <NavLink
                key={item.name}
                to={item.to}
                className={({ isActive }) =>
                  clsx(
                    "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                    isActive
                      ? "bg-primary-700/50 text-white"
                      : "text-white/90 hover:bg-primary-800 hover:text-white"
                  )
                }
              >
                <item.icon className="h-5 w-5 flex-shrink-0" />
                {item.name}
              </NavLink>
            ))}
          </>
        )}

        {user?.role === "super_admin" && (
          <>
            {superAdminNavigation.map((item) => (
              <NavLink
                key={item.name}
                to={item.to}
                className={({ isActive }) =>
                  clsx(
                    "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                    isActive
                      ? "bg-primary-700/50 text-white"
                      : "text-white/90 hover:bg-primary-800 hover:text-white"
                  )
                }
              >
                <item.icon className="h-5 w-5 flex-shrink-0" />
                {item.name}
              </NavLink>
            ))}
          </>
        )}
      </nav>

      <div className="border-t border-primary-800 p-3">
        <div className="flex items-center gap-3 rounded-lg px-3 py-2">
          {user?.picture ? (
            <img
              src={user.picture}
              alt={user.name}
              className="h-8 w-8 rounded-full ring-2 ring-primary-700"
            />
          ) : (
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-primary-700 text-primary-200 text-sm font-medium">
              {user?.name?.charAt(0)?.toUpperCase() || "U"}
            </div>
          )}
          <div className="flex-1 min-w-0">
            <p className="truncate text-sm font-medium text-white">
              {user?.name || "User"}
            </p>
            <p className="truncate text-xs text-white/80">
              {user?.email || ""}
            </p>
          </div>
          <button
            onClick={handleLogout}
            className="rounded-lg p-1.5 text-white/70 hover:bg-primary-800 hover:text-white transition-colors"
            title="Sign out"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
