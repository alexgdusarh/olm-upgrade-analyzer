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

## [Unreleased] - feature/ocp-eus-multi-catalog

Adds OCP cluster upgrade planning across one catalog per OCP release.

### Added
- `ocp_upgrade_planner.py` — CLI taking a cluster plus an operator list as JSON.
- `ocp_planner.py` — planning engine.
- `ocp_report.py` — one HTML report per operator, one row group per phase,
  plus a cluster summary page.
- EUS (current+2) and single-release (current+1) upgrade paths, derived from
  `cluster.channel`. Catalogs outside the path are never read.
- Generic detection of version-pinned operators (versions tracking the OCP
  release, e.g. odf-operator 4.18.x on OCP 4.18).
- Non-monotonic catalog entries are excluded from planning and reported as notes
  for manual verification.
- JSON on stdout and exit codes: 0 ok, 2 manual review, 3 blocked, 1 input error.
- Catalog auto-discovery. `--catalog-dir` is optional; a `data/` directory or a
  flat layout is found automatically, beside the input file first and then in
  the current directory. Missing releases are reported alongside the ones that
  were found.

### Design
- Two planning models, chosen by whether the operator's version stream is
  pinned to the OCP release.
- **Release-pinned operators follow the cluster.** Each catalog carries the
  previous release's channel as well as its own, so an operator at
  `stable-<N>` stays valid when the cluster moves to N+1. The operator is
  therefore upgraded *after* each hop: move the cluster, switch to that
  release's channel, take its latest version. One operator upgrade per OCP
  upgrade. A pre-upgrade phase appears only when the installed version
  predates the release window and cannot survive the first hop.
- **Floating operators lead the cluster.** Constraint is **pairwise per hop**,
  not a global intersection across all catalogs. For a hop from OCP N to N+1 the operator must sit at a
  (channel, version) present in both catalogs; it may be moved again while the
  cluster sits at an intermediate release. Version-pinned operators carry only
  `stable-<N-1>` and `stable-<N>` per catalog, so no tuple exists in all three
  catalogs of an EUS jump — a global model would wrongly report the whole ODF
  family as blocked.
- Hops are counted per upgrade. Switching channel at the same version is free.
- Objective: fewest hops before the cluster can move, then the highest
  channel/version among equal-hop options.
- Presence is modelled as explicit version sets, not min/max floors, because
  channels are not always contiguous.

### Unchanged
- `operator_interactive.py` and the single-catalog v1.0.0 behaviour.
