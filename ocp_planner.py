#!/usr/bin/env python3
"""
Catalog and version helpers for the OCP cluster upgrade planner.

Loads one OLM catalog per OCP release, resolves an installed operator against
them, and provides the upgrade edges - replaces, skips and skipRange - that the
planner in mirror_plan.py searches. Generic: no operator-specific logic.

An OCP upgrade path is:

    EUS      current -> current+1 -> current+2   (target = current+2)
    other    current -> current+1                (target = current+1)
"""

import json
import re
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
    """
    Extract a version from 'package.v4.18.3' - works for any operator.

    The build or vendor suffix is preserved, because it is part of the
    operator's identity and sometimes the only thing distinguishing two
    builds: local-storage-operator ships several 4.18.0-<timestamp> releases
    that would otherwise collapse into a single version.
    """
    if not full_name:
        return None
    parts = str(full_name).split('.')
    for i, part in enumerate(parts):
        # 'package.v4.18.3' and 'package.4.18.3' are both in use
        if part.startswith('v') and len(part) > 1 and part[1].isdigit():
            return normalize_version('.'.join(parts[i:]))
        if i > 0 and part and part[0].isdigit():
            return normalize_version('.'.join(parts[i:]))
    return None


def version_core(ver) -> str:
    """The numeric part of a version, dropping any build or vendor suffix."""
    return normalize_version(ver).split('-', 1)[0]


def version_suffix(ver) -> str:
    parts = normalize_version(ver).split('-', 1)
    return parts[1] if len(parts) > 1 else ''


def safe_parse_version(ver_str):
    """
    Parse the numeric core of a version.

    Comparisons and skipRange bounds operate on the core: a skipRange such as
    '>=4.18.0 <4.18.27' refers to cores, not to build suffixes. Suffixes are
    handled separately by version_sort_key.
    """
    try:
        if not ver_str:
            return None
        return pkg_version.parse(version_core(ver_str))
    except Exception:
        return None


def version_sort_key(ver):
    """
    Total ordering over versions, suffixes included.

    Ordered by numeric core first. Within one core an unsuffixed version sorts
    below suffixed builds, and numeric suffixes (dated builds) sort by value so
    that a later build ranks higher.
    """
    core = safe_parse_version(ver)
    if core is None:
        return (pkg_version.parse('0'), 0, 0, '')
    suffix = version_suffix(ver)
    if not suffix:
        return (core, 0, 0, '')
    digits = re.match(r'^(\d+)', suffix)
    return (core, 1, int(digits.group(1)) if digits else 0, suffix)


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


CATALOG_PATTERN = re.compile(r'^data-v(\d+)[._](\d+)\.json$')


def catalog_filenames(ocp: str) -> List[str]:
    """
    Accepted catalog filenames for an OCP release, most preferred first.

    The dot form is canonical. The underscore form is also accepted because
    some transfer paths rewrite dots in filenames.
    """
    major, minor = parse_ocp(ocp)
    return [f"data-v{major}.{minor}.json", f"data-v{major}_{minor}.json"]


def catalog_filename(ocp: str) -> str:
    return catalog_filenames(ocp)[0]


