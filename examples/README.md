# Examples

## Catalog layout

Catalogs are named `data-v<major>.<minor>.json`, one per OCP release. Either
layout is found automatically:

```
project/                       project/
  cluster.json                   cluster.json
  data/                          data-v4_18.json
    data-v4_18.json              data-v4_19.json
    data-v4_19.json              data-v4_20.json
    data-v4_20.json
```

They are looked for beside the input file first, then in the current directory,
checking `data/` before the directory itself. `--catalog-dir` overrides this.

Only the releases on the upgrade path are read. A 4.18 to 4.20 EUS run reads
4.18, 4.19 and 4.20 and ignores any other catalogs present.

## Running

```bash
python ocp_upgrade_planner.py -i cluster-4.18-to-4.20-eus.json
cat cluster-4.18-to-4.20-eus.json | python ocp_upgrade_planner.py
python ocp_upgrade_planner.py -i cluster.json --catalog-dir /path/to/catalogs
```

Reports land in `html/` — `html/index.html` is the cluster summary, with one
subdirectory per operator. The plan JSON goes to stdout; `-j plan.json` also
writes it to a file.

## Sample reports

- `summary-4.18-to-4.20-eus.html` — cluster summary
- `odf-operator-4.18-to-4.20-eus.html` — version-pinned operator, stepped plan
- `loki-operator-4.18-to-4.20-eus.html` — sliding-window channels
