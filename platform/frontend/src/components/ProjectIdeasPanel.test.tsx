// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

let ideas: any;
let library: any = { ideas: [], running: false };
const draftProjectIdeas = vi.fn(async () => ({ queued: true, running: true }));
const chooseProjectIdea = vi.fn(async () => ({ ok: true }));

vi.mock("@/lib/api", () => ({
  getProjectIdeas: vi.fn(async () => ideas),
  draftProjectIdeas: (...a: any[]) => draftProjectIdeas(...(a as [])),
  chooseProjectIdea: (...a: any[]) => chooseProjectIdea(...(a as [])),
  getProjectIdeasLibrary: vi.fn(async () => library),
  draftProjectIdeasLibrary: vi.fn(async () => ({ queued: true })),
}));

import { ProjectIdeasPanel } from "./ProjectIdeasPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><ProjectIdeasPanel appId="a1" /></MemoryRouter></QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("ProjectIdeasPanel (F406)", () => {
  it("shows what was looked at, marks unverified evidence, and records a pick", async () => {
    ideas = {
      application_id: "a1", running: false,
      research: { found: { github: true, status: false, blog: false, errors: [] }, jd_terms: ["kubernetes"],
        sources: [{ id: "JD", kind: "job_description", title: "Job description: SRE", url: "", chars: 900 }, { id: "GITHUB", kind: "github", title: "GitHub organisation acme (12 public repos)", url: "https://github.com/acme", chars: 500 }] },
      research_verdict: "The operator repo is the strongest signal.",
      ideas: [
        { id: "idea-1", title: "Operator e2e harness", angle: "initiative", summary: "A kind-based test harness for their operator.", specificity: "company", grounded_evidence: 1, total_evidence: 2, effort_hours: 8,
          evidence: [{ source: "GITHUB", quote: "Kubernetes operator for Acme deployments", grounded: true, source_title: "GitHub", url: "https://github.com/acme" }, { source: "BLOG", quote: "we love Nomad", grounded: false, source_title: "(unknown source)", url: "" }],
          build: ["kind cluster", "envtest"], deliverable: "repo + README", skills_shown: ["Go", "Kubernetes"], recipient_role: "Platform lead", why_them: "owns the operator", risks: "scope" },
      ],
      chosen: null,
    };
    renderPanel();
    await screen.findByText("Operator e2e harness");
    expect(screen.getByText("Company-specific")).toBeTruthy();
    expect(screen.getByText("no status page")).toBeTruthy();
    expect(screen.getByText(/not found in the source, treat as unverified/)).toBeTruthy();
    expect(screen.getByText(/The operator repo is the strongest signal/)).toBeTruthy();
    fireEvent.click(screen.getByText("Pick this idea"));
    await waitFor(() => expect(chooseProjectIdea).toHaveBeenCalledWith("a1", "idea-1"));
    fireEvent.click(screen.getByText("What would be built, and for whom"));
    expect(screen.getByText(/Platform lead/)).toBeTruthy();
    expect(screen.getByText(/Building the project comes in the next stage/)).toBeTruthy();
  });

  it("falls back to the library with corpus coverage when nothing is company-specific", async () => {
    ideas = { application_id: "a1", running: false, research: { found: { github: false, status: false, blog: false, errors: [] }, jd_terms: [], sources: [] },
      ideas: [{ id: "idea-1", title: "Generic K8s", angle: "generic", summary: "s", specificity: "role", grounded_evidence: 1, total_evidence: 1, evidence: [], build: [], deliverable: "", skills_shown: [], recipient_role: "" }], chosen: null };
    library = { ideas: [{ id: "generic-1", title: "GitOps lab", terms: ["kubernetes", "argocd"], term_labels: ["Kubernetes", "GitOps (ArgoCD / Flux)"], coverage_pct: 62.5, summary: "s", build: ["a"], deliverable: "d", skills_shown: ["k"] }], jobs_in_corpus: 240, running: false };
    renderPanel();
    await screen.findByText("GitOps lab");
    expect(screen.getByText("fits 62.5% of postings")).toBeTruthy();
    expect(screen.getByText(/Nothing company-specific turned up/)).toBeTruthy();
    expect(screen.getByText(/of the 240 infra and security postings/)).toBeTruthy();
  });
});
