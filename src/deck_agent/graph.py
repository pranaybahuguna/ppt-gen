"""The graph.

A `StateGraph` over `DeckState`. Every node is a function `state -> updates`; every fork is a
*named* function that returns the name of the next node. That is deliberate: when a deck comes
out wrong, the question is always "which way did it go, and why", and the answer should be a
function you can read and a `trace` entry you can look at, not a chain of `if` statements
buried in a pipeline.

What LangGraph is actually buying here, beyond shape:

* **persistence** - the state after every node is written to Postgres under a `thread_id`, so
  the next request in the same conversation starts from "there is a deck, it is v3, it uses
  this kit" instead of re-deriving it;
* **interrupts** - `ask` and `approve_outline` stop the run, let the process exit, and resume
  from exactly that point when an answer arrives. Without a checkpointer that is not possible;
* **loops with a bound** - repair and the code path are real cycles in the graph, with the
  decision to go round again written down in one place;
* **streaming** - a caller can watch node by node rather than waiting for a deck.

What it is not buying: the decisions themselves. Almost all of them are rules (does this
layout hold this content? did the text fit? is there a kit for this template?), which is why
the router asks a model only when the rules are unsure, and why nothing in here is an agent
loop with tools.

        START -> intake -> route -> ask --------------+
                             |                        |
                             +-> ensure_kit -> outline -> approve_outline -> render_slides
                             |                                                   |
                             |                            +----------------------+
                             |                            v
                             |                        assemble ---+        repair
                             |                                    |          ^
                             +-> translate_plan -> translate_apply +-> look --+
                             |                                    |      |
                             +-> edit_plan -----> edit_apply -----+      +-> translate_repair
                             |                                           +-> custom_code
                             |                                           +-> deliver -> END
                             +-> decline ------------------------------------------> END
"""
from __future__ import annotations

import contextlib
from functools import partial

from langgraph.graph import END, START, StateGraph

from . import nodes
from .infra.checkpointer import checkpointer
from .settings import Settings, settings
from .state import DeckState


# --------------------------------------------------------------------------- gates

def _redirected(state: DeckState) -> str | None:
    """Any node may say "I need to ask" or "I will not do this". Honour it before anything else."""
    chosen = state.get("route")
    if chosen == "ask":
        return "ask"
    if chosen == "decline":
        return "decline"
    return None


def after_outline(state: DeckState) -> str:
    return _redirected(state) or "approve_outline"


def after_translate_plan(state: DeckState) -> str:
    return _redirected(state) or "translate_apply"


def after_edit_plan(state: DeckState) -> str:
    return _redirected(state) or "edit_apply"


def after_ensure_kit(state: DeckState) -> str:
    return _redirected(state) or "outline"


def after_look(state: DeckState) -> str:
    """Every build passes through `look` before its own decision, so a problem a vision model
    saw is decided on exactly like a problem the geometry checks found.

    A node that already said "I will not do this" is honoured first: without that check, an
    edit that applied nothing fell through to the create route's decision and was delivered as
    though it had worked.
    """
    redirected = _redirected(state)
    if redirected:
        return redirected
    route = state.get("route")
    if route == "translate":
        return nodes.after_translate(state)
    if route == "edit":
        return nodes.after_edit(state)
    return nodes.after_assemble(state)


# --------------------------------------------------------------------------- wiring

def build_graph(planner=None, log=print) -> StateGraph:
    """The graph, uncompiled. `planner` is injected so a test can drive it with no model."""
    g = StateGraph(DeckState)

    g.add_node("intake", nodes.intake)
    g.add_node("route", partial(nodes.route, planner=planner))
    g.add_node("ask", nodes.ask)
    g.add_node("decline", nodes.decline)

    g.add_node("ensure_kit", partial(nodes.ensure_kit, planner=planner, log=log))
    g.add_node("outline", partial(nodes.outline, planner=planner, log=log))
    g.add_node("approve_outline", nodes.approve_outline)
    g.add_node("render_slides", partial(nodes.render_slides, planner=planner, log=log))
    g.add_node("assemble", partial(nodes.assemble, log=log))
    g.add_node("repair", partial(nodes.repair, planner=planner, log=log))

    g.add_node("translate_plan", partial(nodes.translate_plan, planner=planner, log=log))
    g.add_node("translate_apply", partial(nodes.translate_apply, log=log))
    g.add_node("translate_repair", partial(nodes.translate_repair, planner=planner, log=log))

    g.add_node("edit_plan", partial(nodes.edit_plan, planner=planner, log=log))
    g.add_node("edit_apply", partial(nodes.edit_apply, planner=planner, log=log))

    g.add_node("look", partial(nodes.look, planner=planner, log=log))
    g.add_node("custom_code", partial(nodes.custom_code, planner=planner, log=log))
    g.add_node("deliver", partial(nodes.deliver, log=log))

    g.add_edge(START, "intake")
    g.add_edge("intake", "route")

    g.add_conditional_edges("route", nodes.after_route,
                            ["ask", "ensure_kit", "translate_plan", "edit_plan",
                             "custom_code", "decline"])
    # An answer goes back through the router: the question may have changed the route entirely.
    g.add_edge("ask", "route")

    g.add_conditional_edges("ensure_kit", after_ensure_kit, ["outline", "ask", "decline"])
    g.add_conditional_edges("outline", after_outline, ["approve_outline", "ask", "decline"])
    g.add_conditional_edges("approve_outline", nodes.after_approval, ["render_slides", "decline"])
    g.add_conditional_edges("render_slides", nodes.after_render,
                            ["assemble", "custom_code", "decline"])
    # Every build goes through `look` before its decision. When `vision_review` is off the node
    # returns immediately, so this costs nothing and the graph has one shape either way.
    g.add_edge("assemble", "look")
    g.add_conditional_edges("look", after_look,
                            ["deliver", "repair", "custom_code", "translate_repair",
                             "ask", "decline"])
    g.add_edge("repair", "assemble")

    g.add_conditional_edges("translate_plan", after_translate_plan,
                            ["translate_apply", "ask", "decline"])
    g.add_edge("translate_apply", "look")
    g.add_edge("translate_repair", "translate_apply")

    g.add_conditional_edges("edit_plan", after_edit_plan, ["edit_apply", "ask", "decline"])
    g.add_edge("edit_apply", "look")

    g.add_conditional_edges("custom_code", nodes.after_code, ["deliver"])
    g.add_edge("deliver", END)
    g.add_edge("decline", END)
    return g


@contextlib.contextmanager
def deck_graph(planner=None, log=print, s: Settings | None = None):
    """A compiled graph with its checkpointer, as a context manager.

    Use it around a whole request, not around a process: the Postgres pool is opened and closed
    with it.

        with deck_graph(planner) as graph:
            graph.invoke(state, config={"configurable": {"thread_id": "..."}})
    """
    s = s or settings()
    if planner is None:
        from .llm.planner import planner as make_planner

        planner = make_planner(s)
    with checkpointer(s) as saver:
        yield build_graph(planner, log).compile(checkpointer=saver)


def mermaid() -> str:
    """The graph as a diagram, for the documentation and for arguing about."""
    return build_graph().compile().get_graph().draw_mermaid()
