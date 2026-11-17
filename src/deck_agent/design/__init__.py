"""Designed slides: a template's design profile, measured text, and slide pieces.

The model never places a shape. It writes a *slide spec* - what the slide says and which pieces
show it, on a 12-column grid - and this package draws it:

    profile.py  what a template looks like: layouts, title box, safe area, brand colours, fonts
    measure.py  how much room a piece of text needs, with the real fonts
    pieces.py   the slide pieces (KPI cards, native charts, steps, callouts, ...)
    render.py   specs + template -> a deck
"""
