#!/usr/bin/env python3
"""
OCP cluster upgrade planner for OLM operators.

Plans operator upgrades around an OpenShift cluster upgrade, across one catalog
per OCP release. Generic: no operator-specific logic anywhere.

Model
-----
An OCP upgrade is a sequence of single-release hops:

    EUS      current -> current+1 -> current+2   (target = current+2)
    other    current -> current+1                (target = current+1)

For a hop from OCP N to N+1 the operator must sit at a (channel, version) that
is present in BOTH catalogs, because the cluster runs N when the hop starts and
N+1 when it finishes. That constraint is pairwise, not global: an operator may
be moved again while the cluster sits at an intermediate release.

This matters. Version-pinned operators (odf-operator, lvms-operator, ...) carry
only stable-<N-1> and stable-<N> in each catalog, so no single channel exists in
all three catalogs of an EUS jump. A global intersection would call them blocked;
the pairwise model produces the stepped plan that is actually used in the field.

Each hop yields a phase, plus a final phase that takes the operator to latest on
the target release once the cluster upgrade is done.

Objective, in order:
    1. fewest hops before the cluster can move (unblock the cluster fast)
    2. among equal-hop options, the highest channel and version (fewer upgrades
       overall across cluster and operators)
"""

import json
import re
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from packaging import version as pkg_version


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def normalize_version(ver) -> str:
    if not ver:
        return ""
    ver = str(ver)
    return ver[1:] if ver.startswith('v') else ver


def extract_version_from_name(full_name) -> Optional[str]:
    """Extract a version from 'package.v4.18.3' - works for any operator."""
    if not full_name:
        return None
    parts = str(full_name).split('.')
    for i, part in enumerate(parts):
        if part.startswith('v') and len(part) > 1 and part[1].isdigit():
            ver = '.'.join(parts[i:])
            if '-' in ver:
                ver = ver.split('-')[0]
            return normalize_version(ver)
    return None


def safe_parse_version(ver_str):
    try:
        if not ver_str:
            return None
        return pkg_version.parse(normalize_version(ver_str))
    except Exception:
        return None


def parse_skip_range(skip_range: str) -> Tuple[Optional[str], Optional[str]]:
    if not skip_range:
        return None, None
    lo = re.search(r'(>=|>)\s*([\d\.\-\w]+)', skip_range)
    hi = re.search(r'(<=|<)\s*([\d\.\-\w]+)', skip_range)
    return (re.sub(r'-.*$', '', lo.group(2)) if lo else None,
            re.sub(r'-.*$', '', hi.group(2)) if hi else None)


def version_in_skip_range(version_obj, skip_range: str) -> bool:
    if not skip_range or version_obj is None:
        return False
    lo, hi = parse_skip_range(skip_range)
    if not lo and not hi:
        return False
    if lo:
        lo_obj = safe_parse_version(lo)
        if lo_obj is not None and version_obj < lo_obj:
            return False
    if hi:
        hi_obj = safe_parse_version(hi)
        if hi_obj is not None and version_obj >= hi_obj:
            return False
    return True


# ---------------------------------------------------------------------------
# OCP release helpers
# ---------------------------------------------------------------------------

def parse_ocp(ver: str) -> Tuple[int, int]:
    m = re.match(r'^v?(\d+)\.(\d+)', str(ver).strip())
    if not m:
        raise ValueError(f"Cannot parse OCP version: {ver}")
    return int(m.group(1)), int(m.group(2))


def format_ocp(major: int, minor: int) -> str:
    return f"{major}.{minor}"


def catalog_filename(ocp: str) -> str:
    major, minor = parse_ocp(ocp)
    return f"data-v{major}_{minor}.json"


