/**
 * F404 — "Reach out": the people behind an application.
 *
 * Tsenta's Networking tab, with two deliberate differences: nothing is
 * sent from here (the user opens Gmail/Outlook/LinkedIn with the draft
 * prefilled and presses Send there), and every draft went through the
 * same fact-check as application answers, so the panel can say whether
 * every claim traced back to the résumé. Email status is shown as what
 * it is: "valid" only after a real check, "likely" for a pattern match.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, ExternalLink, Linkedin, Mail, RefreshCw, Send } from "lucide-react";
import { draftOutreach, editOutreach, getOutreach, markOutreachSent } from "@/lib/api";
import type { OutreachContact } from "@/lib/types";

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  valid: { text: "Email verified", cls: "bg-green-100 text-green-800" },
  catch_all: { text: "Domain accepts all mail", cls: "bg-amber-100 text-amber-800" },
  likely: { text: "Email likely", cls: "bg-amber-100 text-amber-800" },
  unverified: { text: "Email unverified", cls: "bg-gray-100 text-gray-600" },
  unknown: { text: "Email unverified", cls: "bg-gray-100 text-gray-600" },
  invalid: { text: "Email bounced", cls: "bg-red-100 text-red-700" },
};

function emailBadge(c: OutreachContact) {
  if (!c.email) return { text: "No email — LinkedIn only", cls: "bg-gray-100 text-gray-600" };
  return STATUS_LABEL[c.email_status] ?? STATUS_LABEL.unverified;
}

function whenText(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function OutreachPanel({ appId }: { appId: string }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["outreach", appId],
    queryFn: () => getOutreach(appId),
    refetchInterval: (query) => (query.state.data?.running ? 3000 : false),
  });
  const draftM = useMutation({
    mutationFn: (ids?: string[]) => draftOutreach(appId, ids),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["outreach", appId] }),
  });

  const data = q.data;
  const bundle = data?.bundle ?? {};
  const contacts = bundle.contacts ?? [];
  const candidates = data?.candidates ?? [];
  const running = Boolean(data?.running);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    if (!open && contacts.length) setOpen(contacts[0].contact_id);
  }, [contacts, open]);

  if (q.isLoading) return null;

  const nobody = !contacts.length && !candidates.length;

  return (
    <section className="mt-6 rounded-xl border border-gray-200 bg-white" data-testid="outreach-panel">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-100 px-5 py-3">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">Reach out</h2>
          <p className="text-xs text-gray-500">
            The people most likely to read your application. Drafts are written from your résumé and checked; you send them yourself.{" "}
            <Link to="/docs#reach-out" className="underline hover:text-gray-800">How this works</Link>
          </p>
        </div>
        <button
          type="button"
          onClick={() => draftM.mutate(undefined)}
          disabled={running || draftM.isPending || nobody}
          className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          title={nobody ? "No reachable contact at this company yet" : contacts.length ? "Draft again for the top contacts" : "Verify emails and draft a message per contact"}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${running ? "animate-spin" : ""}`} />
          {running ? "Drafting…" : contacts.length ? "Redraft" : "Draft messages"}
        </button>
      </div>

      {bundle.error && (
        <p className="border-b border-red-100 bg-red-50 px-5 py-2 text-xs text-red-700">Drafting failed: {bundle.error}</p>
      )}

      {nobody && (
        <p className="px-5 py-4 text-sm text-gray-500">
          {data?.company_contacts
            ? "Contacts are on file for this company, but none has an email or LinkedIn profile we can use."
            : "No contacts on file for this company yet. Enrichment adds them as it runs; check the company page."}
        </p>
      )}

      {!contacts.length && candidates.length > 0 && (
        <div className="px-5 py-4">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">Who we would write to</p>
          <ul className="space-y-1.5">
            {candidates.map((c) => (
              <li key={c.contact_id} className="flex flex-wrap items-center gap-2 text-sm">
                <span className="font-medium text-gray-900">{c.name || "Unnamed"}</span>
                <span className="text-gray-500">{c.title}</span>
                <span className={`rounded px-1.5 py-0.5 text-[11px] ${emailBadge(c).cls}`}>{emailBadge(c).text}</span>
                {c.last_outreach_at && (
                  <span className="text-[11px] text-gray-500">contacted {whenText(c.last_outreach_at)}</span>
                )}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-gray-500">Emails are verified when you press Draft messages.</p>
        </div>
      )}

      {contacts.length > 0 && (
        <div className="grid gap-0 md:grid-cols-[220px_1fr]">
          <ul className="border-b border-gray-100 md:border-b-0 md:border-r">
            {contacts.map((c) => (
              <li key={c.contact_id}>
                <button
                  type="button"
                  onClick={() => setOpen(c.contact_id)}
                  className={`block w-full px-4 py-3 text-left hover:bg-gray-50 ${open === c.contact_id ? "bg-gray-50" : ""}`}
                >
                  <div className="text-sm font-medium text-gray-900">{c.name || "Unnamed"}</div>
                  <div className="truncate text-xs text-gray-500">{c.title || c.role_category}</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    <span className={`rounded px-1.5 py-0.5 text-[11px] ${emailBadge(c).cls}`}>{emailBadge(c).text}</span>
                    {(c.sent?.length ?? 0) > 0 && (
                      <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[11px] text-blue-800">
                        Sent {whenText(c.sent![c.sent!.length - 1].at)}
                      </span>
                    )}
                  </div>
                </button>
              </li>
            ))}
          </ul>
          <div className="px-5 py-4">
            {contacts.filter((c) => c.contact_id === open).map((c) => (
              <ContactDrafts key={c.contact_id} appId={appId} contact={c} onRedraft={() => draftM.mutate([c.contact_id])} running={running} />
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function ContactDrafts({ appId, contact, onRedraft, running }: { appId: string; contact: OutreachContact; onRedraft: () => void; running: boolean }) {
  const qc = useQueryClient();
  const [tab, setTab] = useState<"email" | "linkedin">(contact.email && contact.email_status !== "invalid" ? "email" : "linkedin");
  const d = contact.draft;
  const [subject, setSubject] = useState(d?.email_subject ?? "");
  const [body, setBody] = useState(d?.email_body ?? "");
  const [note, setNote] = useState(d?.linkedin_note ?? "");
  const [copied, setCopied] = useState<string | null>(null);

  // Re-seed the fields only when the stored draft itself changes (a
  // redraft), never on an unrelated re-render — an effect here raced the
  // user's first keystroke and threw it away.
  const seed = `${d?.email_subject ?? ""}\u0000${d?.email_body ?? ""}\u0000${d?.linkedin_note ?? ""}`;
  const [seen, setSeen] = useState(seed);
  if (seed !== seen) {
    setSeen(seed);
    setSubject(d?.email_subject ?? "");
    setBody(d?.email_body ?? "");
    setNote(d?.linkedin_note ?? "");
  }

  const editM = useMutation({
    mutationFn: () => editOutreach(appId, { contact_id: contact.contact_id, email_subject: subject, email_body: body, linkedin_note: note }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["outreach", appId] }),
  });
  const sentM = useMutation({
    mutationFn: (channel: "email" | "linkedin") => markOutreachSent(appId, { contact_id: contact.contact_id, channel }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["outreach", appId] }),
  });

  const dirty = subject !== (d?.email_subject ?? "") || body !== (d?.email_body ?? "") || note !== (d?.linkedin_note ?? "");
  const encode = encodeURIComponent;
  const gmail = contact.email ? `https://mail.google.com/mail/?view=cm&fs=1&to=${encode(contact.email)}&su=${encode(subject)}&body=${encode(body)}` : "";
  const outlook = contact.email ? `https://outlook.office.com/mail/deeplink/compose?to=${encode(contact.email)}&subject=${encode(subject)}&body=${encode(body)}` : "";
  const mailto = contact.email ? `mailto:${contact.email}?subject=${encode(subject)}&body=${encode(body)}` : "";

  async function copy(text: string, what: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
      setTimeout(() => setCopied(null), 1500);
    } catch {
      /* clipboard blocked; the textarea is selectable */
    }
  }

  const v = contact.verification;

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <div className="text-base font-semibold text-gray-900">{contact.name || "Unnamed"}</div>
          <div className="text-sm text-gray-600">{contact.title}</div>
          {contact.email && <div className="text-xs text-gray-500">{contact.email}</div>}
          {v && v.detail && (
            <div className="text-[11px] text-gray-500" title={`checked via ${v.method}`}>
              {v.status === "valid" ? "Verified" : v.status === "likely" ? "Likely" : v.status === "invalid" ? "Bounced" : "Unverified"}: {v.detail}
            </div>
          )}
        </div>
        <div className="flex gap-1 text-xs">
          <button type="button" onClick={() => setTab("email")} className={`rounded-lg px-2.5 py-1 ${tab === "email" ? "bg-gray-900 text-white" : "border border-gray-300 text-gray-700"}`} disabled={!contact.email}>
            <Mail className="mr-1 inline h-3 w-3" />Email
          </button>
          <button type="button" onClick={() => setTab("linkedin")} className={`rounded-lg px-2.5 py-1 ${tab === "linkedin" ? "bg-gray-900 text-white" : "border border-gray-300 text-gray-700"}`}>
            <Linkedin className="mr-1 inline h-3 w-3" />LinkedIn
          </button>
        </div>
      </div>

      {d?.error && <p className="mb-2 text-sm text-red-700">Not drafted: {d.error}.</p>}
      {d && !d.error && !d.enough_information && <p className="mb-2 text-sm text-amber-700">{d.note}</p>}
      {d && d.email_body && (
        <p className={`mb-2 text-xs ${d.unsupported_claims?.length ? "text-amber-700" : "text-gray-500"}`}>
          {d.note}
          {d.unsupported_claims?.length ? (
            <span className="block">Check: {d.unsupported_claims.join("; ")}</span>
          ) : null}
        </p>
      )}

      {tab === "email" && contact.email && (
        <div className="space-y-2">
          <input
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            placeholder="Subject"
            className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm"
            aria-label="Email subject"
          />
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={9}
            placeholder={running ? "Drafting…" : "No draft yet. Press Draft messages, or write your own."}
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"
            aria-label="Email body"
          />
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <button type="button" onClick={() => copy(`Subject: ${subject}\n\n${body}`, "email")} className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-2.5 py-1.5 text-gray-700 hover:bg-gray-50">
              {copied === "email" ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />} Copy
            </button>
            <a href={gmail} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-2.5 py-1.5 text-gray-700 hover:bg-gray-50"><ExternalLink className="h-3.5 w-3.5" /> Gmail</a>
            <a href={outlook} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-2.5 py-1.5 text-gray-700 hover:bg-gray-50"><ExternalLink className="h-3.5 w-3.5" /> Outlook</a>
            <a href={mailto} className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-2.5 py-1.5 text-gray-700 hover:bg-gray-50"><Mail className="h-3.5 w-3.5" /> Mail app</a>
            {dirty && (
              <button type="button" onClick={() => editM.mutate()} disabled={editM.isPending} className="rounded-lg bg-gray-900 px-2.5 py-1.5 text-white">Save edits</button>
            )}
            <span className="grow" />
            <button type="button" onClick={() => sentM.mutate("email")} disabled={sentM.isPending} className="inline-flex items-center gap-1 rounded-lg border border-blue-300 px-2.5 py-1.5 text-blue-800 hover:bg-blue-50" title="Record that you sent this from your mail client">
              <Send className="h-3.5 w-3.5" /> I sent it
            </button>
          </div>
        </div>
      )}

      {tab === "linkedin" && (
        <div className="space-y-2">
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value.slice(0, 200))}
            rows={4}
            placeholder={running ? "Drafting…" : "No note yet. Press Draft messages, or write your own (200 characters)."}
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"
            aria-label="LinkedIn connection note"
          />
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className={`${note.length > 200 ? "text-red-600" : "text-gray-500"}`}>{note.length}/200</span>
            <button type="button" onClick={() => copy(note, "note")} className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-2.5 py-1.5 text-gray-700 hover:bg-gray-50">
              {copied === "note" ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />} Copy
            </button>
            {contact.linkedin_url ? (
              <a href={contact.linkedin_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-2.5 py-1.5 text-gray-700 hover:bg-gray-50"><Linkedin className="h-3.5 w-3.5" /> Open profile</a>
            ) : (
              <a href={`https://www.linkedin.com/search/results/people/?keywords=${encode(contact.name)}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-2.5 py-1.5 text-gray-700 hover:bg-gray-50"><Linkedin className="h-3.5 w-3.5" /> Find on LinkedIn</a>
            )}
            {dirty && (
              <button type="button" onClick={() => editM.mutate()} disabled={editM.isPending} className="rounded-lg bg-gray-900 px-2.5 py-1.5 text-white">Save edits</button>
            )}
            <span className="grow" />
            <button type="button" onClick={() => sentM.mutate("linkedin")} disabled={sentM.isPending} className="inline-flex items-center gap-1 rounded-lg border border-blue-300 px-2.5 py-1.5 text-blue-800 hover:bg-blue-50">
              <Send className="h-3.5 w-3.5" /> I sent it
            </button>
          </div>
        </div>
      )}

      {(contact.sent?.length ?? 0) > 0 && (
        <p className="mt-3 text-xs text-gray-500">
          Sent log: {contact.sent!.map((s) => `${s.channel} on ${whenText(s.at)}`).join(", ")}.
        </p>
      )}
      <div className="mt-3 text-right">
        <button type="button" onClick={onRedraft} disabled={running} className="text-xs text-gray-500 underline hover:text-gray-800">Redraft for this person</button>
      </div>
    </div>
  );
}
