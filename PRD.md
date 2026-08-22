# PRD — Prayaan Business Network

**Customer micro-sites with loan lead capture**

| | |
|---|---|
| Status | Draft v1.0 — reflects what is built and running |
| Date | 2026-08-12 (IST) |
| Owner | Santhosh (Product) |
| One-liner | Every Prayaan customer gets a public web page for their business at a memorable URL. They share it with friends and family. Beside their content sits a Prayaan loan-enquiry form, so their network becomes our lead source. |

---

## 1. Problem

**Prayaan has no digital lead-capture channel.** Every CTA on the marketing site routes to a phone number or an email address. There is no async "request a callback" path anywhere, so anyone who is interested outside business hours is lost.

**Prayaan's customers have no web presence.** MSME owners across Tamil Nadu — hardware shops, garment units, workshops, traders — have nothing to send when someone asks for their details.

These two gaps solve each other. A page that flatters the customer's business gets shared willingly, and everyone who sees it is a business owner in the same circles — precisely Prayaan's target borrower, arriving with a peer's implicit endorsement rather than a cold call.

## 2. Goals

| # | Goal | Measure |
|---|------|---------|
| G1 | Customers actually share their page | ≥60% of live pages receive traffic from outside the branch within 30 days of going live |
| G2 | The network produces leads | ≥1 qualified lead per 10 live pages per month |
| G3 | Leads convert | ≥1 lead reaches a login file within the first quarter |
| G4 | Cheap to operate | A page is created and published centrally in under 5 minutes; no per-page engineering |
| G5 | No harm | No compliance, brand or data incident; takedown honoured within 24 hours |

## 3. Non-goals

1. **No loan application, KYC, sanction or disbursal on the page — ever.** Lead capture alone is what keeps this outside RBI's Digital Lending Directions. Adding any of them requires a fresh regulatory assessment first.
2. **No interest rates, EMI figures, approval promises or eligibility claims.** Nothing that reads as an offer.
3. **No customer self-service editing.** The central team authors; the segment's digital literacy makes self-serve a support burden, not a feature.
4. **No e-commerce, catalogue ordering or payments.** This is a presence page, not a storefront.
5. **No page hosted on the existing marketing site.** It cannot produce per-page link previews (see §5).
6. **No lead data as the system of record in a spreadsheet.** Excel is an export, not a database.

## 4. Users and flow

**Customer (the borrower).** Wants to look credible and be findable. Receives a URL, shares it on WhatsApp.

**Visitor (friend, relative, fellow trader).** Reads about the business. If they want a loan themselves, leaves name, mobile and pincode.

**Central team.** Creates pages in bulk from a spreadsheet, publishes, takes down on request.

**Customer service team.** Works the lead inbox: calls, qualifies loan interest, routes the interested ones to the nearest branch.

```
central team ──creates──► page published at /TN/vellore/santhosh-enterprise
                               │
        customer shares the link on WhatsApp
                               │
                    visitor reads, submits form
                               │
                    lead lands in the inbox
                               │
              CS calls ──not interested──► closed
                       └──interested──► routed to nearest branch
```

## 5. Why a separate service (decided, and load-bearing)

The existing marketing site is a client-side React SPA on Vercel with no server rendering. Its social tags are written by JavaScript after the page loads, and the built HTML body is an empty `<div id="root">`. **WhatsApp's link crawler does not run JavaScript.** Every customer page shared from that site would preview with the same generic "Prayaan Capital — Secured Business Loans" card, and Google would index empty shells.

For a product whose entire distribution is WhatsApp sharing and whose pages must be indexed, that is disqualifying. Hence a separate server-rendered service on its own subdomain.

It is server-rendered rather than statically built for one specific reason: pages publish without prior consent under a takedown-on-request model, so **takedown speed is a compliance control**. Flipping a status field removes a page on the very next request; a static build would need a regenerate-and-redeploy cycle.

## 6. URL scheme

```
https://<subdomain>/TN/vellore/santhosh-enterprise
                    ── ─────── ──────────────────
                  state branch    business
```

These links live forever in WhatsApp history, so:

- Slugs are ASCII lowercase, hyphenated. Tamil names are transliterated, never percent-encoded — `%E0%AE%95…` in a chat reads as spam.
- Duplicates within a branch get a deterministic suffix: `santhosh-enterprise-2`.
- **A published path never moves.** A branch transfer or rename keeps the original URL working and adds the new one as an alias that 301s to it. A path is never reassigned to a different business.
- Reserved segments (`api`, `admin`, `static`, `robots.txt`, brand terms) can never become a slug.

