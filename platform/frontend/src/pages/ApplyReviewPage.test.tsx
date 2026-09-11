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
  answerGap: (...a: any[]) => answerGap(...(a as [string, any])),
}));

import { ApplyReviewPage } from "./ApplyReviewPage";

const answerGap = vi.fn(async (_id: string, _p: any) => ({ entry_id: "e1", question_key: "k", remaining: [], status: "prepared" }));

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

describe("single-application mode (?app=<id>)", () => {
  /** F361 — the entry point from the Applications table. Without it the
   *  feature was unreachable from the UI: the queue only lists things
   *  already in needs_user, and nothing could get there except the
   *  sweep, which is off by default. */

  function renderFocused() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/applications/review?app=a1"]}>
          <ApplyReviewPage />
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("reviews the requested application even when the queue is empty", async () => {
    queueItems = [];  // nothing in needs_user
    appDetail = {
      id: "a1",
      status: "prepared",
      platform_response: {},
      job: {
        id: "j1", title: "Cloud Engineering Manager", company_name: "Canonical",
        platform: "greenhouse", url: "https://boards.greenhouse.io/canonical/jobs/1",
      },
    };
    renderFocused();
    expect(await screen.findByText("Cloud Engineering Manager")).toBeTruthy();
    expect(screen.getByText(/Canonical/)).toBeTruthy();
  });

  it("says not found rather than 'nothing needs you' for a bad id", async () => {
    queueItems = [];
    appDetail = {};
    renderFocused();
    expect(await screen.findByText(/Application not found/i)).toBeTruthy();
  });
});

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