def build_ocp_path(current: str, target: str, channel: str) -> List[str]:
    """
    Build the sequence of OCP releases the cluster passes through.

    EUS jumps two releases, everything else jumps one. The channel decides the
    expected span; the target is authoritative and is validated against it.
    """
    cmaj, cmin = parse_ocp(current)
    tmaj, tmin = parse_ocp(target)

    if tmaj != cmaj:
        raise ValueError(
            f"Cross-major upgrade {current} -> {target} is not supported")
    if tmin <= cmin:
        raise ValueError(
            f"Target {target} must be newer than current {current}")

    span = tmin - cmin
    is_eus = str(channel).strip().lower() == 'eus'
    expected = 2 if is_eus else 1

    if span != expected:
        kind = 'EUS' if is_eus else f"'{channel}'"
        raise ValueError(
            f"{kind} upgrade expects a {expected}-release jump, "
            f"but {current} -> {target} spans {span}")

    return [format_ocp(cmaj, m) for m in range(cmin, tmin + 1)]


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------

def load_catalog(path: str) -> Dict[str, Dict[str, Dict[str, Dict]]]:
    """
    Parse an OLM catalog into {package: {channel: {version: entry}}}.

    Presence is modelled as an explicit version set rather than a min/max floor,
    because channels are not always contiguous - some releases drop a version
    from the middle of a channel.
    """
    content = Path(path).read_text()
    if not content.strip():
        raise ValueError(f"Catalog is empty: {path}")

    blocks = content.strip().split('}\n{')
    catalog: Dict[str, Dict[str, Dict[str, Dict]]] = {}

    for i, block in enumerate(blocks):
        if i == 0:
            if not block.startswith('{'):
                block = '{' + block
        else:
            block = '{' + block
        if i < len(blocks) - 1:
            block = block + '}'
        elif not block.endswith('}'):
            block = block + '}'

        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue

        pkg = data.get('package')
        chan = data.get('name')
        if not pkg or not chan:
            continue

        bucket = catalog.setdefault(pkg, {}).setdefault(chan, {})
        for entry in data.get('entries', []):
            ver = extract_version_from_name(entry.get('name', ''))
            if ver and safe_parse_version(ver) is not None:
                # keep whichever entry carries a skipRange
                if ver not in bucket or (not bucket[ver].get('skipRange')
                                         and entry.get('skipRange')):
                    bucket[ver] = entry

    return catalog


def discover_catalog_dir(explicit: Optional[str] = None,
                         search_from: Optional[str] = None) -> str:
    """
    Locate the directory holding the data-v<major>_<minor>.json catalogs.

    An explicit path always wins. Otherwise the conventional locations are
    tried in order: a 'data' directory, then the directory itself. Each
    candidate is checked against the input file's directory first (when given),
    then the current working directory.
    """
    if explicit:
        path = Path(explicit)
        if not path.is_dir():
            raise FileNotFoundError(f"Catalog directory not found: {explicit}")
        if not find_catalogs(str(path)):
            raise FileNotFoundError(
                f"No data-v<major>_<minor>.json files in {explicit}")
        return str(path)

    roots = []
    if search_from:
        roots.append(Path(search_from))
    roots.append(Path.cwd())

    for root in roots:
        for candidate in (root / 'data', root):
            if candidate.is_dir() and find_catalogs(str(candidate)):
                return str(candidate)

    tried = ", ".join(str(r / 'data') + " and " + str(r) for r in roots)
    raise FileNotFoundError(
        "Could not find any data-v<major>_<minor>.json catalogs. "
        f"Looked in: {tried}. Pass --catalog-dir to point at them.")


def find_catalogs(catalog_dir: str) -> Dict[str, str]:
    """Map every OCP release found in a directory to its catalog file path."""
    found = {}
    directory = Path(catalog_dir)
    if not directory.is_dir():
        return found
    for path in directory.glob('data-v*.json'):
        m = re.match(r'^data-v(\d+)_(\d+)\.json$', path.name)
        if m:
            found[f"{int(m.group(1))}.{int(m.group(2))}"] = str(path)
    return found


def load_catalogs(catalog_dir: str, ocp_path: List[str]) -> Dict[str, Dict]:
    catalogs = {}
    missing = []
    for ocp in ocp_path:
        path = Path(catalog_dir) / catalog_filename(ocp)
        if not path.exists():
            missing.append(catalog_filename(ocp))
            continue
        catalogs[ocp] = load_catalog(str(path))
    if missing:
        available = sorted(find_catalogs(catalog_dir),
                           key=lambda v: parse_ocp(v))
        raise FileNotFoundError(
            f"Missing catalog file(s) in {catalog_dir}: {', '.join(missing)}. "
            f"Available releases: {', '.join(available) if available else 'none'}")
    return catalogs


