// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

/**
 * F371 — bring your own link.
 *
 * What's worth locking: a link we can't read produces the server's
 * reason and NO navigation (Tsenta left Apply enabled on a garbage
 * link); a link we can read prepares an application and lands on its
 * review screen.
 */

const resolveJobFromUrl = vi.fn();
const prepareApplication = vi.fn();
vi.mock("@/lib/api", () => ({
  resolveJobFromUrl: (...a: any[]) => resolveJobFromUrl(...a),
  prepareApplication: (...a: any[]) => prepareApplication(...a),
}));

import { AddJobLink } from "./AddJobLink";

function Probe() {
  const loc = useLocation();
  return <div>AT {loc.pathname}{loc.search}</div>;
}

function renderIt() {
  return render(
    <MemoryRouter initialEntries={["/applications"]}>
      <Routes>
        <Route path="/applications" element={<AddJobLink />} />
        <Route path="/applications/review" element={<Probe />} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  resolveJobFromUrl.mockReset();
  prepareApplication.mockReset();
});
afterEach(cleanup);

describe("AddJobLink", () => {
  it("shows the server's reason for a link it can't read, and goes nowhere", async () => {
    resolveJobFromUrl.mockRejectedValue(new Error("That doesn't look like a job posting link we can read."));
    renderIt();
    fireEvent.change(screen.getByLabelText("Job posting link"), { target: { value: "https://example.com/x" } });
    fireEvent.click(screen.getByText("Prepare"));
    expect((await screen.findByRole("alert")).textContent).toMatch(/doesn't look like a job posting link/i);
    expect(prepareApplication).not.toHaveBeenCalled();
    expect(screen.queryByText(/^AT /)).toBeNull();
  });

  it("prepares a resolved posting and lands on its review screen", async () => {
    resolveJobFromUrl.mockResolvedValue({ job_id: "j9", platform: "ashby", auto_submittable: true, wall: null });
    prepareApplication.mockResolvedValue({ id: "app9", status: "prepared" });
    renderIt();
    fireEvent.change(screen.getByLabelText("Job posting link"), {
      target: { value: "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245" },
    });
    fireEvent.click(screen.getByText("Prepare"));
    await waitFor(() => expect(prepareApplication).toHaveBeenCalledWith("j9"));
    expect(await screen.findByText("AT /applications/review?app=app9")).toBeTruthy();
  });

  it("disables Prepare until something is typed", () => {
    renderIt();
    expect((screen.getByText("Prepare") as HTMLButtonElement).disabled).toBe(true);
  });
});
