# AGENTS.md: {title}

Input for `topo-tools topo-detect` and `topo-tools topo-clean`: the {rows} land gemeenten from the CBS Wijk- en Buurtkaart 2025 ([full precision](../../2025/nld_admin2/AGENTS.md)), with digitization errors added on purpose. The CRS is EPSG:28992, in metres.

| Error | Gemeenten | Size |
|---|---|---|
| Overlap | {overlaps} | grown {overlap_m} m into their neighbours |
| Sliver gap | {slivers} | a strip {sliver_m} m wide cut along one border |

## Run the demo

```bash
topo-tools topo-detect {url} nld_admin2_issues.parquet
topo-tools topo-clean {url} nld_admin2_cleaned.parquet --maximum-gap-width {gap_deg}
```

`topo-detect` reports {overlap_issues} overlaps and {gap_issues} gaps: the {sliver_count} slivers, {water_gaps} water bodies and {noise_gaps} zero-width gaps where the grown gemeenten meet their neighbours. `topo-clean` at {gap_deg} degrees (about {gap_m} m) fixes every overlap and fills every gap narrower than that, leaving the {water_gaps} water bodies open because the narrowest is {water_min_m} m wide. In the browser: [topo-clean]({web}/topo-clean?url={url}&gap={gap_m}), [topo-detect]({web}/topo-detect?url={url}).

## Quirks

- Water rows are left out, so the water bodies, such as the IJsselmeer, are real gaps. `--maximum-gap-width thin` fills them too, because a long, narrow lake counts as a sliver.
- Boundaries are simplified to 100 m. Use the full-precision layer for areas.
