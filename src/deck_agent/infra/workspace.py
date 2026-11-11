"""A scratch directory for one run, with the store attached.

The rule this enforces: **local paths never enter the graph state; object URIs do.** A node
opens a workspace, pulls in the few files it needs, works on them, publishes what matters, and
the directory is deleted. If the process dies mid-run, nothing is lost that was published, and
nothing is kept that was not.

    with open_workspace(state["thread_id"], state["run_id"]) as ws:
        src = ws.fetch(state["deck_versions"][-1], "source.pptx")
        ...
        uri = ws.publish("out.pptx", "v002.pptx")
"""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..settings import Settings, settings
from .storage import Store, store


@dataclass
class Workspace:
    root: Path
    store: Store
    thread_id: str
    run_id: str
    published: dict[str, str] = field(default_factory=dict)

    # -- local paths -------------------------------------------------------------------
    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def dir(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # -- object storage ----------------------------------------------------------------
    def key(self, name: str, *, versioned: bool = False) -> str:
        """Where a name lives in the bucket. Run artefacts are namespaced by run; deck
        versions and kits belong to the thread, because they outlive the run that made them."""
        return f"{self.thread_id}/{name}" if versioned else f"{self.thread_id}/{self.run_id}/{name}"

    def fetch(self, uri: str, name: str) -> Path:
        local = self.path(name)
        if local.exists():
            return local
        return self.store.get(uri, local)

    def publish(self, name: str, as_name: str | None = None, *, versioned: bool = False) -> str:
        uri = self.store.put(self.path(name), self.key(as_name or name, versioned=versioned))
        self.published[as_name or name] = uri
        return uri

    def publish_json(self, obj, name: str, *, versioned: bool = False) -> str:
        uri = self.store.put_json(obj, self.key(name, versioned=versioned))
        self.published[name] = uri
        return uri

    def publish_dir(self, folder: str, pattern: str = "*") -> list[str]:
        out = []
        for f in sorted(self.dir(folder).glob(pattern)):
            if f.is_file():
                out.append(self.store.put(f, self.key(f"{folder}/{f.name}")))
        return out

    def close(self, keep: bool = False) -> None:
        if not keep:
            shutil.rmtree(self.root, ignore_errors=True)


@contextlib.contextmanager
def open_workspace(thread_id: str, run_id: str, s: Settings | None = None, keep: bool = False):
    """A temporary directory plus the store, cleaned up on the way out."""
    s = s or settings()
    root = Path(tempfile.mkdtemp(dir=s.workspace_root, prefix=f"{run_id}-"))
    ws = Workspace(root=root, store=store(s), thread_id=thread_id, run_id=run_id)
    try:
        yield ws
    finally:
        ws.close(keep=keep)