# ---------------------------------------------------------------------------
# Per-catalog operator view
# ---------------------------------------------------------------------------

def tuples_in(catalog: Dict, pkg: str) -> set:
    """Every (channel, version) pair for a package in one catalog."""
    out = set()
    for chan, versions in catalog.get(pkg, {}).items():
        for ver in versions:
            out.add((chan, ver))
    return out


def is_version_pinned(catalogs: Dict[str, Dict], pkg: str) -> bool:
    """
    True when the operator's version stream tracks the OCP release, e.g.
    odf-operator 4.18.x on OCP 4.18. Detected generically by checking whether
    each catalog carries versions matching its own OCP release.
    """
    hits = 0
    for ocp, catalog in catalogs.items():
        prefix = ocp + '.'
        for versions in catalog.get(pkg, {}).values():
            if any(v.startswith(prefix) for v in versions):
                hits += 1
                break
    return hits >= max(2, len(catalogs) - 1)


def find_monotonicity_gaps(catalogs: Dict[str, Dict], ocp_path: List[str],
                           pkg: str) -> List[Dict]:
    """
    Detect tuples present at both ends of the path but absent in the middle.

    Such a tuple would strand a cluster mid-upgrade. These cases are rare and
    are reported for manual review rather than being planned around.
    """
    gaps = []
    if len(ocp_path) < 3:
        return gaps

    first, last = ocp_path[0], ocp_path[-1]
    both = tuples_in(catalogs[first], pkg) & tuples_in(catalogs[last], pkg)
    for mid in ocp_path[1:-1]:
        mid_tuples = tuples_in(catalogs[mid], pkg)
        for chan, ver in sorted(both - mid_tuples):
            gaps.append({'channel': chan, 'version': ver,
                         'present_in': [first, last], 'absent_in': mid})
    return gaps


# ---------------------------------------------------------------------------
# Shortest path search within one catalog
# ---------------------------------------------------------------------------

def _reachable_from(catalog: Dict, pkg: str, chan: str, ver: str) -> List[Tuple[str, str, str]]:
    """
    One upgrade hop from (chan, ver). Returns (channel, version, reason).

    A hop may land in any channel: switching channel as part of an upgrade costs
    nothing extra operationally, so hops are counted by upgrade, not by whether
    the channel label changed.
    """
    ver_obj = safe_parse_version(ver)
    out = []
    if ver_obj is None:
        return out

    for to_chan, versions in catalog.get(pkg, {}).items():
        for to_ver, entry in versions.items():
            to_obj = safe_parse_version(to_ver)
            if to_obj is None or to_obj <= ver_obj:
                continue
            skip_range = entry.get('skipRange', '') or ''
            if skip_range and version_in_skip_range(ver_obj, skip_range):
                out.append((to_chan, to_ver, 'skipRange'))
                continue
            replaces = extract_version_from_name(entry.get('replaces', ''))
            if replaces and replaces == ver:
                out.append((to_chan, to_ver, 'replaces'))
    return out


def _same_version_channels(catalog: Dict, pkg: str, ver: str) -> List[str]:
    """Channels that carry this exact version - switching between them is free."""
    return [c for c, versions in catalog.get(pkg, {}).items() if ver in versions]


def _rank(node: Tuple[str, str]):
    """Higher is better: compare version first, then channel name."""
    chan, ver = node
    return (safe_parse_version(ver), chan)