def build_ocp_path(current: str, target: str, channel: str) -> List[str]:
    """
    Build the sequence of OCP releases the cluster passes through.

    EUS jumps two releases, everything else jumps one. The channel decides the
    expected span; the target is authoritative and is validated against it.
    EUS releases are the even minors, so an EUS path runs even to even.
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
    if is_eus and cmin % 2:
        raise ValueError(
            f"EUS releases are the even minors, so {current} -> {target} is "
            f"not an EUS upgrade")

    return [format_ocp(cmaj, m) for m in range(cmin, tmin + 1)]


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------

def load_catalog(path: str, allow_empty: bool = False
                 ) -> Dict[str, Dict[str, Dict[str, Dict]]]:
    """
    Parse an OLM catalog into {package: {channel: {version: entry}}}.

    An empty file is an error unless allow_empty is set, as it is for a
    catalog pulled for packages the image turned out not to carry.

    Presence is modelled as an explicit version set rather than a min/max floor,
    because channels are not always contiguous - some releases drop a version
    from the middle of a channel.
    """
    content = Path(path).read_text()
    if not content.strip():
        if allow_empty:
            return {}
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


def check_catalog_dir(catalog_dir: str) -> str:
    """Validate a directory of hand-supplied data-v<major>.<minor>.json files."""
    path = Path(catalog_dir).expanduser()
    if not path.is_dir():
        raise FileNotFoundError(f"Catalog directory not found: {catalog_dir}")
    if not find_catalogs(str(path)):
        raise FileNotFoundError(
            f"No data-v<major>.<minor>.json files in {path}")
    return str(path)


def find_catalogs(catalog_dir: str) -> Dict[str, str]:
    """Map every OCP release found in a directory to its catalog file path."""
    found = {}
    directory = Path(catalog_dir)
    if not directory.is_dir():
        return found
    for path in directory.glob('data-v*.json'):
        m = CATALOG_PATTERN.match(path.name)
        if m:
            found[f"{int(m.group(1))}.{int(m.group(2))}"] = str(path)
    return found


def load_catalogs(catalog_dir: str, ocp_path: List[str],
                  allow_empty: bool = False) -> Dict[str, Dict]:
    catalogs = {}
    missing = []
    for ocp in ocp_path:
        path = None
        for candidate in catalog_filenames(ocp):
            option = Path(catalog_dir) / candidate
            if option.exists():
                path = option
                break
        if path is None:
            missing.append(catalog_filename(ocp))
            continue
        catalogs[ocp] = load_catalog(str(path), allow_empty)
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


def normalize_package_name(name: str) -> str:
    """
    Reduce a package name to a comparable form.

    Subscriptions frequently carry a display name that differs from the OLM
    package name - 'openshift-mtv' for 'mtv-operator', 'cert-manager-operator'
    for 'openshift-cert-manager-operator'. Stripping the common vendor prefix
    and role suffix makes those comparable without any per-operator mapping.
    """
    n = str(name).strip().lower().replace('_', '-')
    for prefix in ('openshift-', 'redhat-', 'rhel-'):
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    for suffix in ('-operator', '-rhel9', '-rhel8'):
        if n.endswith(suffix):
            n = n[:-len(suffix)]
            break
    return n


def resolve_package(catalog: Dict, name: str) -> Tuple[Optional[str], List[str]]:
    """
    Map an input operator name onto a package in the catalog.

    Returns (resolved_name, candidates). An exact match always wins. Otherwise
    the normalized form is matched; ambiguity is reported rather than guessed.
    """
    if name in catalog:
        return name, [name]
    target = normalize_package_name(name)
    candidates = sorted(p for p in catalog if normalize_package_name(p) == target)
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def resolve_version(catalog: Dict, pkg: str, chan: str,
                    version: str) -> Tuple[Optional[str], List[str]]:
    """
    Map an input version onto a version in a channel.

    An exact match wins. Otherwise the numeric core is matched, so an installed
    '4.18.27-rhodf' finds the catalog entry regardless of how the suffix was
    recorded, and a bare '4.18.0' finds the dated build when only one exists.
    """
    versions = catalog.get(pkg, {}).get(chan, {})
    ver = normalize_version(version)
    if ver in versions:
        return ver, [ver]
    core = version_core(ver)
    candidates = sorted((v for v in versions if version_core(v) == core),
                        key=version_sort_key)
    if len(candidates) == 1:
        return candidates[0], candidates
    if candidates:
        # several builds share this core; the newest is the sensible reading
        return candidates[-1], candidates
    return None, []


def release_channel(catalog: Dict, pkg: str, ocp: str,
                    prefer: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """
    The (channel, latest version) that a version-pinned operator should sit at
    on a given OCP release: the channel carrying that release's version stream,
    at its highest version.

    The channel currently in use is preferred when it carries this release, so
    an operator on 'stable' is never quietly moved onto 'candidate' or another
    pre-release channel just because it holds a higher version. Operators whose
    channel name encodes the release, such as stable-4.18, have no such channel
    available and fall back to the highest-ranked candidate.

    Resolved from the data rather than by name, so any naming scheme works.
    """
    prefix = ocp + '.'
    candidates = []
    for chan, versions in catalog.get(pkg, {}).items():
        matching = [v for v in versions if v.startswith(prefix)]
        if matching:
            candidates.append((chan, max(matching, key=version_sort_key)))
    if not candidates:
        return None
    if prefer:
        for chan, ver in candidates:
            if chan == prefer:
                return (chan, ver)
    return max(candidates, key=_rank)


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
            if to_obj is None:
                continue
            if version_sort_key(to_ver) <= version_sort_key(ver):
                continue
            skip_range = entry.get('skipRange', '') or ''
            if skip_range and version_in_skip_range(ver_obj, skip_range):
                out.append((to_chan, to_ver, 'skipRange'))
                continue
            replaces = extract_version_from_name(entry.get('replaces', ''))
            if replaces and replaces == ver:
                out.append((to_chan, to_ver, 'replaces'))
                continue
            skips = {extract_version_from_name(s)
                     for s in entry.get('skips') or []}
            if ver in skips:
                out.append((to_chan, to_ver, 'skips'))
    return out


def _same_version_channels(catalog: Dict, pkg: str, ver: str) -> List[str]:
    """Channels that carry this exact version - switching between them is free."""
    return [c for c, versions in catalog.get(pkg, {}).items() if ver in versions]


def _rank(node: Tuple[str, str]):
    """Higher is better: compare version first, then channel name."""
    chan, ver = node
    return (version_sort_key(ver), chan)


def highest_tuple(catalog: Dict, pkg: str) -> Optional[Tuple[str, str]]:
    tuples = tuples_in(catalog, pkg)
    if not tuples:
        return None
    return max(tuples, key=_rank)


# ---------------------------------------------------------------------------
# Installed operator
# ---------------------------------------------------------------------------

def resolve_installed(catalogs: Dict[str, Dict], ocp_path: List[str],
                      name: str, channel: str, version: str
                      ) -> Tuple[Optional[Tuple[str, str, str]], List[str]]:
    """
    Map an installed operator onto the catalogs: (package, channel, version).

    The current release's catalog is searched first, since that is where the
    installed bundle comes from, then the target, then any release between.
    A version no catalog lists is kept as given: upgrade edges are evaluated
    by version, so the target catalog can still cover it.

    Returns (None, notes) when the package cannot be identified.
    """
    order = [ocp_path[0], ocp_path[-1]] + ocp_path[1:-1]
    notes: List[str] = []

    pkg = None
    for ocp in order:
        resolved, candidates = resolve_package(catalogs[ocp], name)
        if resolved:
            pkg = resolved
            break
        if len(candidates) > 1:
            notes.append(
                f"Operator '{name}' matches more than one package in the {ocp} "
                f"catalog ({', '.join(candidates)}). Pass the exact package "
                f"name. Check manually.")
            return None, notes
    if pkg is None:
        notes.append(
            f"Operator '{name}' is not in any catalog on the path "
            f"({', '.join(ocp_path)}) of this catalog image. It may come from "
            f"another catalog image. Check manually.")
        return None, notes
    if pkg != name:
        notes.append(f"Resolved '{name}' to catalog package '{pkg}'.")

    ver = normalize_version(version)
    for ocp in order:
        channels = catalogs[ocp].get(pkg, {})
        # the subscribed channel first, then wherever the version lives
        for chan in [channel] + sorted(c for c in channels if c != channel):
            if chan not in channels:
                continue
            matched, matches = resolve_version(catalogs[ocp], pkg, chan, ver)
            if matched is None:
                continue
            if chan != channel:
                notes.append(
                    f"Version {ver} is not in channel '{channel}' of the {ocp} "
                    f"catalog; it was located in '{chan}', which is used "
                    f"instead. Confirm this matches the subscription.")
            if matched != ver:
                extra = (f" ({len(matches)} builds share this version; the "
                         f"newest was used)" if len(matches) > 1 else "")
                notes.append(f"Resolved installed version {ver} to catalog "
                             f"entry {matched}{extra}.")
            return (pkg, chan, matched), notes

    notes.append(
        f"Version {ver} of '{pkg}' is not listed in any catalog on the path. "
        f"It is checked as installed, by version.")
    return (pkg, channel, ver), notes
