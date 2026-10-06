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


def _required(ocp_path: List[str], current: str, target: str) -> Dict:
    """The OpenShift version each release on the path must be certified to."""
    need = {r: None for r in ocp_path}
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
             component_version: Optional[str],
             operator_version: str) -> Dict:
    """
    Check one operator against its vendor support matrix.

    Besides the verdict, the result carries the whole matrix for this path as
    'table' - every release with its certified z-stream per OpenShift release
    on the path, whether it covers the path, and which release is installed
    and which recommended - since that is what a reader acts on.
    """
    res = _evaluate(constraint, ocp_path, current, target, component_version,
                    operator_version)
    need = _required(ocp_path, current, target)
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
        })
    res['table'] = table
    return res


def _evaluate(constraint: Dict, ocp_path: List[str], current: str,
              target: str, component_version: Optional[str],
              operator_version: str) -> Dict:
    label = constraint.get('label') or constraint['component']
    releases = constraint['releases']
    need = _required(ocp_path, current, target)
    res = {'component': constraint['component'], 'label': label,
           'source': constraint.get('source'), 'installed': component_version,
           'operator_version': operator_version, 'notes': []}

    path_txt = (f"{current} -> {target}" if _has_z(target)
                else ' -> '.join(ocp_path))
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
