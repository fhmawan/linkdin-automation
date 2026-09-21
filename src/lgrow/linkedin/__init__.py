"""LinkedIn integration — official versioned REST API only.

There is deliberately no browser automation here. LinkedIn's User Agreement
prohibits accessing the platform outside its API, and its 2026 detection
suspends sessions flagged as non-human. Everything in this package uses your
own OAuth token against documented endpoints, which is the same sanctioned path
Buffer and Hootsuite use. See RISKS.md.
"""
