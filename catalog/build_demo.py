"""Build the hdx/topo-tools NLD demo inputs: one folder per topo-tools tool."""

import argparse
import unicodedata
from pathlib import Path

import duckdb
from build_nld import get
from outputs import write_parquet
from topo_tools.api.code_create import code_create
from topo_tools.api.code_update import code_update
from topo_tools.api.edge_match import match
from topo_tools.api.name_clean import clean as name_clean
from topo_tools.api.package import package
from topo_tools.api.schema_join import join
from topo_tools.api.schema_map import map as schema_map
from topo_tools.api.topo_clean import clean
from topo_tools.api.topo_detect import detect

SIMPLIFY_M = 100.0
SNAP_M = 0.01
# CBS land/water slivers; each one seeds a Voronoi wedge in edge-match.
CRUMB_M2 = 10_000.0
# Vlissingen's Sloe harbour parts sit inside Borsele, and their split pier stripes edge-match.
MAIN_PART_ONLY = ("GM0718",)
# Zeeland digitized its gemeenten on a base map shifted (dx, dy) metres, so only its borders
# with Zuid-Holland and Noord-Brabant disagree.
SHIFTED_PROVINCIE = "PV29"
SHIFT_M = (4.0, 6.0)
# (code, name, stored name, finding): one of each kind name-detect reports.
NAMES = [
    (
        "GM1900",
        "Súdwest-Fryslân",
        "Súdwest-Fryslân".encode().decode("cp1252"),
        "encoding-artifact",
    ),
    (
        "GM1970",
        "Noardeast-Fryslân",
        unicodedata.normalize("NFD", "Noardeast-Fryslân"),
        "unnormalized-unicode",
    ),
    ("GM0484", "Alphen aan den Rijn", "Alphen aan den  Rijn", "whitespace"),
    (
        "GM0502",
        "Capelle aan den IJssel",
        "Capelle\u00a0aan\u00a0den\u00a0IJssel",
        "invisible-character",
    ),
    ("GM0599", "Rotterdam", "ROTTERDAM", "case-outlier"),
    (
        "GM0352",
        "Wijk bij Duurstede",
        "Utrechtse heuvelrug",
        "normalized-duplicate-name",
    ),
]
NAME_FIXES = {
    "encoding-artifact",
    "unnormalized-unicode",
    "whitespace",
    "invisible-character",
}
# CBS StatLine "Gebieden in Nederland 2025"; Code_28/Naam_29 are its Provincies group.
GEBIEDEN = "https://opendata.cbs.nl/ODataApi/odata/86059NED/TypedDataSet?$format=json&$select=RegioS,Code_28,Naam_29"
# topo-tools writes EPSG:4326; the demos stay in RD New like their sources.
TO_RD = "ST_Transform(geometry, 'EPSG:4326', 'EPSG:28992', always_xy := true)::GEOMETRY('EPSG:28992') AS geometry"
# Edge Matcher runs groups in overlay row order: the coast south to north, then the interior back south.
EDGE_OVERLAY_ORDER = [
    "PV29",
    "PV28",
    "PV27",
    "PV24",
    "PV21",
    "PV20",
    "PV22",
    "PV23",
    "PV25",
    "PV26",
    "PV30",
    "PV31",
]
PROVINCIEGEBIED = "https://api.pdok.nl/kadaster/bestuurlijkegebieden/ogc/v1/collections/provinciegebied/items?f=json&limit=100&crs=http://www.opengis.net/def/crs/EPSG/0/28992"