def shortest_path_to(catalog: Dict, pkg: str, start: Tuple[str, str],
                     allowed: set, prefer_highest: bool = True
                     ) -> Optional[List[Dict]]:
    """
    Fewest-hop path from start to any (channel, version) in `allowed`.

    Channel switches at the same version cost zero hops, so this is a 0-1 BFS.
    Among equal-cost destinations the highest is chosen, per the objective of
    minimising total upgrades over the whole exercise.
    """
    if not allowed:
        return None
    if start in allowed:
        return []

    dist = {start: 0}
    prev: Dict[Tuple[str, str], Tuple[Tuple[str, str], str]] = {}
    dq = deque([start])
    best: Optional[Tuple[str, str]] = None
    best_cost = None

    while dq:
        node = dq.popleft()
        cost = dist[node]

        if best_cost is not None and cost > best_cost:
            break

        if node in allowed:
            if best is None or cost < best_cost or (
                    cost == best_cost and prefer_highest
                    and _rank(node) > _rank(best)):
                best, best_cost = node, cost
            continue

        chan, ver = node

        # zero-cost: same version, different channel
        for alt_chan in _same_version_channels(catalog, pkg, ver):
            alt = (alt_chan, ver)
            if alt not in dist or dist[alt] > cost:
                dist[alt] = cost
                prev[alt] = (node, 'channel-switch')
                dq.appendleft(alt)

        # cost 1: an actual upgrade
        for to_chan, to_ver, reason in _reachable_from(catalog, pkg, chan, ver):
            nxt = (to_chan, to_ver)
            if nxt not in dist or dist[nxt] > cost + 1:
                dist[nxt] = cost + 1
                prev[nxt] = (node, reason)
                dq.append(nxt)

    if best is None:
        return None

    # reconstruct
    steps = []
    node = best
    while node != start:
        parent, reason = prev[node]
        steps.append({
            'from_channel': parent[0], 'from_version': parent[1],
            'to_channel': node[0], 'to_version': node[1],
            'via': reason,
        })
        node = parent
    steps.reverse()
    return steps


