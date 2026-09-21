# LinkedIn Automation & Growth Guide (`lgrow`)

This document explains **what is currently built** in this repository for LinkedIn automation, how it works under the hood, its existing capabilities and limitations, and **actionable options to scale your profile growth, connections, and engagement** in the software engineering / Node.js & TypeScript space.

---

## 1. What Is Currently Built (Current State)

The project currently contains a production-grade, safety-first **content creation and publishing pipeline** built specifically to avoid sounding like AI and avoid LinkedIn account suspensions.

### System Architecture Flow

```
┌────────────────────────────────────────────────────────┐
│                   1. Activity Harvest                  │
│  - Terminal Notes (lgrow note "...")                   │
│  - Git Commits (Your actual work from configured repos)│
│  - Market Intelligence (Real tech trends from crawled  │
│    job postings)                                       │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│                   2. LLM Drafting Pass                 │
│  - Pillar Selection (Build in public, How-to, Market,  │
│    Reflection)                                         │
│  - Tone Enforced (Direct, friend-to-friend, concrete)  │
│  - Grounded in evidence (Must cite specific facts)     │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│           3. Hostile Self-Critique & Gatekeeper        │
│  - LLM Hostile Review: Flags AI-isms & vagueness       │
│  - Deterministic Python Rules (`quality.py`):          │
│    * Hook length ≤ 140 chars (before "...see more")    │
│    * Total length 700 - 1300 chars                     │
│    * Hard-banned phrases ("delve", "game-changer",     │
│      "thrilled to announce", etc.)                     │
│    * Banned characters: em-dashes (— / –)              │
│    * Duplicate / similarity check against last 10 posts│
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│            4. Human-in-the-Loop Review CLI             │
│  `lgrow posts review`                                  │
│  [a]pprove & schedule | [e]dit | [r]egenerate | [s]kip │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│               5. Official LinkedIn Publisher           │
│  - OAuth 2.0 with `w_member_social`                    │
│  - Encodes LinkedIn's "little" format & live hashtags  │
│  - Scheduled publishing at human-jittered times        │
│    (Tue/Wed/Thu at 08:15 / 12:30 ± 20 mins)            │
└────────────────────────────────────────────────────────┘
```

---

## 2. Deep-Dive into the Existing Modules

### A. Authentication & Safety (`src/lgrow/linkedin/auth.py` & `client.py`)
- **Sanctioned OAuth 2.0 (3-legged):** Uses official LinkedIn scopes (`w_member_social`, `openid`, `profile`).
- **Zero Ban Risk for Posting:** Posts are submitted directly via official `POST /rest/posts` REST endpoints.
- **Token Management:**
  - Access tokens expire in 60 days; auto-refreshed via the refresh token (valid for 365 days).
  - Credentials stored safely in `data/.tokens.json` (ignored by Git).
- **Text Commentary Encoding:** Automatically escapes reserved LinkedIn markdown characters while keeping hashtags clickable.

### B. Activity-Driven Drafting (`src/lgrow/posts/activity.py` & `generate.py`)
- **Anti-Hallucination:** The model does not invent scenarios. It drafts only from:
  1. `data/journal.md`: Quick thoughts you write via `lgrow note "fixed a N+1 query issue in NestJS with DataLoader"`.
  2. Local Git history: Scans your recent commits for real engineering challenges and technical details.
  3. Market crawl data: Analyzes real job postings to extract hard numbers on skill demand.
- **Content Pillars Rotation:**
  - `build_in_public` (40%): What you built/broke this week with specific numbers/tech stack.
  - `technical_howto` (30%): A concrete gotcha and actionable fix (e.g. Node.js/PostgreSQL/TypeScript).
  - `market_observation` (20%): Real data on tech stacks and hiring trends.
  - `reflection` (10%): Lessons learned from building and job hunting.

