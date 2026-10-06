#!/usr/bin/env python3
"""
Operator upgrade and catalog mirroring plan, worked backwards from the target.

Customers mirror only their own operators, and only from the target release's
catalog, so every operator is judged against that catalog first:

    1. The target catalog covers the installed version when it still ships it,
       or has an upgrade edge from it (skipRange, replaces or skips). The
       shortest path to the latest bundle is taken from that catalog.
    2. When it does not, earlier catalogs are added, working backwards from
       the target: one extra catalog first, closest to the target first (4.19
       on a 4.18 to 4.20 EUS path, then 4.18), then two. That intermediate
       catalog must be mirrored and deployed as well, and the operator
       upgraded from it before the target catalog takes over.

Verdicts:

    no_action_required             the target catalog still ships the installed
                                   bundle, and its max_ocp_version reaches the
                                   target
    operator_upgrade_required      covered by the target catalog, but the
                                   installed channel/version is gone from it, or
                                   its max_ocp_version is below the target
    intermediate_catalog_required  the target catalog does not cover it (CRITICAL)
    blocked                        the package is gone from the target catalog
    manual_review                  not identified, or no catalog combination
                                   covers it

From the result an oc-mirror ImageSetConfiguration is produced, listing per
catalog version the packages, channels and version ranges to mirror.
"""

from collections import deque
from itertools import combinations
from typing import Dict, List, Optional, Tuple

from ocp_planner import (
    _rank,
    _reachable_from,
    _same_version_channels,
    highest_tuple,
    is_version_pinned,
    normalize_version,
    parse_ocp,
    release_channel,
    resolve_installed,
    tuples_in,
    version_sort_key,
)

NO_ACTION = 'no_action_required'
UPGRADE = 'operator_upgrade_required'
INTERMEDIATE = 'intermediate_catalog_required'
BLOCKED = 'blocked'
REVIEW = 'manual_review'


def target_goal(catalogs: Dict[str, Dict], ocp_path: List[str], pkg: str,
                start: Tuple[str, str]) -> Optional[Tuple[str, str]]:
    """Where the operator should end up in the target catalog."""
    target = ocp_path[-1]
    if is_version_pinned(catalogs, pkg):
        goal = release_channel(catalogs[target], pkg, target, prefer=start[0])
        if goal:
            return goal
    return highest_tuple(catalogs[target], pkg)

def _staged_path(stages: List[Tuple[str, Dict]], pkg: str,
                 start: Tuple[str, str], goal: Optional[Tuple[str, str]]
                 ) -> Optional[List[Dict]]:
    """
    Fewest upgrades from start to goal, moving through the catalogs in order.
    With no goal, the highest bundle reachable in the last catalog is used.

    A node is (stage, channel, version). Within a stage the operator upgrades
    along that catalog's edges; moving on to the next catalog is free, since
    the installed bundle stays put while the catalog source is replaced.
    Returns the steps, each tagged with the catalog it is taken from.
    """
    last = len(stages) - 1
    first = (0,) + start
    dist = {first: 0}
    prev = {}
    dq = deque([first])
    found = None

    while dq:
        node = dq.popleft()
        stage, chan, ver = node
        cost = dist[node]
        if stage == last and goal is not None and (chan, ver) == goal:
            found = node
            break
        catalog = stages[stage][1]

        moves = [((stage, c, ver), 0, 'channel-switch')
                 for c in _same_version_channels(catalog, pkg, ver) if c != chan]
        if stage < last:
            moves.append(((stage + 1, chan, ver), 0, 'next-catalog'))
        moves += [((stage, c, v), 1, why)
                  for c, v, why in _reachable_from(catalog, pkg, chan, ver)]

        for nxt, step, why in moves:
            if nxt not in dist or dist[nxt] > cost + step:
                dist[nxt] = cost + step
                prev[nxt] = (node, why)
                (dq.appendleft if step == 0 else dq.append)(nxt)

    if goal is None:
        # Only bundles the last catalog actually carries count as a landing
        # point; the start counts when that catalog still ships it.
        present = tuples_in(stages[last][1], pkg)
        ends = [n for n in dist if n[0] == last and n[1:] in present]
        if ends:
            found = max(ends, key=lambda n: (_rank(n[1:]), -dist[n]))
    if found is None:
        return None

    steps = []
    node = found
    while node != first:
        parent, why = prev[node]
        if why != 'next-catalog':
            steps.append({'catalog': stages[node[0]][0],
                          'from_channel': parent[1], 'from_version': parent[2],
                          'to_channel': node[1], 'to_version': node[2],
                          'via': why})
        node = parent
    steps.reverse()
    return steps