def coverage(
    con: duckdb.DuckDBPyConnection, table: str, call: str, crs: str = "EPSG:28992"
) -> None:
    """Replace `table`'s geometry with a coverage function's per-row result, keyed by `i`."""
    con.execute(f"""
        CREATE OR REPLACE TABLE {table} AS
        WITH parts AS (
            SELECT unnest(ST_Dump({call.format(geoms="list(geometry::GEOMETRY ORDER BY i)")})) AS d
            FROM {table}
        ), merged AS (
            SELECT d.path[1] AS i, ST_Union_Agg(d.geom) AS geometry FROM parts GROUP BY 1
        )
        SELECT t.i, m.geometry::GEOMETRY('{crs}') AS geometry, t.* EXCLUDE (i, geometry)
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


def provincies(con: duckdb.DuckDBPyConnection, provinces: Path) -> None:
    """Load the CBS gemeente-to-provincie lookup as table `p`."""
    con.execute(
        f"CREATE TABLE p AS SELECT trim(r.RegioS) AS gemeentecode, trim(r.Code_28) AS provinciecode, "
        f"trim(r.Naam_29) AS provincienaam FROM (SELECT unnest(value) AS r FROM read_json('{provinces}'))"
    )


def gemeenten(src: Path, provinces: Path) -> duckdb.DuckDBPyConnection:
    """Simplified land gemeenten with provincie columns, as table `g`."""
    con = duckdb.connect()
    con.execute("LOAD spatial")
    provincies(con, provinces)
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
        SELECT gemeentecode, gemeentenaam, ST_Union_Agg(geom) AS geometry
        FROM (
            SELECT gemeentecode, gemeentenaam, d.geom,
                   row_number() OVER (PARTITION BY gemeentecode ORDER BY ST_Area(d.geom) DESC) AS rank
            FROM (SELECT gemeentecode, gemeentenaam, unnest(ST_Dump(geometry::GEOMETRY)) AS d FROM g)
        )
        WHERE ST_Area(geom) >= {CRUMB_M2}
          AND (rank = 1 OR gemeentecode NOT IN {MAIN_PART_ONLY!r})
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


def edge_overlay(join_path: Path, out: Path) -> None:
    con = duckdb.connect()
    con.execute("LOAD spatial")
    write_parquet(
        con,
        "SELECT geometry, adm1_code, adm1_name "
        f"FROM read_parquet('{join_path}') ORDER BY list_position({EDGE_OVERLAY_ORDER}, adm1_code)",
        out,
        ordered=True,
    )


def topo_input(src: Path, provinces: Path, out: Path) -> None:
    """Every gemeente with its water as one gap-free coverage, then SHIFTED_PROVINCIE moved by SHIFT_M."""
    con = duckdb.connect()
    con.execute("LOAD spatial")
    provincies(con, provinces)
    con.execute(
        "CREATE TABLE w AS SELECT row_number() OVER (ORDER BY gemeentecode) AS i, gemeentecode, "
        "any_value(gemeentenaam) AS gemeentenaam, ST_Union_Agg(geometry::GEOMETRY) AS geometry "
        f"FROM read_parquet('{src}') GROUP BY gemeentecode"
    )
    simplify(con, "w")
    con.execute(
        "CREATE TABLE r AS SELECT gemeentecode, gemeentenaam, CASE WHEN provinciecode = ? "
        "THEN ST_Affine(geometry::GEOMETRY, 1, 0, 0, 1, ?, ?) ELSE geometry::GEOMETRY END AS geometry "
        "FROM w LEFT JOIN p USING (gemeentecode)",
        [SHIFTED_PROVINCIE, *SHIFT_M],
    )
    copy(
        con,
        "SELECT geometry::GEOMETRY('EPSG:28992') AS geometry, "
        "gemeentecode AS adm2_code, gemeentenaam AS adm2_name FROM r ORDER BY adm2_code",
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
    counts = issue_counts(path, cache)
    if not counts.get("overlap") or not counts.get("gap"):
        msg = f"{path}: {counts}, expected overlaps and gaps"
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


def check_edge(input_path: Path, overlay_path: Path, cache: Path) -> None:
    out = cache / "edge" / "nld_admin2_matched.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    # One thread: multi-threaded edge-match output varies run to run, and package/names build on it.
    match(
        input_path,
        overlay_path,
        out,
        assign="many",
        threads=1,
        tmp_dir=cache / "tmp",
    )
    kinds = dict(
        duckdb.sql(
            f"SELECT kind, count(*) FROM read_parquet('{out.with_stem(out.stem + '_issues')}') "
            "WHERE kind NOT IN ('micro-polygon', 'detached-part') GROUP BY kind"
        ).fetchall()
    )
    # Overlay holes (the Baarle enclaves) lie outside every clip target, so they are not gaps.
    if kinds:
        msg = f"edge-match --assign many reported {kinds}"
        raise SystemExit(msg)


def join_issues(input_path: Path, join_path: Path, cache: Path) -> int:
    out = cache / "schema-join" / "nld_admin2_join.parquet"
    join(input_path, join_path, out, tmp_dir=cache / "tmp")
    issues = out.with_stem(out.stem + "_issues")
    if not issues.exists():
        return 0
    return duckdb.sql(f"SELECT count(*) FROM read_parquet('{issues}')").fetchone()[0]


def hierarchy(src: Path, join_path: Path, out: Path, cache: Path) -> None:
    """`src`'s adm2 columns under adm0 NL and the provincie layer's adm1, via schema-join."""
    joined = cache / "hierarchy" / f"{out.parent.name}_{out.name}"
    joined.parent.mkdir(parents=True, exist_ok=True)
    join(src, join_path, joined, tmp_dir=cache / "tmp")
    con = duckdb.connect()
    con.execute("LOAD spatial")
    copy(
        con,
        f"SELECT {TO_RD}, 'NL' AS adm0_code, 'Nederland' AS adm0_name, adm1_code, adm1_name, "
        f"adm2_code, adm2_name FROM read_parquet('{joined}') ORDER BY adm2_code",
        out,
    )


def code_inputs(
    catalog: Path, join_path: Path, old: Path, new: Path, cache: Path
) -> None:
    """Old year coded from the root by code-create, new year with only its CBS codes."""
    for year, out in (
        (2022, cache / "code" / "nld_admin2_2022_cbs.parquet"),
        (2023, new),
    ):
        out.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        con.execute("LOAD spatial")
        con.execute(
            "CREATE TABLE t AS SELECT row_number() OVER (ORDER BY gemeentecode) AS i, geometry, gemeentecode, gemeentenaam "
            f"FROM read_parquet('{catalog / 'nld' / str(year) / 'nld_admin2' / 'nld_admin2.parquet'}') WHERE water = 'NEE'"
        )
        simplify(con, "t")
        land = cache / "code" / f"nld_admin2_{year}_land.parquet"
        copy(
            con,
            "SELECT geometry, gemeentecode AS adm2_code, gemeentenaam AS adm2_name FROM t ORDER BY adm2_code",
            land,
        )
        hierarchy(land, join_path, out, cache)
    coded = cache / "code" / "nld_admin2_2022_coded.parquet"
    code_create(
        cache / "code" / "nld_admin2_2022_cbs.parquet",
        coded,
        root_code="NL",
        delimiter="",
        min_width=2,
        source_codes="copy",
        tmp_dir=cache / "tmp",
    )
    con = duckdb.connect()
    con.execute("LOAD spatial")
    copy(
        con,
        f"SELECT {TO_RD}, * EXCLUDE (geometry) FROM read_parquet('{coded}') ORDER BY adm2_code",
        old,
    )


def names_input(src: Path, out: Path) -> None:
    con = duckdb.connect()
    con.execute("LOAD spatial")
    con.execute(f"CREATE TABLE t AS SELECT * FROM read_parquet('{src}')")
    for code, name, defect, _ in NAMES:
        found = con.execute(
            "SELECT adm2_name FROM t WHERE adm2_code = ?", [code]
        ).fetchone()
        if found != (name,):
            msg = f"{code}: expected {name!r}, found {found}"
            raise SystemExit(msg)
        con.execute("UPDATE t SET adm2_name = ? WHERE adm2_code = ?", [defect, code])
    copy(
        con,
        "SELECT geometry, adm0_code, adm0_name, adm1_code, adm1_name, adm2_code, adm2_name "
        "FROM t ORDER BY adm2_code",
        out,
    )


def check_code(old: Path, new: Path, cache: Path) -> None:
    out = cache / "code" / "nld_admin2_2023_coded.parquet"
    changelog = cache / "code" / "nld_admin2_changelog.csv"
    code_update(old, new, out, changelog, tmp_dir=cache / "tmp")
    outcomes = dict(
        duckdb.sql(
            f"SELECT code_outcome, count(*) FROM read_csv('{changelog}') WHERE level = 2 GROUP BY 1"
        ).fetchall()
    )
    expected = {"retained": 340, "retired": 5, "new": 2}
    if outcomes != expected:
        msg = f"code-update adm2 outcomes {outcomes}, expected {expected}"
        raise SystemExit(msg)


def check_names(path: Path, cache: Path) -> None:
    out = cache / "names" / "nld_admin2_clean.parquet"
    issues = out.with_name("nld_admin2_clean_issues.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    name_clean(path, out, issues, tmp_dir=cache / "tmp")
    found = duckdb.sql(
        f"SELECT code_a, kind, fixed FROM read_csv('{issues}') ORDER BY ALL"
    ).fetchall()
    expected = sorted((code, kind, kind in NAME_FIXES) for code, _, _, kind in NAMES)
    if found != expected:
        msg = f"name-clean reported {found}, expected {expected}"
        raise SystemExit(msg)
    names = dict(
        duckdb.sql(f"SELECT adm2_code, adm2_name FROM read_parquet('{out}')").fetchall()
    )
    if wrong := [
        code
        for code, name, _, kind in NAMES
        if kind in NAME_FIXES and names[code] != name
    ]:
        msg = f"name-clean did not restore {wrong}"
        raise SystemExit(msg)


def check_package(path: Path, cache: Path) -> None:
    out = cache / "package" / "nld_{x}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    package(path, out, "adm{n}_name", "adm{n}_code", tmp_dir=cache / "tmp")
    expected = {"admin0": 1, "admin1": 12, "admin2": 342, "points": 355}
    counts = {
        x: duckdb.sql(
            f"SELECT count(*) FROM read_parquet('{str(out).replace('{x}', x)}')"
        ).fetchone()[0]
        for x in expected
    }
    if counts != expected:
        msg = f"package wrote {counts}, expected {expected}"
        raise SystemExit(msg)


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

    gebieden = cached(GEBIEDEN, cache / "gebieden_2025.json")
    con = gemeenten(src, gebieden)
    schema_map_input(con, raw)
    schema_join_input(con, input_path)
    topo_input(src, gebieden, topo_path)
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
    edge_overlay_path = edge_path.with_stem("nld_admin1")
    edge_overlay(join_path, edge_overlay_path)
    check_edge(edge_path, edge_overlay_path, cache)

    old, new = (
        demo / "code" / "nld_admin2_2022.parquet",
        demo / "code" / "nld_admin2_2023.parquet",
    )
    code_inputs(catalog, join_path, old, new, cache)
    check_code(old, new, cache)
    release = demo / "package" / "nld_admin2.parquet"
    hierarchy(cache / "edge" / "nld_admin2_matched.parquet", join_path, release, cache)
    names_path = demo / "names" / "nld_admin2.parquet"
    names_input(release, names_path)
    check_names(names_path, cache)
    check_package(release, cache)


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
