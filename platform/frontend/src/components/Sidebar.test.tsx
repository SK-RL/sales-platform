// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

/**
 * Full-component regression test for the "Relevant Jobs → All Jobs" switch.
 *
 * Where jobsNav.test.ts locks the pure decision, this renders the ACTUAL
 * <Sidebar> and asserts which nav link carries the active class at the
 * exact URLs from the bug report — so the wiring (import, the
 * relevantClusterNames it derives from getRoleClusters, the isActive
 * computation) is covered end to end, not just the helper.
 */

// Logged-in reviewer — enough to render the primary nav (admin/super_admin
// sections are gated separately and irrelevant here).
vi.mock("@/lib/auth", () => ({
  useAuth: () => ({ user: { id: "u1", email: "t@example.io", role: "reviewer" } }),
}));

// getRoleClusters drives relevantClusterNames. infra/security/qa are
// relevant; sales is a real cluster that is NOT relevant (guards the
// "specific but non-relevant cluster still reads as All Jobs" edge).
vi.mock("@/lib/api", () => ({
  logout: vi.fn(),
  getRoleClusters: vi.fn(async () => ({
    items: [
      { name: "infra", is_active: true, is_relevant: true },
      { name: "security", is_active: true, is_relevant: true },
      { name: "qa", is_active: true, is_relevant: true },
      { name: "sales", is_active: true, is_relevant: false },
    ],
  })),
}));

import { Sidebar } from "./Sidebar";

const ACTIVE = "bg-primary-700/50";

function renderAt(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <Sidebar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const relevantLink = () => screen.getByRole("link", { name: "Relevant Jobs" });
const allJobsLink = () => screen.getByRole("link", { name: "All Jobs" });

afterEach(() => cleanup());

/**
 * Wait for the getRoleClusters query to resolve, then assert which of the
 * two Jobs links is highlighted. `expectRelevant` = which one should be
 * active. Exactly one of the two must carry the active class.
 */
async function expectActive(expectRelevant: boolean) {
  await waitFor(() => {
    expect(relevantLink().className.includes(ACTIVE)).toBe(expectRelevant);
  });
  expect(allJobsLink().className.includes(ACTIVE)).toBe(!expectRelevant);
}

describe("<Sidebar> Relevant vs All Jobs highlight", () => {
  it("highlights Relevant Jobs on the relevant pseudo-value", async () => {
    renderAt("/jobs?role_cluster=relevant");
    await expectActive(true);
  });

  // The exact screenshotted repro: Relevant + UAE, then click the infra
  // role. Pre-fix this flipped the highlight to "All Jobs".
  it("KEEPS Relevant Jobs highlighted when narrowed to infra + UAE", async () => {
    renderAt("/jobs?geography=uae_only&role_cluster=infra");
    await expectActive(true);
  });

  it("keeps Relevant Jobs highlighted for security and qa clusters", async () => {
    renderAt("/jobs?role_cluster=security");
    await expectActive(true);
    cleanup();
    renderAt("/jobs?role_cluster=qa");
    await expectActive(true);
  });

  it("highlights All Jobs on the any sentinel", async () => {
    renderAt("/jobs?role_cluster=any");
    await expectActive(false);
  });

  it("highlights All Jobs for a non-relevant cluster (sales)", async () => {
    renderAt("/jobs?role_cluster=sales");
    await expectActive(false);
  });

  it("highlights All Jobs on plain /jobs with no role_cluster", async () => {
    renderAt("/jobs?geography=global_remote");
    await expectActive(false);
  });
});
