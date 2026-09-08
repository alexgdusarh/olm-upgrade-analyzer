# OLM Operator Upgrade Path Analyzer

Two tools that read OLM catalogs and work out how operators need to move.

| Tool | Question it answers |
|------|---------------------|
| `ocp_upgrade_planner.py` | I am upgrading a cluster. What must happen to my operators, and when? |
| `operator_interactive.py` | How do I get one operator from version A to the latest, in one catalog? |

Both are generic. No operator names, versions or channel naming schemes are
hardcoded anywhere.

## Install

```bash
pip install -r requirements.txt
```

---

# 1. Cluster upgrade planner

Plans every operator around an OCP cluster upgrade, using one catalog per OCP
release. Writes one HTML report per operator plus a cluster summary, and prints
the plan as JSON.

```bash
python ocp_upgrade_planner.py -i cluster.json
```

## Input

JSON, from a file (`-i`) or stdin.

```json
{
  "cluster": {
    "current": "4.18",
    "target":  "4.20",
    "channel": "eus"
  },
  "operators": [
    { "name": "odf-operator",  "channel": "stable-4.18", "version": "4.18.3" },
    { "name": "loki-operator", "channel": "stable-6.1",  "version": "6.1.0"  }
  ]
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `cluster.current` | yes | OCP release the cluster is on now |
| `cluster.target` | yes | OCP release to reach |
| `cluster.channel` | no | `eus` or anything else (`stable`, `fast`, ...). Default `stable` |
| `operators[].name` | yes | OLM **package** name, as it appears in the catalog |
| `operators[].channel` | yes | Subscription channel currently in use |
| `operators[].version` | yes | Version currently installed |

Names and versions are matched leniently against the catalogs, since a
subscription rarely records them exactly as the catalog does. A leading `v` is
optional on any version. Package names are resolved past vendor prefixes and
role suffixes (`openshift-mtv` finds `mtv-operator`). Build and vendor suffixes
such as `-rhodf` or `-202608142236` are kept and compared.

A channel that no longer exists is not an error. Resolution order:

1. the requested channel, when it exists
2. the channel actually holding the installed version
3. the newest channel named `stable` or `latest`
4. the newest channel ending in a version number
5. the channel carrying the highest version

Every substitution is recorded in that operator's `notes` so it can be checked
against the real subscription.

`cluster.channel` decides the path length and nothing else:

- `eus` — jumps two releases: `4.18 -> 4.19 -> 4.20`
- anything else — jumps one: `4.18 -> 4.19`

A mismatch is rejected rather than guessed:

```
'stable' upgrade expects a 1-release jump, but 4.18 -> 4.20 spans 2
```

## Command-line flags

| Flag | Default | Description |
|------|---------|-------------|
| `-i`, `--input` | stdin | Input JSON file |
| `--catalog-dir` | auto | Directory holding the catalogs. Falls back to `OCP_CATALOG_DIR`, then discovery |
| `-d`, `--output-dir` | `.` | Where `html/` is written |
| `-j`, `--json-out` | — | Also write the plan JSON to this file |
| `-q`, `--quiet` | off | Suppress progress output on stderr |

## Catalogs

Named `data-v<major>_<minor>.json`, one per OCP release. Either layout works:

```
project/                      project/
  cluster.json                  cluster.json
  data/                         data-v4_18.json
    data-v4_18.json             data-v4_19.json
    data-v4_19.json             data-v4_20.json
    data-v4_20.json
```

Catalogs are usually kept outside the project that consumes them, so they are
looked for in this order:

1. `--catalog-dir`
2. the `OCP_CATALOG_DIR` environment variable
3. a conventional catalog directory — `data/`, `catalogs/`, `catalog/`,
   `data-catalogs/`, `ocp-catalogs/` — beside the input file, then beside the
   current directory, then walking up their parents
4. a bounded recursive scan below the input file's directory and the current
   directory

So a layout like this needs no flag at all:

```
/home/you/
  ocp-operator-upgrade/    <- run from here
    cluster.json
  catalogs/                <- found by the parent walk
    data-v4_18.json
    ...
