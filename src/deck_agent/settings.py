"""Configuration, in one place.

Two kinds of setting, deliberately kept apart (deckops hard rule 6):

* **Model settings** - base URL, model name, API key - come *only* from a git-ignored JSON
  file (`llm.json`), never from the environment and never from code. Nothing about a model
  endpoint is ever committed.
* **Infrastructure settings** - Postgres DSN, S3 endpoint, image name, limits - come from
  `config/agent.json` if it exists, and may be overridden by environment variables
  (`DECKOPS_<FIELD>`) so that `docker compose` can wire the containers together.

`redacted()` is what you print.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG_FILE = REPO / "config" / "agent.json"


def _env(name: str, default):
    """A setting overridden by an environment variable, converted to the default's type (bool,
    int, float or str). Returns the default when the variable is not set.

    Called by: Settings.load, once per field.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


@dataclass
class Settings:
    # ---- object storage: every artefact of record lives here, not on a local disk ----
    """Every setting, with its default. Loaded once by settings(): defaults, then
    config/agent.json, then DECKOPS_<FIELD> environment variables. Model credentials are not
    here - they come only from llm.json (Settings.llm_config).
    """
    s3_endpoint: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "deckops"
    s3_access_key: str = "deckops"
    s3_secret_key: str = "deckops-dev-secret"
    store_backend: str = "s3"              # s3 | memory (tests, and one-shot runs with no containers)

    # ---- checkpointer: the conversation's state, one thread per deck session ----
    pg_dsn: str = "postgresql://deckops:deckops-dev-secret@localhost:55432/deckops"
    checkpointer: str = "postgres"              # postgres | memory (tests, one-shot CLI runs)

    # ---- the sandbox image: throwaway containers for model code and for rendering ----
    sandbox_image: str = "deckops/sandbox:0.1.0"
    docker_bin: str = "docker"
    render_backend: str = "docker"              # docker | local (soffice on PATH) | off
    code_runner: str = "docker"                 # docker | off  (never "local": it is untrusted code)
    sandbox_memory: str = "2g"
    sandbox_cpus: str = "2"
    sandbox_pids: int = 256
    render_timeout_s: int = 300
    code_timeout_s: int = 60                    # model-written code gets much less than a render
    code_memory: str = "512m"
    font_dir: str = "local/fonts"               # metric-compatible fonts, extracted from the image

    # ---- model: a file path only; the file itself is git-ignored ----
    llm_file: str = "llm.json"
    llm_client: str = "openai-compat"           # openai-compat | langchain | stub

    # ---- behaviour ----
    max_repair_rounds: int = 2
    render_retries: int = 2
    render_workers: int = 4
    code_attempts: int = 2                      # tries the model gets to write working code
    router_confidence_floor: float = 0.6        # below this the graph asks the user
    approve_outline: bool = False               # pause for a human before building
    allow_custom_code: bool = False             # the escape hatch, off until asked for
    vision_review: bool = False                 # look at each rendered slide with a vision
                                                # model and feed what it sees back as findings
    vision_workers: int = 4
    local_root: str = "local"                   # scratch only; nothing of record lives here
    default_template: str = ""                  # object URI used when a create request names no template
    layout_reuse: int = 0                       # each template layout at most this many times
                                                # per deck, never twice in a row; slides over it
                                                # are designed by model-written code instead.
                                                # 0 = no limit. Needs allow_custom_code.
    server_host: str = "127.0.0.1"              # `deckops serve`: where the MCP + REST server listens
    server_port: int = 8765
    server_url: str = ""                        # how callers reach it, for download links; empty =
                                                # http://<server_host>:<server_port>
    server_workers: int = 2                     # long operations run at once (one per deck)
    single_kit: bool = False                    # kits/ holds one kit only: ingesting a template
                                                # (`deckops kit`) replaces the one there, and a
                                                # deck made from any other template keeps its kit
                                                # beside the deck instead. See nodes/kit.py
    tidy_after_turn: bool = False               # after each delivered turn, drop the deck's older
                                                # run artefacts (previews, plans, ops), keeping
                                                # what the session still needs. See infra/cleanup.py

    def redacted(self) -> dict:
        """These settings with secrets masked (the S3 secret, the Postgres password) - safe to
        print.

        Called by: cmd_doctor, deliver (into the manifest), checkpointer error messages.
        """
        out = asdict(self)
        out["s3_secret_key"] = "***"
        if "@" in self.pg_dsn:
            out["pg_dsn"] = "postgresql://***@" + self.pg_dsn.split("@", 1)[1]
        return out

    def _under(self, *parts) -> Path:
        """A directory under local_root, created if missing.

        Called by: workspace_root.
        """
        p = REPO.joinpath(self.local_root, *parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def workspace_root(self) -> Path:
        """Where per-run scratch directories go (local/workspaces). Never of record; safe to
        delete.

        Called by: infra.workspace, the sandbox clients, cmd_kit, cmd_selftest.
        """
        return self._under("workspaces")

    @property
    def font_path(self) -> Path:
        """The metric-compatible fonts copied out of the sandbox image (local/fonts).

        Called by: engine.use_local_fonts, so the overflow check measures with the right fonts.
        """
        d = Path(self.font_dir)
        return d if d.is_absolute() else REPO / d

    def llm_config(self) -> dict:
        """The model endpoint, from the git-ignored JSON file."""
        path = Path(self.llm_file)
        if not path.is_absolute():
            path = REPO / path
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found. Write it as "
                '{"base_url": "https://...", "model": "...", "api_key": "..."} '
                "- it is git-ignored on purpose. Or set llm_client=stub for the offline planner."
            )
        cfg = json.loads(path.read_text())
        cfg.pop("$comment", None)
        return cfg

    @classmethod
    def load(cls) -> "Settings":
        """Defaults <- config/agent.json <- environment (DECKOPS_<FIELD>)."""
        data: dict = {}
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            data.pop("$comment", None)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"{CONFIG_FILE}: unknown settings {sorted(unknown)}")
        s = cls(**data)
        for name in known:
            setattr(s, name, _env("DECKOPS_" + name.upper(), getattr(s, name)))
        return s


_cached: Settings | None = None


def settings() -> Settings:
    """The process-wide settings, loaded once (Settings.load) and cached.

    Called by: almost everything that needs a setting. Tests swap it with reset().
    """
    global _cached
    if _cached is None:
        _cached = Settings.load()
    return _cached


def reset(new: Settings | None = None) -> None:
    """Tests only: swap the process-wide settings.

    Called by: tests/demo.py, tests/validate_real_model.py.
    """
    global _cached
    _cached = new
