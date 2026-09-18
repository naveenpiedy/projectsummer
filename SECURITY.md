# Security

Project Summer runs on your own machine, against your own Letterboxd library.
There is no server and no account: the things worth protecting are your TMDB
token, the database file, and the rest of your filesystem.

## Reporting a vulnerability

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/naveenpiedy/projectsummer/security/advisories/new),
not as a public issue. This is a spare-time project, so expect a first reply
within a week rather than within a day.

Please include what an attacker would have to control — an export file, a
Letterboxd page, a TMDB response, a question put to an assistant — and what
they get out of it. A proof of concept against a throwaway library is worth
more than a description.

You will get credit in the advisory unless you would rather not.

## What is in scope

The boundaries the code deliberately enforces, each of which is a bug if it
can be crossed:

- **An export is untrusted input.** It is a file: it can be edited, and it can
  come from someone else. Every URI in one is fetched during `resolve`, so
  only Letterboxd's own hosts are ever requested (`resolve.is_letterboxd_url`).
  Anything that turns an export into a request to another host — a cloud
  metadata endpoint, a service on your network — is in scope.
- **An assistant's SQL cannot reach your files.** Every MCP session opens with
  `external_access=False`, because DuckDB quotes unconvertible values back in
  its error messages, and a connection with file access would leak file
  contents through them. Reading tools also run on a read-only connection.
- **MCP exposure is decided by the server, not the model.** A plugin marked
  `access="write"` is not offered unless it opts in, `serves=True` plugins are
  never offered, and `call_tool` refuses write tools. A way to invoke a tool
  the server did not offer is in scope.
- **Plugins write only where they are allowed to.** A file written outside the
  output folder is in scope.
- **Secrets stay out of the repository and out of the database.** `TMDB_API_KEY`
  lives in `.env`. The email address in `profile.csv` is deliberately never
  stored. Anything that writes either somewhere durable is in scope.
- **Terminal output is data, not markup.** Values from your library and from
  TMDB are escaped before they reach Rich, so a film title cannot forge
  console output.

## What is not in scope

- **Third-party plugins.** `LETTERBOXD_PLUGIN_PATH` loads and runs Python you
  point it at, with your privileges. That is the feature. Only load plugins
  you would run by hand.
- **`summer query`, and SQL you write yourself.** It is a SQL prompt onto your
  own database, by design. Over MCP it is restricted; from the CLI it is not.
- **Your own database file.** It is protected by your filesystem permissions
  and nothing else. Anyone who can read it can read your viewing history.
- **Letterboxd and TMDB.** Report anything about their sites to them. Issues
  in how this project *uses* them are in scope here.
- **Denial of service against yourself** — a huge export, a pathological
  query — and anything that needs an attacker to already be running code as
  you.

## Keeping your own copy safe

- Keep `TMDB_API_KEY` in `.env`, which is gitignored. Never paste it into an
  issue; revoke it at TMDB if you do.
- Set `LETTERBOXD_MCP_READ_ONLY=true` if you want an assistant that can
  answer questions but change nothing.
- Treat an export from anyone but Letterboxd the way you would treat any other
  file from a stranger.
