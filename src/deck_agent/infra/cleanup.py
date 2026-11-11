"""Cleaning up: what can be deleted, and what a session still needs.

A deck lives in two places, and cleaning only one of them leaves the other broken:

    the bucket      <deck_id>/v001.pptx, <deck_id>/<run>/...   the files
    the checkpoint  thread <deck_id> in Postgres                the conversation

Delete the files and keep the thread, and the next "make slide 2 shorter" finds a session
pointing at a deck that is not there. Delete the thread and keep the files, and nothing will
ever read them again. So everything here works per deck, on both at once.

Three kinds of clean-up, from gentlest to bluntest:

* **tidy** - keep the deck usable and drop the bulk. A session keeps every file its state
  still refers to (every version, for undo; the plan that built the current deck, for
  rebuilds; model-written slide code, for replays; the template and kit), the whole of its
  latest run, and every run's `manifest.json` (a few KB each - the history of what was done).
  What goes is older runs' previews, outlines, plans and ops, and any version that undo
  already dropped. This is what to run after a deck is finished, and what `tidy_after_turn`
  runs on its own.
* **purge** - one deck gone completely: its files and its conversation.
* **everything** - the whole bucket and every conversation. Templates and kits can be kept.

Every function here *plans* first and returns what it would delete, so the command line can
show a dry run; nothing is deleted until `execute()` is called with that plan.
"""
from __future__ import annotations

import contextlib
import fnmatch
import time
from dataclasses import dataclass, field

from .storage import Obj, Ref, Store

#: Top-level prefixes that are not decks. Shared by every deck, so never swept as one.
SHARED = ("kits", "uploads", "seed")
#: What the test suites leave behind when they are interrupted before cleaning up.
TEST_PATTERNS = ("test-*", "tests")


@dataclass
class Plan:
    """What a clean-up would delete. Nothing has happened yet."""

    objects: list[Obj] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)
    kept: int = 0                              # objects looked at and deliberately kept
    notes: list[str] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(o.size for o in self.objects)

    def add(self, other: "Plan") -> "Plan":
        seen = {o.uri for o in self.objects}
        self.objects += [o for o in other.objects if o.uri not in seen]
        self.threads += [t for t in other.threads if t not in self.threads]
        self.kept += other.kept
        self.notes += other.notes
        return self

    def summary(self) -> dict:
        by_prefix: dict[str, list[int]] = {}
        for o in self.objects:
            top = o.key.split("/", 1)[0]
            n = by_prefix.setdefault(top, [0, 0])
            n[0] += 1
            n[1] += o.size
        return {"objects": len(self.objects), "size": human(self.bytes),
                "sessions": len(self.threads), "kept": self.kept,
                "by_prefix": {k: f"{n} objects, {human(b)}" for k, (n, b) in sorted(by_prefix.items())},
                "sessions_to_delete": sorted(self.threads), "notes": self.notes}


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# --------------------------------------------------------------------------- what is there

def top_level(objects: list[Obj]) -> dict[str, list[Obj]]:
    out: dict[str, list[Obj]] = {}
    for o in objects:
        out.setdefault(o.key.split("/", 1)[0], []).append(o)
    return out


def thread_ids(saver) -> set[str]:
    """Every conversation the checkpointer holds.

    Postgres is asked directly: `saver.list()` would load every checkpoint's channel values
    just to read one column.
    """
    conn = getattr(saver, "conn", None)
    if conn is not None:
        # a plain connection, or a pool that lends one
        ctx = contextlib.nullcontext(conn) if hasattr(conn, "execute") else conn.connection()
        with ctx as c:
            rows = c.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        return {r["thread_id"] if isinstance(r, dict) else r[0] for r in rows}
    return {c.config["configurable"]["thread_id"] for c in saver.list(None)}


def state_of(saver, thread_id: str) -> dict:
    tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    return dict(tup.checkpoint.get("channel_values") or {}) if tup else {}


def referenced(values, bucket: str) -> set[str]:
    """Every object URI anywhere in a session's state, however deeply nested."""
    out: set[str] = set()
    prefix = f"s3://{bucket}/"

    def walk(v):
        if isinstance(v, str):
            if v.startswith(prefix):
                out.add(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)

    walk(values)
    return out


def inventory(store: Store, saver) -> dict:
    """What is in the bucket and the checkpointer, per deck."""
    groups = top_level(store.objects(""))
    threads = thread_ids(saver)
    decks = {}
    for name in sorted(set(groups) | threads):
        objs = groups.get(name, [])
        kind = ("shared" if name in SHARED else "test" if _matches(name, TEST_PATTERNS)
                else "deck")
        newest = max((o.modified for o in objs), default=0)
        decks[name] = {"kind": kind, "objects": len(objs), "size": human(sum(o.size for o in objs)),
                       "session": name in threads,
                       "last_written": time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))
                       if newest else None}
    total = sum(len(v) for v in groups.values())
    return {"bucket": store.bucket, "objects": total,
            "size": human(sum(o.size for v in groups.values() for o in v)),
            "sessions": len(threads), "prefixes": decks}


