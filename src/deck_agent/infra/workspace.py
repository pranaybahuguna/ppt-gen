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
    """A run's local scratch directory and its folder in the bucket, together.

    Files are worked on locally and published to `<thread>/<run>/` (or `<thread>/` for deck
    versions); the scratch directory is deleted when the node finishes.

    Built by: open_workspace, through nodes.common.workspace in every node that touches a file.
    """
    root: Path
    store: Store
    thread_id: str
    run_id: str
    published: dict[str, str] = field(default_factory=dict)

    # -- local paths -------------------------------------------------------------------
    def path(self, *parts: str) -> Path:
        """A file path inside this run's scratch directory (its parent created).

        Called by: every node that writes a local file.
        """
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def dir(self, *parts: str) -> Path:
        """A directory inside this run's scratch directory, created.

        Called by: make_previews, ensure_kit, publish_dir.
        """
        p = self.root.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # -- object storage ----------------------------------------------------------------
    def key(self, name: str, *, versioned: bool = False) -> str:
        """Where a name lives in the bucket. Run artefacts are namespaced by run; deck
        versions and kits belong to the thread, because they outlive the run that made them."""
        return f"{self.thread_id}/{name}" if versioned else f"{self.thread_id}/{self.run_id}/{name}"

    def fetch(self, uri: str, name: str) -> Path:
        """Download an object into the scratch directory as `name`, or reuse it if already there.

        Called by: every node that works on a deck or a template.
        """
        local = self.path(name)
        if local.exists():
            return local
        return self.store.get(uri, local)

    def publish(self, name: str, as_name: str | None = None, *, versioned: bool = False) -> str:
        """Upload a scratch file to the run folder, or with versioned=True to the deck's own folder
        (where deck versions live). -> its URI, also kept in `published`.

        Called by: publish_version, ensure_kit (a prepared template).
        """
        uri = self.store.put(self.path(name), self.key(as_name or name, versioned=versioned))
        self.published[as_name or name] = uri
        return uri

    def publish_json(self, obj, name: str, *, versioned: bool = False) -> str:
        """Store an object as JSON in the run folder (or the deck folder with versioned=True). ->
        URI.

        Called by: nodes recording what they did: outline.json, plan.json, repair-N.json,
        manifest.json...
        """
        uri = self.store.put_json(obj, self.key(name, versioned=versioned))
        self.published[name] = uri
        return uri

    def publish_dir(self, folder: str, pattern: str = "*") -> list[str]:
        """Upload every file in a scratch subdirectory to the run folder. -> their URIs, sorted.

        Called by: make_previews (the rendered slide PNGs).
        """
        out = []
        for f in sorted(self.dir(folder).glob(pattern)):
            if f.is_file():
                out.append(self.store.put(f, self.key(f"{folder}/{f.name}")))
        return out

    def close(self, keep: bool = False) -> None:
        """Delete the scratch directory, unless keep=True.

        Called by: open_workspace (and nodes.common.workspace) when the node finishes.
        """
        if not keep:
            shutil.rmtree(self.root, ignore_errors=True)


@contextlib.contextmanager
def open_workspace(thread_id: str, run_id: str, s: Settings | None = None, keep: bool = False):
    """A temporary directory plus the store, cleaned up on the way out.

    Called by: nodes.common.workspace.
    """
    s = s or settings()
    root = Path(tempfile.mkdtemp(dir=s.workspace_root, prefix=f"{run_id}-"))
    ws = Workspace(root=root, store=store(s), thread_id=thread_id, run_id=run_id)
    try:
        yield ws
    finally:
        ws.close(keep=keep)
