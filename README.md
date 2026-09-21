# lgrow

Finds remote jobs across the English-speaking market, prepares tailored
application materials, and drafts 2–3 LinkedIn posts a week about what you're
actually working on.

Two things define the design:

- **All AI runs through the Gemini CLI**, because that is the only route that
  spends a Google AI Pro subscription (~1,500 Pro-model requests/day) instead of
  metered API credits. A Gemini API key would be billed per token and its free
  tier has been Flash-only since April 2026.
- **Your LinkedIn account is never put at risk.** Posting uses the official
  versioned API with your own OAuth token. Job discovery never touches LinkedIn
  at all. There is no browser automation against `linkedin.com` anywhere in this
  codebase. See [RISKS.md](RISKS.md) — read it before extending anything.

Typical daily cost: **~15 Gemini calls out of ~1,500 available**, because a
pure-Python prefilter cuts roughly 1,000 fetched postings to a 60-job shortlist
before the model sees anything, and scoring is batched ~12 jobs per call.

> [!TIP]
> - Quick reference for all CLI commands & scripts: **[COMMANDS.md](COMMANDS.md)**
> - Full LinkedIn automation guide & growth architecture: **[docs/LINKEDIN_AUTOMATION.md](docs/LINKEDIN_AUTOMATION.md)**

---

## Setup

### 1. Install

```bash
cd /home/dev/Linkdin_automation
python3 -m venv .venv
./.venv/bin/pip install -e .
./.venv/bin/lgrow init
```

Optional but recommended: `sudo apt install libreoffice-writer` so tailored
resumes can be exported to PDF as well as .docx.

### 2. Authenticate the Gemini CLI  *(one-time, needs a browser)*

```bash
npm install -g @google/gemini-cli   # if not already installed
gemini                              # choose "Login with Google"
```

Sign in with the account that holds your **Google AI Pro** subscription. That
subscription is the whole point — it's what makes the AI calls free at the
volume this tool uses.

If you have a Pro-model tier available, run `/settings` inside `gemini` and set
**Preview features** to `true`.

### 3. Fill in your configuration

Four files in `config/`. Everything marked `FILL_ME` blocks `lgrow doctor` until
you set it.

| File | What it holds |
|---|---|
| `profile.yaml` | Who you are, target roles, skills, geos, work authorisation, **your git identity** |
| `sources.yaml` | Which job feeds to use, search queries, and your ATS company watchlist |
| `voice.yaml` | Content pillars, tone rules, banned phrases, posting cadence |
| `ai.yaml` | Model tiers, daily call budget, retry behaviour |

Two worth attention:

- **`git.authors`** — set this to the name/email *you* commit under. Nothing is
  inferred from your environment, so if you leave it empty, commit harvesting is
  simply off. Find your identity with:
  ```bash
  git -C <your-repo> log --format='%an <%ae>' | sort -u | head
  ```
- **`ats:`** in `sources.yaml` — the highest-value source in the tool. These are
  employers' own job feeds, often days ahead of any aggregator. Add slugs from
  careers-page URLs (`jobs.lever.co/SLUG`, `boards.greenhouse.io/SLUG`,
  `jobs.ashbyhq.com/SLUG`).

### 4. Add your base resume

Save your master resume as `data/resume_base.docx` — **.docx, not PDF**, because
it gets edited. Tailoring is skipped until it exists.

### 5. LinkedIn app setup  *(one-time, ~15 minutes)*

Slightly tedious, and unavoidable: LinkedIn requires an app to be associated
with a Page, and being that Page's super admin is what lets you approve your own
app instantly.

