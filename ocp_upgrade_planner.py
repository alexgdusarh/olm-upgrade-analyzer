#!/usr/bin/env python3
"""
OCP cluster upgrade planner - CLI.

Reads a cluster + operator list as JSON, plans the operator upgrades required
around the cluster upgrade, and writes one HTML report per operator plus a
cluster summary. Machine-readable JSON goes to stdout.

    python ocp_upgrade_planner.py -i catalog_mirror_check.json
    python ocp_upgrade_planner.py -i cluster.json --catalog-dir ./catalogs

Two input shapes are accepted.

The catalog mirror check written by ocp_preupgrade_health_check, grouped by
catalog index image. The catalogs are pulled from those images at run time,
and the operators are also checked against the target catalog alone to find
what has to be mirrored. Packages with main=false are dependencies: they are
not planned, but are kept in the oc-mirror configuration.

    {
      "cluster": { "current": "4.18.28", "target": "4.20", "channel": "eus",
                   "ocp_path": ["4.18", "4.19", "4.20"] },
      "operators": [
        { "pull_image": "registry.redhat.io/redhat/redhat-operator-index:v4.18",
          "packages": [ { "name": "odf-operator", "channel": "stable-4.18",
                          "version": "4.18.3", "max_ocp_version": "",
                          "main": true, "required_by": [] } ] }
      ]
    }

A flat operator list, planned against catalogs that are already on disk:

    {
      "cluster": { "current": "4.18", "target": "4.20", "channel": "eus" },
      "operators": [
        { "name": "odf-operator",  "channel": "stable-4.18", "version": "4.18.3" },
        { "name": "loki-operator", "channel": "stable-6.1",  "version": "6.1.0"  }
      ]
    }

Catalog files are named data-v<major>.<minor>.json, one per OCP release.

Exit codes:
    0  no action required, or operator upgrades are required and planned
    2  manual review required
    3  at least one operator blocks the cluster upgrade
    4  an intermediate catalog must be mirrored as well as the target catalog
    1  usage or input error
"""

import argparse
import json
import sys
from pathlib import Path

from catalog_fetch import FetchError, fetch_all, load_default_channels, retag
from mirror_plan import (
    CRITICAL,
    OK,
    build_imageset,
    check_operator,
    mirror_sets,
    pick_default_channel,
)
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
EXIT_CRITICAL = 4


def parse_payload(payload):
    """
    Return (cluster, groups) for either input shape.

    Each group is {'pull_image': str or None, 'operators': [...]}, where every
    operator carries name, channel, version, max_ocp_version, main and
    required_by.
    """
    cluster = payload.get('cluster') or {}
    entries = payload.get('operators') or []
    if not entries:
        raise ValueError("at least one entry in 'operators' is required")

    if any('pull_image' in e for e in entries):
        groups = []
        for e in entries:
            image = e.get('pull_image')
            if not image:
                raise ValueError("each operators[] entry requires 'pull_image'")
            ops = [{'name': p.get('name'),
                    'channel': p.get('channel', ''),
                    'version': p.get('version', ''),
                    'max_ocp_version': p.get('max_ocp_version') or '',
                    'main': p.get('main', True),
                    'required_by': p.get('required_by') or []}
                   for p in e.get('packages') or []]
            groups.append({'pull_image': image, 'operators': ops})
    else:
        groups = [{'pull_image': None, 'operators': [
            {'name': o.get('name'), 'channel': o.get('channel', ''),
             'version': o.get('version', ''), 'max_ocp_version': '',
             'main': True, 'required_by': []} for o in entries]}]

    for g in groups:
        for op in g['operators']:
            if not op['name']:
                raise ValueError("each operator requires a 'name'")
    if not any(op['main'] for g in groups for op in g['operators']):
        raise ValueError("no operator with main=true to plan")
    return cluster, groups


def resolve_ocp_path(cluster):
    current = cluster.get('current')
    target = cluster.get('target')
    channel = cluster.get('channel', 'stable')
    if not current or not target:
        raise ValueError("cluster.current and cluster.target are required")
    ocp_path = build_ocp_path(current, target, channel)
    given = cluster.get('ocp_path')
    if given and [str(o) for o in given] != ocp_path:
        raise ValueError(
            f"cluster.ocp_path {' -> '.join(map(str, given))} does not match "
            f"the {channel} path {' -> '.join(ocp_path)}")
    return current, target, channel, ocp_path


