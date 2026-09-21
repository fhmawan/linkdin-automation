# Command & Script Reference (`COMMANDS.md`)

A simple quick-reference guide for all scripts and CLI commands in this project, explaining what each one does and how to run it.

---

## 1. Setup & Environment

| Command / Script | What It Does | Example |
| :--- | :--- | :--- |
| `python3 -m venv .venv` | Creates the isolated Python environment. | `python3 -m venv .venv` |
| `source .venv/bin/activate` | Activates virtual environment on Linux/macOS. | `source .venv/bin/activate` |
| `.venv\Scripts\activate` | Activates virtual environment on Windows PowerShell. | `.venv\Scripts\activate` |
| `pip install -e .` | Installs `lgrow` locally in editable mode. | `pip install -e .` |
| `lgrow init` | Initializes project data folders (`data/`) and SQLite database (`data/app.db`). | `lgrow init` |
| `lgrow doctor` | Runs self-diagnostics on your setup, `.env` keys, and database. | `lgrow doctor` |
| `lgrow doctor --probe` | Verifies LLM API authentication by sending a test prompt. | `lgrow doctor --probe` |
| `lgrow usage` | Displays your daily model usage, API calls count, and token caps. | `lgrow usage` |

---

## 2. LinkedIn Authentication

| Command | What It Does | Example |
| :--- | :--- | :--- |
| `lgrow linkedin login` | Starts 3-legged OAuth flow in your browser to authorize your LinkedIn account. | `lgrow linkedin login` |
| `lgrow linkedin whoami` | Shows the currently authenticated profile name, URN, and token expiry dates. | `lgrow linkedin whoami` |
| `lgrow linkedin refresh` | Manually refreshes your 60-day access token. | `lgrow linkedin refresh` |

---

## 3. Feeding Content: Notes & Journaling

The AI drafts posts based on your real engineering work, not hallucinations. Use this command whenever you learn or build something:

| Command | What It Does | Example |
| :--- | :--- | :--- |
| `lgrow note "<text>"` | Appends a timestamped note to `data/journal.md` as raw material for future posts. | `lgrow note "Fixed N+1 queries in NestJS using DataLoader batching"` |

---

## 4. LinkedIn Posts: Creation & Publishing

| Command | What It Does | Example |
| :--- | :--- | :--- |
| `lgrow posts draft` | Harvests recent notes/commits and drafts weekly posts across content pillars. | `lgrow posts draft` |
| `lgrow posts review` | **Interactive Review CLI**: View drafted posts, approve (`a`), edit (`e`), regenerate (`r`), or skip (`s`). | `lgrow posts review` |
| `lgrow posts list` | Lists all posts grouped by state (`draft`, `approved`, `scheduled`, `published`). | `lgrow posts list` |
| `lgrow posts schedule` | Assigns approved posts to upcoming calendar slots (Tue/Wed/Thu with jitter). | `lgrow posts schedule` |
| `lgrow run publish` | Publishes any post whose scheduled time has arrived to your LinkedIn feed. | `lgrow run publish` |
| `lgrow posts engagement <id>` | Records feedback metrics (likes, comments) for a published post. | `lgrow posts engagement 12 --likes 45 --comments 8` |

---

## 5. LinkedIn Feed Engagement & In-Field Comments (Option A)

Target and engage with other software engineers, tech leads, and posts in your domain:

| Command | What It Does | Example |
| :--- | :--- | :--- |
| `lgrow engage add <url>` | Adds a specific LinkedIn post by URL, fetches its content, and drafts 3 technical comments. | `lgrow engage add "https://www.linkedin.com/feed/update/urn:li:activity:..."` |
| `lgrow engage scan` | Uses the stealth browser to scan target hashtags (`#nestjs`, `#typescript`, `#graphql`, etc.) for recent posts. | `lgrow engage scan` |
| `lgrow engage draft` | Generates 3 anti-AI human archetypes (*Gotcha*, *Trade-off*, *Recipe*) for pending posts. | `lgrow engage draft` |
| `lgrow engage review` | **Interactive Mode**: View post + author, pick option `[1]`, `[2]`, `[3]`, edit with `[e]`, copy to clipboard with `[c]`, or post with `[p]`. | `lgrow engage review` |
| `lgrow engage run --auto` | **Autonomous Mode**: Hands-free background worker that scans, picks the best comment, and auto-posts with human typing delays and random intervals. | `lgrow engage run --auto` |
| `lgrow engage stats` | Shows engagement stats: number of posts discovered, comments approved, and comments posted. | `lgrow engage stats` |

---

## 6. Background Scheduling & Automation Scripts

These scripts run in the background on your machine so you don't have to trigger commands manually every day:

| Script File | OS | What It Does | How to Run |
| :--- | :--- | :--- | :--- |
| `scripts/install-cron.sh` | Linux / macOS | Installs cron jobs that run daily checks and publish scheduled posts automatically. | `bash scripts/install-cron.sh` |
| `scripts/install-schedule.ps1` | Windows | Configures Windows Task Scheduler to run the scheduled pipeline in the background. | `powershell -ExecutionPolicy Bypass -File scripts/install-schedule.ps1` |
| `lgrow run catchup` | All | Replays any scheduled tasks that were missed while your computer was asleep or turned off. | `lgrow run catchup` |

---

## 7. Configuration Files Reference

All settings can be customized in the `config/` directory:

- **`config/profile.yaml`**: Your identity, headline, core skills (TypeScript, NestJS, etc.), and local Git repos to harvest commits from.
- **`config/voice.yaml`**: Post tone guidelines, pillar weights (`build_in_public`, `technical_howto`, etc.), and banned AI words/characters.
- **`config/engagement.yaml`**: Target hashtags, monitored creators, daily comment caps, and autonomous mode settings.
- **`config/ai.yaml`**: Model tier selection (Gemini vs OpenRouter) and daily budget limits.
