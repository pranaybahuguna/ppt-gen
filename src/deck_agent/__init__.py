"""deck_agent - a LangGraph agent layer over the deckops engine.

The engine (engine/deckops) knows how to edit PowerPoint safely. This package decides
*what* to do: it routes a request, drives the stages as graph nodes, keeps the deck's
state across turns in Postgres, keeps the files in object storage, and runs every
byte-touching step - and any code a model writes - inside a throwaway container.
"""
__version__ = "0.1.0"
