// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

/**
 * F355 — the "Needs you" review queue.
 *
 * The queue is where the whole apply gate becomes visible to a human, so
 * the things worth locking are the ones that change what someone does:
 *
 *  - the blocked reason is shown, and names the field
 *  - Submit is DISABLED while anything blocks, so the UI cannot ask the
 *    API to do what the gate will refuse
 *  - a guessed form is called out as guessed
 *  - locked fields explain themselves rather than being mysteriously
 *    greyed out
 *  - Prev/Next actually pages the queue
 *
 * Rendered against the real component with mocked API responses shaped
 * exactly like the backend's, so the wiring is covered rather than a
 * reimplementation of it.
 */

const submitApplication = vi.fn(async () => ({
  task_id: "t1", status: "queued", application_id: "a1", dry_run: false,
}));
const updateApplication = vi.fn(async () => ({}));

let queueItems: any[] = [];
let appDetail: any = {};
let questions: any = {};

vi.mock("@/lib/api", () => ({
  getApplications: vi.fn(async () => ({
    items: queueItems, total: queueItems.length, page: 1, page_size: 100, total_pages: 1,
  })),
  getApplication: vi.fn(async () => appDetail),
  getJobQuestions: vi.fn(async () => questions),
  submitApplication: (...a: any[]) => submitApplication(...(a as [])),
  updateApplication: (...a: any[]) => updateApplication(...(a as [])),
}));

import { ApplyReviewPage } from "./ApplyReviewPage";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ApplyReviewPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const APP = {
  id: "a1", job_id: "j1", job_title: "Senior SRE", company_name: "Acme",
  platform: "greenhouse", job_url: "https://boards.greenhouse.io/acme/jobs/1",
};

beforeEach(() => {
  queueItems = [APP];
  appDetail = { id: "a1", status: "needs_user", platform_response: {} };
  questions = { questions: [], coverage: { total: 0, answered: 0, high_confidence: 0, new_entries: 0 } };
  submitApplication.mockClear();
  updateApplication.mockClear();
});
afterEach(cleanup);

describe("empty queue", () => {
  it("says nothing needs you rather than showing an empty form", async () => {
    queueItems = [];
    renderPage();
    expect(await screen.findByText(/Nothing needs you/i)).toBeTruthy();
  });
});