def _candidate_sets(ocp_path: List[str]):
    """Extra catalogs to try, fewest first, closest to the target first."""
    extra = list(reversed(ocp_path[:-1]))
    for size in range(len(extra) + 1):
        for combo in combinations(extra, size):
            yield sorted(combo, key=parse_ocp)


def _search(catalogs: Dict[str, Dict], ocp_path: List[str], pkg: str,
            start: Tuple[str, str], goal: Tuple[str, str]):
    """
    Fewest catalogs first, then the fewest upgrades: (extra catalogs, steps).

    Within a set of catalogs the latest bundle is preferred; failing that, the
    highest one the operator can reach at all, since its own stream may never
    lead to the overall latest. None when no set covers the operator.
    """
    target = ocp_path[-1]
    for extra in _candidate_sets(ocp_path):
        stages = [(o, catalogs[o]) for o in extra + [target]]
        for wanted in (goal, None):
            steps = _staged_path(stages, pkg, start, wanted)
            if steps is not None:
                return extra, steps
    return None


def _max_ocp_below(max_ocp_version: str, target: str) -> bool:
    if not max_ocp_version:
        return False
    try:
        return parse_ocp(max_ocp_version) < parse_ocp(target)
    except ValueError:
        return False


def _phases(steps: List[Dict], catalogs_used: List[str],
            start: Tuple[str, str]) -> List[Dict]:
    """One phase per catalog the operator is upgraded from, in order."""
    phases = []
    current = start
    for ocp in catalogs_used:
        mine = [s for s in steps if s['catalog'] == ocp]
        end = ((mine[-1]['to_channel'], mine[-1]['to_version'])
               if mine else current)
        phases.append({
            'phase': len(phases) + 1,
            'kind': 'target' if ocp == catalogs_used[-1] else 'intermediate',
            'on_ocp': ocp,
            'status': 'upgrade_required' if mine else 'no_action',
            'from': {'channel': current[0], 'version': current[1]},
            'to': {'channel': end[0], 'version': end[1]},
            'hops': sum(1 for s in mine if s['via'] != 'channel-switch'),
            'steps': mine,
        })
        current = end
    return phases


def plan_operator(catalogs: Dict[str, Dict], ocp_path: List[str],
                  op: Dict) -> Dict:
    """Plan one installed operator against the target catalog first."""
    target = ocp_path[-1]
    result = {
        'operator': op['name'],
        'input': {'channel': op['channel'],
                  'version': normalize_version(op['version'])},
        'max_ocp_version': op.get('max_ocp_version') or '',
        'verdict': REVIEW,
        'blocking': False,
        'version_pinned': False,
        'catalogs': [],
        'phases': [],
        'steps': [],
        'notes': [],
    }

    installed, notes = resolve_installed(
        catalogs, ocp_path, op['name'], op['channel'], op['version'])
    result['notes'] += notes
    if installed is None:
        return result

    pkg, chan, ver = installed
    start = (chan, ver)
    result['operator'] = pkg
    if chan != op['channel']:
        result['input']['resolved_channel'] = chan
    if ver != result['input']['version']:
        result['input']['resolved_version'] = ver
    result['version_pinned'] = is_version_pinned(catalogs, pkg)

    too_old = _max_ocp_below(result['max_ocp_version'], target)
    if too_old:
        result['notes'].append(
            f"The installed bundle declares olm.maxOpenShiftVersion "
            f"{result['max_ocp_version']}, below the {target} target. It must "
            f"be upgraded from the {target} catalog.")

    if not tuples_in(catalogs[target], pkg):
        result.update(verdict=BLOCKED, blocking=True)
        result['notes'].append(
            f"'{pkg}' is not in the {target} catalog. It has been removed or "
            f"renamed, and must be replaced or uninstalled before the cluster "
            f"reaches {target}.")
        return result

    goal = target_goal(catalogs, ocp_path, pkg, start)
    found = _search(catalogs, ocp_path, pkg, start, goal)
    if found is None:
        result['notes'].append(
            f"No combination of the {', '.join(ocp_path)} catalogs covers "
            f"'{pkg}' {ver} (channel {chan}) for the {target} catalog. "
            f"Check manually.")
        return result

    extra, steps = found
    end = (steps[-1]['to_channel'], steps[-1]['to_version']) if steps else start
    in_target = start in tuples_in(catalogs[target], pkg)

    if extra:
        verdict = INTERMEDIATE
        result['notes'].append(_critical_note(
            pkg, start, target, extra, ocp_path[0]))
    elif in_target and not too_old:
        # Still shipped by the target catalog: nothing has to move. The
        # latest is reported, and only the installed bundle is mirrored.
        verdict = NO_ACTION
        if end[1] != ver:
            result['notes'].append(
                f"{end[1]} (channel {end[0]}) is available in the {target} "
                f"catalog; upgrading is optional.")
        steps, end = [], start
    elif not steps:
        verdict = REVIEW
        result['notes'].append(
            f"'{pkg}' must be upgraded, but the {target} catalog has nothing "
            f"newer than {ver}. Check manually.")
    else:
        verdict = UPGRADE
        if not in_target:
            result['notes'].append(
                f"{ver} (channel {chan}) is not in the {target} catalog, which "
                f"upgrades it to {end[1]} (channel {end[0]}).")
        if end != goal:
            result['notes'].append(
                f"The latest {goal[1]} (channel {goal[0]}) is not reachable "
                f"from {ver}; {end[1]} (channel {end[0]}) is the highest that "
                f"is.")

    result.update(verdict=verdict, catalogs=extra + [target], steps=steps,
                  goal={'channel': end[0], 'version': end[1]},
                  phases=_phases(steps, extra + [target], start))
    return result