def _mirror_group(catalogs, ocp_path, group, results, catalog_dir):
    """Mirror check for one catalog index. Returns (checks, imageset entries)."""
    checks = []
    wanted = {}  # ocp -> {pkg: {channel: [versions]}}
    by_name = {op['name']: op for op in group['operators']}

    for res in results:
        op = by_name[res['input_name']]
        entry = {'operator': res['operator'], 'catalog_image': group['pull_image']}
        if res['verdict'] == 'manual_review' and not res['phases']:
            entry.update({'status': 'unresolved', 'catalogs': [],
                          'steps': [], 'notes': [
                              "Not checked: the installed operator could not be "
                              "matched in the current catalog."]})
            checks.append(entry)
            continue
        start = (res['input'].get('resolved_channel') or res['input']['channel'],
                 res.get('resolved_version') or res['input']['version'])
        chk = check_operator(catalogs, ocp_path, res['operator'], start,
                             op['max_ocp_version'])
        entry.update(chk)
        checks.append(entry)
        for ocp, chans in mirror_sets(chk).items():
            slot = wanted.setdefault(ocp, {}).setdefault(res['operator'], {})
            for chan, versions in chans.items():
                slot.setdefault(chan, []).extend(versions)

    entries = []
    for ocp in sorted(wanted, key=ocp_path.index):
        defaults = load_default_channels(catalog_dir, ocp)
        packages = []
        for name, chans in sorted(wanted[ocp].items()):
            packages.append({'name': name, 'channels': chans,
                             'defaultChannel': pick_default_channel(
                                 chans, defaults.get(name))})
        # Dependencies are not planned, but the mirrored catalog must still
        # carry them or OLM cannot resolve the operators that need them.
        for op in group['operators']:
            if op['main']:
                continue
            chans = catalogs[ocp].get(op['name'], {})
            chan = (op['channel'] if op['channel'] in chans
                    else defaults.get(op['name']))
            if not chan or chan not in chans:
                continue
            by = ', '.join(op['required_by']) or 'another operator'
            packages.append({'name': op['name'], 'channels': {chan: None},
                             'defaultChannel': pick_default_channel(
                                 {chan: None}, defaults.get(op['name'])),
                             'comment': f"dependency of {by}; channel head"})
        entries.append({'catalog': retag(group['pull_image'], ocp),
                        'packages': packages})
    return checks, entries


def run(payload, catalog_dirs, output_dir, quiet=False, imageset_out=None,
        fetched=False):
    """
    catalog_dirs maps each group's pull_image (None for a flat operator list)
    to the directory holding its catalogs. fetched marks catalogs pulled at
    run time, which are empty when the image carries none of the packages.
    """
    cluster, groups = parse_payload(payload)
    current, target, channel, ocp_path = resolve_ocp_path(cluster)
    cluster_info = {'current': current, 'target': target,
                    'channel': channel, 'ocp_path': ocp_path}

    if not quiet:
        print(f"Cluster: OCP {current} -> {target} ({channel})", file=sys.stderr)
        print(f"Path:    {' -> '.join(ocp_path)}", file=sys.stderr)

    results = []
    checks = []
    imageset = []
    for group in groups:
        catalog_dir = catalog_dirs[group['pull_image']]
        if not quiet:
            label = group['pull_image'] or 'operators'
            print(f"{label}: {len(ocp_path)} catalog(s) from {catalog_dir}",
                  file=sys.stderr)
        catalogs = load_catalogs(catalog_dir, ocp_path, allow_empty=fetched)

        group_results = []
        for op in group['operators']:
            if not op['main']:
                continue
            res = plan_operator(catalogs, ocp_path, op['name'],
                                op['channel'], op['version'])
            res['input_name'] = op['name']
            if group['pull_image']:
                res['catalog_image'] = group['pull_image']
            group_results.append(res)
            if not quiet:
                pre = sum(p['hops'] for p in res['phases']
                          if p['kind'] == 'pre-upgrade')
                print(f"  {op['name']:42} {res['verdict']:28} "
                      f"{pre} upgrade(s) before cluster move", file=sys.stderr)

        if group['pull_image']:
            g_checks, g_entries = _mirror_group(
                catalogs, ocp_path, group, group_results, catalog_dir)
            checks += g_checks
            imageset += g_entries
            if not quiet:
                for c in g_checks:
                    print(f"  {c['operator']:42} mirror {c['status']:10} "
                          f"catalogs {', '.join(c['catalogs']) or '-'}",
                          file=sys.stderr)

        for res in group_results:
            res['html'] = generate_operator_report(
                catalogs, res, cluster_info, output_dir)
        results += group_results

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

    plan = {
        'cluster': cluster_info,
        'verdict': verdict,
        'blocking_operators': blocking,
        'manual_review_operators': review,
        'operators_requiring_upgrade': upgrades,
        'catalogs': {o: catalog_filename(o) for o in ocp_path},
        'operators': results,
    }

    if checks:
        critical = [c['operator'] for c in checks if c['status'] != OK]
        to_mirror = {}
        for entry in imageset:
            ocp = entry['catalog'].rsplit(':v', 1)[-1]
            to_mirror.setdefault(ocp, []).append(entry['catalog'])
        plan['mirror'] = {
            'verdict': CRITICAL if critical else OK,
            'critical_operators': critical,
            'catalogs_to_mirror': to_mirror,
            'operators': checks,
        }
        path = Path(imageset_out or Path(output_dir) / 'imageset-config.yaml')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_imageset(imageset))
        plan['mirror']['imageset_config'] = str(path)

    plan['summary_html'] = generate_summary_report(plan, output_dir)
    return plan


