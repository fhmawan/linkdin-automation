"""Local review dashboard.

Binds to 127.0.0.1 only. It reads your job queue and post drafts from SQLite and
never talks to LinkedIn — the publish path stays in the CLI, behind explicit
commands, so nothing can go out from a stray browser click.
"""
