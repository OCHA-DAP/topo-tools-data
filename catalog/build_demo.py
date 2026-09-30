"""Build the hdx/topo-tools NLD demo inputs: one folder per topo-tools tool."""

import argparse
import json
import math
from pathlib import Path

import duckdb
from build_nld import get
from outputs import copy_parquet, write_parquet
from topo_tools.api.edge_match import match
from topo_tools.api.schema_join import join
from topo_tools.api.schema_map import map as schema_map
from topo_tools.api.topo_clean import clean
from topo_tools.api.topo_detect import detect

SIMPLIFY_M = 100.0
SNAP_M = 0.01
# CBS land/water slivers; each one seeds a Voronoi wedge in edge-match.
CRUMB_M2 = 10_000.0
# (unit, neighbour, metres): one vertex on their shared border moves into the neighbour
# (positive, an overlap) or back into the unit (negative, a gap).
DISPLACED = [
    ("GM0344", "GM0310", 30.0),
    ("GM0363", "GM0362", 25.0),
    ("GM0599", "GM0542", -20.0),
    ("GM0772", "GM0861", -15.0),
    ("GM0014", "GM1966", -25.0),
]
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
    write_parquet(con, query, out)


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


def edge_input(con: duckdb.DuckDBPyConnection, out: Path) -> None:
    con.execute(f"""
        CREATE OR REPLACE TABLE e AS
        SELECT gemeentecode, gemeentenaam, ST_Union_Agg(d.geom) AS geometry
        FROM (SELECT gemeentecode, gemeentenaam, unnest(ST_Dump(geometry::GEOMETRY)) AS d FROM g)
        WHERE ST_Area(d.geom) >= {CRUMB_M2}
        GROUP BY ALL
    """)
    if missing := con.execute(
        "SELECT count(*) FROM g ANTI JOIN e USING (gemeentecode)"
    ).fetchone()[0]:
        msg = f"{missing} gemeenten have no part of {CRUMB_M2:g} m² or more"
        raise SystemExit(msg)
    copy(
        con,
        "SELECT geometry::GEOMETRY('EPSG:28992') AS geometry, gemeentecode AS adm2_code, gemeentenaam AS adm2_name "
        "FROM e ORDER BY adm2_code",
        out,
    )


def rings(geometry: dict) -> list[list[list[float]]]:
    polygons = geometry["coordinates"]
    if geometry["type"] == "Polygon":
        polygons = [polygons]
    return [ring for polygon in polygons for ring in polygon]