def main():
    ap = argparse.ArgumentParser(
        description="Plan OLM operator upgrades around an OCP cluster upgrade.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -i outputs/catalog_mirror_check.json
  %(prog)s -i outputs/catalog_mirror_check.json --authfile ~/pull-secret.json
  %(prog)s -i cluster.json --catalog-dir ./catalogs -j plan.json
""")
    ap.add_argument('-i', '--input', help='Input JSON file (default: stdin)')
    ap.add_argument('--catalog-dir',
                    help='Use the data-v<major>.<minor>.json catalogs in this '
                         'directory instead of pulling them. Required for a '
                         'flat operator list unless they can be discovered.')
    ap.add_argument('--fetch-dir',
                    help='Where pulled catalogs are kept, one subdirectory per '
                         'catalog index (default: <output-dir>/catalogs)')
    ap.add_argument('--refresh', action='store_true',
                    help='Pull catalogs again even when a previous pull '
                         'already covers the packages')
    ap.add_argument('-a', '--authfile',
                    help='Registry credentials for pulling catalogs (default: '
                         'the cluster pull secret, read with oc extract)')
    ap.add_argument('--filter-by-os', default='linux/amd64',
                    help='Platform of the catalog image to pull '
                         '(default: linux/amd64)')
    ap.add_argument('--jobs', type=int, default=4,
                    help='Catalog pulls to run at once (default: 4)')
    ap.add_argument('--insecure-registry', action='store_true',
                    help='Allow pulling catalogs over HTTP or with an '
                         'untrusted certificate')
    ap.add_argument('-d', '--output-dir', default='.',
                    help='Where to write html/ and imageset-config.yaml '
                         '(default: .)')
    ap.add_argument('--imageset-out',
                    help='Where to write the oc-mirror ImageSetConfiguration '
                         '(default: <output-dir>/imageset-config.yaml)')
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
        cluster, groups = parse_payload(payload)
        images = [g['pull_image'] for g in groups]

        fetched = not (args.catalog_dir or images == [None])
        if not fetched:
            search_from = str(Path(args.input).parent) if args.input else None
            catalog_dir = discover_catalog_dir(args.catalog_dir, search_from)
            if not args.quiet:
                print(f"Catalogs: {catalog_dir}", file=sys.stderr)
            catalog_dirs = {img: catalog_dir for img in images}
        else:
            _, _, _, ocp_path = resolve_ocp_path(cluster)
            fetch_dir = args.fetch_dir or str(Path(args.output_dir) / 'catalogs')
            if not args.quiet:
                print(f"Pulling catalogs into {fetch_dir}", file=sys.stderr)
            catalog_dirs = fetch_all(
                [{'pull_image': g['pull_image'],
                  'packages': [op['name'] for op in g['operators']]}
                 for g in groups],
                ocp_path, fetch_dir, args.authfile, args.refresh,
                args.filter_by_os, args.insecure_registry, args.quiet,
                args.jobs)

        plan = run(payload, catalog_dirs, args.output_dir, args.quiet,
                   args.imageset_out, fetched)
    except (ValueError, FileNotFoundError, FetchError) as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_ERROR

    out = json.dumps(plan, indent=2)
    print(out)
    if args.json_out:
        Path(args.json_out).write_text(out)

    if not args.quiet:
        print(f"\nSummary: {plan['summary_html']}", file=sys.stderr)
        if 'mirror' in plan:
            print(f"oc-mirror: {plan['mirror']['imageset_config']}",
                  file=sys.stderr)

    if plan['verdict'] == 'blocked':
        return EXIT_BLOCKED
    if plan.get('mirror', {}).get('verdict') == CRITICAL:
        return EXIT_CRITICAL
    if plan['verdict'] == 'manual_review':
        return EXIT_REVIEW
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
