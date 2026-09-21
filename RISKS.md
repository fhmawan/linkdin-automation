# Account safety: the rules this project does not break

The point of this tool is to grow a LinkedIn presence and find remote work
**without putting the account at risk**. That is a design constraint, not a
preference, and it is the reason several obvious-looking features are absent.

If you extend this project, read this file first. Every rule below exists
because breaking it is how people lose accounts.

---

## The hard rules

### 1. Never automate a logged-in LinkedIn browser session

No Playwright, Puppeteer, Selenium, or headless Chrome pointed at
`linkedin.com`. Not for job search, not for Easy Apply, not for connection
requests, not "just for reading".

**Why.** LinkedIn's User Agreement prohibits accessing the platform outside its
official API. Their 2026 detection does not merely rate-limit sessions it judges
non-human — it moves straight to suspension. Independent analyses put roughly
23% of automation-tool users under restriction within 90 days. The behavioural
fingerprinting looks for the mathematical regularity of a script rather than any
single forbidden action, so "going slowly" and "adding random delays" do not
solve it.

The asymmetry is what matters: a suspension destroys the network that the
posting flow exists to build. There is no version of the automated-apply feature
whose upside justifies that.

### 2. Publishing goes only through the official versioned API

`POST https://api.linkedin.com/rest/posts`, authenticated with your own OAuth
token, holding the `w_member_social` scope from the self-serve *Share on
LinkedIn* product.

This is explicitly sanctioned use — the same mechanism Buffer, Hootsuite and
every other scheduler runs on. There is no risk here worth managing.

The member throttle is **150 requests per day**. Three posts a week uses about
0.3% of it, so the limit is never a constraint; if you ever approach it,
something is wrong.

### 3. Job discovery never touches LinkedIn

Jobs come from documented public JSON APIs only:

| Source | Type |
|---|---|
| Himalayas, Remotive, Arbeitnow, Jobicy, RemoteOK | remote-work aggregators |
| Greenhouse, Lever, Ashby | the employer's own ATS board |

No LinkedIn job scraping, ever. This costs nothing in coverage — the ATS
endpoints are the employer's source of truth and often carry a posting days
before any aggregator indexes it.

### 4. A human approves every published word

Drafts are generated automatically; nothing publishes without you running
`lgrow posts review` and approving it. The weekly cron **drafts only**.

This is partly reputational — one hallucinated claim under your own name is
expensive — and partly about safety: a pipeline that cannot publish
unsupervised cannot develop a spam pattern.

### 5. A human submits every application

The tool scores jobs, tailors your resume, drafts a cover letter and prepares
screener answers. **You** open the posting and press submit.

### 6. Nothing invented, ever

Resume tailoring may only re-word and re-emphasise experience already in your
base resume. This is enforced structurally, not merely requested in a prompt:

- Edits are applied only where the model's quoted original matches a paragraph
  **exactly**, so it cannot append new sections or employers.
- Any figure appearing in a replacement that was not in the source paragraph is
  refused and reported. Fabricated metrics are the most damaging and the most
  likely invention.

See `suspicious_numbers()` and `apply_rewrites()` in
`src/lgrow/jobs/tailor.py`, and the tests in `tests/test_tailor.py`.

### 7. Nothing looks machine-timed

Publishing slots are jittered by up to ±20 minutes, capped at one post per day,
and restricted to the weekdays you configure. A post landing at exactly
08:15:00 every Tuesday is a signature; there is no reason to produce one.

### 8. Be a good citizen of the free APIs

Identifying User-Agent, a delay between requests, bounded retries with backoff,
no concurrency, and the original posting URL preserved as the only apply route.
Remotive, Jobicy and RemoteOK all ask for attribution and link-backs in their
terms; RemoteOK states plainly that they will suspend API access for misuse.
These are small teams giving away useful data.

---

## What is deliberately not built

| Not built | Why |
|---|---|
| LinkedIn Easy Apply automation | Rule 1. This is the feature that gets accounts suspended. |
| Auto connection requests / DMs | Rule 1, and the fastest route to a restriction. |
| LinkedIn job scraping | Rule 3. Unnecessary — better sources exist. |
| Reading your own post analytics | `r_member_social` is approval-gated and unavailable. Log numbers by hand with `lgrow posts engagement`. |
| Fully automatic publishing | Rule 4. Available as a change you could make; the trade is your reputation. |
| Posting to a company page | Needs `w_organization_social`, which is not self-serve. |

## Adjacent automation that is *not* a LinkedIn risk

Auto-filling a **Greenhouse / Lever / Ashby** application form with Playwright
touches none of LinkedIn's systems and carries no suspension risk. It was left
out for a different reason: per-ATS form maintenance is significant and the
forms change often. If you add it, stop before the submit button and keep
rules 5 and 6 intact.

## If your account does get restricted

Nothing in this tool should cause it, but if it happens: stop all scheduled runs
(`scripts/install-cron.sh remove`), appeal through LinkedIn's own flow, and do
not create a second account — that converts a temporary restriction into a
permanent ban.

---

## Reviewing a change against this file

Before adding anything that touches LinkedIn, ask:

1. Does it use a documented API endpoint with my own OAuth token? If no, stop.
2. Could its request pattern be distinguished from a person using the app? If
   yes, stop.
3. Does it publish or transmit anything a human has not explicitly approved? If
   yes, add the approval gate first.
4. Could it state something about me that is not verifiably true? If yes, add
   the structural check, not just a prompt instruction.