def displace(
    con: duckdb.DuckDBPyConnection,
    geoms: dict[str, dict],
    unit: str,
    neighbour: str,
    m: float,
) -> None:
    """Move the middle vertex of `unit`'s border with `neighbour` `m` metres across it."""
    owners: dict[tuple[float, float], set[str]] = {}
    for code, geometry in geoms.items():
        for ring in rings(geometry):
            for v in ring:
                owners.setdefault(tuple(v), set()).add(code)
    pair = {unit, neighbour}
    ring = next(
        r for r in rings(geoms[unit]) if any(owners[tuple(v)] == pair for v in r)
    )
    n = len(ring) - 1
    # Both neighbours on the same border, so only this pair's edges move.
    border = [
        k
        for k in range(n)
        if all(owners[tuple(ring[(k + d) % n])] >= pair for d in (-1, 0, 1))
        and owners[tuple(ring[k])] == pair
    ]
    k = border[len(border) // 2]
    (px, py), (qx, qy), (x, y) = ring[(k - 1) % n], ring[(k + 1) % n], ring[k]
    length = math.hypot(qx - px, qy - py)
    nx, ny = (py - qy) / length, (qx - px) / length
    target = neighbour if m > 0 else unit
    for sign in (1, -1):
        point = [x + sign * nx * abs(m), y + sign * ny * abs(m)]
        inside = con.execute(
            "SELECT ST_Contains(geometry, ST_Point(?, ?)) FROM w WHERE gemeentecode = ?",
            [*point, target],
        ).fetchone()[0]
        if inside:
            ring[k] = point
            if k == 0:
                ring[n] = point
            return
    msg = f"{unit}: no side of its border with {neighbour} lies in {target}"
    raise SystemExit(msg)


def topo_input(src: Path, out: Path) -> None:
    """Every gemeente with its water, as one gap-free coverage, then the DISPLACED vertices."""
    con = duckdb.connect()
    con.execute("LOAD spatial")
    con.execute(
        "CREATE TABLE w AS SELECT row_number() OVER (ORDER BY gemeentecode) AS i, gemeentecode, "
        "any_value(gemeentenaam) AS gemeentenaam, ST_Union_Agg(geometry::GEOMETRY) AS geometry "
        f"FROM read_parquet('{src}') GROUP BY gemeentecode"
    )
    simplify(con, "w")
    geoms = {
        code: json.loads(g)
        for code, g in con.execute(
            "SELECT gemeentecode, ST_AsGeoJSON(geometry) FROM w"
        ).fetchall()
    }
    for unit, neighbour, m in DISPLACED:
        displace(con, geoms, unit, neighbour, m)
    con.execute("CREATE TABLE edits (gemeentecode VARCHAR, geojson VARCHAR)")
    con.executemany(
        "INSERT INTO edits VALUES (?, ?)",
        [(unit, json.dumps(geoms[unit])) for unit, _, _ in DISPLACED],
    )
    copy(
        con,
        "SELECT coalesce(ST_GeomFromGeoJSON(e.geojson), w.geometry::GEOMETRY)::GEOMETRY('EPSG:28992') AS geometry, "
        "gemeentecode AS adm2_code, gemeentenaam AS adm2_name "
        "FROM w LEFT JOIN edits e USING (gemeentecode) ORDER BY adm2_code",
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


def check_topo(path: Path, cache: Path) -> None:
    expected = {
        "overlap": sum(m > 0 for *_, m in DISPLACED),
        "gap": sum(m < 0 for *_, m in DISPLACED),
    }
    if (counts := issue_counts(path, cache)) != expected:
        msg = f"{path}: {counts}, expected {expected}"
        raise SystemExit(msg)
    out = cache / "topo" / "nld_admin2_clean.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    clean(
        path,
        out,
        out.with_stem(out.stem + "_issues"),
        maximum_gap_width="thin",
        tmp_dir=cache / "tmp",
    )
    if cleaned := issue_counts(out, cache):
        msg = f"topo-clean --maximum-gap-width thin left {cleaned}"
        raise SystemExit(msg)


def check_edge(
    input_path: Path, overlay_path: Path, overlay_gaps: int, cache: Path
) -> None:
    out = cache / "edge" / "nld_admin2_matched.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    match(input_path, overlay_path, out, per_feature=True, tmp_dir=cache / "tmp")
    kinds = dict(
        duckdb.sql(
            f"SELECT kind, count(*) FROM read_parquet('{out.with_stem(out.stem + '_issues')}') "
            "WHERE kind <> 'micro-polygon' GROUP BY kind"
        ).fetchall()
    )
    if kinds != {"gap": overlay_gaps}:
        msg = f"edge-match --per-feature reported {kinds}, expected only the overlay's {overlay_gaps} gaps"
        raise SystemExit(msg)


def join_issues(input_path: Path, join_path: Path, cache: Path) -> int:
    out = cache / "schema-join" / "nld_admin2_join.parquet"
    join(input_path, join_path, out, tmp_dir=cache / "tmp")
    issues = out.with_stem(out.stem + "_issues")
    if not issues.exists():
        return 0
    return duckdb.sql(f"SELECT count(*) FROM read_parquet('{issues}')").fetchone()[0]


def build(catalog: Path, cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    src = catalog / "nld" / "2025" / "nld_admin2" / "nld_admin2.parquet"
    demo = catalog / "nld" / "demo"
    raw = demo / "schema-map" / "nld_admin2.parquet"
    input_path = demo / "schema-join" / "nld_admin2.parquet"
    join_path = demo / "schema-join" / "nld_admin1.parquet"
    topo_path = demo / "topo" / "nld_admin2.parquet"
    edge_path = demo / "edge" / "nld_admin2.parquet"
    mapped = cache / "schema-map" / "nld_admin2_mapped.parquet"

    con = gemeenten(src, cached(GEBIEDEN, cache / "gebieden_2025.json"))
    schema_map_input(con, raw)
    schema_join_input(con, input_path)
    topo_input(src, topo_path)
    edge_input(con, edge_path)
    schema_join_layer(
        cached(PROVINCIEGEBIED, cache / "provinciegebied.json"), join_path
    )
    counts = {path: issue_counts(path, cache) for path in (raw, input_path, join_path)}
    for path, kinds in counts.items():
        if n := kinds.get("overlap", 0):
            msg = f"{path}: {n} overlaps after simplify and clean"
            raise SystemExit(msg)
    schema_map(
        raw,
        mapped,
        csv_output=mapped.with_name("nld_admin2_crosswalk.csv"),
        tmp_dir=cache / "tmp",
    )
    if n := join_issues(input_path, join_path, cache):
        msg = f"schema-join reported {n} issues"
        raise SystemExit(msg)
    check_topo(topo_path, cache)
    edge_overlay = edge_path.with_stem("nld_admin1")
    copy_parquet(join_path, edge_overlay)
    check_edge(edge_path, edge_overlay, counts[join_path]["gap"], cache)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog", type=Path, default=Path("portolan"), help="Catalog root directory"
    )
    parser.add_argument("--cache", type=Path, default=Path("tmp/catalog/cache/demo"))
    args = parser.parse_args()
    build(args.catalog, args.cache)


if __name__ == "__main__":
    main()
