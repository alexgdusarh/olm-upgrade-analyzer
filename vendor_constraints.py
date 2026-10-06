#!/usr/bin/env python3
"""
Vendor support matrices: constraints OLM metadata does not carry.

Some operators are certified only on specific OpenShift z-streams per release
of the product they manage - Portworx Enterprise, for one, publishes which
OpenShift versions each of its releases is qualified on, up to a z-stream, and
the minimum Portworx Operator each release needs. constraints/vendor-support.json
records those matrices, keyed by OLM package:

    {
      "<package>": {
        "component": "<component name, as in the input's component_versions>",
        "label": "<display name>",
        "source": "<where the matrix was taken from>",
        "releases": [
          { "version": "3.6.2",
            "operator_min": "26.3.0",
            "operator_min_per_openshift": { "4.21": "25.6.0" },
            "openshift": { "4.18": "4.18.54", "4.19": "4.19.45", ... } }
        ]
      }
    }

The installed component release must be certified on every OpenShift release
the cluster passes through: its certified z-stream must reach the cluster's
current version on the current release and the target version on the target
release, and an intermediate release must be listed (its z-stream is not known
in advance). The installed operator must meet the release's operator minimum,
including any per-release minimum on the path. Otherwise the operator blocks
the cluster upgrade, and the lowest release at or above the installed one that
covers the whole path is recommended.

When the cluster's upgrade path is known (4.18.14 -> 4.18.30 -> 4.19.33 ->
4.20.34), each release must be certified up to the highest version the path
reaches on it, the intermediate release included.

When the installed release is not known, it is narrowed down from the matrix:
a release can only run with an operator at or above its minimum, so the
installed operator rules out every release needing a newer one. If none of
the remaining releases covers the path the operator blocks the upgrade;
otherwise the possibilities are reported for confirmation.

Nothing here is specific to one vendor.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from packaging import version as pkg_version

DEFAULT_FILE = Path(__file__).resolve().parent / 'constraints' / 'vendor-support.json'

OK = 'ok'
BLOCKED = 'blocked'
UNKNOWN = 'unknown'
INFERRED = 'inferred'


def load_constraints(path: Optional[str]) -> Dict:
    """The constraints file, {} when there is none."""
    p = Path(path) if path else DEFAULT_FILE
    if not p.is_file():
        if path:
            raise FileNotFoundError(f"Constraints file not found: {path}")
        return {}
    return json.loads(p.read_text())


def _v(text: str):
    """Comparable version; anything after the numeric core is ignored."""
    m = re.match(r'^v?(\d+(?:\.\d+)*)', str(text).strip())
    return pkg_version.parse(m.group(1)) if m else pkg_version.parse('0')


def _has_z(ocp: str) -> bool:
    return bool(re.match(r'^v?\d+\.\d+\.\d+', str(ocp).strip()))


def _minor(ver: str) -> str:
    m = re.match(r'^v?(\d+)\.(\d+)', str(ver).strip())
    return f"{m.group(1)}.{m.group(2)}" if m else ''


def _required(ocp_path: List[str], current: str, target: str,
              upgrade_path: Optional[List[str]] = None) -> Dict:
    """
    The OpenShift version each release on the path must be certified to: the
    highest version the upgrade path reaches on it, or, without a path, the
    current and target versions (an intermediate release just listed).
    """
    need = {r: None for r in ocp_path}
    if upgrade_path:
        for ver in upgrade_path:
            r = _minor(ver)
            if r in need and (need[r] is None or _v(ver) > _v(need[r])):
                need[r] = ver
        return need
    if _has_z(current):
        need[ocp_path[0]] = current
    if _has_z(target):
        need[ocp_path[-1]] = target
    return need


def _covers(release: Dict, ocp_path: List[str], need: Dict) -> Dict:
    """Per OpenShift release: certified z-stream, required version, verdict."""
    out = {}
    for r in ocp_path:
        cert = release['openshift'].get(r)
        req = need[r]
        ok = cert is not None and (req is None or _v(cert) >= _v(req))
        out[r] = {'certified': cert, 'required': req, 'ok': ok}
    return out


def _operator_min(release: Dict, ocp_path: List[str]) -> str:
    mins = [release.get('operator_min') or '0']
    per = release.get('operator_min_per_openshift') or {}
    mins += [per[r] for r in ocp_path if r in per]
    return max(mins, key=_v)


def _recommend(releases: List[Dict], ocp_path: List[str], need: Dict,
               floor: Optional[str]) -> Optional[Dict]:
    """The lowest release at or above floor that covers the whole path."""
    for rel in sorted(releases, key=lambda r: _v(r['version'])):
        if floor and _v(rel['version']) < _v(floor):
            continue
        if all(c['ok'] for c in _covers(rel, ocp_path, need).values()):
            return {'version': rel['version'],
                    'operator_min': _operator_min(rel, ocp_path),
                    'openshift': {r: rel['openshift'][r] for r in ocp_path}}
    return None


def evaluate(constraint: Dict, ocp_path: List[str], current: str, target: str,
             component_version: Optional[str], operator_version: str,
             upgrade_path: Optional[List[str]] = None) -> Dict:
    """
    Check one operator against its vendor support matrix.

    Besides the verdict, the result carries the whole matrix for this path as
    'table' - every release with its certified z-stream per OpenShift release
    on the path, whether it covers the path, and which release is installed
    and which recommended - since that is what a reader acts on.
    """
    res = _evaluate(constraint, ocp_path, current, target, component_version,
                    operator_version, upgrade_path)
    need = _required(ocp_path, current, target, upgrade_path)
    res['required'] = need
    rec = (res.get('recommended') or {}).get('version')
    table = []
    for rel in sorted(constraint['releases'], key=lambda r: _v(r['version'])):
        cells = _covers(rel, ocp_path, need)
        table.append({
            'version': rel['version'],
            'operator_min': _operator_min(rel, ocp_path),
            'openshift': cells,
            'covers': all(c['ok'] for c in cells.values()),
            'installed': bool(component_version)
                         and _v(rel['version']) == _v(component_version),
            'recommended': bool(rec) and _v(rel['version']) == _v(rec),
            'possible': rel['version'] in (res.get('possible') or []),
        })
    res['table'] = table
    return res


def _infer(res: Dict, releases: List[Dict], ocp_path: List[str], need: Dict,
           operator_version: str, path_txt: str, label: str) -> Dict:
    """
    The installed release is not known: narrow it down from the matrix. A
    release needs at least its operator minimum, so the installed operator
    rules out every release needing a newer one.
    """
    possible = [r for r in sorted(releases, key=lambda r: _v(r['version']))
                if _v(r.get('operator_min') or '0') <= _v(operator_version)]
    res['possible'] = [r['version'] for r in possible]
    covering = [r for r in possible
                if all(c['ok'] for c in _covers(r, ocp_path, need).values())]
    ruled_out = [r for r in releases if r not in possible]

    res['notes'].append(
        f"Warning: the {label} version is not in the input, so it is "
        f"narrowed down from the support matrix and operator "
        f"{operator_version}.")
    if not possible:
        res['status'] = UNKNOWN
        res['releases'] = {r: {'certified': None, 'required': need[r],
                               'ok': None} for r in ocp_path}
        res['notes'].append(
            f"Warning: operator {operator_version} is older than every "
            f"release in the matrix needs, so the installed {label} predates "
            f"the matrix and its support for {path_txt} cannot be checked.")
    else:
        names = ', '.join(r['version'] for r in possible)
        newer = (f" (later releases need operator "
                 f"{min((r['operator_min'] for r in ruled_out), key=_v)} or "
                 f"later)" if ruled_out else "")
        res['notes'].append(
            f"With operator {operator_version}, the installed {label} can "
            f"only be {names}{newer}, or a release older than the matrix.")
        if len(possible) == 1:
            res['installed_inferred'] = possible[0]['version']
            res['releases'] = _covers(possible[0], ocp_path, need)
        else:
            res['releases'] = {r: {'certified': None, 'required': need[r],
                                   'ok': None} for r in ocp_path}
        if not covering:
            res['status'] = BLOCKED
            res['notes'].append(
                f"BLOCKED: none of them is certified for {path_txt}.")
        else:
            res['status'] = INFERRED
            missing = [r for r in possible if r not in covering]
            res['notes'].append(
                f"{', '.join(r['version'] for r in covering)} "
                f"{'is' if len(covering) == 1 else 'are'} certified for "
                f"{path_txt}"
                + (f"; {', '.join(r['version'] for r in missing)} "
                   f"{'is' if len(missing) == 1 else 'are'} not"
                   if missing else "")
                + ". Confirm the installed release.")
            for rel in missing:
                fix = _recommend(releases, ocp_path, need, rel['version'])
                if fix:
                    res['notes'].append(
                        f"If it is {rel['version']}, upgrade {label} to "
                        f"{fix['version']} with operator {fix['operator_min']} "
                        f"or later before the cluster upgrade.")

    # Prefer a covering release the installed operator can already run.
    rec = (_recommend(covering, ocp_path, need, None) if covering
           else _recommend(releases, ocp_path, need, None))
    if rec:
        res['recommended'] = rec
        if res['status'] != INFERRED:
            res['notes'].append(
                f"{label} {rec['version']} with operator "
                f"{rec['operator_min']} or later is certified for the whole "
                f"path.")
    return res


def _evaluate(constraint: Dict, ocp_path: List[str], current: str,
              target: str, component_version: Optional[str],
              operator_version: str,
              upgrade_path: Optional[List[str]] = None) -> Dict:
    label = constraint.get('label') or constraint['component']
    releases = constraint['releases']
    need = _required(ocp_path, current, target, upgrade_path)
    res = {'component': constraint['component'], 'label': label,
           'source': constraint.get('source'), 'installed': component_version,
           'operator_version': operator_version, 'notes': []}

    path_txt = (' -> '.join(upgrade_path) if upgrade_path else
                f"{current} -> {target}" if _has_z(target)
                else ' -> '.join(ocp_path))
    if not component_version:
        return _infer(res, releases, ocp_path, need, operator_version,
                      path_txt, label)
    if not _has_z(target):
        res['notes'].append(
            f"Warning: the target {target} has no z-stream, so the "
            f"{label} certification is checked by release only.")

    release = next((r for r in releases
                    if component_version and
                    _v(r['version']) == _v(component_version)), None)
    floor = component_version
    if release is None:
        res['status'] = UNKNOWN
        res['releases'] = {r: {'certified': None, 'required': need[r],
                               'ok': None} for r in ocp_path}
        rec = _recommend(releases, ocp_path, need, floor)
        if not component_version:
            res['notes'].append(
                f"Warning: the {label} version is not in the input, so its "
                f"support for {path_txt} cannot be checked.")
        else:
            res['notes'].append(
                f"Warning: {label} {component_version} is not in the vendor "
                f"support matrix, so its support for {path_txt} cannot be "
                f"checked.")
        if rec:
            res['recommended'] = rec
            res['notes'].append(
                f"{label} {rec['version']} with operator {rec['operator_min']} "
                f"or later is certified for the whole path.")
        return res

    covers = _covers(release, ocp_path, need)
    res['releases'] = covers
    op_min = _operator_min(release, ocp_path)
    res['operator_min'] = op_min
    res['operator_ok'] = _v(operator_version) >= _v(op_min)

    failed = [r for r, c in covers.items() if not c['ok']]
    if not failed and res['operator_ok']:
        res['status'] = OK
        certified = ', '.join(c['certified'] for c in covers.values())
        res['notes'].append(
            f"{label} {component_version} is certified on {certified}, which "
            f"covers {path_txt}.")
        return res

    res['status'] = BLOCKED
    for r in failed:
        c = covers[r]
        if c['certified'] is None:
            why = f"is not certified on OpenShift {r} at all"
        else:
            why = (f"is certified up to {c['certified']}, below "
                   f"{c['required']}")
        res['notes'].append(
            f"BLOCKED: {label} {component_version} {why}.")
    if not res['operator_ok']:
        res['notes'].append(
            f"BLOCKED: {label} {component_version} needs operator {op_min} or "
            f"later on this path; {operator_version} is installed.")
    rec = _recommend(releases, ocp_path, need, floor)
    if rec and _v(rec['version']) == _v(component_version):
        # the release itself is fine; only the operator is too old
        res['recommended'] = rec
        res['notes'].append(
            f"Upgrade the operator to {rec['operator_min']} or later before "
            f"the cluster upgrade; {label} {component_version} is certified "
            f"on {', '.join(rec['openshift'].values())}.")
    elif rec:
        res['recommended'] = rec
        certified = ', '.join(rec['openshift'].values())
        res['notes'].append(
            f"Upgrade {label} to {rec['version']} with operator "
            f"{rec['operator_min']} or later before the cluster upgrade: it is "
            f"certified on {certified}.")
    else:
        res['notes'].append(
            f"No {label} release in the support matrix covers {path_txt}. "
            f"Check the vendor's support matrix.")
    return res
