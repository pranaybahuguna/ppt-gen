"""Storage: where decks and artefacts of record live - a folder on this machine.

Why references and not paths? Because the graph is checkpointed. A conversation can be resumed
in another process, a week later - so the state holds *references* (`local://deckops/<key>`)
that any process on this machine resolves to the same file, never a scratch path. Scratch
directories (local/workspaces) can be deleted at any moment without losing anything; the store
(`local/store/`, `store_root`) cannot.

What goes in here (under local/store/deckops/):

    <thread>/v001.pptx            every version of the deck (never overwritten: that is undo)
    <thread>/<run>/storyline.json intermediate artefacts, so a run can be replayed or debugged
    <thread>/<run>/manifest.json
    <thread>/<run>/previews/slide-01.png
    uploads/<hash>-<name>.pptx    templates and decks handed in (and a template's .guide.json)

Deleting any of it goes through `infra/cleanup.py`, which removes a deck's files and its
checkpointed conversation together.

References made when this was an S3 bucket (`s3://deckops/<key>`) still resolve: they name the
same key, and the files were copied across. `MemoryStore` is the same interface with a dict
behind it, for tests.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..errors import Boundary
from ..settings import REPO, Settings, settings

#: The scheme of every reference this store makes; "s3" is read too (references from before).
SCHEME = "local"
_SCHEMES = ("local", "s3")


def is_ref(value) -> bool:
    """Whether a string is a stored-object reference (local://... or a legacy s3://...)."""
    return isinstance(value, str) and value.startswith(tuple(f"{s}://" for s in _SCHEMES))


def sha256_file(path: str | Path, length: int = 16) -> str:
    """The first `length` hex digits of a file's SHA-256: the content hash used to key templates
    and to name uploads.

    Called by: the CLI's uploads, templates.ingest, nodes.common.template_fingerprint, tests.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:length]


@dataclass(frozen=True)
class Ref:
    """A parsed `local://<store>/<key>` (or a legacy `s3://<store>/<key>`)."""

    bucket: str
    key: str

    @property
    def uri(self) -> str:
        """This reference as `local://<store>/<key>`."""
        return f"{SCHEME}://{self.bucket}/{self.key}"

    @property
    def name(self) -> str:
        """The last part of the key - the file name."""
        return self.key.rsplit("/", 1)[-1]

    @classmethod
    def parse(cls, uri: str) -> "Ref":
        """Parse a reference; raise Boundary for anything else.

        Called by: every Store method that takes a URI, cleanup, gather.
        """
        u = urlparse(uri)
        key = u.path.lstrip("/")
        if u.scheme not in _SCHEMES or not u.netloc or not key:
            raise Boundary(f"not an object reference: {uri!r} (expected {SCHEME}://<store>/<key>)")
        if any(part in ("..", "") for part in key.split("/")):
            raise Boundary(f"not an object reference: {uri!r} (a key may not climb out of the store)")
        return cls(u.netloc, key)


def canonical(uri: str) -> str:
    """The one spelling of a reference (a legacy s3:// one becomes local://), for comparisons."""
    return Ref.parse(uri).uri


@dataclass(frozen=True)
class Obj:
    """One object, as a listing reports it: enough to decide whether to keep it."""

    uri: str
    size: int
    modified: float          # unix seconds

    @property
    def key(self) -> str:
        """The object's key within the store."""
        return Ref.parse(self.uri).key


class Store:
    """The interface the graph uses. Two implementations: FileStore and MemoryStore."""

    bucket: str

    def ensure_bucket(self) -> None:
        """Create the store's folder if it does not exist.

        Called by: store() on first use, cmd_init, cmd_doctor.
        """
        ...

    def put(self, local: str | Path, key: str) -> str:
        """Upload a local file to `key`. -> its local:// URI.

        Called by: Workspace.publish / publish_dir (run artefacts, deck versions), the CLI's
        uploads
        (templates, context documents, attached decks), templates.ingest.
        """
        ...

    def put_bytes(self, data: bytes, key: str) -> str:
        """Store bytes at `key`. -> its local:// URI.

        Called by: put_json, the server's uploads, tests.
        """
        ...

    def get(self, uri: str, local: str | Path) -> Path:
        """Download an object to a local path (parent directories created). -> the path.

        Called by: Workspace.fetch (every node that needs a deck or template), cmd_get.
        """
        ...

    def get_bytes(self, uri: str) -> bytes:
        """An object's bytes. Raises when it does not exist.

        Called by: get_json, gather (context documents), the code-slide replay (stored code).
        """
        ...

    def exists(self, uri: str) -> bool:
        """Whether an object exists.

        Called by:
        cleanup, tests.
        """
        ...

    def list(self, prefix: str) -> list[str]:
        """Every object URI under a prefix, in key order.

        Called by: cmd_doctor (object count), tests, ad hoc inspection.
        """
        ...

    def objects(self, prefix: str) -> list["Obj"]:
        """Every object under a prefix, with size and last-modified time (Obj).

        Called by: infra.cleanup (planning what to delete), cmd_ls.
        """
        ...

    def delete(self, uris: list[str]) -> int:
        """Delete objects by URI. -> how many were deleted.

        Called by: cleanup.execute.
        """
        ...

    def url(self, uri: str, seconds: int = 3600) -> str:
        """Where a person can open the object: a file:// link to it on this machine.

        Called by: deliver (the result's `url`).
        """
        ...

    # -- convenience shared by both implementations ------------------------------------
    def put_json(self, obj, key: str) -> str:
        """Store an object as indented UTF-8 JSON. -> its URI.

        Called by: Workspace.publish_json (run artefacts), tests.
        """
        import json
        return self.put_bytes(json.dumps(obj, indent=1, ensure_ascii=False).encode(), key)

    def get_json(self, uri: str):
        """Read a JSON object.

        Called by: the nodes that load a stored JSON artefact.
        """
        import json
        return json.loads(self.get_bytes(uri))


class FileStore(Store):
    """Objects as files: `<root>/<store>/<key>`. Writes go to a temporary file first and are then
    renamed into place, so a reader never sees half a deck.

    Built by: store(), when store_backend is 'local' (the default).
    """

    def __init__(self, root: str | Path, bucket: str = "deckops"):
        """A store rooted at `root` (relative paths are under the repository)."""
        root = Path(root)
        self.root = (root if root.is_absolute() else REPO / root).resolve()
        self.bucket = bucket

    def _path(self, uri_or_key: str, is_key: bool = False) -> Path:
        ref = Ref(self.bucket, uri_or_key) if is_key else Ref.parse(uri_or_key)
        if is_key and any(part in ("..", "") for part in uri_or_key.split("/")):
            raise Boundary(f"not a key: {uri_or_key!r}")
        p = (self.root / ref.bucket / ref.key).resolve()
        if self.root not in p.parents:
            raise Boundary(f"not an object reference: {uri_or_key!r}")
        return p

    def ensure_bucket(self) -> None:
        """Create the store's folder if it does not exist."""
        (self.root / self.bucket).mkdir(parents=True, exist_ok=True)

    def _write(self, path: Path, writer) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            writer(tmp)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def put(self, local: str | Path, key: str) -> str:
        """Copy a local file to `key`. -> its local:// URI."""
        self._write(self._path(key, True), lambda tmp: shutil.copyfile(local, tmp))
        return Ref(self.bucket, key).uri

    def put_bytes(self, data: bytes, key: str) -> str:
        """Store bytes at `key`. -> its local:// URI."""
        self._write(self._path(key, True), lambda tmp: tmp.write_bytes(bytes(data)))
        return Ref(self.bucket, key).uri

    def get(self, uri: str, local: str | Path) -> Path:
        """Copy an object to a local path (parent directories created). -> the path."""
        src = self._path(uri)
        if not src.is_file():
            raise Boundary(f"no such object: {uri}")
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, local)
        return local

    def get_bytes(self, uri: str) -> bytes:
        """An object's bytes. Raises when it does not exist."""
        src = self._path(uri)
        if not src.is_file():
            raise Boundary(f"no such object: {uri}")
        return src.read_bytes()

    def exists(self, uri: str) -> bool:
        """Whether an object exists."""
        try:
            return self._path(uri).is_file()
        except Boundary:
            return False

    def objects(self, prefix: str) -> list[Obj]:
        """Every object under a key prefix, with size and last-modified time."""
        base = self.root / self.bucket
        if not base.exists():
            return []
        out = []
        for p in base.rglob("*"):
            if not p.is_file() or p.name.startswith(".") and p.name.endswith(".tmp"):
                continue
            key = p.relative_to(base).as_posix()
            if key.startswith(prefix):
                st = p.stat()
                out.append(Obj(Ref(self.bucket, key).uri, st.st_size, st.st_mtime))
        return sorted(out, key=lambda o: o.uri)

    def list(self, prefix: str) -> list[str]:
        """Every object URI under a key prefix, in key order."""
        return [o.uri for o in self.objects(prefix)]

    def delete(self, uris: list[str]) -> int:
        """Delete objects by URI, and the folders they leave empty. -> how many were deleted."""
        gone = 0
        base = self.root / self.bucket
        for u in uris:
            try:
                p = self._path(u)
            except Boundary:
                continue
            if p.is_file():
                p.unlink()
                gone += 1
                d = p.parent
                while d != base and base in d.parents and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
        return gone

    def url(self, uri: str, seconds: int = 3600) -> str:
        """A file:// link to the object on this machine."""
        return self._path(uri).as_uri()


class MemoryStore(Store):
    """The same interface over a dict. For tests, and for a one-shot run that should leave nothing."""

    def __init__(self, bucket: str = "memory"):
        """An empty store held in a dict.

        Called by: store() with store_backend=memory, and the test fixtures.
        """
        self.bucket = bucket
        self._data: dict[str, bytes] = {}
        self._modified: dict[str, float] = {}
        self._lock = threading.Lock()

    def ensure_bucket(self) -> None:
        """Create the store's folder if it does not exist (nothing to do in memory).

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        return None

    def put(self, local: str | Path, key: str) -> str:
        """Upload a local file to `key`. -> its local:// URI.

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        return self.put_bytes(Path(local).read_bytes(), key)

    def put_bytes(self, data: bytes, key: str) -> str:
        """Store bytes at `key`. -> its local:// URI.

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        import time

        with self._lock:
            self._data[key] = bytes(data)
            self._modified[key] = time.time()
        return Ref(self.bucket, key).uri

    def get(self, uri: str, local: str | Path) -> Path:
        """Download an object to a local path (parent directories created). -> the path.

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.get_bytes(uri))
        return local

    def get_bytes(self, uri: str) -> bytes:
        """An object's bytes. Raises when it does not exist.

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        r = Ref.parse(uri)
        with self._lock:
            if r.key not in self._data:
                raise Boundary(f"no such object: {uri}")
            return self._data[r.key]

    def exists(self, uri: str) -> bool:
        """Whether an object exists.

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        return Ref.parse(uri).key in self._data

    def list(self, prefix: str) -> list[str]:
        """Every object URI under a prefix, in key order.

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        return sorted(Ref(self.bucket, k).uri for k in self._data if k.startswith(prefix))

    def objects(self, prefix: str) -> list[Obj]:
        """Every object under a prefix, with size and last-modified time (Obj).

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        with self._lock:
            return sorted((Obj(Ref(self.bucket, k).uri, len(v), self._modified.get(k, 0.0))
                           for k, v in self._data.items() if k.startswith(prefix)),
                          key=lambda o: o.uri)

    def delete(self, uris: list[str]) -> int:
        """Delete objects by URI. -> how many were deleted.

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        gone = 0
        with self._lock:
            for u in uris:
                k = Ref.parse(u).key
                if self._data.pop(k, None) is not None:
                    self._modified.pop(k, None)
                    gone += 1
        return gone

    def url(self, uri: str, seconds: int = 3600) -> str:
        """Where a person can open the object (in memory: just its URI).

        The in-memory implementation, for tests and one-shot runs; see Store.
        """
        return uri


_store: Store | None = None


def store(s: Settings | None = None) -> Store:
    """The process-wide store: files under store_root, or (store_backend=memory) a dict.

    Called by: cli._upload_if_local, cli.cmd_clean, cli.cmd_doctor, cli.cmd_get, cli.cmd_init,
        cli.cmd_ls, templates.ingest, and most nodes; tests/demo.py.
    """
    global _store
    if _store is None:
        s = s or settings()
        _store = MemoryStore(s.store_name) if s.store_backend == "memory" else FileStore(s.store_root, s.store_name)
        _store.ensure_bucket()
    return _store


def reset(new: Store | None = None) -> None:
    """Tests only.

    Called by: the test fixtures only (tests/conftest.py).
    """
    global _store
    _store = new
