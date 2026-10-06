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

Release-pinned operators, whose versions track the OCP release (odf-operator
4.18.x on OCP 4.18, nfd, kubevirt-hyperconverged, ...), are the exception. They
are upgraded with the cluster, so they follow the strict EUS path: each
release's own version, from that release's catalog, in turn - 4.19.z from the
4.19 catalog, then 4.20.z from the 4.20 catalog - even when a target bundle's
skipRange would allow the jump. The intermediate catalog is then always needed.

An installed bundle whose olm.maxOpenShiftVersion is below the target cannot
stay through the jump. When the target catalog covers it, a bundle valid on
every release of the path is looked for - present in every catalog on it, with
its own maxOpenShiftVersion reaching the target - and upgraded to from the
current catalog before the cluster upgrade, so the operator need not move
during the jump.

Verdicts:

    no_action_required             the target catalog still ships the installed
                                   bundle, and its max_ocp_version reaches the
                                   target
    operator_upgrade_required      covered by the target catalog, but the
                                   installed channel/version is gone from it, or
                                   its max_ocp_version is below the target
    intermediate_catalog_required  the target catalog does not cover it, or it
                                   is release-pinned on an EUS path (CRITICAL)
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
    _reachable_from,
    _same_version_channels,
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


def _keep_channel(chan: str):
    """
    Rank (channel, version): highest version first and, among channels
    carrying the same version, the subscribed one, so the operator is never
    moved to another channel just because its name sorts higher.
    """
    def key(node: Tuple[str, str]):
        return (version_sort_key(node[1]), node[0] == chan, node[0])
    return key


def target_goal(catalogs: Dict[str, Dict], ocp_path: List[str], pkg: str,
                start: Tuple[str, str]) -> Optional[Tuple[str, str]]:
    """Where the operator should end up in the target catalog."""
    target = ocp_path[-1]
    if is_version_pinned(catalogs, pkg):
        goal = release_channel(catalogs[target], pkg, target, prefer=start[0])
        if goal:
            return goal
    tuples = tuples_in(catalogs[target], pkg)
    return max(tuples, key=_keep_channel(start[0])) if tuples else None


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
            rank = _keep_channel(start[0])
            found = max(ends, key=lambda n: (rank(n[1:]), -dist[n]))
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


def _pinned_path(catalogs: Dict[str, Dict], ocp_path: List[str], pkg: str,
                 start: Tuple[str, str]):
    """
    The strict EUS path of a release-pinned operator: (catalogs, steps).

    For each release after the current one, the operator lands on that
    release's channel head using that release's catalog, or, failing that,
    the previous release's catalog first. None when a release has no version
    of its own for the operator, or its head cannot be reached.
    """
    steps, used, current = [], [], start
    for i, ocp in enumerate(ocp_path[1:], 1):
        head = release_channel(catalogs[ocp], pkg, ocp, prefer=current[0])
        if head is None:
            return None
        for names in ([ocp], [ocp_path[i - 1], ocp]):
            hop = _staged_path([(o, catalogs[o]) for o in names], pkg,
                               current, head)
            if hop is not None:
                break
        else:
            return None
        steps += hop
        used += [o for o in names if o not in used]
        current = head
    return sorted(used, key=parse_ocp), steps


def declared_max_ocp(bundle_max: Dict, ocp_path: List[str], pkg: str,
                     ver: str) -> Optional[str]:
    """A bundle's olm.maxOpenShiftVersion as recorded in any pulled catalog."""
    for o in reversed(ocp_path):
        top = ((bundle_max or {}).get(o, {}).get(pkg) or {}).get(ver)
        if top:
            return top
    return None


