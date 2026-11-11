"""The graph's nodes. One module per route, plus the shared steps.

Every node is a plain function `state -> dict of updates`. They are importable and callable on
their own, which is how they are tested: no graph, no checkpointer, no containers unless the
step itself needs one.
"""
from __future__ import annotations

from .common import warnings_of  # noqa: F401
from .create import (  # noqa: F401
    after_approval,
    after_assemble,
    after_render,
    approve_outline,
    assemble,
    outline,
    render_slides,
    repair,
)
from .custom_code import after_code, custom_code  # noqa: F401
from .deliver import deliver  # noqa: F401
from .edit import after_edit, edit_apply, edit_plan  # noqa: F401
from .intake import intake  # noqa: F401
from .kit import after_kit, ensure_kit  # noqa: F401
from .look import look, review_previews  # noqa: F401
from .router import after_route, ask, decline, route  # noqa: F401
from .translate import after_translate, translate_apply, translate_plan, translate_repair  # noqa: F401