## 7. The page

**Customer's content is dominant** — photo, business name, owner, category, a few headline numbers, and paragraphs about the business. Headline figures are rendered as *"Figures shared by <owner>"*: Prayaan publishes them, it does not certify them.

**Prayaan's ask is the narrower column** — an explicit loan enquiry ("Want a business loan for your own shop?") with three fields: name, mobile, pincode. Plus an 18-or-older confirmation and a consent line linking to the privacy notice.

**Attribution comes from the URL, never a form field.** The form posts to the page's own address, so a scripted submission cannot credit an arbitrary customer and poison referral numbers.

**Compliance footer on every page**, not editable by the central team: legal entity and RBI-registered NBFC status; *"Prayaan never asks for any fee before loan processing"*; the loans-subject-to-assessment line; and links to Grievance & GRO, the privacy notice, and a report-this-page route.

## 8. Lead handling

Statuses: `NEW → CONTACTED → INTERESTED | NOT_INTERESTED → SENT_TO_BRANCH → CLOSED`.

**Nearest branch** resolves from the lead's own pincode first, falling back to the referring page's branch, and CS can always override — the person filling the form may live nowhere near the shop that referred them.

**Excel.** The CS team gets a one-click CSV export that opens directly in Excel, but the database stays the system of record. A shared spreadsheet loses leads to concurrent edits, carries no audit trail, and leaves customer phone numbers in a loose file — a poor position for a regulated lender. Every export is itself an audited event, because the lead book is the most sellable asset in this system.

## 9. Anti-abuse

A public form on a lender's domain attracts spam and scraping. Layered defences: a honeypot field, a minimum time-on-page, a request body cap, and per-IP plus global daily quotas enforced by an atomic counter (a check-then-insert would be a spam amplifier under concurrency).

The per-IP cap is deliberately generous — Indian mobile networks put many genuine users behind one CGNAT address, and silently dropping real leads is worse than admitting some spam. Honeypot and time-floor rejections return the normal success screen, so a bot learns nothing from being caught.

## 10. Access control

Three new permissions: `site:manage` (create, publish, take down), `lead:view` (work the inbox), `lead:export` (download the book). Export is split from view on purpose — a leak should require its own permission, not come free with reading.

The public service holds **narrowly scoped database credentials**: read-only for pages, insert-only for leads. It cannot read the lead book it writes to.

## 11. Risks accepted

Two decisions carry real exposure. Both were made deliberately.

**Pages are indexed by Google.** This gives customers genuine organic visibility, and makes the roster of Prayaan customers publicly crawlable — names, photos, locations, and an implied lending relationship that persists in search caches after takedown. Mitigations in place: no address more precise than the locality, no loan amounts or dates, no use of the word "borrower", and the sitemap lists live pages only. A per-page `noindex` flag exists for customers who want the link without the visibility.

**Pages publish without prior consent, with takedown on request.** This is the weakest available position under the DPDP Act — the first objection is handled after the fact. Mitigations: a consent record is captured at creation even when collected offline, so who obtained it and when is provable; the takedown route is one tap from the page footer; takedown is instant in the database; and because pages are indexed, search-engine removal must be part of the takedown runbook rather than assuming a 404 suffices.

Both become materially cheaper to hold if a preview-and-approve step is added later. Pages are already created as drafts, so that is a small addition rather than a redesign.

## 12. Open questions

| # | Question | Owner |
|---|----------|-------|
| OQ1 | Subdomain name and certificate | Eng + brand |
| OQ2 | Photo hosting — which host serves customer images (the allow-list currently assumes a CDN) | Eng |
| OQ3 | Branch pincode map — who owns and maintains it | Ops |
| OQ4 | Written confirmation that lead-only capture keeps this outside Digital Lending / LSP scope | Compliance |
| OQ5 | Lead retention period before purge or anonymisation | Legal |
| OQ6 | First-call SLA and who owns the inbox day to day | Ops |

## 13. Status

Built and verified: data model with append-only audit history, URL permanence with alias redirects, bulk CSV import with dry-run validation, the public page with per-page link-preview tags, lead capture with anti-abuse, the lead inbox with CSV export, and access control.

Not yet done: deployment to the subdomain, the branch pincode map, and the compliance sign-offs in §12.