def _bridge(catalogs: Dict[str, Dict], ocp_path: List[str], pkg: str,
            start: Tuple[str, str], bundle_max: Dict):
    """
    A bundle valid on every release of the path, reachable from the installed
    one using the current catalog: (steps, (channel, version), its max).

    It must be present in every catalog on the path, and its own
    olm.maxOpenShiftVersion, if it declares one, must reach the target.
    bundle_max is {ocp: {package: {version: max}}}, recorded when the catalogs
    are pulled; without it nothing can be verified and None is returned. Fewest
    upgrades first, then the highest version.
    """
    target, current = ocp_path[-1], ocp_path[0]
    known = [bundle_max.get(o, {}).get(pkg) for o in ocp_path]
    if not any(known):
        return None

    common = set.intersection(*(tuples_in(catalogs[o], pkg) for o in ocp_path))
    best, best_cost = None, None
    for tup in sorted(common, key=_keep_channel(start[0]), reverse=True):
        top = declared_max_ocp(bundle_max, ocp_path, pkg, tup[1])
        try:
            if top and parse_ocp(top) < parse_ocp(target):
                continue
        except ValueError:
            continue
        steps = _staged_path([(current, catalogs[current])], pkg, start, tup)
        if not steps:
            continue
        cost = sum(1 for s in steps if s['via'] != 'channel-switch')
        if best is None or cost < best_cost:
            best, best_cost = (steps, tup, top), cost
    return best


def _max_ocp_below(max_ocp_version: str, target: str) -> bool:
    if not max_ocp_version:
        return False
    try:
        return parse_ocp(max_ocp_version) < parse_ocp(target)
    except ValueError:
        return False


