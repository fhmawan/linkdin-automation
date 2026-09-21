"""lgrow — LinkedIn growth + remote job application engine.

All AI work goes through `lgrow.llm`, which calls the Gemini and OpenRouter
REST APIs directly. Job scoring routes to OpenRouter's free models — those
prompts carry no personal detail — while anything containing the resume, name
or contact details stays on Gemini. See the routing note in config/ai.yaml.

LinkedIn is touched exclusively through its official versioned API. There is no
browser automation against linkedin.com anywhere in this package, by design —
see RISKS.md.
"""

__version__ = "0.1.0"
