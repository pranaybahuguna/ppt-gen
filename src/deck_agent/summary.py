"""What a finished operation did, in lines a person reads, and where to download the deck.

Used by the server (every job's result) and the command line (the jobs table's summary).
"""
from __future__ import annotations

from .settings import settings


def base_url() -> str:
    """Where this server is reachable, for download links in results."""
    s = settings()
    return s.server_url or f"http://{s.server_host}:{s.server_port}"


def describe(result: dict, deck_id: str | None) -> dict:
    """A facade result plus `summary` (lines a person reads) and `download_url`.

    Called by: every operation below, on its way out.
    """
    lines: list[str] = []
    if result.get("question"):
        lines.append(f"question: {result['question']}")
    elif not result.get("ok") and not result.get("deck"):
        lines.append(f"declined: {result.get('reason') or 'nothing was produced'}")
        lines += [f"error: {e.get('message')}" for e in result.get("errors") or []]
    else:
        lines.append(f"{result.get('route') or 'done'}: version "
                     f"{result.get('version') or result.get('versions')} of the deck")
        for c in result.get("changelog") or []:
            if c.get("change") == "text":
                lines.append(f"slide {c.get('slide')}: text {c.get('before', '')!r} -> {c.get('after', '')!r}")
            else:
                where = f"slide {c['slide']}: " if c.get("slide") and "slide" not in str(c.get("change")) else ""
                extra = f" - {c['explanation']}" if c.get("explanation") else ""
                counts = c.get("pattern_counts")
                extra += f" ({', '.join(f'{k} x{v}' for k, v in counts.items())})" if counts else ""
                lines.append(f"{where}{c.get('change')}{extra}")
        lines += [f"warning: {w}" for w in result.get("warnings") or []]
        lines += [f"error: {e.get('message')}" for e in result.get("errors") or []]
    out = {**result, "summary": lines}
    if deck_id and result.get("deck"):
        out["download_url"] = f"{base_url()}/decks/{deck_id}/download"
    return out
