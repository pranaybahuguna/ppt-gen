"""Failure kinds the graph distinguishes.

The split matters for routing: a `Refused` is the engine protecting the deck (never retry
blindly), a `PlanInvalid` is the model's fault (feed the reason back and retry), and a
`Boundary` failure is ours (infrastructure; surface it, don't blame the model).
"""
from __future__ import annotations


class DeckAgentError(Exception):
    """Base class. `.detail` is JSON-safe and may be shown to the caller."""

    kind = "error"

    def __init__(self, message: str, detail: dict | list | None = None):
        """A message for people, and `detail`: the structured specifics (a list of problems, a file
        name) for logs, manifests and model feedback.
        """
        super().__init__(message)
        self.detail = detail


class Refused(DeckAgentError):
    """The engine refused the whole batch - e.g. a fingerprint no longer matches, so the
    deck changed under us. Re-probe and re-plan; never force it through."""

    kind = "refused"


class PlanInvalid(DeckAgentError):
    """The model's plan did not compile. The message is written to be fed back to the model."""

    kind = "plan_invalid"


class Boundary(DeckAgentError):
    """Something outside the model failed: the container, Postgres, object storage, the engine."""

    kind = "boundary"


class NoFit(DeckAgentError):
    """No layout in the template can hold this slide's content."""

    kind = "no_fit"


class CodeRejected(DeckAgentError):
    """Model-written code failed the static gate before it was ever run."""

    kind = "code_rejected"