```

For a fixed location, set it once:

```bash
export OCP_CATALOG_DIR=/srv/ocp/catalogs
```

Only releases on the path are read. A 4.18 to 4.20 EUS run opens 4.18, 4.19 and
4.20 and ignores any other catalogs sitting there.

## Output

`html/index.html` is the cluster summary; `html/<operator>/index.html` is the
per-operator report, one row group per phase — info table, graph, steps. The
plan JSON goes to stdout.

Abbreviated — each object carries more keys than shown:

```json
{
  "cluster": { "current": "4.18", "target": "4.20",
               "channel": "eus", "ocp_path": ["4.18", "4.19", "4.20"] },
  "verdict": "operator_upgrade_required",
  "blocking_operators": [],
  "manual_review_operators": [],
  "operators_requiring_upgrade": ["odf-operator"],
  "operators": [
    {
      "operator": "odf-operator",
      "verdict": "operator_upgrade_required",
      "version_pinned": true,
      "phases": [
        { "phase": 1, "kind": "per-release", "on_ocp": "4.19",
          "status": "upgrade_required", "hops": 1,
          "from": { "channel": "stable-4.18", "version": "4.18.3"  },
          "to":   { "channel": "stable-4.19", "version": "4.19.22" },
          "steps": [ { "to_channel": "stable-4.19",
                       "to_version": "4.19.22", "via": "skipRange" } ] }
      ],
      "notes": [],
      "html": "html/odf-operator/index.html"
    }
  ]
}
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | No action required, or operator upgrades are required and planned |
| 1 | Usage or input error |
| 2 | Manual review required |
| 3 | An operator blocks the cluster upgrade |

### Verdicts

`no_action_required`, `operator_upgrade_required`, `manual_review`, `blocked`.

## How operators are planned

Two models, chosen automatically per operator.

### Release-pinned operators follow the cluster

An operator whose version stream tracks the OCP release — `odf-operator` 4.18.x
on OCP 4.18, `lvms-operator`, the ODF family. Detected from the version data,
not from channel names.

Every catalog carries the previous release's channel alongside its own, so an
operator at `stable-<N>` is still valid once the cluster reaches N+1. Nothing
has to happen before the hop. The upgrade happens **after** the cluster
arrives: switch to that release's channel, take its latest version. One
operator upgrade per OCP upgrade.

```
odf-operator, installed stable-4.18 / 4.18.3

  cluster 4.18 -> 4.19
  on 4.19:  switch to stable-4.19, install 4.19.22
  cluster 4.19 -> 4.20
  on 4.20:  switch to stable-4.20, install 4.20.17
```

A pre-upgrade phase appears only when the installed version predates the
release window and could not survive the first hop — then it is aligned to the
current release's channel head under the same rule.

### Floating operators lead the cluster

Everything else — `loki-operator`, `compliance-operator`, `openshift-gitops-operator`.

For each hop the operator must already sit at a (channel, version) present in
**both** the current and next catalogs, so it is upgraded before the cluster
moves. The constraint is pairwise per hop, not a single tuple valid across every
catalog. A final phase takes it to latest once the cluster has arrived.

```
loki-operator, installed stable-6.1 / 6.1.0

  on 4.18:  upgrade to stable-6.3 / 6.3.4   (stable-6.1 is gone by 4.19)
  cluster 4.18 -> 4.19                       (nothing to do)
  cluster 4.19 -> 4.20
  on 4.20:  upgrade to stable-6.6 / 6.6.0
```

### Objective

Fewest upgrades before the cluster can move, so the cluster is unblocked
quickly. Among options costing the same number of upgrades, the highest channel
and version, since that means fewer upgrades overall afterwards. Switching
channel at the same version is free and does not count as an upgrade.

### Catalog anomalies

Occasionally a channel or version is present in two releases but missing from
one in between, or is dropped from the middle of a channel. Those entries are
excluded from planning and reported in the notes for manual checking rather
than being planned around.

---

# 2. Single-catalog analyzer

The original tool. One operator, one catalog, shortest path to a target.

```bash
python operator_interactive.py -f data.json -o OPERATOR -v VERSION [-c CHANNEL] [-t TARGET]
```

| Flag | Required | Description |
|------|----------|-------------|
| `-f`, `--file` | yes | OLM catalog JSON file |
| `-o`, `--operator` | yes | Operator (package) name |
| `-v`, `--version` | yes | Current / start version |
| `-c`, `--target-channel` | no | Restrict the path to this channel |
| `-t`, `--target-version` | no | Stop here instead of the latest |
| `-d`, `--output-dir` | no | Output directory (default `.`) |

Output: `html/<operator>/index.html`

```bash
# latest version, crossing channels as needed
python operator_interactive.py -f data.json -o loki-operator -v 6.0.0
#   6.0.0 -> 6.2.12 (stable-6.2) -> 6.6.0 (stable-6.6)

# stay inside one channel
python operator_interactive.py -f data.json -o loki-operator -v 6.2.9 -c stable-6.2
#   6.2.9 -> 6.2.12

# target a specific version
python operator_interactive.py -f data.json -o compliance-operator -v 0.1.32 -t 1.9.2
```

Graph edges come from three sources: START via `skipRange`, the `replaces` chain
within a channel, and each upgrade-path version via `skipRange` — the last is
what draws jumps such as `1.21.0 -> 1.21.4`.

---

## Files

```
ocp_upgrade_planner.py    cluster planner CLI
ocp_planner.py            planning engine
ocp_report.py             HTML reports
operator_interactive.py   single-catalog analyzer
docs/ALGORITHM_DOCUMENTATION.md
examples/                 sample input and generated reports
```
