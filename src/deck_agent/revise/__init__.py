"""Changing an existing deck - any deck, including one edited by hand - by divide and conquer.

A request is split into one task per slide (split), the tasks run in parallel (work), their
results are applied to the deck in one pass (join), and the changed slides are looked at and
revised once if something is wrong (review). See graph.build_graph for the wiring.

    read.py     what is on a slide: its content (for a redesign) and its shapes (for a tweak)
    surgery.py  replace, insert, delete and move slides, leaving every other slide byte for byte
    prompts.py  the four prompts: split, redesign, tweak, review
    work.py     one task: a redesign writes a slide spec, a tweak writes python-pptx code
    nodes.py    the graph nodes and the decisions between them
"""
