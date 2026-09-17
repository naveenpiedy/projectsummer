# Draft bug report for DuckDB: WAL replay fails after `COMMENT ON COLUMN`

Status: **not yet filed.** To post at <https://github.com/duckdb/duckdb/issues>
(search for an existing report first). Project Summer works around it; see
"Workaround" below and `db.init_schema`.

---

## Title

WAL replay fails with "GetDefaultDatabase with no default database set" after
`COMMENT ON COLUMN` on a table with a function default

## What happens

If a process runs `COMMENT ON COLUMN` on a table that has a column with a
function default (`DEFAULT current_timestamp`, `DEFAULT nextval('seq')`), and
then exits without closing the connection -- so the change is still only in
the write-ahead log -- the database can no longer be opened. Every
`duckdb.connect()` fails while replaying the WAL, read-only or not, with any
configuration:

```
duckdb.InternalException: INTERNAL Error: Failure while replaying WAL file
"repro.duckdb.wal": Calling DatabaseManager::GetDefaultDatabase with no default
database set
This error signals an assertion failure within DuckDB.
```

The database stays unopenable until the `.wal` file is moved aside, which
discards whatever else it held.

## Reproduction

```python
import os
import subprocess
import sys
import tempfile

import duckdb

path = os.path.join(tempfile.mkdtemp(), "repro.duckdb")

con = duckdb.connect(path)
con.execute("CREATE TABLE t (x INTEGER, created TIMESTAMP DEFAULT current_timestamp)")
con.close()

# A process that comments on a column and is killed before closing.
subprocess.run([sys.executable, "-c", (
    "import duckdb, os\n"
    f"con = duckdb.connect({path!r})\n"
    "con.execute(\"COMMENT ON COLUMN t.x IS 'a comment'\")\n"
    "os._exit(0)\n"
)], check=True)

duckdb.connect(path)  # InternalException: Failure while replaying WAL file ...
```

## Narrowed down

Each row is the same reproduction with one thing changed.

| Table and statement | WAL left | Reopens |
|---|---|---|
| `DEFAULT current_timestamp` column, `COMMENT ON COLUMN` | 116 bytes | **fails** |
| `DEFAULT nextval('s')` column, `COMMENT ON COLUMN` | 116 bytes | **fails** |
| `DEFAULT current_timestamp` column, `INSERT` instead | none | yes |
| `DEFAULT current_timestamp` column, `COMMENT ON TABLE` instead | none | yes |
| `DEFAULT 1` (literal) column, `COMMENT ON COLUMN` | none | yes |
| `DEFAULT FALSE` column, `COMMENT ON COLUMN` | none | yes |
| `NOT NULL` column, no default, `COMMENT ON COLUMN` | none | yes |
| Plain table, primary key, or index, `COMMENT ON COLUMN` | none | yes |
| `COMMENT ON COLUMN` on a view's column | none | yes |

It looks as though replaying the column comment re-binds the table's column
defaults, and binding a function default needs a default database that is
not set yet during WAL replay.

Closing the connection normally, or running `CHECKPOINT` after the comment,
avoids it: the change reaches the main file and nothing is left to replay.

## Environment

- DuckDB 1.5.5 (Python package; also the latest stable release on PyPI at the
  time of writing)
- Python 3.14.2
- Windows 11

## How we hit it

Our application re-applied an idempotent schema script -- `CREATE ... IF NOT
EXISTS` plus about a hundred `COMMENT ON COLUMN` statements -- on every
read-write open. A server process was killed by its host application while a
connection was open, and the user's database would not open afterwards.

## Workaround

Only re-apply schema DDL when the script has changed, and run `CHECKPOINT`
immediately after it, so comments never sit in the WAL.
