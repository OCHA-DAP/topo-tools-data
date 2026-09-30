"""Build the hdx/topo-tools NLD demo inputs: one folder per topo-tools tool."""

import argparse
from pathlib import Path

import duckdb
from build_nld import get
from topo_tools.api.schema_join import join
from topo_tools.api.schema_map import map as schema_map
from topo_tools.api.topo_clean import clean
from topo_tools.api.topo_detect import detect

SIMPLIFY_M = 100.0
SNAP_M = 0.01
# Utrecht and Eindhoven grow 200 m into their neighbours.
OVERLAP_M = {"GM0344": 200.0, "GM0772": 200.0}
# Amersfoort, Apeldoorn and Tilburg each lose a strip this wide along their border with a neighbour.
SLIVER_M = {
    "GM0307": ("GM0327", 0.25),
    "GM0200": ("GM0232", 0.5),
    "GM0855": ("GM0824", 1.0),
}
# Wider than every sliver, narrower than every water gap. Matches topo-clean's `gap` URL param.
TOPO_GAP_M = 2.0
METERS_PER_DEGREE = 111_320
# CBS StatLine "Gebieden in Nederland 2025"; Code_28/Naam_29 are its Provincies group.
GEBIEDEN = "https://opendata.cbs.nl/ODataApi/odata/86059NED/TypedDataSet?$format=json&$select=RegioS,Code_28,Naam_29"
PROVINCIEGEBIED = "https://api.pdok.nl/kadaster/bestuurlijkegebieden/ogc/v1/collections/provinciegebied/items?f=json&limit=100&crs=http://www.opengis.net/def/crs/EPSG/0/28992"


def coverage(con: duckdb.DuckDBPyConnection, table: str, call: str) -> None:
    """Replace `table`'s geometry with a coverage function's per-row result, keyed by `i`."""
    con.execute(f"""
        CREATE OR REPLACE TABLE {table} AS
        WITH parts AS (
            SELECT unnest(ST_Dump({call.format(geoms="list(geometry::GEOMETRY ORDER BY i)")})) AS d
            FROM {table}
        ), merged AS (
            SELECT d.path[1] AS i, ST_Union_Agg(d.geom) AS geometry FROM parts GROUP BY 1
        )
        SELECT t.i, m.geometry::GEOMETRY('EPSG:28992') AS geometry, t.* EXCLUDE (i, geometry)
        FROM {table} t JOIN merged m USING (i)
    """)


def simplify(con: duckdb.DuckDBPyConnection, table: str) -> None:
    coverage(con, table, f"ST_CoverageSimplify({{geoms}}, {SIMPLIFY_M}::DOUBLE)")
    # Snap only (gap width 0): removes the simplify's zero-area overlaps, keeps every water gap.
    coverage(con, table, f"ST_CoverageClean({{geoms}}, {SNAP_M}::DOUBLE, 0::DOUBLE)")


def cached(url: str, path: Path) -> Path:
    if not path.exists():
        path.write_text(get(url))
    return path


def gemeenten(src: Path, provinces: Path) -> duckdb.DuckDBPyConnection:
    """Simplified land gemeenten with provincie columns, as table `g`."""
    con = duckdb.connect()
    con.execute("LOAD spatial")
    con.execute(
        f"CREATE TABLE p AS SELECT trim(r.RegioS) AS gemeentecode, trim(r.Code_28) AS provinciecode, "
        f"trim(r.Naam_29) AS provincienaam FROM (SELECT unnest(value) AS r FROM read_json('{provinces}'))"
    )
    con.execute(
        f"CREATE TABLE t AS SELECT row_number() OVER (ORDER BY gemeentecode) AS i, * EXCLUDE (bbox) "
        f"FROM read_parquet('{src}') WHERE water = 'NEE'"
    )
    simplify(con, "t")
    missing = con.execute(
        "SELECT count(*) FROM t ANTI JOIN p USING (gemeentecode)"
    ).fetchone()[0]
    if missing:
        msg = f"{missing} gemeenten missing from the provincie lookup"
        raise SystemExit(msg)
    con.execute("CREATE TABLE g AS SELECT * FROM t JOIN p USING (gemeentecode)")
    return con


