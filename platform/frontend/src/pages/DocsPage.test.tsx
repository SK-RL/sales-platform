// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

/** F393 — the per-ATS guide renders what the server says, and the
 * /docs#auto-apply deep link from the Auto-apply page opens the section. */

vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { role: "reviewer" } }) }));
vi.mock("@/lib/api", () => ({
  getAtsCoverage: vi.fn(async () => ({
    counts: { automatic: 1, review: 1, link: 0, closed: 1 },
    items: [
      { platform: "gem", name: "Gem", level: "automatic", level_label: "Automatic", link_example: "jobs.gem.com/{company}/{id}",
        automatic: ["Reads the form schema"], you: ["Answer gate stops"], notes: ["Invisible hCaptcha"] },
      { platform: "lever", name: "Lever", level: "review", level_label: "We fill, you submit", link_example: "jobs.lever.co/{company}/{id}",
        automatic: ["Reads the real form"], you: ["Tick the hCaptcha and submit"], notes: [] },
      { platform: "join", name: "JOIN", level: "closed", level_label: "Not supported", link_example: "join.com/companies/{company}/{id}",
        automatic: [], you: ["Apply yourself"], notes: [] },
    ],
  })),
}));

import { DocsPage } from "./DocsPage";

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={[path]}><DocsPage /></MemoryRouter></QueryClientProvider>);
}
afterEach(cleanup);

describe("DocsPage auto-apply guide", () => {
  it("opens the section on the deep link and lists every site with its level", async () => {
    renderAt("/docs#auto-apply");
    expect(await screen.findByText("1 automatic")).toBeTruthy();
    expect(screen.getByText("Gem")).toBeTruthy();
    expect(screen.getAllByText("We fill, you submit").length).toBe(2); // legend + Lever row badge
    expect(screen.getByText("Lever")).toBeTruthy();
    expect(screen.getByText(/Nothing is invented/)).toBeTruthy();
  });

  it("expands a site to show what is automatic and what the user does", async () => {
    renderAt("/docs#auto-apply");
    fireEvent.click(await screen.findByText("Gem"));
    expect(screen.getByText("Reads the form schema")).toBeTruthy();
    expect(screen.getByText("Answer gate stops")).toBeTruthy();
    fireEvent.click(screen.getByText("JOIN"));
    expect(screen.getByText(/we can't drive this site/)).toBeTruthy();
  });

  it("stays collapsed without the hash", () => {
    renderAt("/docs");
    expect(screen.queryByText("1 automatic")).toBeNull();
  });
});
