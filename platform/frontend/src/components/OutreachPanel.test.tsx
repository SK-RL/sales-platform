// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

/**
 * F404 — the Reach out panel. What matters to the person using it:
 *  - contacts are shown with an honest email status ("likely" is not "verified")
 *  - the draft is editable, the fact-check warning is visible
 *  - Gmail/Outlook links carry the recipient and the draft; nothing is sent by us
 *  - "I sent it" records the channel
 */

let outreach: any;
const draftOutreach = vi.fn(async () => ({ queued: true, running: true, task_id: "t" }));
const markOutreachSent = vi.fn(async () => ({ ok: true, outreach_status: "emailed", last_outreach_at: "2026-09-12T00:00:00Z" }));
const editOutreach = vi.fn(async () => ({ ok: true }));

vi.mock("@/lib/api", () => ({
  getOutreach: vi.fn(async () => outreach),
  draftOutreach: (...a: any[]) => draftOutreach(...(a as [])),
  markOutreachSent: (...a: any[]) => markOutreachSent(...(a as [])),
  editOutreach: (...a: any[]) => editOutreach(...(a as [])),
}));

import { OutreachPanel } from "./OutreachPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <OutreachPanel appId="a1" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("OutreachPanel", () => {
  it("shows who would be written to before anything is drafted, with honest email status", async () => {
    outreach = {
      application_id: "a1", running: false, company_contacts: 5, bundle: {},
      candidates: [
        { contact_id: "c1", name: "Jane Doe", title: "Talent Partner", role_category: "hiring", email: "jane@acme.com", email_status: "likely", linkedin_url: "", outreach_status: "not_contacted", last_outreach_at: null },
        { contact_id: "c2", name: "Bob Ray", title: "VP Engineering", role_category: "engineering_lead", email: "", email_status: "", linkedin_url: "https://linkedin.com/in/bob", outreach_status: "emailed", last_outreach_at: "2026-09-01T00:00:00Z" },
      ],
    };
    renderPanel();
    await screen.findByText("Jane Doe");
    expect(screen.getByText("Email likely")).toBeTruthy();
    expect(screen.queryByText("Email verified")).toBeNull();
    expect(screen.getByText("No email — LinkedIn only")).toBeTruthy();
    expect(screen.getByText(/contacted Sep 1/)).toBeTruthy();
    fireEvent.click(screen.getByText("Draft messages"));
    await waitFor(() => expect(draftOutreach).toHaveBeenCalledWith("a1", undefined));
  });

  it("renders the draft with its fact-check warning, deep links with the recipient, and records a send", async () => {
    outreach = {
      application_id: "a1", running: false, company_contacts: 1, candidates: [],
      bundle: {
        contacts: [{
          contact_id: "c1", name: "Jane Doe", title: "Talent Partner", role_category: "hiring", email: "jane@acme.com", email_status: "valid",
          verification: { status: "valid", method: "hunter", detail: "hunter: valid, score 92" }, linkedin_url: "https://linkedin.com/in/jane",
          outreach_status: "not_contacted", last_outreach_at: null, sent: [],
          draft: { email_subject: "Platform Engineer application", email_body: "Hi Jane, four years on EKS.", linkedin_note: "Hi Jane, applied for Platform Engineer.", enough_information: true, unsupported_claims: ["led a team at Google"], note: "The fact-check still flags claims your material does not support. Check them before sending." },
        }],
      },
    };
    renderPanel();
    await screen.findByText("Talent Partner");
    expect(screen.getByText(/Check: led a team at Google/)).toBeTruthy();
    expect(screen.getByText(/hunter: valid/)).toBeTruthy();
    const gmail = screen.getByText("Gmail").closest("a")!;
    expect(gmail.getAttribute("href")).toContain("to=jane%40acme.com");
    expect(gmail.getAttribute("href")).toContain("su=Platform%20Engineer%20application");
    expect(gmail.getAttribute("target")).toBe("_blank");
    // edit → Save edits appears and posts the new body
    fireEvent.change(screen.getByLabelText("Email body"), { target: { value: "Hi Jane, four years on EKS at Foo Corp." } });
    fireEvent.click(screen.getByText("Save edits"));
    await waitFor(() => expect(editOutreach).toHaveBeenCalled());
    expect((editOutreach.mock.calls[0] as any)[1].email_body).toBe("Hi Jane, four years on EKS at Foo Corp.");
    fireEvent.click(screen.getByText("I sent it"));
    await waitFor(() => expect(markOutreachSent).toHaveBeenCalledWith("a1", { contact_id: "c1", channel: "email" }));
    // LinkedIn tab caps at 200 characters
    fireEvent.click(screen.getByText("LinkedIn"));
    fireEvent.change(screen.getByLabelText("LinkedIn connection note"), { target: { value: "x".repeat(250) } });
    expect(screen.getByText("200/200")).toBeTruthy();
  });

  it("says plainly when nobody can be reached", async () => {
    outreach = { application_id: "a1", running: false, company_contacts: 0, candidates: [], bundle: {} };
    renderPanel();
    await screen.findByText(/No contacts on file/);
    expect((screen.getByText("Draft messages") as HTMLButtonElement).disabled).toBe(true);
  });
});