def _critical_note(pkg, start, target, extra, current) -> str:
    names = ' and '.join(extra)
    plural = len(extra) > 1
    note = (f"CRITICAL: the {target} catalog does not cover '{pkg}' "
            f"{start[1]} (channel {start[0]}). The {names} "
            f"catalog{'s' if plural else ''} must also be mirrored and "
            f"deployed, and the operator upgraded from "
            f"{'them' if plural else 'it'} first.")
    if current in extra:
        note += (f" {current} is the current release: its mirror must carry "
                 f"the versions listed, not only the installed one.")
    return note


def mirror_sets(result: Dict) -> Dict[str, Dict[str, List[str]]]:
    """
    {ocp: {channel: [versions]}} that must be mirrored for one operator.

    Every bundle the operator lands on is needed, including same-version
    channel switches. When no upgrade is needed the installed bundle itself is
    kept, so the package still exists in the mirrored catalog.
    """
    out: Dict[str, Dict[str, List[str]]] = {}
    for s in result['steps']:
        out.setdefault(s['catalog'], {}).setdefault(
            s['to_channel'], []).append(s['to_version'])
    goal = result.get('goal')
    if not result['catalogs']:
        return out
    target = result['catalogs'][-1]
    if goal and target not in out:
        out[target] = {goal['channel']: [goal['version']]}
    return out


# ---------------------------------------------------------------------------
# oc-mirror ImageSetConfiguration
# ---------------------------------------------------------------------------

def _q(value: str) -> str:
    """Quote a scalar so YAML never reads a version as a number."""
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def build_imageset(entries: List[Dict]) -> str:
    """
    Render an oc-mirror v2 ImageSetConfiguration.

    entries: [{'catalog': image ref, 'packages': [
                 {'name', 'channels': {channel: [versions] or None},
                  'defaultChannel': str or None, 'comment': str or None}]}]

    A channel mapped to None is mirrored without version bounds (its head).
    """
    lines = ["kind: ImageSetConfiguration",
             "apiVersion: mirror.openshift.io/v2alpha1",
             "mirror:",
             "  operators:" + ("" if entries else " []")]
    for entry in entries:
        lines.append(f"  - catalog: {entry['catalog']}")
        lines.append("    packages:")
        for pkg in entry['packages']:
            if pkg.get('comment'):
                lines.append(f"    # {pkg['comment']}")
            lines.append(f"    - name: {pkg['name']}")
            if pkg.get('defaultChannel'):
                lines.append(f"      defaultChannel: {pkg['defaultChannel']}")
            lines.append("      channels:")
            for chan in sorted(pkg['channels']):
                lines.append(f"      - name: {chan}")
                versions = pkg['channels'][chan]
                if versions:
                    ordered = sorted(set(versions), key=version_sort_key)
                    lines.append(f"        minVersion: {_q(ordered[0])}")
                    lines.append(f"        maxVersion: {_q(ordered[-1])}")
    return "\n".join(lines) + "\n"


def pick_default_channel(channels: Dict[str, Optional[List[str]]],
                         known_default: Optional[str]) -> Optional[str]:
    """
    oc-mirror needs the package's default channel among the mirrored ones.
    When it is not, the mirrored channel carrying the highest version takes
    its place.
    """
    if known_default and known_default in channels:
        return None

    def top(chan):
        versions = channels[chan] or []
        return max((version_sort_key(v) for v in versions),
                   default=version_sort_key('0'))

    return max(sorted(channels), key=top)