def copy(con: duckdb.DuckDBPyConnection, query: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({query}) TO '{out}' (FORMAT parquet, COMPRESSION zstd)")


def schema_map_input(con: duckdb.DuckDBPyConnection, out: Path) -> None:
    copy(
        con,
        "SELECT geometry, gemeentecode, gemeentenaam, provinciecode, provincienaam, "
        "'NL' AS landcode, 'Nederland' AS landnaam, water, jaar "
        "FROM g ORDER BY gemeentecode",
        out,
    )


def schema_join_input(con: duckdb.DuckDBPyConnection, out: Path) -> None:
    copy(
        con,
        "SELECT geometry, gemeentecode AS adm2_code, gemeentenaam AS adm2_name "
        "FROM g ORDER BY adm2_code",
        out,
    )


def schema_join_layer(provinciegebied: Path, out: Path) -> None:
    con = duckdb.connect()
    con.execute("LOAD spatial")
    con.execute(
        "CREATE TABLE t AS SELECT row_number() OVER (ORDER BY identificatie) AS i, geom AS geometry, identificatie, naam "
        f"FROM ST_Read('{provinciegebied}')"
    )
    simplify(con, "t")
    copy(
        con,
        "SELECT geometry, identificatie AS adm1_code, naam AS adm1_name FROM t ORDER BY adm1_code",
        out,
    )


def topo_input(con: duckdb.DuckDBPyConnection, out: Path) -> None:
    overlaps = ", ".join(f"('{code}', {m})" for code, m in OVERLAP_M.items())
    slivers = ", ".join(f"('{a}', '{b}', {m})" for a, (b, m) in SLIVER_M.items())
    copy(
        con,
        f"""
        SELECT CASE
                WHEN o.m IS NOT NULL THEN ST_Buffer(g.geometry::GEOMETRY, o.m)
                WHEN s.m IS NOT NULL THEN ST_Difference(g.geometry::GEOMETRY, ST_Buffer(n.geometry::GEOMETRY, s.m))
                ELSE g.geometry::GEOMETRY
            END::GEOMETRY('EPSG:28992') AS geometry,
            g.gemeentecode AS adm2_code, g.gemeentenaam AS adm2_name
        FROM g
        LEFT JOIN (VALUES {overlaps}) o(code, m) ON o.code = g.gemeentecode
        LEFT JOIN (VALUES {slivers}) s(code, neighbour, m) ON s.code = g.gemeentecode
        LEFT JOIN g n ON n.gemeentecode = s.neighbour
        ORDER BY adm2_code
        """,
        out,
    )


def issue_counts(path: Path, cache: Path) -> dict[str, int]:
    """Count issues by kind, leaving out zero-width noise gaps."""
    issues = cache / f"{path.parent.name}_{path.stem}_issues.parquet"
    detect(path, issues, tmp_dir=cache / "tmp")
    return dict(
        duckdb.sql(
            f"SELECT kind, count(*) FROM read_parquet('{issues}') "
            f"WHERE kind <> 'gap' OR max_width_m >= {SNAP_M} GROUP BY kind"
        ).fetchall()
    )


def check_topo(path: Path, water_gaps: int, cache: Path) -> None:
    slivers = len(SLIVER_M)
    counts = issue_counts(path, cache)
    if counts.get("gap") != water_gaps + slivers or not counts.get("overlap"):
        msg = f"{path}: {counts}, expected {water_gaps} water and {slivers} sliver gaps plus overlaps"
        raise SystemExit(msg)
    out = cache / "topo" / "nld_admin2_clean.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    clean(
        path,
        out,
        out.with_stem(out.stem + "_issues"),
        maximum_gap_width=str(TOPO_GAP_M / METERS_PER_DEGREE),
        tmp_dir=cache / "tmp",
    )
    if (cleaned := issue_counts(out, cache)) != {"gap": water_gaps}:
        msg = f"topo-clean at {TOPO_GAP_M} m left {cleaned}, expected only the {water_gaps} water gaps"
        raise SystemExit(msg)


def join_issues(input_path: Path, join_path: Path, cache: Path) -> int:
    out = cache / "schema-join" / "nld_admin2_join.parquet"
    join(input_path, join_path, out, tmp_dir=cache / "tmp")
    issues = out.with_stem(out.stem + "_issues")
    if not issues.exists():
        return 0
    return duckdb.sql(f"SELECT count(*) FROM read_parquet('{issues}')").fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog", type=Path, required=True, help="Catalog root directory"
    )
    parser.add_argument("--cache", type=Path, default=Path("tmp/catalog/cache/demo"))
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)

    src = args.catalog / "nld" / "2025" / "nld_admin2" / "nld_admin2.parquet"
    demo = args.catalog / "nld" / "demo"
    raw = demo / "schema-map" / "nld_admin2.parquet"
    input_path = demo / "schema-join" / "nld_admin2.parquet"
    join_path = demo / "schema-join" / "nld_admin1.parquet"
    topo_path = demo / "topo" / "nld_admin2.parquet"
    mapped = args.cache / "schema-map" / "nld_admin2_mapped.parquet"

    con = gemeenten(src, cached(GEBIEDEN, args.cache / "gebieden_2025.json"))
    schema_map_input(con, raw)
    schema_join_input(con, input_path)
    topo_input(con, topo_path)
    schema_join_layer(
        cached(PROVINCIEGEBIED, args.cache / "provinciegebied.json"), join_path
    )
    counts = {
        path: issue_counts(path, args.cache) for path in (raw, input_path, join_path)
    }
    for path, kinds in counts.items():
        if n := kinds.get("overlap", 0):
            msg = f"{path}: {n} overlaps after simplify and clean"
            raise SystemExit(msg)
    schema_map(
        raw,
        mapped,
        csv_output=mapped.with_name("nld_admin2_crosswalk.csv"),
        tmp_dir=args.cache / "tmp",
    )
    if n := join_issues(input_path, join_path, args.cache):
        msg = f"schema-join reported {n} issues"
        raise SystemExit(msg)
    check_topo(topo_path, counts[input_path]["gap"], args.cache)


if __name__ == "__main__":
    main()
