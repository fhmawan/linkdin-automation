"""A single-file review dashboard on the standard library.

Reading a job queue in a terminal is fine; comparing twelve of them and opening
the right postings is not. This gives you a scannable table, the AI's reasoning
inline, and one-click access to the generated resume and cover letter.

Deliberate constraints:
  - Binds to 127.0.0.1. Never exposed.
  - No LinkedIn calls. Publishing stays behind explicit CLI commands.
  - No external assets, so it works offline.
"""

from __future__ import annotations

import html
import json
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import config, db, paths
from ..jobs import store as job_store
from ..posts import quality as post_quality, store as post_store

HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_CSS = """
:root {
  --bg:#f6f7f9; --card:#fff; --ink:#1a1d21; --muted:#6b7280; --line:#e5e7eb;
  --accent:#0a66c2; --good:#0a7a3d; --warn:#b45309; --bad:#b91c1c;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#14171a; --card:#1c2024; --ink:#e8eaed; --muted:#9aa0a6;
          --line:#2c3238; --accent:#4a9eff; --good:#4ade80; --warn:#fbbf24;
          --bad:#f87171; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:20px 28px;border-bottom:1px solid var(--line);background:var(--card);
  display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
h1{margin:0;font-size:19px;letter-spacing:-.01em}
nav a{color:var(--muted);text-decoration:none;margin-right:16px;font-size:14px}
nav a.on{color:var(--accent);font-weight:600}
.wrap{padding:24px 28px;max-width:1180px}
.stats{display:flex;gap:22px;flex-wrap:wrap;margin-bottom:22px;color:var(--muted);
  font-size:13px}
.stats b{color:var(--ink);font-size:17px;display:block;font-variant-numeric:tabular-nums}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin-bottom:14px}
.row{display:flex;gap:16px;align-items:flex-start}
.score{flex:0 0 54px;height:54px;border-radius:9px;display:flex;align-items:center;
  justify-content:center;font-weight:700;font-size:19px;color:#fff;
  font-variant-numeric:tabular-nums}
.s-hi{background:var(--good)} .s-mid{background:var(--warn)} .s-lo{background:var(--muted)}
.title{font-weight:640;font-size:16px;margin:0 0 3px}
.meta{color:var(--muted);font-size:13px;margin-bottom:9px}
ul.pts{margin:7px 0 0;padding-left:19px}
ul.pts li{margin:2px 0}
li.g{color:var(--good)} li.b{color:var(--warn)} li.r{color:var(--bad)}
.acts{margin-top:13px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
a.btn,button.btn{display:inline-block;padding:6px 13px;border-radius:7px;
  border:1px solid var(--line);background:transparent;color:var(--ink);
  text-decoration:none;font-size:13px;cursor:pointer;font-family:inherit}
a.btn.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.btn:hover,a.btn:hover{border-color:var(--accent)}
.tag{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
  border:1px solid var(--line);border-radius:5px;padding:2px 6px}
pre.post{white-space:pre-wrap;font:inherit;background:var(--bg);padding:14px;
  border-radius:8px;border:1px solid var(--line);margin:0 0 10px}
.empty{color:var(--muted);padding:36px 0;text-align:center}
code{font-size:12px;color:var(--muted)}
.warn{color:var(--warn)} .bad{color:var(--bad)}
footer{padding:16px 28px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}
"""

_JS = """
async function act(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                            body: JSON.stringify(body)});
  if (r.ok) location.reload(); else alert('Failed: ' + await r.text());
}
"""