describe("blocked application", () => {
  beforeEach(() => {
    appDetail = {
      id: "a1",
      status: "needs_user",
      platform_response: {
        gate: "blocked",
        reason: "1 required field(s) need your answer",
        blocking: [{
          field_key: "do_you_have_a_legal_right_to_work_in_the_us",
          label: "Legal right to work in the US?",
          reason:
            "This is a legal or protected-class question. We only answer it from an exact saved answer, never by inference — please set it.",
        }],
      },
    };
  });

  it("shows the gate's reason", async () => {
    renderPage();
    expect(await screen.findByText(/1 required field\(s\) need your answer/i)).toBeTruthy();
  });

  it("names the blocked field and why", async () => {
    renderPage();
    expect(await screen.findByText("Legal right to work in the US?")).toBeTruthy();
    expect(screen.getByText(/never by inference/i)).toBeTruthy();
  });

  it("points at the Answer Book for legal questions", async () => {
    renderPage();
    expect(await screen.findByText(/Answer Book/i)).toBeTruthy();
  });

  it("DISABLES submit while anything blocks", async () => {
    renderPage();
    const btn = (await screen.findByText("Submit application")) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("still allows a dry run — it sends nothing", async () => {
    renderPage();
    const btn = (await screen.findByText("Dry run")) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });
});

describe("guessed form", () => {
  beforeEach(() => {
    questions = {
      questions: [],
      coverage: { total: 0, answered: 0, high_confidence: 0, new_entries: 0 },
      schema: { extraction_mode: "fallback", platform: "workday", supported: false },
      blocking: [],
      safe_to_auto_submit: false,
    };
  });

  it("says we could not read the real form", async () => {
    renderPage();
    expect(
      await screen.findByText(/couldn't read this posting's real application form/i)
    ).toBeTruthy();
  });

  it("disables submit even with no per-field blockers", async () => {
    renderPage();
    const btn = (await screen.findByText("Submit application")) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });
});

describe("clean application", () => {
  beforeEach(() => {
    questions = {
      questions: [
        {
          field_key: "first_name", label: "First Name", field_type: "text",
          required: true, options: [], description: "", answer: "Sarthak",
          match_source: "manual", question_key: "first_name", confidence: "high",
          extraction_mode: "extracted",
        },
      ],
      coverage: { total: 1, answered: 1, high_confidence: 1, new_entries: 0 },
      schema: { extraction_mode: "extracted", platform: "greenhouse", supported: true },
      blocking: [],
      safe_to_auto_submit: true,
    };
  });

  it("enables submit", async () => {
    renderPage();
    await waitFor(async () => {
      const btn = (await screen.findByText("Submit application")) as HTMLButtonElement;
      expect(btn.disabled).toBe(false);
    });
  });

  it("shows the filled answer", async () => {
    renderPage();
    expect(await screen.findByText("Sarthak")).toBeTruthy();
  });

  it("submits for real when clicked", async () => {
    renderPage();
    const btn = await screen.findByText("Submit application");
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(btn);
    await waitFor(() => expect(submitApplication).toHaveBeenCalledWith("a1", { dryRun: false }));
  });

  it("dry run passes dryRun: true", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("Dry run"));
    await waitFor(() => expect(submitApplication).toHaveBeenCalledWith("a1", { dryRun: true }));
  });
});

describe("field provenance", () => {
  it("explains a never-infer field instead of just greying it", async () => {
    questions = {
      questions: [{
        field_key: "work_authorization", label: "Work authorization",
        field_type: "text", required: true, options: [], description: "",
        answer: "", match_source: "unmatched", question_key: "",
        confidence: "none", never_infer: true, needs_user: true,
        extraction_mode: "extracted",
      }],
      coverage: { total: 1, answered: 0, high_confidence: 0, new_entries: 0 },
    };
    renderPage();
    expect(
      await screen.findByText(/only ever answered from a saved answer, never inferred/i)
    ).toBeTruthy();
  });

  it("marks a field that needs the user", async () => {
    questions = {
      questions: [{
        field_key: "github", label: "GitHub", field_type: "text",
        required: true, options: [], description: "", answer: "",
        match_source: "unmatched", question_key: "", confidence: "none",
        needs_user: true, extraction_mode: "extracted",
      }],
      coverage: { total: 1, answered: 0, high_confidence: 0, new_entries: 0 },
    };
    renderPage();
    // Never invents a placeholder like "N/A" — the failure mode we saw
    // in Tsenta, which put a literal N/A into a required GitHub field.
    expect(await screen.findByText("Needs your answer")).toBeTruthy();
  });
});

describe("queue navigation", () => {
  beforeEach(() => {
    queueItems = [APP, { ...APP, id: "a2", job_title: "Platform Engineer" }];
  });

  it("shows position in the queue", async () => {
    renderPage();
    expect(await screen.findByText("1 of 2")).toBeTruthy();
  });

  it("Next moves to the following application", async () => {
    renderPage();
    expect(await screen.findByText("Senior SRE")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Next application"));
    expect(await screen.findByText("Platform Engineer")).toBeTruthy();
  });

  it("Prev is disabled at the start", async () => {
    renderPage();
    const prev = (await screen.findByLabelText("Previous application")) as HTMLButtonElement;
    expect(prev.disabled).toBe(true);
  });
});

describe("manual escape hatch", () => {
  it("marks the application applied", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("I applied manually"));
    await waitFor(() =>
      expect(updateApplication).toHaveBeenCalledWith("a1", { status: "applied" })
    );
  });
});
