# Changelog

## [1.0.0] - 2026-09-07

First stable release.

### Features
- Generic upgrade path calculation for any operator in an OLM catalog, driven by
  `skipRange` and `replaces` metadata. No operator-specific logic.
- Top-down channel traversal by max version, selecting the highest reachable
  version at each hop.
- Optional `-c/--target-channel` to restrict the path to a single channel,
  enabling same-channel upgrades (e.g. `6.2.9 -> 6.2.12`).
- Optional `-t/--target-version` to stop at a specific version instead of the latest.
- networkx graph rendered as inline SVG in a self-contained HTML report.
- Graph edges from three sources: START via `skipRange`, the `replaces` chain
  within a channel, and each upgrade-path version via `skipRange`.
- HTML info table showing operator, current version, channel(s) and target version.
- Handles operators with no `skipRange` by walking the `replaces` chain.

### Validated against
- loki-operator (multi-channel and same-channel)
- openshift-gitops-operator
- compliance-operator
- devspaces
- jws-operator
