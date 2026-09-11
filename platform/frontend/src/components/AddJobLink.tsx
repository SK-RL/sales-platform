import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Link2 } from "lucide-react";
import { getApplyReadiness, getJob, prepareApplication, resolveJobFromUrl } from "@/lib/api";

/**
 * F371 — "Add your own link", the Tsenta feature done honestly.
 *
 * Paste a posting URL; if it's on an ATS we can read, the job is
 * resolved (fetched from the board when we never scanned it), an
 * application is prepared, and you land on its review screen. If it
 * isn't, you get the reason and nothing is created — Tsenta leaves
 * Apply enabled on a link it understood nothing about; we don't.
 */
export function AddJobLink({ className = "" }: { className?: string }) {
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // F376c — a repost's resolution runs in the worker: poll the job until
  // it lands on the employer's form (or says why it can't).
  async function awaitResolution(jobId: string): Promise<string> {
    setPhase("Finding the employer's form…");
    const deadline = Date.now() + 120_000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2500));
      const job = await getJob(jobId);
      if (job.resolved_job_id) return job.resolved_job_id;
      if (job.apply_resolve_status && job.apply_resolve_status !== "blocked") {
        const plat = job.apply_platform;
        const where = plat
          ? ["workday", "icims", "taleo", "successfactors", "linkedin"].includes(plat)
            ? ` The employer's form is on ${plat}, which needs an account on their site.`
            : ` The employer's form is on ${plat}, which we can't drive from here.`
          : "";
        throw new Error(
          `Found the repost, but couldn't reach that role on an ATS we can drive.${where} Open it on Himalayas and paste the employer's apply link instead.`
        );
      }
    }
    throw new Error("Still looking for the employer's form — try again in a minute.");
  }

  async function go() {
    const u = url.trim();
    if (!u || busy) return;
    setBusy(true);
    setError(null);
    setPhase(null);
    try {
      const resolved = await resolveJobFromUrl(u);
      const jobId = resolved.pending ? await awaitResolution(resolved.job_id) : resolved.job_id;
      setPhase("Preparing…");
      // Pasting a job you already have should open it, not error
      // ("Application already exists for this job" — seen on production).
      const ready = await getApplyReadiness(jobId).catch(() => null);
      const existingId = ready?.existing_application?.exists ? ready.existing_application.id : null;
      const app = existingId ? { id: existingId } : await prepareApplication(jobId);
      const id = app?.id ?? app?.application_id;
      if (!id) throw new Error("The application could not be prepared.");
      navigate(`/applications/review?app=${id}`);
    } catch (e: any) {
      const msg: string = e?.message || "";
      setError(
        /status 50[24]|timed? ?out|gateway/i.test(msg)
          ? "Finding the employer's form is taking longer than usual. Try again in a minute — the lookup continues in the background."
          : msg || "Couldn't use that link."
      );
    } finally {
      setBusy(false);
      setPhase(null);
    }
  }

  return (
    <div className={className}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void go();
        }}
        className="flex items-center gap-2"
      >
        <label htmlFor="add-job-link" className="sr-only">
          Job posting link
        </label>
        <div className="relative flex-1">
          <Link2 className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400" />
          <input
            id="add-job-link"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="Paste a job posting link (Greenhouse, Lever, Ashby, Workable…)"
            className="w-full rounded-lg border border-gray-200 bg-white py-1.5 pl-7 pr-2 text-xs focus:border-gray-400 focus:outline-none"
          />
        </div>
        <button
          type="submit"
          disabled={busy || !url.trim()}
          className="shrink-0 rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-700 disabled:opacity-40"
        >
          {busy ? phase ?? "Reading…" : "Prepare"}
        </button>
      </form>
      {error && (
        <p role="alert" className="mt-1.5 text-xs text-red-600">
          {error}
        </p>
      )}
    </div>
  );
}
