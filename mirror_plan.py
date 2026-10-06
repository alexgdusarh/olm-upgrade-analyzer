#!/usr/bin/env python3
"""
Catalog mirroring plan for disconnected clusters.

Customers mirror only their own operators, and usually only from the target
release's catalog. That is enough when every installed operator can be upgraded
straight from the target catalog. When it cannot - typically a release-pinned
operator on an EUS jump, whose target-release bundles only skip from the
previous release - the operator is reported CRITICAL and the intermediate
catalog that bridges the gap is identified, so it can be mirrored and deployed
as well.

The search reuses the planner's upgrade edges (replaces, skips, skipRange).
Catalogs are tried smallest set first: the target alone, then one extra
catalog (closest to the target first), then two, and so on. Within a set the
operator is moved through the catalogs in release order with the fewest
upgrades.

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
    parse_ocp,
    release_channel,
    tuples_in,
    version_sort_key,
)

OK = 'ok'
CRITICAL = 'critical'
UNRESOLVED = 'unresolved'


def mirror_goal(catalogs: Dict[str, Dict], ocp_path: List[str], pkg: str,
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


def check_operator(catalogs: Dict[str, Dict], ocp_path: List[str], pkg: str,
                   start: Tuple[str, str],
                   max_ocp_version: str = '') -> Dict:
    """Decide which catalogs one operator needs mirrored, and what from each."""
    target = ocp_path[-1]
    result = {
        'operator': pkg,
        'installed': {'channel': start[0], 'version': start[1]},
        'status': OK,
        'catalogs': [target],
        'steps': [],
        'notes': [],
    }

    if max_ocp_version:
        try:
            if parse_ocp(max_ocp_version) < parse_ocp(target):
                result['notes'].append(
                    f"The installed bundle declares olm.maxOpenShiftVersion "
                    f"{max_ocp_version}, so the cluster cannot upgrade past "
                    f"{max_ocp_version} until '{pkg}' is upgraded.")
        except ValueError:
            pass

    if not tuples_in(catalogs[target], pkg):
        result['status'] = CRITICAL
        result['notes'].append(
            f"CRITICAL: '{pkg}' is not in the {target} catalog. It has been "
            f"removed or renamed, and must be replaced or uninstalled before "
            f"the cluster reaches {target}.")
        return result

    goal = mirror_goal(catalogs, ocp_path, pkg, start)

    # The latest bundle first; failing that, the highest one the operator can
    # reach at all, since its own stream may never lead to the overall latest.
    for wanted in (goal, None):
        for extra in _candidate_sets(ocp_path):
            stages = [(o, catalogs[o]) for o in extra + [target]]
            steps = _staged_path(stages, pkg, start, wanted)
            if steps is None:
                continue
            end = ((steps[-1]['to_channel'], steps[-1]['to_version'])
                   if steps else start)
            result['goal'] = {'channel': end[0], 'version': end[1]}
            result['steps'] = steps
            result['catalogs'] = extra + [target]
            if wanted is None:
                result['notes'].append(
                    f"The latest {goal[1]} (channel {goal[0]}) is not "
                    f"reachable from {start[1]}; {end[1]} (channel {end[0]}) "
                    f"is the highest that is.")
            if extra:
                result['status'] = CRITICAL
                result['notes'].append(_critical_note(
                    pkg, start, target, extra, ocp_path[0]))
            return result

    result['status'] = UNRESOLVED
    result['notes'].append(
        f"CRITICAL: no combination of the {', '.join(ocp_path)} catalogs "
        f"offers any upgrade of '{pkg}' from {start[1]} (channel {start[0]}) "
        f"into the {target} catalog. Check manually.")
    return result


def _critical_note(pkg, start, target, extra, current) -> str:
    names = ' and '.join(extra)
    plural = len(extra) > 1
    note = (f"CRITICAL: '{pkg}' {start[1]} (channel {start[0]}) cannot be "
            f"upgraded from the {target} catalog alone. The {names} "
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