describe("human wall (F368)", () => {
  /** SmartRecruiters (DataDome), BambooHR and Lever (captcha checkboxes)
   *  can be read but never driven. The page must say WHY and what to
   *  do, not "no submitter" — that reads as our bug, and the user
   *  would wait for a fix that isn't coming. */
  beforeEach(() => {
    questions = {
      questions: [],
      coverage: { total: 0, answered: 0, high_confidence: 0, new_entries: 0 },
      schema: {
        extraction_mode: "fallback", platform: "smartrecruiters", supported: false,
        wall: { vendor: "DataDome", reason: "SmartRecruiters protects its application form with DataDome bot detection, so it has to be applied in your own browser." },
      },
      blocking: [],
      safe_to_auto_submit: false,
    };
  });

  it("names the wall instead of the generic guessed-form text", async () => {
    renderPage();
    expect(await screen.findByText(/DataDome bot detection/i)).toBeTruthy();
    expect(screen.queryByText(/couldn't read this posting's real application form/i)).toBeNull();
  });

  it("tells the user what to do next", async () => {
    renderPage();
    expect(await screen.findByText(/Apply on the posting page/i)).toBeTruthy();
  });

  it("keeps submit disabled", async () => {
    renderPage();
    const btn = (await screen.findByText("Submit application")) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("disables submit even when the form was read and nothing else blocks (Lever)", async () => {
    // Seen on production: Lever, 10 extracted fields, zero blockers —
    // the only thing in the way is the hCaptcha, and the button must
    // say so by being disabled, not by failing after a click.
    questions = {
      questions: [{
        field_key: "name", label: "Full name", field_type: "text", required: true,
        options: [], description: "", answer: "Sarthak", match_source: "manual",
        question_key: "name", confidence: "high", extraction_mode: "extracted",
      }],
      coverage: { total: 1, answered: 1, high_confidence: 1, new_entries: 0 },
      schema: {
        extraction_mode: "extracted", platform: "lever", supported: true,
        wall: { vendor: "hCaptcha", reason: "Lever puts an hCaptcha checkbox on every application, so a person has to submit it." },
      },
      blocking: [],
      safe_to_auto_submit: false,
    };
    renderPage();
    expect(await screen.findByText(/hCaptcha checkbox/i)).toBeTruthy();
    const btn = (await screen.findByText("Submit application")) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(screen.getByText("Sarthak")).toBeTruthy();  // answers still shown to copy
  });
});

describe("dry run result (F373)", () => {
  it("shows what a passed dry run placed, and that nothing was sent", async () => {
    appDetail = {
      id: "a1", status: "prepared",
      platform_response: { gate: "passed", dry_run: true, placed: 7, field_count: 8, unplaceable: ["cover_letter_file"] },
    };
    questions = {
      questions: [], coverage: { total: 0, answered: 0, high_confidence: 0, new_entries: 0 },
      schema: { extraction_mode: "extracted", platform: "ashby", supported: true, wall: null },
      blocking: [], safe_to_auto_submit: true,
    };
    renderPage();
    expect(await screen.findByText(/Dry run passed — 7 of 8 fields/i)).toBeTruthy();
    expect(screen.getByText(/Nothing was sent/i)).toBeTruthy();
    expect(screen.getByText(/Skipped \(optional\): cover_letter_file/i)).toBeTruthy();
    // and it is not mistaken for a blocker
    expect(screen.queryByText(/needs your input/i)).toBeNull();
    await waitFor(() => {
      expect((screen.getByText("Submit application") as HTMLButtonElement).disabled).toBe(false);
    });
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

  it("calls a low-confidence answer a guess and says it is not sent", async () => {
    questions = {
      questions: [{
        field_key: "cSummary", label: "Summary", field_type: "textarea", required: true,
        options: [], description: "", answer: "No. My release automation…", match_source: "category",
        question_key: "release_process", confidence: "low", extraction_mode: "extracted",
      }],
      coverage: { total: 1, answered: 1, high_confidence: 0, new_entries: 0 },
    };
    renderPage();
    expect(await screen.findByText(/Guessed — not sent/i)).toBeTruthy();
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


describe("F396 — answering a gap inline", () => {
  beforeEach(() => {
    answerGap.mockClear();
    appDetail = {
      id: "a1", status: "needs_user",
      platform_response: {
        gate: "blocked", reason: "2 required field(s) need your answer",
        blocking: [
          { field_key: "q1", label: "Explain your Cloud Inference experience in 3-4 lines.", reason: "No saved answer matches this required field." },
          { field_key: "q2", label: "Which office are you closest to?", reason: "No saved answer matches this required field." },
        ],
        drafts: { q1: { text: "At Acme I ran GPU inference clusters on Kubernetes.", enough_information: true, unsupported_claims: [], note: "Every claim traced back to your material. Edit freely, then save." } },
      },
    };
    questions = {
      ...questions,
      questions: [
        { field_key: "q1", label: "Explain your Cloud Inference experience in 3-4 lines.", field_type: "textarea", required: true, options: [], description: "", answer: "", match_source: "unmatched", question_key: "", confidence: "none", extraction_mode: "extracted", needs_user: true },
        { field_key: "q2", label: "Which office are you closest to?", field_type: "select", required: true, options: ["Bristol", "London"], description: "", answer: "", match_source: "unmatched", question_key: "", confidence: "none", extraction_mode: "extracted", needs_user: true },
      ],
    };
  });

  it("pre-fills the draft with its fact-check note and saves it under the question", async () => {
    renderPage();
    const box = (await screen.findByLabelText("Explain your Cloud Inference experience in 3-4 lines.")) as HTMLTextAreaElement;
    expect(box.value).toContain("GPU inference clusters");
    expect(screen.getByText(/Every claim traced back/)).toBeTruthy();
    fireEvent.click(screen.getByText("Use this answer"));
    await waitFor(() => expect(answerGap).toHaveBeenCalled());
    expect(answerGap.mock.calls[0][1]).toEqual({ field_key: "q1", question: "Explain your Cloud Inference experience in 3-4 lines.", answer: "At Acme I ran GPU inference clusters on Kubernetes." });
  });

  it("shows the form's options for a choice question", async () => {
    renderPage();
    const sel = (await screen.findByLabelText("Which office are you closest to?")) as HTMLSelectElement;
    expect([...sel.options].map((o) => o.value)).toEqual(["", "Bristol", "London"]);
    fireEvent.change(sel, { target: { value: "London" } });
    fireEvent.click(screen.getAllByText("Save answer")[0]);
    await waitFor(() => expect(answerGap).toHaveBeenCalled());
    expect(answerGap.mock.calls[0][1].answer).toBe("London");
  });
});

describe("F398 — a queued run is visible", () => {
  beforeEach(() => {
    appDetail = {
      id: "a1", status: "prepared",
      platform_response: { gate: "passed", dry_run: true, placed: 5, field_count: 5, queued: { task_id: "t1", dry_run: true, at: new Date(Date.now() - 65_000).toISOString() } },
    };
  });

  it("says the dry run is queued, shows elapsed time, and disables the buttons meanwhile", async () => {
    renderPage();
    expect(await screen.findByText(/Dry run queued — waiting for a free worker/)).toBeTruthy();
    expect(screen.getByText(/1 min 5s so far/)).toBeTruthy();
    const dry = screen.getByText("Running…") as HTMLButtonElement;
    expect(dry.disabled).toBe(true);
    expect(screen.queryByText(/Dry run passed/)).toBeNull(); // the old result is not shown as if it were current
  });
});
