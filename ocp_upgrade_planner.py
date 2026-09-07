#!/usr/bin/env python3
"""
OCP cluster upgrade planner - CLI.

Reads a cluster + operator list as JSON, plans the operator upgrades required
around the cluster upgrade, and writes one HTML report per operator plus a
cluster summary. Machine-readable JSON goes to stdout.

    python ocp_upgrade_planner.py -i cluster.json --catalog-dir ./catalogs

Input:

    {
      "cluster": { "current": "4.18", "target": "4.20", "channel": "eus" },
      "operators": [
        { "name": "odf-operator",  "channel": "stable-4.18", "version": "4.18.3" },
        { "name": "loki-operator", "channel": "stable-6.1",  "version": "6.1.0"  }
      ]
    }

Catalog files are named data-v<major>_<minor>.json, one per OCP release.
They are looked for in ./data or the current directory (and in the same two
locations beside the input file), or wherever --catalog-dir points.

Exit codes:
    0  no action required, or operator upgrades are required and planned
    2  manual review required
    3  at least one operator blocks the cluster upgrade
    1  usage or input error
"""

import argparse
import json
import sys
from pathlib import Path

from ocp_planner import (
    build_ocp_path,
    discover_catalog_dir,
    load_catalogs,
    plan_operator,
    catalog_filename,
)
from ocp_report import generate_operator_report, generate_summary_report

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REVIEW = 2
EXIT_BLOCKED = 3


def run(payload, catalog_dir, output_dir, quiet=False):
    cluster = payload.get('cluster') or {}
    operators = payload.get('operators') or []

    current = cluster.get('current')
    target = cluster.get('target')
    channel = cluster.get('channel', 'stable')

    if not current or not target:
        raise ValueError("cluster.current and cluster.target are required")
    if not operators:
        raise ValueError("at least one entry in 'operators' is required")

    ocp_path = build_ocp_path(current, target, channel)

    if not quiet:
        print(f"Cluster: OCP {current} -> {target} ({channel})", file=sys.stderr)
        print(f"Path:    {' -> '.join(ocp_path)}", file=sys.stderr)
        print(f"Loading {len(ocp_path)} catalog(s) from {catalog_dir}",
              file=sys.stderr)

    catalogs = load_catalogs(catalog_dir, ocp_path)

    results = []
    for op in operators:
        name = op.get('name')
        if not name:
            raise ValueError("each operator requires a 'name'")
        res = plan_operator(catalogs, ocp_path, name,
                            op.get('channel', ''), op.get('version', ''))
        results.append(res)
        if not quiet:
            pre = sum(p['hops'] for p in res['phases']
                      if p['kind'] == 'pre-upgrade')
            print(f"  {name:42} {res['verdict']:28} "
                  f"{pre} upgrade(s) before cluster move", file=sys.stderr)

    blocking = [r['operator'] for r in results if r['blocking']]
    review = [r['operator'] for r in results if r['verdict'] == 'manual_review']
    upgrades = [r['operator'] for r in results
                if r['verdict'] == 'operator_upgrade_required']

    if blocking:
        verdict = 'blocked'
    elif review:
        verdict = 'manual_review'
    elif upgrades:
        verdict = 'operator_upgrade_required'
    else:
        verdict = 'no_action_required'

    cluster_info = {'current': current, 'target': target,
                    'channel': channel, 'ocp_path': ocp_path}

    plan = {
        'cluster': cluster_info,
        'verdict': verdict,
        'blocking_operators': blocking,
        'manual_review_operators': review,
        'operators_requiring_upgrade': upgrades,
        'catalogs': {o: catalog_filename(o) for o in ocp_path},
        'operators': results,
    }

    for res in results:
        res['html'] = generate_operator_report(
            catalogs, res, cluster_info, output_dir)
    plan['summary_html'] = generate_summary_report(plan, output_dir)

    return plan


def main():
    ap = argparse.ArgumentParser(
        description="Plan OLM operator upgrades around an OCP cluster upgrade.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -i cluster.json
  cat cluster.json | %(prog)s
  %(prog)s -i cluster.json --catalog-dir ./catalogs -j plan.json
""")
    ap.add_argument('-i', '--input', help='Input JSON file (default: stdin)')
    ap.add_argument('--catalog-dir',
                    help='Directory holding data-v<major>_<minor>.json files. '
                         'Default: ./data or the current directory, or the '
                         'same locations beside the input file.')
    ap.add_argument('-d', '--output-dir', default='.',
                    help='Where to write html/ (default: .)')
    ap.add_argument('-j', '--json-out',
                    help='Also write the plan JSON to this file')
    ap.add_argument('-q', '--quiet', action='store_true',
                    help='Suppress progress output on stderr')
    args = ap.parse_args()

    try:
        raw = (Path(args.input).read_text() if args.input
               else sys.stdin.read())
    except OSError as e:
        print(f"Cannot read input: {e}", file=sys.stderr)
        return EXIT_ERROR

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Input is not valid JSON: {e}", file=sys.stderr)
        return EXIT_ERROR

    try:
        search_from = str(Path(args.input).parent) if args.input else None
        catalog_dir = discover_catalog_dir(args.catalog_dir, search_from)
        if not args.quiet:
            print(f"Catalogs: {catalog_dir}", file=sys.stderr)
        plan = run(payload, catalog_dir, args.output_dir, args.quiet)
    except (ValueError, FileNotFoundError) as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_ERROR

    out = json.dumps(plan, indent=2)
    print(out)
    if args.json_out:
        Path(args.json_out).write_text(out)

    if not args.quiet:
        print(f"\nSummary: {plan['summary_html']}", file=sys.stderr)

    return {'blocked': EXIT_BLOCKED,
            'manual_review': EXIT_REVIEW}.get(plan['verdict'], EXIT_OK)


if __name__ == '__main__':
    sys.exit(main())