1. **Create a LinkedIn Page** (free). You will be its super admin.
2. Go to [developer.linkedin.com](https://developer.linkedin.com) → **My Apps** →
   **Create app**, and associate the Page from step 1.
3. **Settings → Verify → Generate URL.** Open that URL yourself and approve. As
   the Page super admin, you are approving your own request.
4. **Products** tab → add both:
   - **Share on LinkedIn** → grants `w_member_social` (publishing)
   - **Sign In with LinkedIn using OpenID Connect** → grants `openid profile`
     (reads your person ID, which is required to author a post)

   Both are self-serve, free, instant, and need no approval.
5. **Auth** tab → add redirect URL `http://localhost:8765/callback`.
6. Copy `.env.example` to `.env` and paste in your Client ID and Secret.
7. Authorise:
   ```bash
   ./.venv/bin/lgrow linkedin login
   ```

### 6. Verify

```bash
./.venv/bin/lgrow doctor          # config, credentials, database, tooling
./.venv/bin/lgrow doctor --probe  # spends 2 Gemini calls to prove auth works
./.venv/bin/lgrow doctor --network # checks every job source is reachable
```

Fix everything marked ✗. Warnings (`!`) reduce quality but won't block a run.

---

## Daily use

```bash
lgrow run crawl          # fetch → filter → score → queue → prepare materials
lgrow dashboard          # review the queue in a browser (localhost only)
lgrow jobs applied <id>  # you applied; sets a 7-day follow-up reminder
lgrow jobs skip <id>     # not interested; never shown again
```

```bash
lgrow note "spent 3h on a redis connection bug; it was thread reuse"
lgrow posts draft        # draft from your notes and commits
lgrow posts review       # approve / edit / regenerate / skip  ← the gate
lgrow posts list         # see drafts, scheduled and published
```

After a post has been up a day or two, feed the numbers back so drafting learns
which pillars land. LinkedIn's `r_member_social` scope is approval-gated, so
this can't be automatic:

```bash
lgrow posts engagement 3 --likes 42 --comments 6
```

### Run it on a schedule

```bash
./scripts/install-cron.sh          # install
./scripts/install-cron.sh remove   # uninstall
```

| When | Task |
|---|---|
| 07:07 daily | `crawl` — jobs, scoring, materials |
| every 30 min | `publish-due` — publishes anything whose slot arrived |
| Sun 18:00 | `weekly` — drafts next week's posts, refreshes the token, lists follow-ups |
| on boot | `catchup` — replays whatever was missed while the machine was off |

`catchup` is what makes a laptop schedule trustworthy: it checks each task's
last success against a grace window rather than assuming the machine was on.

The installer generates a wrapper script that pins your current `PATH`. This
matters — cron runs with an almost-empty PATH and would not find `gemini`,
producing failures that look like an auth problem.

**The weekly task drafts but never publishes.** Nothing reaches your feed
without `lgrow posts review`.

---

## How it works

### Jobs

```
8 public APIs                    ~1,000 postings
      ↓
dedupe (company+title, normalised)      ~970
      ↓
prefilter — pure Python, no AI            ~80
  age · excluded keywords · geo gate · salary floor · relevance heuristic
      ↓
shortlist, ranked                          60   → ~5 Gemini calls (batched, Flash tier)
      ↓
AI scoring: fit, gaps, red flags, hidden location restrictions
      ↓
your queue, capped daily                   10   → tailoring for the strongest (Pro tier)
```

The prefilter is what makes this cheap. Scoring verdicts are cached against
(job description, profile revision), so re-crawling an unchanged posting costs
nothing.

The AI's most valuable job is catching **hidden location restrictions** — a
posting whose metadata says "remote" while the description says "must be
US-based" or "requires EST overlap". Those are demoted, not surfaced.

### Posts

```
lgrow note … ─┐
your commits ─┼→ activity pool → pillar rotation → draft (Pro tier)
job market   ─┘                                        ↓
                                    deterministic checks (quality.py)
                                                       ↓
                                    AI self-critique for AI tells
                                                       ↓
                                    YOU approve ──→ jittered slot ──→ official API
```

Every draft must be grounded in a specific harvested item — a post with no
concrete detail is worse for your reputation than not posting.

Voice rules are enforced **twice**: in the prompt, and again in Python
afterwards. A model told "no em-dashes" still produces them, so
`posts/quality.py` is the backstop that actually holds.

---

## Layout

```
config/            profile · sources · voice · ai
data/              app.db · journal.md · resume_base.docx · applications/ · logs/
src/lgrow/
  gemini.py        the only module that talks to a model
  doctor.py        preflight diagnostics
  runner.py        cron dispatch, locking, catchup
  jobs/            sources/ · normalize · score · tailor · store · crawl
  posts/           activity · generate · quality · schedule · publish · store
  linkedin/        auth (OAuth) · client (Posts API)
  dashboard/       localhost review UI
tests/             108 tests, no network required
RISKS.md           the account-safety rules — read before extending
```

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

No network, no API keys, no Gemini calls. The suite concentrates on the places
where a silent bug is expensive: LinkedIn commentary escaping, resume-invention
detection, geo ambiguity (`San Francisco, CA` is not Canada), and the post
quality gates.

## Gotchas worth knowing

- **The Gemini CLI can exit 0 while reporting an error in its JSON.** Verified on
  v0.57.0: a missing auth method returns status 0 with `{"error": …}`. Never
  trust the exit code alone — `gemini.py` checks the payload.
- **LinkedIn `commentary` is not plain text.** All of `| { } @ [ ] ( ) < > # * _ ~ \`
  must be backslash-escaped *even when unused as markup*. Unescaped parentheses
  are enough to break a post. `client.render_commentary()` handles this.
- **Model IDs churn** (`gemini-3-pro-preview` → `gemini-3.1-pro-preview` → …).
  `ai.yaml` defaults to `null`, meaning "let the CLI auto-route", which is the
  durable choice. `doctor --probe` tells you if a pinned name has gone stale.
- **RemoteOK's API returns a legal-notice object as the first array element.** It
  is not a job. Filtered by shape, not index.
- **LinkedIn tokens**: access expires in 60 days (refreshed weekly by cron),
  refresh token in 365 days (needs `lgrow linkedin login` again). `doctor` warns
  30 days out.
