"""The graph.

A `StateGraph` over `DeckState`. Every node is a function `state -> updates`; every fork is a
*named* function that returns the name of the next node - or, where work splits up, a list of
`Send`s that run one node many times at once.

The shape is divide and conquer. A new deck is planned as a whole (its storyline), then every
slide is written by its own `write`, all in parallel. A change to a deck - any deck, including
one edited by hand - is split into one task per slide, done by its own `work`, in parallel. Both
end in `review`: a vision model looks at every new or changed slide next to what was asked, and
the weak ones go round once more through `work` and `join`.

        START -> intake -> route -+-> gather -> storyline -> approve_outline -> write x N -> build -+
                                  |                                                                 v
                                  +-> split -> work x N -> join ------------------------------> review
                                  |               ^                                                 |
                                  |               +------------- weak slides, once ----------------+
                                  |                                                                 |
                                  +-> translate_plan -> translate_apply -> look -> translate_repair |
                                  |                                         |                       |
                                  +-> ask -> route                          +--------> deliver <---+
                                  +-> decline -> END                                     |
                                                                                         v
                                                                                        END

What LangGraph is buying here:

* **parallel work** - `Send` runs a writer per slide and a worker per task at the same time, and
  the node after them (`build`, `join`) waits for all of them;
* **persistence** - the state after every node is written to Postgres under a `thread_id`, so
  the next request in the same conversation starts from "there is a deck, it is v3";
* **interrupts** - `ask` and `approve_outline` stop the run, let the process exit, and resume
  from exactly that point when an answer arrives;
* **a bounded loop** - review's second pass is a real cycle, with the decision in one place.
"""
from __future__ import annotations

import contextlib
from functools import partial

from langgraph.graph import END, START, StateGraph

from . import nodes
from .design import nodes as design
from .infra.checkpointer import checkpointer
from .revise import nodes as revise
from .settings import Settings, settings
from .state import DeckState


# --------------------------------------------------------------------------- gates

def _redirected(state: DeckState) -> str | None:
    """Any node may say "I need to ask" or "I will not do this". Honour it before anything else.

    Called by: after_look, after_translate_plan.
    """
    chosen = state.get("route")
    if chosen == "ask":
        return "ask"
    if chosen == "decline":
        return "decline"
    return None


def after_translate_plan(state: DeckState) -> str:
    """Decision after translate_plan: ask or decline if translate_plan said so, otherwise
    translate_apply.

    Called by: LangGraph, as the conditional edge out of translate_plan (build_graph).
    """
    return _redirected(state) or "translate_apply"


def after_look(state: DeckState) -> str:
    """Decision after a translation was checked: repair text that overflows, or deliver.

    Called by: LangGraph, as the conditional edge out of look (build_graph).
    """
    return _redirected(state) or nodes.after_translate(state)


# --------------------------------------------------------------------------- wiring

def build_graph(planner=None, log=print) -> StateGraph:
    """The graph, uncompiled. `planner` is injected so a test can drive it with no model.

    Called by: deck_graph, mermaid.
    """
    g = StateGraph(DeckState)

    g.add_node("intake", nodes.intake)
    g.add_node("route", partial(nodes.route, planner=planner))
    g.add_node("ask", nodes.ask)
    g.add_node("decline", nodes.decline)

    # create: facts -> storyline -> (approval) -> one writer per slide, in parallel -> build
    g.add_node("gather", partial(nodes.gather, planner=planner, log=log))
    g.add_node("storyline", partial(design.storyline, planner=planner, log=log))
    g.add_node("approve_outline", design.approve_outline)
    g.add_node("write", partial(design.write, planner=planner, log=log))
    g.add_node("build", partial(design.build, log=log))

    # edit: one task per slide, in parallel -> join. Shared with create from here on: `review`
    # looks at every new or changed slide and sends weak ones back through `work` and `join`.
    g.add_node("split", partial(revise.split, planner=planner, log=log))
    g.add_node("work", partial(revise.work, planner=planner, log=log))
    g.add_node("join", partial(revise.join, log=log))
    g.add_node("review", partial(revise.review, planner=planner, log=log))
    # then the whole deck once more, across slides: code fixes, one model check of the numbers;
    # what it finds goes back through `work` like review's notes do.
    g.add_node("audit", partial(revise.audit, planner=planner, log=log))

    # translate: unchanged - the text is replaced in place, every shape stays
    g.add_node("translate_plan", partial(nodes.translate_plan, planner=planner, log=log))
    g.add_node("translate_apply", partial(nodes.translate_apply, log=log))
    g.add_node("translate_repair", partial(nodes.translate_repair, planner=planner, log=log))
    g.add_node("look", partial(nodes.look, planner=planner, log=log))

    g.add_node("deliver", partial(nodes.deliver, log=log))

    g.add_edge(START, "intake")
    g.add_edge("intake", "route")
    g.add_conditional_edges("route", nodes.after_route,
                            ["ask", "gather", "translate_plan", "split", "decline"])
    # An answer goes back through the router: the question may have changed the route entirely.
    g.add_edge("ask", "route")

    g.add_conditional_edges("gather", nodes.after_gather, ["storyline", "ask", "decline"])
    g.add_conditional_edges("storyline", design.after_storyline, ["approve_outline", "ask", "decline"])
    # approval fans out with Send: one `write` per content slide; build waits for all of them.
    g.add_conditional_edges("approve_outline", design.after_approval, ["write", "build", "decline"])
    g.add_edge("write", "build")
    g.add_edge("build", "review")

    # split fans out the same way: one `work` per task; join waits for all of them.
    g.add_conditional_edges("split", revise.after_split, ["work", "join", "ask", "decline"])
    g.add_edge("work", "join")
    g.add_edge("join", "review")
    # review may send weak slides back to work (Send again), or pass the deck to the check.
    g.add_conditional_edges("review", revise.after_review, ["work", "audit"])
    g.add_conditional_edges("audit", revise.after_audit, ["work", "deliver"])

    g.add_conditional_edges("translate_plan", after_translate_plan,
                            ["translate_apply", "ask", "decline"])
    g.add_edge("translate_apply", "look")
    g.add_conditional_edges("look", after_look, ["deliver", "translate_repair", "ask", "decline"])
    g.add_edge("translate_repair", "translate_apply")

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

    Called by: facade._invoke, facade.answer, facade.session, facade.undo.
    """
    s = s or settings()
    if planner is None:
        from .llm.planner import planner as make_planner

        planner = make_planner(s)
    with checkpointer(s) as saver:
        yield build_graph(planner, log).compile(checkpointer=saver)


def mermaid() -> str:
    """The graph as a diagram, for the documentation and for arguing about.

    Called by: cli.cmd_graph.
    """
    return build_graph().compile().get_graph().draw_mermaid()