### C. Quality Engine (`src/lgrow/posts/quality.py`)
- Checks the hook (the first 140 characters before LinkedIn's `...see more` fold).
- Blocks robotic buzzwords ("testament to", "unlock the power", "in today's fast-paced world", "thoughts?").
- Strips em-dashes (`—`), which are classic AI signatures.
- Rejects drafts with >55% similarity to your last 10 published posts.

---

## 3. Current Limitations

While posting is automated and high quality, LinkedIn growth relies on **distribution and reciprocal engagement**:

1. **One-Way Broadcasting (No Feed Interaction):**
   - The official LinkedIn API does **not** allow reading your feed, searching public posts, or commenting on other people's posts.
2. **No Connection Automation:**
   - The official API does not permit sending 2nd/3rd-degree connection requests or personalized invitation notes.
3. **Text-Only Content:**
   - Currently publishes text posts with hashtags. LinkedIn's algorithm prioritizes document carousels (PDF slides) and images with 2.5x - 4x more impressions.
4. **Manual Metrics Collection:**
   - Engagement tracking (`likes`, `comments`, `impressions`) requires running `lgrow posts engagement <post-id> --likes N --comments N`.

---

## 4. Growth Strategy & Roadmap: Scaling Your Presence

To grow your profile in the **Full Stack / Backend Engineer (Node.js, NestJS, TypeScript, GraphQL, PostgreSQL)** domain, you need a balanced strategy of **Content + Engagement + Inbound Networking**.

```
                           LinkedIn Growth Flywheel
                                     ┌───┐
                    ┌───────────────►│ 1 │ Post High-Value Tech Content
                    │                └───┘ (Build in public & How-tos)
                    │                  │
                    │                  ▼
                  ┌───┐              ┌───┐
Targeted Connects │ 3 │              │ 2 │ Engage on Peer & Lead Posts
(Tech Leads & CTOs)└───┘              └───┘ (Comments with technical insight)
                    ▲                  │
                    │                  ▼
                    └────────── Profile Visits & Inbound Follows
```

---

## 5. Next Evolution Options to Implement

Here are 4 concrete paths we can build into this repository to turn it into a full-scale growth engine:

### Option 1: AI-Powered Feed Engagement Assistant (High ROI)
> *The secret to rapid LinkedIn growth is writing high-value comments on posts by creators and tech leaders in your niche within the first 1-2 hours of posting.*

- **How it works:**
  1. A headless stealth browser (Playwright with your session cookie) monitors top hashtags (`#typescript`, `#nodejs`, `#nestjs`, `#graphql`, `#softwareengineering`, `#webdevelopment`) and a curated list of tech influencers / hiring managers.
  2. When a relevant post is detected, the post text is extracted.
  3. Gemini drafts **2-3 insightful technical comments** (e.g., sharing a practical edge-case or alternative architectural approach).
  4. You review and click "Approve" (or have it post automatically within safe daily limits).
- **Impact:** Positions you as an expert in front of the creator's audience, driving profile visits and inbound connection requests.

---

### Option 2: Smart Targeted Connection Engine
> *Proactively building your network with Engineering Managers, CTOs, and Senior Developers in your target markets (US, Europe, Remote).*

- **How it works:**
  1. Uses stealth automation with realistic human delays and jitter.
  2. Searches for target titles (e.g., `Engineering Manager`, `Lead Backend Engineer`, `CTO`, `Tech Lead`) in target locations.
  3. Filters for people who are active on LinkedIn (posted in last 30 days).
  4. Crafts personalized invitation notes referencing their company or shared tech stack:
     > *"Hi [Name], saw your work at [Company]. I'm also deep into Node.js and TypeScript backend architectures. Would love to connect and follow your engineering updates."*
  5. Implements a hard safety cap (e.g., 10–15 invites/day) to stay completely under LinkedIn's radar.
  6. Tracks acceptance rates in SQLite (`data/app.db`).

---

### Option 3: Document Carousel & Visual Content Generator (2.5x Algorithm Reach)
> *LinkedIn gives the highest organic reach to multi-page PDF documents (carousels).*

- **How it works:**
  1. An automated template engine (HTML/CSS to PDF via Puppeteer/WeasyPrint) turns technical how-tos and code comparisons into clean, modern slides.
  2. Slide structure:
     - Slide 1: Bold hook / title card
     - Slides 2–5: Code snippet comparisons (e.g., "Standard REST vs GraphQL Apollo Federation", "Prisma vs TypeORM vs Kysely query execution")
     - Slide 6: Summary + Call to Action
  3. Publishes as an attached document or gives you a ready-to-upload PDF.

---

### Option 4: Automated Engagement & Analytics Tracker
> *Understand which content pillars actually drive profile views, likes, and followers.*

- **How it works:**
  1. Automates the retrieval of your post stats (views, likes, comments, reposts) without manual entry.
  2. Analyzes post performance by pillar and publishing time in `lgrow dashboard`.
  3. Automatically updates `config/voice.yaml` weights so the generator produces more of what your audience engages with.

---

## 6. Safety & Account Protection Best Practices

When expanding automation into browser interactions (likes, comments, connections):
- **Never exceed safe human thresholds:**
  - Connection requests: Max 10–15 per day (approx 50–70/week).
  - Comments: Max 10–20 per day.
  - Likes/Reactions: Max 20–30 per day.
- **Randomized Jitter & Delays:** Emulate human mouse movements, realistic scroll pauses, and random intervals between actions (never fixed timer intervals).
- **Human-in-the-loop for Notes & Comments:** Reviewing AI-drafted comments before they go live ensures your professional reputation stays top-tier.
