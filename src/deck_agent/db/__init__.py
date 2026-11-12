"""The project's own tables - decks and jobs - through SQLAlchemy (async) on Postgres (asyncpg).

LangGraph's checkpoint tables live in the same database and are not touched here: they hold
each conversation's state; these hold the records people and programs query.
"""
