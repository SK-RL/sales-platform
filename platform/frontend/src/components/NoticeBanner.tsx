import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { X, AlertTriangle, Info, AlertOctagon } from "lucide-react";
import { getMyNotices, dismissNotice } from "@/lib/api";
import type { UserNotice } from "@/lib/types";

// Per-level styling + icon. `critical` is the loud red (used for the
// document re-upload notice); `warning` amber; `info` blue.
const LEVEL_STYLES: Record<
  UserNotice["level"],
  { wrap: string; icon: typeof Info }
> = {
  info: { wrap: "border-blue-200 bg-blue-50 text-blue-800", icon: Info },
  warning: {
    wrap: "border-amber-200 bg-amber-50 text-amber-900",
    icon: AlertTriangle,
  },
  critical: {
    wrap: "border-red-200 bg-red-50 text-red-900",
    icon: AlertOctagon,
  },
};

/**
 * Renders the current user's undismissed login notices as a stack of
 * dismissible banners at the top of the app content. Silent when there
 * are none (the common case) — no layout cost, no spinner.
 */
export function NoticeBanner() {
  const queryClient = useQueryClient();
  const { data: notices } = useQuery({
    queryKey: ["notices", "me"],
    queryFn: getMyNotices,
    // Not critical-path; a slightly stale banner is fine and we don't
    // want it refetching aggressively on every window focus.
    staleTime: 5 * 60 * 1000,
  });

  const dismiss = useMutation({
    mutationFn: (id: string) => dismissNotice(id),
    // Optimistic: drop it from the list immediately so the click feels
    // instant; refetch reconciles.
    onMutate: async (id: string) => {
      await queryClient.cancelQueries({ queryKey: ["notices", "me"] });
      const prev = queryClient.getQueryData<UserNotice[]>(["notices", "me"]);
      queryClient.setQueryData<UserNotice[]>(
        ["notices", "me"],
        (old) => (old ?? []).filter((n) => n.id !== id)
      );
      return { prev };
    },
    onError: (_e, _id, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(["notices", "me"], ctx.prev);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["notices", "me"] });
    },
  });

  if (!notices || notices.length === 0) return null;

  return (
    <div className="mb-4 space-y-2">
      {notices.map((n) => {
        const style = LEVEL_STYLES[n.level] ?? LEVEL_STYLES.info;
        const Icon = style.icon;
        return (
          <div
            key={n.id}
            role="alert"
            className={`flex items-start gap-3 rounded-lg border px-4 py-3 ${style.wrap}`}
          >
            <Icon className="mt-0.5 h-5 w-5 flex-shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold">{n.title}</p>
              {n.body && (
                <p className="mt-0.5 whitespace-pre-line text-sm opacity-90">
                  {n.body}
                </p>
              )}
              {n.action_label && n.action_href && (
                <Link
                  to={n.action_href}
                  className="mt-2 inline-block text-sm font-semibold underline underline-offset-2 hover:opacity-80"
                >
                  {n.action_label}
                </Link>
              )}
            </div>
            <button
              type="button"
              onClick={() => dismiss.mutate(n.id)}
              disabled={dismiss.isPending}
              aria-label="Dismiss notice"
              className="flex-shrink-0 rounded p-1 hover:bg-black/5 disabled:opacity-50"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
