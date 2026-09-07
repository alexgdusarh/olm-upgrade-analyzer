# OLM Operator Upgrade Path Analyzer

Analyzes OpenShift operator upgrade paths from an OLM catalog using `skipRange`
and `replaces` metadata, and renders the result as a networkx graph inside a
self-contained HTML report.

Generic: works for any operator in the catalog. No hardcoded operator names or versions.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
python operator_interactive.py -f data.json -o OPERATOR -v VERSION [-c CHANNEL] [-t TARGET]
```

| Flag | Required | Description |
|------|----------|-------------|
| `-f, --file` | yes | OLM catalog JSON file |
| `-o, --operator` | yes | Operator (package) name |
| `-v, --version` | yes | Current / start version |
| `-c, --target-channel` | no | Restrict the path to this channel |
| `-t, --target-version` | no | Stop at this version instead of the latest |
| `-d, --output-dir` | no | Output directory (default: `.`) |

Output: `html/<operator>/index.html`

## Examples

```bash
# Upgrade to the latest version, crossing channels as needed
python operator_interactive.py -f data.json -o loki-operator -v 6.0.0
#   6.0.0 -> 6.2.12 (stable-6.2) -> 6.6.0 (stable-6.6)

# Stay inside one channel
python operator_interactive.py -f data.json -o loki-operator -v 6.2.9 -c stable-6.2
#   6.2.9 -> 6.2.12 (stable-6.2)

# Target a specific channel
python operator_interactive.py -f data.json -o openshift-gitops-operator -v 1.14.1 -c gitops-1.21
#   1.14.1 -> 1.21.0 -> 1.21.4 (gitops-1.21)

# Target a specific version
python operator_interactive.py -f data.json -o compliance-operator -v 0.1.32 -t 1.9.2
```

## Target channel / target version

Both flags are optional and can be used together or separately.

- Neither: the target is the highest version across all channels.
- `-c` only: the path is restricted to that channel and stops at its highest version.
  This is what makes a same-channel upgrade such as `6.2.9 -> 6.2.12` possible.
- `-t` only: the path stops at that version, wherever it lives.
- Both: the version must exist in the given channel.

See `ALGORITHM_DOCUMENTATION.md` for the full algorithm and worked examples.

## Report contents

- Info table: operator, current version, channel(s), target version
- networkx graph (inline SVG) — green nodes are the upgrade path, blue are other
  available versions in the same channels
- Step-by-step upgrade instructions
- Summary

## Graph edges

1. START to every version whose `skipRange` covers it
2. `replaces` chain within a channel
3. Each upgrade-path version to every version whose `skipRange` covers it
   (this is what draws jumps such as `1.21.0 -> 1.21.4`)
