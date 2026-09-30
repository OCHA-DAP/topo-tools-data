"""Write catalog outputs only when their rows change, so `portolan add` leaves unchanged files alone."""

import json
import shutil
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from pathlib import Path


def same_rows(new: Path, old: Path) -> bool:
    """Check that `old` has every column of `new` with the same rows, ignoring extras like portolan's `bbox`."""
    con = duckdb.connect()
    con.execute("LOAD spatial")
    columns = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{new}')").fetchall()
    old_columns = {
        name: kind
        for name, kind, *_ in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{old}')"
        ).fetchall()
    }
    if any(old_columns.get(name) != kind for name, kind, *_ in columns):
        return False
    select = ", ".join(
        f"ST_AsWKB({name}) AS {name}" if kind.startswith("GEOMETRY") else name
        for name, kind, *_ in columns
    )
    a, b = (f"SELECT {select} FROM read_parquet('{p}')" for p in (new, old))
    differ = con.execute(
        f"SELECT count(*) FROM (({a} EXCEPT ALL {b}) UNION ALL ({b} EXCEPT ALL {a}))"
    ).fetchone()[0]
    return differ == 0


def write_parquet(con: duckdb.DuckDBPyConnection, query: str, out: Path) -> bool:
    """Write `query` to `out` unless `out` already holds the same rows. Returns True when written."""
    out.parent.mkdir(parents=True, exist_ok=True)
    new = out.with_name(f"{out.stem}.new.parquet")
    con.execute(f"COPY ({query}) TO '{new}' (FORMAT parquet, COMPRESSION zstd)")
    if out.exists() and same_rows(new, out):
        new.unlink()
        return False
    new.replace(out)
    return True


def copy_parquet(src: Path, out: Path) -> bool:
    """Copy `src` to `out` unless `out` already holds the same rows. Returns True when copied."""
    if out.exists() and same_rows(src, out):
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out)
    return True


def read_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