def _matches(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


# --------------------------------------------------------------------------- planning

def plan_purge(store: Store, saver, patterns: list[str]) -> Plan:
    """Decks matching any of `patterns` (shell-style: `demo-*`), files and conversation both."""
    groups = top_level(store.objects(""))
    threads = thread_ids(saver)
    plan = Plan()
    names = {n for n in set(groups) | threads if _matches(n, patterns)}
    for name in sorted(names):
        if name in SHARED:
            plan.notes.append(f"{name}/ is shared by every deck, not a deck: left alone "
                              "(use --all to clear it)")
            continue
        plan.objects += groups.get(name, [])
        if name in threads:
            plan.threads.append(name)
    if not names:
        plan.notes.append(f"nothing matches {patterns}")
    return plan


def plan_tidy(store: Store, saver, deck_id: str) -> Plan:
    """Keep what the session needs, the latest run, and every manifest. Drop the rest."""
    plan = Plan()
    values = state_of(saver, deck_id)
    objs = store.objects(f"{deck_id}/")
    if not values:
        plan.notes.append(f"{deck_id}: no session, so there is nothing to keep it usable for "
                          "- skipped (purge it, or clean --orphans)")
        plan.kept += len(objs)
        return plan
    keep = referenced(values, store.bucket)
    latest = values.get("run_id")
    for o in objs:
        parts = o.key.split("/")
        in_latest = len(parts) > 2 and parts[1] == latest
        if o.uri in keep or in_latest or parts[-1] == "manifest.json":
            plan.kept += 1
        else:
            plan.objects.append(o)
    return plan


def plan_tidy_all(store: Store, saver, patterns: list[str] | None = None) -> Plan:
    plan = Plan()
    for deck in sorted(thread_ids(saver)):
        if patterns and not _matches(deck, patterns):
            continue
        plan.add(plan_tidy(store, saver, deck))
    return plan


def plan_orphans(store: Store, saver) -> Plan:
    """Files with no conversation (nothing will read them) and conversations with no files
    (they point at decks that are gone)."""
    groups = top_level(store.objects(""))
    threads = thread_ids(saver)
    plan = Plan()
    for name, objs in groups.items():
        if name not in SHARED and name not in threads:
            plan.objects += objs
    plan.threads += sorted(t for t in threads if t not in groups)
    return plan


def plan_older_than(store: Store, saver, seconds: float) -> Plan:
    """Decks nobody has written to for `seconds`: purged."""
    cutoff = time.time() - seconds
    groups = top_level(store.objects(""))
    stale = [n for n, objs in groups.items()
             if n not in SHARED and max(o.modified for o in objs) < cutoff]
    return plan_purge(store, saver, stale) if stale else Plan(notes=["nothing that old"])


def plan_everything(store: Store, saver, keep_templates: bool = False) -> Plan:
    plan = Plan()
    for o in store.objects(""):
        if keep_templates and o.key.split("/", 1)[0] in ("kits", "uploads"):
            plan.kept += 1
        else:
            plan.objects.append(o)
    plan.threads = sorted(thread_ids(saver))
    return plan


# --------------------------------------------------------------------------- doing it

def execute(store: Store, saver, plan: Plan) -> dict:
    """Delete what the plan says. Conversations first: a thread with no files is broken, but a
    file with no thread is merely unreachable, so if this stops halfway the second is better."""
    for t in plan.threads:
        saver.delete_thread(t)
    gone = store.delete([o.uri for o in plan.objects]) if plan.objects else 0
    return {"deleted_objects": gone, "freed": human(plan.bytes),
            "deleted_sessions": len(plan.threads), "kept": plan.kept}


def tidy_session(store: Store, saver, deck_id: str) -> dict:
    """Tidy one deck now. What `tidy_after_turn` calls once a turn has delivered."""
    return execute(store, saver, plan_tidy(store, saver, deck_id))


def keep_only_kit(store: Store, template_id: str) -> dict:
    """Make `kits/<template_id>.json` the only kit, for `single_kit`.

    Each other kit goes with the template it was built from (`uploads/<its hash>-*`): a kit
    addresses that file's shape ids and is useless without it, and the template is useless to
    a builder without its kit. Other uploads - decks people attached - are not touched.

    Decks already built from a removed kit keep their files and stay editable as decks; what
    they lose is a rebuild from their template (a layout change asks for one), which says so.
    """
    removed_kits, removed_templates = [], []
    for o in store.objects("kits/"):
        other = o.key[len("kits/"):].removesuffix(".json")
        if other == template_id:
            continue
        removed_kits.append(o.uri)
        removed_templates += [u.uri for u in store.objects(f"uploads/{other}-")]
    if removed_kits or removed_templates:
        store.delete(removed_kits + removed_templates)
    return {"removed_kits": removed_kits, "removed_templates": removed_templates}


def parse_age(text: str) -> float:
    """`7d`, `12h`, `30m` -> seconds."""
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        return float(text[:-1]) * units[text[-1].lower()]
    except (KeyError, ValueError, IndexError):
        raise ValueError(f"not an age: {text!r} (use e.g. 30m, 12h, 7d, 2w)") from None


__all__ = ["Plan", "inventory", "plan_purge", "plan_tidy", "plan_tidy_all", "plan_orphans",
           "plan_older_than", "plan_everything", "execute", "tidy_session", "parse_age",
           "keep_only_kit",
           "referenced", "thread_ids", "Ref"]
