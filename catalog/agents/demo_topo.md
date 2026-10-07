# AGENTS.md: {title}

Input for `topo-tools topo-detect` and `topo-tools topo-clean`: the {rows} gemeenten from the CBS Wijk- en Buurtkaart 2025 ([full precision](../../2025/nld_admin2/AGENTS.md)), each with its water, so before the errors are added they cover the country with no gaps or overlaps. Zeeland's gemeenten are then all moved {dx} m east and {dy} m north, as if Zeeland had digitized them on its own base map. Borders inside Zeeland match. Its borders with Zuid-Holland and Noord-Brabant come apart into thin gaps and overlaps, with notches where they meet. The CRS is EPSG:28992, in metres.

## Run the demo

```bash
topo-tools topo-detect {url} nld_admin2_issues.parquet
topo-tools topo-clean {url} nld_admin2_cleaned.parquet --maximum-gap-width thin
```

`topo-detect` reports {overlap_issues} overlaps, {gap_issues} gaps and {notch_issues} notches, up to {max_w} m wide, all along Zeeland's border. `topo-clean` always fixes the overlaps and notches. `--maximum-gap-width thin` also fills the gaps, because each is a thin sliver along a border. In the browser: [topo-clean]({web}/topo-clean?url={url}&gap=thin), [topo-detect]({web}/topo-detect?url={url}).

## Quirks

- `GM0998` Buitenland is the Belgian enclaves in Baarle-Nassau. It stays in so the coverage has no holes.
- Boundaries are simplified to 100 m. Use the full-precision layer for areas.
