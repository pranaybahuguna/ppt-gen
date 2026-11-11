"""A static gate on code a model wrote, before it is ever shipped to the sandbox.

Read this first: **this is not the security boundary.** Anyone who wants to get past an AST
filter can, and the literature on escaping restricted-Python is long and conclusive. The
boundary is the container - no network, read-only root filesystem, no capabilities, hard
memory and CPU caps, destroyed after one call.

What this gate is actually for:

* it fails obviously-wrong code in milliseconds instead of spending a container on it;
* it gives the model a precise, feedable reason ("you imported `requests`; this code has no
  network and may only use: ...") which turns a dead end into a retry;
* it keeps the *intent* of the generated code honest and reviewable: the allowlist is the
  written-down answer to "what is this code allowed to be about?".

So: treat a rejection here as a lint error, and treat the container as the thing that protects
you. Both are cheap; neither is sufficient alone.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

#: What slide-drawing code legitimately needs. Nothing here can touch a file, a process or a
#: socket, which is why the list is short and why adding to it deserves a second thought.
ALLOWED_IMPORTS = {
    "pptx", "math", "copy", "json", "re", "textwrap", "itertools", "functools",
    "dataclasses", "typing", "collections", "colorsys", "statistics", "decimal", "fractions",
    "datetime", "enum", "random", "string", "unicodedata",
}

#: Names that have no business in slide-drawing code. A model that reaches for one of these has
#: misunderstood the job, and the message says so.
DENIED_NAMES = {
    "eval": "run a string as code", "exec": "run a string as code",
    "compile": "compile code at runtime", "__import__": "import dynamically",
    "open": "read or write files", "input": "read from stdin",
    "globals": "reach into the module namespace", "vars": "reach into an object's namespace",
    "getattr": "reach attributes by name", "setattr": "set attributes by name",
    "delattr": "delete attributes by name", "breakpoint": "start a debugger",
    "memoryview": "alias raw memory", "exit": "kill the process", "quit": "kill the process",
}

MAX_CHARS = 20_000
MAX_NODES = 4_000


@dataclass
class Verdict:
    ok: bool
    problems: list[str]

    def feedback(self) -> list[str]:
        """The problems, phrased for the model that wrote the code."""
        return self.problems


def inspect(code: str) -> Verdict:
    problems: list[str] = []
    if len(code) > MAX_CHARS:
        return Verdict(False, [f"the code is {len(code)} characters; keep it under {MAX_CHARS}"])
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return Verdict(False, [f"line {e.lineno}: {e.msg}"])

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        problems.append(f"the code has {len(nodes)} syntax nodes; keep it simpler")

    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    problems.append(
                        f"line {node.lineno}: `import {a.name}` is not available. This code runs with "
                        f"no network and no filesystem; you may import only: {', '.join(sorted(ALLOWED_IMPORTS))}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level:
                problems.append(f"line {node.lineno}: relative imports do not work here")
            elif root not in ALLOWED_IMPORTS:
                problems.append(
                    f"line {node.lineno}: `from {node.module} import ...` is not available; "
                    f"you may import only: {', '.join(sorted(ALLOWED_IMPORTS))}")
        elif isinstance(node, ast.Name) and node.id in DENIED_NAMES and isinstance(node.ctx, ast.Load):
            problems.append(f"line {node.lineno}: `{node.id}` is not available "
                            f"(it would {DENIED_NAMES[node.id]}); write the slide directly instead")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            problems.append(f"line {node.lineno}: `.{node.attr}` is not available; "
                            "use the documented python-pptx API")

    fns = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if "build" not in fns:
        problems.append("the code must define a top-level function `build(prs, ctx)`")
    for n in tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "build":
            problems.append("`build` must be a plain function, not `async def`")

    # Statements at import time are a smell: everything should happen inside build().
    stray = [n for n in tree.body
             if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom,
                                   ast.Assign, ast.AnnAssign, ast.Expr))]
    if stray:
        problems.append(f"line {stray[0].lineno}: put the work inside `build(prs, ctx)`, "
                        "not at the top level of the module")

    # De-duplicate while keeping order, and keep the message short enough to feed back.
    seen, unique = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return Verdict(not unique, unique[:8])
