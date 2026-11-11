"""Object storage: where decks and artefacts actually live.

Why not just a folder? Because the graph is checkpointed. A conversation can be resumed in
another process, on another machine, a week later - so the state may only hold *references*
that any process can resolve. A local path in the state is a lie waiting to happen. Every
artefact of record therefore has an `s3://bucket/key` URI, and local directories are scratch
that can be deleted at any moment without losing anything.

What goes in here:

    <thread>/v001.pptx            every version of the deck (never overwritten: that is undo)
    <thread>/kits/<hash>.json     a style kit, keyed by the template's content hash
    <thread>/<run>/outline.json   intermediate artefacts, so a run can be replayed or debugged
    <thread>/<run>/plan.json
    <thread>/<run>/ops.json
    <thread>/<run>/previews/slide-01.png

In development this is MinIO in a container; in production it is whatever S3 the host app
already has, with the same two settings changed. `MemoryStore` is the same interface with a
dict behind it, for tests that should not need a container.
"""
from __future__ import annotations

import hashlib
import io
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..errors import Boundary
from ..settings import Settings, settings

CONTENT_TYPES = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".json": "application/json", ".png": "image/png", ".pdf": "application/pdf",
    ".py": "text/x-python", ".txt": "text/plain",
}


def content_type(name: str) -> str:
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def sha256_file(path: str | Path, length: int = 16) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:length]


@dataclass(frozen=True)
class Ref:
    """A parsed `s3://bucket/key`."""

    bucket: str
    key: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    @classmethod
    def parse(cls, uri: str) -> "Ref":
        u = urlparse(uri)
        if u.scheme != "s3" or not u.netloc or not u.path.lstrip("/"):
            raise Boundary(f"not an object reference: {uri!r} (expected s3://bucket/key)")
        return cls(u.netloc, u.path.lstrip("/"))


class Store:
    """The interface the graph uses. Two implementations: S3Store and MemoryStore."""

    bucket: str

    def ensure_bucket(self) -> None: ...
    def put(self, local: str | Path, key: str) -> str: ...
    def put_bytes(self, data: bytes, key: str) -> str: ...
    def get(self, uri: str, local: str | Path) -> Path: ...
    def get_bytes(self, uri: str) -> bytes: ...
    def exists(self, uri: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def url(self, uri: str, seconds: int = 3600) -> str: ...

    # -- convenience shared by both implementations ------------------------------------
    def put_json(self, obj, key: str) -> str:
        import json
        return self.put_bytes(json.dumps(obj, indent=1, ensure_ascii=False).encode(), key)

    def get_json(self, uri: str):
        import json
        return json.loads(self.get_bytes(uri))


class S3Store(Store):
    def __init__(self, s: Settings | None = None):
        import boto3
        from botocore.config import Config

        self.s = s or settings()
        self.bucket = self.s.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=self.s.s3_endpoint or None,
            region_name=self.s.s3_region,
            aws_access_key_id=self.s.s3_access_key,
            aws_secret_access_key=self.s.s3_secret_key,
            # MinIO serves path-style addressing; real S3 accepts it too.
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3},
                          connect_timeout=5, read_timeout=60),
        )

    def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError, EndpointConnectionError

        try:
            self._client.head_bucket(Bucket=self.bucket)
        except EndpointConnectionError:
            raise Boundary(f"object storage is not reachable at {self.s.s3_endpoint}. "
                           "Run: make up") from None
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchBucket", "403"):
                try:
                    self._client.create_bucket(Bucket=self.bucket)
                except ClientError as e2:
                    raise Boundary(f"could not create bucket {self.bucket}: {e2}") from None
            else:
                raise Boundary(f"object storage rejected us: {e}") from None

    def put(self, local: str | Path, key: str) -> str:
        self._client.upload_file(str(local), self.bucket, key,
                                 ExtraArgs={"ContentType": content_type(key)})
        return Ref(self.bucket, key).uri

    def put_bytes(self, data: bytes, key: str) -> str:
        self._client.upload_fileobj(io.BytesIO(data), self.bucket, key,
                                    ExtraArgs={"ContentType": content_type(key)})
        return Ref(self.bucket, key).uri

    def get(self, uri: str, local: str | Path) -> Path:
        r = Ref.parse(uri)
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(r.bucket, r.key, str(local))
        return local

    def get_bytes(self, uri: str) -> bytes:
        r = Ref.parse(uri)
        return self._client.get_object(Bucket=r.bucket, Key=r.key)["Body"].read()

    def exists(self, uri: str) -> bool:
        from botocore.exceptions import ClientError

        r = Ref.parse(uri)
        try:
            self._client.head_object(Bucket=r.bucket, Key=r.key)
            return True
        except ClientError:
            return False

    def list(self, prefix: str) -> list[str]:
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            page = self._client.list_objects_v2(**kw)
            out += [Ref(self.bucket, o["Key"]).uri for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return out
            token = page["NextContinuationToken"]

    def url(self, uri: str, seconds: int = 3600) -> str:
        """A link the caller can hand to a browser. The deck itself never leaves the bucket."""
        r = Ref.parse(uri)
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": r.bucket, "Key": r.key}, ExpiresIn=seconds)


class MemoryStore(Store):
    """The same interface over a dict. For tests, and for a one-shot CLI run with no containers."""

    def __init__(self, bucket: str = "memory"):
        self.bucket = bucket
        self._data: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def ensure_bucket(self) -> None:
        return None

    def put(self, local: str | Path, key: str) -> str:
        return self.put_bytes(Path(local).read_bytes(), key)

    def put_bytes(self, data: bytes, key: str) -> str:
        with self._lock:
            self._data[key] = bytes(data)
        return Ref(self.bucket, key).uri

    def get(self, uri: str, local: str | Path) -> Path:
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.get_bytes(uri))
        return local

    def get_bytes(self, uri: str) -> bytes:
        r = Ref.parse(uri)
        with self._lock:
            if r.key not in self._data:
                raise Boundary(f"no such object: {uri}")
            return self._data[r.key]

    def exists(self, uri: str) -> bool:
        return Ref.parse(uri).key in self._data

    def list(self, prefix: str) -> list[str]:
        return sorted(Ref(self.bucket, k).uri for k in self._data if k.startswith(prefix))

    def url(self, uri: str, seconds: int = 3600) -> str:
        return uri


_store: Store | None = None


def store(s: Settings | None = None) -> Store:
    """The process-wide store. `store_backend=memory` avoids needing a container."""
    global _store
    if _store is None:
        s = s or settings()
        _store = MemoryStore(s.s3_bucket) if s.store_backend == "memory" else S3Store(s)
        _store.ensure_bucket()
    return _store


def reset(new: Store | None = None) -> None:
    """Tests only."""
    global _store
    _store = new
