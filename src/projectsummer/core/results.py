"""The base class for everything a plugin returns.

A plugin's result is a contract: over MCP its fields become the tool's output
schema, and an agent -- or code an agent writes -- relies on them being there
and meaning what they say. So results are Pydantic models rather than loose
dicts, and a result is validated when it is built, not when something
downstream trips over a missing key.

    class Pick(Result):
        '''A film chosen from your watchlist.'''

        title: str
        '''The film's title.'''

The docstring under each field is its description in the output schema, so
results read like ordinary documented classes rather than a wall of
``Field(description=...)`` calls.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Result(BaseModel):
    """Base for plugin results."""

    model_config = ConfigDict(
        # Field docstrings become field descriptions in the JSON schema.
        use_attribute_docstrings=True,
        # A key the model does not declare is a bug in the plugin, not data
        # to pass along: the schema would not mention it, so no caller could
        # know to expect it.
        extra="forbid",
    )
