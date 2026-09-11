// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

/** F384 — the auto-apply home shows only what changes what you do next. */

let items: any[] = [];
let prefs: any = { auto_apply_enabled: false, auto_apply_daily_cap: 0, auto_apply_min_score: 80 };
const putRoutinePreferences = vi.fn(async (p: any) => p);
vi.mock("@/lib/api", () => ({
  getApplications: vi.fn(async (params: any) => ({
    items: params?.status ? items.filter((a) => a.status === params.status) : items,
    total: items.length, page: 1, page_size: 100, total_pages: 1,
  })),
  getRoutinePreferences: vi.fn(async () => prefs),
  putRoutinePreferences: (...a: any[]) => putRoutinePreferences(...(a as [any])),
  resolveJobFromUrl: vi.fn(), prepareApplication: vi.fn(), getJob: vi.fn(),
}));

import { AutoApplyPage } from "./AutoApplyPage";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><AutoApplyPage /></MemoryRouter></QueryClientProvider>);
}

const today = new Date().toISOString();
beforeEach(() => {
  putRoutinePreferences.mockClear();
  prefs = { auto_apply_enabled: true, auto_apply_daily_cap: 5, auto_apply_min_score: 80 };
  items = [
    { id: "n1", status: "needs_user", job_title: "SRE", company_name: "Acme", gate_reason: "1 required field(s) need your answer" },
    { id: "s1", status: "submitted", job_title: "Platform Eng", company_name: "Beta", submitted_at: today },
    { id: "f1", status: "failed", job_title: "DevOps", company_name: "Gamma", gate_error: "submitted the form but saw no confirmation" },
    { id: "p1", status: "prepared", job_title: "Cloud Eng", company_name: "Delta", gate: "passed" },
  ];
});
afterEach(cleanup);

describe("AutoApplyPage", () => {
  it("shows needs-you with the reason, and one line per recent outcome", async () => {
    renderPage();
    expect(await screen.findByText(/Needs you · 1/)).toBeTruthy();
    expect(screen.getByText(/1 required field\(s\) need your answer/)).toBeTruthy();
    expect(await screen.findByText(/Submitted — the ATS confirmed it/)).toBeTruthy();
    expect(screen.getByText(/saw no confirmation/)).toBeTruthy();
    expect(screen.getByText(/Dry run passed — ready to submit/)).toBeTruthy();
  });

  it("shows today's count against the cap and the on/off state", async () => {
    renderPage();
    expect(await screen.findByText("Auto-apply is on")).toBeTruthy();
    expect(screen.getByText(/1 of 5 sent today/)).toBeTruthy();
  });

  it("turning it on from off sets a cap so the switch actually does something", async () => {
    prefs = { auto_apply_enabled: false, auto_apply_daily_cap: 0, auto_apply_min_score: 80 };
    renderPage();
    fireEvent.click(await screen.findByText("Turn on"));
    await waitFor(() => expect(putRoutinePreferences).toHaveBeenCalled());
    const sent = putRoutinePreferences.mock.calls[0][0];
    expect(sent.auto_apply_enabled).toBe(true);
    expect(sent.auto_apply_daily_cap).toBe(5);
  });

  it("says so when nothing needs you", async () => {
    items = [];
    renderPage();
    expect(await screen.findByText(/Nothing needs you right now/)).toBeTruthy();
  });
});

describe("AutoApplyPage → guide link (F393)", () => {
  it("links to the per-site guide section in the docs", async () => {
    renderPage();
    const a = (await screen.findByText(/Which sites are automatic\?/)).closest("a");
    expect(a?.getAttribute("href")).toBe("/docs#auto-apply");
  });
});