def _e(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def _score_class(score: int) -> str:
    return "s-hi" if score >= 80 else "s-mid" if score >= 65 else "s-lo"


def _page(title: str, active: str, body: str) -> bytes:
    nav = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
        for key, href, label in (
            ("jobs", "/", "Jobs"),
            ("posts", "/posts", "Posts"),
            ("applied", "/applied", "Applied"),
        )
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · lgrow</title><style>{_CSS}</style></head>
<body><header><h1>lgrow</h1><nav>{nav}</nav></header>
<div class="wrap">{body}</div>
<footer>Local only · publishing happens through the CLI, never from this page</footer>
<script>{_JS}</script></body></html>""".encode("utf-8")


# ─── views ───────────────────────────────────────────────────────────────────


def view_jobs() -> bytes:
    with db.session() as conn:
        entries = job_store.queue(conn, limit=100)
        stats = job_store.stats(conn)

    tiles = "".join(
        f'<div><b>{stats.get(k, 0)}</b>{label}</div>'
        for k, label in (
            ("jobs_known", "jobs seen"),
            ("scored", "scored"),
            (job_store.QUEUED, "in queue"),
            (job_store.APPLIED, "applied"),
            (job_store.SKIPPED, "skipped"),
        )
    )

    if not entries:
        body = (
            f'<div class="stats">{tiles}</div>'
            '<div class="empty">Queue is empty.<br><br>'
            '<code>lgrow run crawl</code></div>'
        )
        return _page("Jobs", "jobs", body)

    cards = []
    for entry in entries:
        job = entry.job
        points = "".join(
            f'<li class="g">{_e(w)}</li>' for w in entry.why_fit
        ) + "".join(
            f'<li class="b">{_e(g)}</li>' for g in entry.gaps
        ) + "".join(
            f'<li class="r">⚠ {_e(r)}</li>' for r in entry.red_flags
        )
        geo_warn = (
            f'<li class="r">⚠ location: {_e(entry.geo_note)}</li>'
            if not entry.geo_ok and entry.geo_note else ""
        )

        artifacts = ""
        if entry.artifacts_dir and Path(entry.artifacts_dir).is_dir():
            links = []
            for name, label in (
                ("resume.pdf", "resume (pdf)"),
                ("resume.docx", "resume (docx)"),
                ("cover_letter.md", "cover letter"),
                ("answers.md", "answers"),
                ("gaps.md", "gaps"),
            ):
                if (Path(entry.artifacts_dir) / name).exists():
                    q = urllib.parse.quote(f"{job.id}/{name}")
                    links.append(f'<a class="btn" href="/file/{q}" target="_blank">{label}</a>')
            artifacts = "".join(links)
        else:
            artifacts = (
                f'<button class="btn" onclick="act(\'/api/tailor\','
                f'{{job_id:\'{job.id}\'}})">prepare materials</button>'
            )

        salary = f' · {_e(job.salary_raw)}' if job.salary_raw else ""
        cards.append(f"""<div class="card"><div class="row">
<div class="score {_score_class(entry.score)}">{entry.score}</div>
<div style="flex:1">
  <p class="title">{_e(job.title)}</p>
  <div class="meta">{_e(job.company)} · {_e(job.location_raw or "location not stated")}
    {salary} · <span class="tag">{_e(job.source)}</span></div>
  <ul class="pts">{points}{geo_warn}</ul>
  <div class="acts">
    <a class="btn primary" href="{_e(job.apply_url or job.url)}" target="_blank"
       rel="noopener">Open &amp; apply →</a>
    {artifacts}
    <button class="btn" onclick="act('/api/state',{{job_id:'{job.id}',state:'applied'}})">
      Mark applied</button>
    <button class="btn" onclick="act('/api/state',{{job_id:'{job.id}',state:'skipped'}})">
      Skip</button>
    <code>{job.id}</code>
  </div>
</div></div></div>""")

    body = f'<div class="stats">{tiles}</div>' + "".join(cards)
    return _page("Jobs", "jobs", body)


def view_applied() -> bytes:
    with db.session() as conn:
        applied = job_store.queue(conn, state=job_store.APPLIED, limit=100)
        due = {e.job.id for e in job_store.followups_due(conn)}

    if not applied:
        return _page("Applied", "applied",
                     '<div class="empty">Nothing marked applied yet.</div>')

    rows = []
    for entry in applied:
        nudge = (
            ' <span class="tag warn">follow up</span>' if entry.job.id in due else ""
        )
        rows.append(f"""<div class="card"><div class="row">
<div class="score {_score_class(entry.score)}">{entry.score}</div>
<div style="flex:1">
  <p class="title">{_e(entry.job.title)}{nudge}</p>
  <div class="meta">{_e(entry.job.company)} · applied
    {_e((entry.applied_at or "")[:10])}</div>
  <div class="acts">
    <a class="btn" href="{_e(entry.job.url)}" target="_blank" rel="noopener">posting</a>
  </div>
</div></div></div>""")
    return _page("Applied", "applied", "".join(rows))


def view_posts() -> bytes:
    voice = config.load_voice()
    with db.session() as conn:
        drafts = post_store.pending_review(conn)
        scheduled = post_store.by_state(conn, post_store.SCHEDULED, limit=20)
        published = post_store.by_state(conn, post_store.PUBLISHED, limit=20)
        perf = post_store.pillar_performance(conn)

    def block(post, show_report: bool) -> str:
        report = post_quality.check(post.text, voice=voice, require_concrete=False)
        issues = "".join(f'<li class="r">{_e(p)}</li>' for p in report.problems)
        issues += "".join(f'<li class="b">{_e(w)}</li>' for w in report.warnings)
        crit = post.critique or {}
        crit_line = (
            f'<div class="meta">critique: specificity '
            f'{_e(crit.get("specificity", "?"))}/100 · '
            f'reads_as_ai={_e(crit.get("reads_as_ai"))}</div>'
            if crit else ""
        )
        when = post.scheduled_for or post.published_at or post.created_at
        link = (
            f'<a class="btn" href="https://www.linkedin.com/feed/update/'
            f'{_e(post.post_urn)}/" target="_blank" rel="noopener">view on LinkedIn</a>'
            if post.post_urn else ""
        )
        err = f'<div class="bad">{_e(post.last_error)}</div>' if post.last_error else ""
        return f"""<div class="card">
<div class="meta"><span class="tag">{_e(post.pillar)}</span>
  #{post.id} · {_e((when or "")[:16])} · {report.chars} chars ·
  hook {len(report.hook)}/{voice.style.hook_max_chars}</div>
<pre class="post">{_e(post.text)}</pre>
{crit_line if show_report else ""}
<ul class="pts">{issues if show_report else ""}</ul>{err}
<div class="acts">{link}<code>state: {_e(post.state)}</code></div></div>"""

    sections = []
    if drafts:
        sections.append(
            f"<h2>Awaiting your review ({len(drafts)})</h2>"
            '<div class="meta">Approve, edit or reject from the terminal — '
            'this page is read-only for posts:<br>'
            '<code>lgrow posts review</code></div>'
            + "".join(block(p, True) for p in drafts)
        )
    if scheduled:
        sections.append(f"<h2>Scheduled ({len(scheduled)})</h2>"
                        + "".join(block(p, False) for p in scheduled))
    if published:
        sections.append(f"<h2>Published ({len(published)})</h2>"
                        + "".join(block(p, False) for p in published))
    if perf:
        rows = "".join(
            f"<div><b>{r['avg_likes']}</b>{_e(r['pillar'])} "
            f"({r['posts']} post(s))</div>"
            for r in perf
        )
        sections.append(f"<h2>Average likes by pillar</h2><div class='stats'>{rows}</div>")

    if not sections:
        return _page(
            "Posts", "posts",
            '<div class="empty">No posts yet.<br><br>'
            '<code>lgrow note "what you did today"</code><br>'
            '<code>lgrow posts draft</code></div>',
        )
    return _page("Posts", "posts", "".join(sections))


# ─── server ──────────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "lgrow"

    def _send(self, payload: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                self._send(view_jobs())
            elif path == "/posts":
                self._send(view_posts())
            elif path == "/applied":
                self._send(view_applied())
            elif path.startswith("/file/"):
                self._serve_artifact(urllib.parse.unquote(path[len("/file/"):]))
            elif path == "/favicon.ico":
                self._send(b"", 204, "image/x-icon")
            else:
                self._send(_page("Not found", "", '<div class="empty">Not found</div>'), 404)
        except Exception as exc:  # noqa: BLE001 — a dev server must not die
            self._send(
                _page("Error", "", f'<div class="empty bad">{_e(exc)}</div>'), 500
            )

    def _serve_artifact(self, rel: str) -> None:
        """Serve a generated file, confined to the applications directory."""
        base = paths.APPLICATIONS_DIR.resolve()
        target = (base / rel).resolve()
        # Path traversal guard: the resolved path must stay under base.
        if not str(target).startswith(str(base) + "/") or not target.is_file():
            self._send(_page("Not found", "", '<div class="empty">No such file</div>'), 404)
            return
        ctypes = {
            ".pdf": "application/pdf",
            ".md": "text/plain; charset=utf-8",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        self._send(
            target.read_bytes(), 200, ctypes.get(target.suffix, "application/octet-stream")
        )

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(b"bad json", 400, "text/plain")
            return

        try:
            if path == "/api/state":
                job_id = str(payload.get("job_id") or "")
                state = str(payload.get("state") or "")
                if state not in (job_store.APPLIED, job_store.SKIPPED):
                    self._send(b"bad state", 400, "text/plain")
                    return
                with db.session() as conn:
                    ok = job_store.set_state(conn, job_id, state)
                self._send(b"ok" if ok else b"unknown job", 200 if ok else 404, "text/plain")
                return

            if path == "/api/tailor":
                from ..jobs import crawl as crawl_mod
                from ..jobs.models import ScoredJob

                job_id = str(payload.get("job_id") or "")
                with db.session() as conn:
                    match = [e for e in job_store.queue(conn, limit=200) if e.job.id == job_id]
                if not match:
                    self._send(b"unknown job", 404, "text/plain")
                    return
                entry = match[0]
                crawl_mod.tailor_queued(
                    [ScoredJob(job=entry.job, score=entry.score)], force=True
                )
                self._send(b"ok", 200, "text/plain")
                return

            self._send(b"not found", 404, "text/plain")
        except Exception as exc:  # noqa: BLE001
            self._send(str(exc).encode()[:500], 500, "text/plain")

    def log_message(self, *args: object) -> None:
        pass  # keep the terminal readable


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    print(f"Dashboard on {url}  (Ctrl+C to stop)")
    print("Local only. Nothing here publishes to LinkedIn.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — headless is fine, the URL is printed
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