def _phases(steps: List[Dict], catalogs_used: List[str],
            start: Tuple[str, str], reason: str = 'not_covered') -> List[Dict]:
    """One phase per catalog the operator is upgraded from, in order."""
    phases = []
    current = start
    for ocp in catalogs_used:
        mine = [s for s in steps if s['catalog'] == ocp]
        end = ((mine[-1]['to_channel'], mine[-1]['to_version'])
               if mine else current)
        phases.append({
            'phase': len(phases) + 1,
            'kind': ('target' if ocp == catalogs_used[-1] else
                     'current' if reason == 'bridge' else 'intermediate'),
            'reason': reason,
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
                  op: Dict, bundle_max: Optional[Dict] = None) -> Dict:
    """
    Plan one installed operator against the target catalog first.

    bundle_max is {ocp: {package: {version: olm.maxOpenShiftVersion}}} from
    the pulled catalogs, used to find a bundle valid across the whole path.
    """
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
    # Whether the pulled catalogs carry this package's bundle metadata, so
    # that a missing maxOpenShiftVersion means "none declared", not "unknown".
    result['bundle_metadata'] = any(
        pkg in (bundle_max or {}).get(o, {}) for o in ocp_path)
    if not result['max_ocp_version']:
        declared = declared_max_ocp(bundle_max, ocp_path, pkg, ver)
        if declared:
            result['max_ocp_version'] = declared
            result['notes'].append(
                f"The input has no max_ocp_version; the {ver} bundle in the "
                f"catalog declares olm.maxOpenShiftVersion {declared}.")

    too_old = _max_ocp_below(result['max_ocp_version'], target)
    if too_old:
        result['notes'].append(
            f"The installed bundle declares olm.maxOpenShiftVersion "
            f"{result['max_ocp_version']}, below the {target} target.")

    if not tuples_in(catalogs[target], pkg):
        result.update(verdict=BLOCKED, blocking=True)
        result['notes'].append(
            f"'{pkg}' is not in the {target} catalog. It has been removed or "
            f"renamed, and must be replaced or uninstalled before the cluster "
            f"reaches {target}.")
        return result

    goal = target_goal(catalogs, ocp_path, pkg, start)

    if result['version_pinned'] and len(ocp_path) > 2:
        pinned = _pinned_path(catalogs, ocp_path, pkg, start)
        if pinned is not None:
            used, steps = pinned
            chain = ' -> '.join(
                [ver] + [f"{s['to_version']} ({s['catalog']})" for s in steps
                         if s['via'] != 'channel-switch'])
            result['notes'].append(
                f"CRITICAL: '{pkg}' is release-pinned, so it is upgraded with "
                f"the cluster on the strict EUS path: {chain}. The "
                f"{' and '.join(used[:-1])} catalog must also be mirrored and "
                f"deployed.")
            end = (steps[-1]['to_channel'], steps[-1]['to_version'])
            result.update(verdict=INTERMEDIATE, catalogs=used, steps=steps,
                          goal={'channel': end[0], 'version': end[1]},
                          phases=_phases(steps, used, start, 'release_pinned'))
            return result
        result['notes'].append(
            f"'{pkg}' is release-pinned, but no step through each release's "
            f"own version was found; planned against the target catalog "
            f"instead. Verify manually.")

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

    bridge = (_bridge(catalogs, ocp_path, pkg, start, bundle_max or {})
              if too_old and not extra else None)

    if bridge:
        b_steps, b_tup, b_top = bridge
        current = ocp_path[0]
        result['notes'].append(
            f"Before the cluster upgrade, upgrade from the current {current} "
            f"catalog to {b_tup[1]} (channel {b_tup[0]}). It is in every "
            f"catalog on the path ({', '.join(ocp_path)}) and declares "
            f"{f'olm.maxOpenShiftVersion {b_top}' if b_top else 'no olm.maxOpenShiftVersion'}"
            f", so the operator stays valid through the whole upgrade. The "
            f"{current} and {target} mirrors must carry it.")
        if end[1] != b_tup[1]:
            result['latest'] = {'channel': end[0], 'version': end[1]}
            result['notes'].append(
                f"{end[1]} (channel {end[0]}) is available in the {target} "
                f"catalog once the cluster is there; upgrading is optional.")
        result.update(verdict=UPGRADE, catalogs=[current, target],
                      steps=b_steps,
                      goal={'channel': b_tup[0], 'version': b_tup[1]},
                      phases=_phases(b_steps, [current, target], start,
                                     'bridge'))
        return result

    if extra:
        verdict = INTERMEDIATE
        result['notes'].append(_critical_note(
            pkg, start, target, extra, ocp_path[0]))
    elif in_target and not too_old:
        # Still shipped by the target catalog: nothing has to move. The
        # latest is reported, and only the installed bundle is mirrored.
        verdict = NO_ACTION
        if end[1] != ver:
            result['latest'] = {'channel': end[0], 'version': end[1]}
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
        if too_old:
            result['notes'].append(
                f"No bundle valid on every release of the path was found, so "
                f"it is upgraded from the {target} catalog while the cluster "
                f"is still on {result['max_ocp_version']} or earlier.")
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


def column_checks(catalogs: Dict[str, Dict], ocp_path: List[str],
                  result: Dict) -> Dict[str, Dict]:
    """
    For each catalog the operator is not upgraded from, whether the bundle it
    is on by then is valid there, working through the path in order.

        max      the bundle declares olm.maxOpenShiftVersion
        present  no max declared, but this catalog ships the channel/version,
                 so it works here; a newer version in the channel is noted
        newer    not shipped here, but a newer version in its channel is: the
                 upgrade is shown
        missing  neither, and the metadata declares no max: a warning
        unknown  neither, and no bundle metadata is available: a warning
        upgrade  the bundle is not shipped here, but the target catalog's
                 planned version is: the planned upgrade happens by this
                 release, still from the target catalog's bundles, and the
                 target phase is marked done_on this release

    Also adds a warning note naming the releases left unvalidated.
    """
    pkg = result['operator']
    by_catalog = {ph['on_ocp']: ph for ph in result['phases']}
    inp = result['input']
    chan = inp.get('resolved_channel', inp['channel'])
    ver = inp.get('resolved_version', inp['version'])
    top = result.get('max_ocp_version') or None
    final = result['phases'][-1] if result['phases'] else None
    columns = {}
    for ocp in ocp_path:
        ph = by_catalog.get(ocp)
        if ph and ph.get('done_on'):
            continue
        if ph:
            if ph['to']['version'] != ph['from']['version']:
                top = ph['to'].get('max_ocp_version')
            chan, ver = ph['to']['channel'], ph['to']['version']
            continue
        versions = catalogs[ocp].get(pkg, {}).get(chan, {})
        newer = [v for v in versions
                 if version_sort_key(v) > version_sort_key(ver)]
        col = {'channel': chan, 'version': ver, 'max_ocp_version': top,
               'newer': max(newer, key=version_sort_key) if newer else None}
        planned = (final['to']['channel'], final['to']['version']) if final else None
        if top:
            col['state'] = 'max'
        elif ver in versions:
            col['state'] = 'present'
        elif (final and final['status'] == 'upgrade_required'
              and final['on_ocp'] == ocp_path[-1]
              and planned[1] in catalogs[ocp].get(pkg, {}).get(planned[0], {})):
            # The installed bundle is gone from this release's catalog but
            # the planned target version is here: upgrade by this release.
            col.update(state='upgrade', to_channel=planned[0],
                       to_version=planned[1], hops=final['hops'])
            final['done_on'] = ocp
            result['notes'].append(
                f"Upgrade while the cluster is on {ocp}: {ver} (channel "
                f"{chan}) is not in the {ocp} catalog, but the planned "
                f"{planned[1]} (channel {planned[0]}) is. The bundles still "
                f"come from the {ocp_path[-1]} catalog.")
            chan, ver = planned
            top = final['to'].get('max_ocp_version')
        elif newer:
            col['state'] = 'newer'
        else:
            col['state'] = ('missing' if result.get('bundle_metadata')
                            else 'unknown')
        columns[ocp] = col

    unvalidated = [o for o, c in columns.items()
                   if c['state'] in ('missing', 'unknown')]
    if unvalidated:
        why = ("the bundle declares no olm.maxOpenShiftVersion"
               if result.get('bundle_metadata')
               else "no bundle metadata is available (pull the catalogs "
                    "instead of --catalog-dir)")
        result['notes'].append(
            f"Warning: {why}, and neither the bundle nor a newer version of "
            f"its channel is in the {' and '.join(unvalidated)} "
            f"catalog{'s' if len(unvalidated) > 1 else ''}.")
    result['columns'] = columns
    return columns


def build_matrix(ocp_path: List[str], result: Dict) -> List[Dict]:
    """
    The operator's row of the summary matrix, one cell per catalog, oldest
    first, from its phases and column_checks.

    Cell kinds: upgrade (a planned upgrade), newer (a newer version of the
    channel, shown as an upgrade), no_action (optionally upgraded_on an earlier
    release), max, present, missing, unknown and not_found.

    A final pass removes duplicates: an upgrade to a (channel, version) the
    row already reaches in a lower column is kept there, and the higher column
    becomes no_action, upgraded_on the lower one. The target phase is marked
    done_on that release to match.
    """
    by_catalog = {ph['on_ocp']: ph for ph in result['phases']}
    columns = result.get('columns') or {}
    cells = []
    for ocp in ocp_path:
        ph, col = by_catalog.get(ocp), columns.get(ocp)
        if ph and ph.get('done_on'):
            cell = {'kind': 'no_action', 'upgraded_on': ph['done_on']}
        elif ph and ph['status'] == 'no_action':
            cell = {'kind': 'no_action'}
            if ocp == ocp_path[-1] and result.get('latest'):
                cell['available'] = dict(result['latest'],
                                         channel_changes=result['latest']['channel'] != ph['to']['channel'])
        elif ph:
            cell = {'kind': 'upgrade', 'channel': ph['to']['channel'],
                    'version': ph['to']['version'], 'hops': ph['hops']}
        elif col and col['state'] == 'upgrade':
            cell = {'kind': 'upgrade', 'channel': col['to_channel'],
                    'version': col['to_version'], 'hops': col['hops'],
                    'replaces_missing': col['version']}
        elif col and col['state'] == 'newer':
            cell = {'kind': 'newer', 'channel': col['channel'],
                    'version': col['newer'], 'from_version': col['version']}
        elif col:
            cell = {'kind': col['state'], 'channel': col['channel'],
                    'version': col['version'], 'newer': col['newer'],
                    'max_ocp_version': col['max_ocp_version']}
        else:
            cell = {'kind': 'not_found'}
        cell['ocp'] = ocp
        vendor = result.get('vendor')
        if vendor:
            cell['vendor'] = dict(vendor['releases'][ocp],
                                  label=vendor['label'],
                                  installed=vendor['installed'],
                                  inferred=vendor.get('installed_inferred'),
                                  status=vendor['status'])
            if ocp == ocp_path[0] and vendor.get('operator_ok') is False:
                cell['vendor']['operator_short'] = {
                    'installed': vendor['operator_version'],
                    'required': vendor['operator_min']}
        cells.append(cell)

    reached = {}  # (channel, version) -> lowest release showing it
    for cell in cells:
        if cell['kind'] not in ('upgrade', 'newer'):
            continue
        key = (cell['channel'], cell['version'])
        if key not in reached:
            reached[key] = cell['ocp']
            continue
        ocp, lower = cell['ocp'], reached[key]
        vendor = cell.get('vendor')
        cell.clear()
        cell.update(kind='no_action', upgraded_on=lower, ocp=ocp)
        if vendor:
            cell['vendor'] = vendor
        ph = by_catalog.get(ocp)
        if ph and not ph.get('done_on'):
            ph['done_on'] = lower
    result['matrix'] = cells
    return cells


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