def highest_tuple(catalog: Dict, pkg: str) -> Optional[Tuple[str, str]]:
    tuples = tuples_in(catalog, pkg)
    if not tuples:
        return None
    return max(tuples, key=_rank)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def plan_operator(catalogs: Dict[str, Dict], ocp_path: List[str],
                  name: str, channel: str, version: str) -> Dict:
    """Build the full phase plan for one operator."""
    version = normalize_version(version)
    result = {
        'operator': name,
        'input': {'channel': channel, 'version': version},
        'ocp_path': ocp_path,
        'version_pinned': False,
        'phases': [],
        'notes': [],
        'verdict': 'no_action_required',
        'blocking': False,
    }

    current_catalog = catalogs[ocp_path[0]]

    if name not in current_catalog:
        result['verdict'] = 'manual_review'
        result['notes'].append(
            f"Operator '{name}' is not present in the {ocp_path[0]} catalog. "
            f"It may be a third-party operator, which this tool does not "
            f"diagnose. Check manually.")
        return result

    if channel not in current_catalog[name]:
        available = ', '.join(sorted(current_catalog[name].keys()))
        result['verdict'] = 'manual_review'
        result['notes'].append(
            f"Channel '{channel}' does not exist for '{name}' in the "
            f"{ocp_path[0]} catalog. Available: {available}. Check manually.")
        return result

    if version not in current_catalog[name][channel]:
        result['verdict'] = 'manual_review'
        result['notes'].append(
            f"Version {version} is not present in channel '{channel}' of the "
            f"{ocp_path[0]} catalog. Check manually.")
        return result

    result['version_pinned'] = is_version_pinned(catalogs, name)

    gaps = find_monotonicity_gaps(catalogs, ocp_path, name)
    for gap in gaps:
        result['notes'].append(
            f"Non-monotonic catalog: {gap['channel']} {gap['version']} is "
            f"present in {' and '.join(gap['present_in'])} but absent in "
            f"{gap['absent_in']}. Excluded from planning - verify manually.")
    excluded = {(g['channel'], g['version']) for g in gaps}

    current = (channel, version)

    # One phase per cluster hop
    for i in range(len(ocp_path) - 1):
        on_ocp, next_ocp = ocp_path[i], ocp_path[i + 1]
        allowed = (tuples_in(catalogs[on_ocp], name)
                   & tuples_in(catalogs[next_ocp], name)) - excluded

        phase = {
            'phase': len(result['phases']) + 1,
            'kind': 'pre-upgrade',
            'on_ocp': on_ocp,
            'satisfies': [on_ocp, next_ocp],
            'from': {'channel': current[0], 'version': current[1]},
        }

        if not allowed:
            phase.update({'to': phase['from'], 'hops': 0, 'steps': [],
                          'status': 'blocked'})
            result['phases'].append(phase)
            result['verdict'] = 'blocked'
            result['blocking'] = True
            result['notes'].append(
                f"No channel/version of '{name}' exists in both the {on_ocp} "
                f"and {next_ocp} catalogs. The cluster cannot move from "
                f"{on_ocp} to {next_ocp} with this operator installed.")
            return result

        steps = shortest_path_to(catalogs[on_ocp], name, current, allowed)

        if steps is None:
            # A valid target exists but nothing reachable lands on it. This is
            # usually a catalog anomaly - the reachable head was retired in the
            # next release - so it is flagged for manual review rather than
            # being called a hard block.
            reachable = shortest_path_to(
                catalogs[on_ocp], name, current,
                tuples_in(catalogs[on_ocp], name) - {current})
            near = ""
            if reachable:
                last = reachable[-1]
                near = (f" The furthest reachable point is "
                        f"{last['to_version']} in channel {last['to_channel']}, "
                        f"which is absent from the {next_ocp} catalog.")

            phase.update({'to': phase['from'], 'hops': 0, 'steps': [],
                          'status': 'unreachable'})
            result['phases'].append(phase)
            result['verdict'] = 'manual_review'
            result['notes'].append(
                f"On OCP {on_ocp}, '{name}' at {current[1]} (channel "
                f"{current[0]}) cannot reach any channel/version valid in both "
                f"{on_ocp} and {next_ocp}.{near} Valid targets do exist "
                f"({', '.join(f'{c}/{v}' for c, v in sorted(allowed)[:4])}"
                f"{', ...' if len(allowed) > 4 else ''}) but no upgrade edge "
                f"leads to them. Verify manually.")
            return result

        if steps:
            current = (steps[-1]['to_channel'], steps[-1]['to_version'])
            phase['status'] = 'upgrade_required'
            result['verdict'] = 'operator_upgrade_required'
        else:
            phase['status'] = 'no_action'

        phase.update({
            'to': {'channel': current[0], 'version': current[1]},
            'hops': sum(1 for s in steps if s['via'] != 'channel-switch'),
            'steps': steps,
        })
        result['phases'].append(phase)

    # Final phase: on the target release, go to latest
    target_ocp = ocp_path[-1]
    target_catalog = catalogs[target_ocp]
    highest = highest_tuple(target_catalog, name)

    final = {
        'phase': len(result['phases']) + 1,
        'kind': 'post-upgrade',
        'on_ocp': target_ocp,
        'satisfies': [target_ocp],
        'from': {'channel': current[0], 'version': current[1]},
    }

    if highest and highest != current:
        steps = shortest_path_to(target_catalog, name, current, {highest})
        if steps:
            current = highest
            final['status'] = 'upgrade_available'
        else:
            steps = []
            final['status'] = 'no_action'
            result['notes'].append(
                f"Latest {highest[1]} (channel {highest[0]}) is not reachable "
                f"from {final['from']['version']} on {target_ocp}.")
    else:
        steps = []
        final['status'] = 'no_action'

    final.update({
        'to': {'channel': current[0], 'version': current[1]},
        'hops': sum(1 for s in steps if s['via'] != 'channel-switch'),
        'steps': steps,
    })
    result['phases'].append(final)

    return result


def plan_cluster(catalog_dir: str, payload: Dict) -> Dict:
    """Plan every operator for one cluster upgrade."""
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
    catalogs = load_catalogs(catalog_dir, ocp_path)

    results = []
    for op in operators:
        name = op.get('name')
        if not name:
            raise ValueError("each operator requires a 'name'")
        results.append(plan_operator(
            catalogs, ocp_path, name,
            op.get('channel', ''), op.get('version', '')))

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

    return {
        'cluster': {'current': current, 'target': target,
                    'channel': channel, 'ocp_path': ocp_path},
        'verdict': verdict,
        'blocking_operators': blocking,
        'manual_review_operators': review,
        'operators_requiring_upgrade': upgrades,
        'operators': results,
        'catalogs': {o: catalog_filename(o) for o in ocp_path},
    }
