# Using it from an AI assistant


`summer-mcp` serves your library to any [MCP](https://modelcontextprotocol.io/)
client — Claude Desktop, Claude Code, and others — so you can ask about your
films in plain language. Install the optional dependencies first:

```bash
uv sync --extra mcp
```

For Claude Code:

```bash
claude mcp add projectsummer -- uv run --directory /path/to/projectsummer --extra mcp summer-mcp
```

For Claude Desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "projectsummer": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/projectsummer", "--extra", "mcp", "summer-mcp"]
    }
  }
}
```

`--directory` also makes the server find your `.env`. Add `--db PATH` to
serve a library other than the default, or `--read-only` to offer no tool
that changes anything.

The assistant gets these tools:

| Tool | Does |
|---|---|
| `describe_schema` | Tables, then a table's columns, then what a column holds |
| `query` | One read-only `SELECT` |
| `overview`, `lists`, `random_watchlist_pick`, `trends`, `taste`, `list_overlap` | The same as the commands |
| `sync` | Fetches your recent diary entries from Letterboxd's feed |
| `set_list_ranked` | Marks a list as ranked |
| `list_builder` | Writes an importable list into the output folder |

Importing, resolving and enriching stay with the CLI: they run for minutes,
and are yours to start.

Not every tool is listed up front. Each listed tool's description is sent
with every message, and the analyses will outnumber what a question usually
needs. So the assistant sees `describe_schema`, `query`, `overview` and every
tool that changes something, plus `search_tools` to find the rest by
describing what it wants and `call_tool` to run what it finds. Tools that
change something are never run through `call_tool`, so your client always
shows them by name before asking you to approve one.

Questions about people — "which women directors do I rate highest?" — work
through the same two tools: `describe_schema` points the assistant to the
`people` and `film_credits` tables.

## Prompts

A tool is a verb; a prompt is a whole question worth asking. The server offers
five, which your client shows by name — in Claude Desktop they are under the
attachment menu, and in Claude Code they are slash commands:

| Prompt | Asks for |
|---|---|
| `year_in_review` | A look back over one year: the shape of it, the films, the people, what changed |
| `what_should_i_watch` | Three candidates from your watchlist for a mood, and an evening's length |
| `taste_profile` | What your ratings say about you, including what would surprise you |
| `person_deep_dive` | Everything your library holds about one director, actor or writer |
| `catch_up` | Runs `sync`, then says what changed |

They are text, not code: each one tells the assistant which tools to reach
for, to check spellings before filtering, and to ask the database for answers
rather than fetching rows to work them out. A prompt cannot read your library
itself. `catch_up` is left out of a read-only server, since it asks for a tool
that server does not offer.

What an assistant can do is enforced by the server, not left to the model:

- **Only these tools exist.** Anything else is never registered, so no prompt
  can reach it.
- **Reading tools cannot write.** They run on a database connection DuckDB
  itself keeps read-only, whatever SQL they are given.
- **No tool can reach your files through the database.** Every connection the
  server opens, writing ones included, has DuckDB's file access switched off.
  That matters beyond `query`: `list_builder` takes SQL too, and a query that
  reads a file could otherwise leak it, `.env` and all, through an error
  message.
- **Files go only into the output folder** (`output` in the per-user data
  directory). An absolute path, or one that climbs out, is refused.
- **Your own commands are never locked out.** The database is opened for each
  call and closed straight after, so `summer sync` works while your assistant
  is open.
- **Unexpected errors stay private.** A tool's own errors explain themselves;
  anything else is reported without tracebacks or file paths.

