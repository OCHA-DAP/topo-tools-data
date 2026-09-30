"""Rebuild the local portolan catalog end to end, optionally pushing it to source.coop."""

import argparse
from pathlib import Path

from annotate_nld import annotate_catalog
from build_demo import build as build_demo
from build_nld import YEARS
from build_nld import build as build_nld
from push import REMOTE, portolan, push

DEMO_DATETIME = "2025-01-01"


def add_all(catalog: Path) -> None:
    for year in YEARS:
        portolan(
            ["add", f"nld/{year}", "--pmtiles", "--datetime", f"{year}-01-01"],
            cwd=catalog,
        )
    for demo in sorted((catalog / "nld" / "demo").iterdir()):
        if (demo / ".portolan").is_dir():
            portolan(
                [
                    "add",
                    demo.relative_to(catalog).as_posix(),
                    "--pmtiles",
                    "--datetime",
                    DEMO_DATETIME,
                ],
                cwd=catalog,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog", type=Path, default=Path("portolan"), help="Catalog root directory"
    )
    parser.add_argument("--cache", type=Path, default=Path("tmp/catalog/cache"))
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument(
        "--detect", action="store_true", help="Add topo-detect counts to the report"
    )
    parser.add_argument("--push", action="store_true", help=f"Push to {REMOTE}")
    args = parser.parse_args()
    catalog = args.catalog.resolve()

    build_nld(catalog, args.cache, refetch=args.refetch, detect_issues=args.detect)
    annotate_catalog(catalog, args.cache, metadata_only=True)
    build_demo(catalog, args.cache / "demo")
    add_all(catalog)
    annotate_catalog(catalog, args.cache)
    portolan(["readme"], cwd=catalog)
    if args.push:
        push(catalog)


if __name__ == "__main__":
    main()
