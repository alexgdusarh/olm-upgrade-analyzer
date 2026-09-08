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
import os
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


CATALOG_ENV_VAR = 'OCP_CATALOG_DIR'
CATALOG_DIR_NAMES = ('data', 'catalogs', 'catalog', 'data-catalogs', 'ocp-catalogs')
CATALOG_SEARCH_DEPTH = 3


def discover_catalog_dir(explicit: Optional[str] = None,
                         search_from: Optional[str] = None) -> str:
    """
    Locate the directory holding the data-v<major>.<minor>.json catalogs.

    Catalogs are commonly kept outside the project that consumes them, so the
    search is deliberately wide. In order of precedence:

        1. --catalog-dir
        2. the OCP_CATALOG_DIR environment variable
        3. a conventional catalog directory beside the input file, then beside
           the current directory, then walking up their parents
        4. a bounded recursive scan below the input file's directory and the
           current directory

    The first location holding at least one catalog wins.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            raise FileNotFoundError(f"Catalog directory not found: {explicit}")
        if not find_catalogs(str(path)):
            raise FileNotFoundError(
                f"No data-v<major>.<minor>.json files in {path}")
        return str(path)

    env = os.environ.get(CATALOG_ENV_VAR)
    if env:
        path = Path(env).expanduser()
        if not path.is_dir():
            raise FileNotFoundError(
                f"{CATALOG_ENV_VAR} points at {env}, which is not a directory")
        if not find_catalogs(str(path)):
            raise FileNotFoundError(
                f"{CATALOG_ENV_VAR} points at {path}, which holds no "
                f"data-v<major>.<minor>.json files")
        return str(path)

    roots = []
    if search_from:
        roots.append(Path(search_from).expanduser().resolve())
    cwd = Path.cwd().resolve()
    if cwd not in roots:
        roots.append(cwd)

    tried = []

    # conventional locations, walking up from each root
    for root in roots:
        for level, base in enumerate([root] + list(root.parents)):
            if level > CATALOG_SEARCH_DEPTH:
                break
            for name in CATALOG_DIR_NAMES:
                candidate = base / name
                tried.append(candidate)
                if candidate.is_dir() and find_catalogs(str(candidate)):
                    return str(candidate)
            tried.append(base)
            if find_catalogs(str(base)):
                return str(base)

    # bounded recursive scan as a last resort
    for root in roots:
        found = _scan_for_catalogs(root, CATALOG_SEARCH_DEPTH)
        if found:
            return found

    hint = (f"Set {CATALOG_ENV_VAR} or pass --catalog-dir to point at them.")
    sample = "\n  ".join(str(p) for p in list(dict.fromkeys(tried))[:12])
    raise FileNotFoundError(
        "Could not find any data-v<major>.<minor>.json catalogs.\n"
        f"Looked in:\n  {sample}\n"
        f"...and scanned {CATALOG_SEARCH_DEPTH} levels below "
        f"{' and '.join(str(r) for r in roots)}.\n{hint}")


def _scan_for_catalogs(root: Path, max_depth: int) -> Optional[str]:
    """Walk below root looking for a directory holding catalogs."""
    root = Path(root)
    if not root.is_dir():
        return None
    base_depth = len(root.parts)
    skip = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'html'}
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        if any(CATALOG_PATTERN.match(f) for f in filenames):
            return str(current)
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in skip
                       and not d.startswith('.')]
    return None


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


def load_catalogs(catalog_dir: str, ocp_path: List[str]) -> Dict[str, Dict]:
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


def channel_max_version(catalog: Dict, pkg: str, chan: str) -> Optional[str]:
    versions = catalog.get(pkg, {}).get(chan, {})
    if not versions:
        return None
    return max(versions, key=version_sort_key)


def _channel_suffix_version(chan: str):
    """The trailing numeric version in a channel name, if it has one."""
    m = re.search(r'(\d+(?:\.\d+)*)$', str(chan))
    return safe_parse_version(m.group(1)) if m else None


def resolve_channel(catalog: Dict, pkg: str,
                    requested: str) -> Tuple[Optional[str], str]:
    """
    Map a requested channel onto one that exists in the catalog.

    A subscription may name a channel that has since been retired, so the
    request is a hint rather than a guarantee. Resolution order:

        1. the requested channel, when it exists
        2. the newest channel whose name contains 'stable' or 'latest'
        3. the newest channel whose name ends in a version number
        4. the channel holding the highest version overall

    'Newest' compares the highest version each channel carries, so it does not
    depend on any particular naming scheme.

    Returns (channel, reason). The channel is None only when the package has no
    channels at all.
    """
    channels = catalog.get(pkg, {})
    if not channels:
        return None, 'no channels'

    if requested in channels:
        return requested, 'exact'

    def newest(names):
        scored = [(c, channel_max_version(catalog, pkg, c)) for c in names]
        scored = [(c, v) for c, v in scored if v]
        if not scored:
            return None
        return max(scored, key=lambda cv: version_sort_key(cv[1]))[0]

    named = [c for c in channels
             if 'stable' in c.lower() or 'latest' in c.lower()]
    pick = newest(named)
    if pick:
        return pick, 'stable/latest'

    versioned = [c for c in channels if _channel_suffix_version(c) is not None]
    pick = newest(versioned)
    if pick:
        return pick, 'versioned'

    pick = newest(list(channels))
    if pick:
        return pick, 'highest'

    return None, 'no versions'


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
    return out


def _same_version_channels(catalog: Dict, pkg: str, ver: str) -> List[str]:
    """Channels that carry this exact version - switching between them is free."""
    return [c for c, versions in catalog.get(pkg, {}).items() if ver in versions]


def _rank(node: Tuple[str, str]):
    """Higher is better: compare version first, then channel name."""
    chan, ver = node
    return (version_sort_key(ver), chan)


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

    resolved, candidates = resolve_package(current_catalog, name)
    if resolved is None:
        result['verdict'] = 'manual_review'
        if candidates:
            result['notes'].append(
                f"Operator '{name}' matches more than one package in the "
                f"{ocp_path[0]} catalog ({', '.join(candidates)}). Pass the "
                f"exact package name. Check manually.")
        else:
            result['notes'].append(
                f"Operator '{name}' is not present in the {ocp_path[0]} "
                f"catalog. It may be a third-party operator, which this tool "
                f"does not diagnose. Check manually.")
        return result

    if resolved != name:
        result['resolved_package'] = resolved
        result['notes'].append(
            f"Resolved '{name}' to catalog package '{resolved}'.")
        name = resolved
        result['operator'] = resolved

    requested_channel = channel
    if channel not in current_catalog[name]:
        picked, reason = resolve_channel(current_catalog, name, channel)
        if picked is None:
            available = ', '.join(sorted(current_catalog[name].keys()))
            result['verdict'] = 'manual_review'
            result['notes'].append(
                f"Channel '{channel}' does not exist for '{name}' in the "
                f"{ocp_path[0]} catalog and no usable alternative was found. "
                f"Available: {available}. Check manually.")
            return result

        explain = {
            'stable/latest': "the newest channel named stable or latest",
            'versioned': "the newest version-numbered channel",
            'highest': "the channel carrying the highest version",
        }.get(reason, reason)
        result['resolved_channel'] = picked
        result['notes'].append(
            f"Channel '{channel}' does not exist for '{name}' in the "
            f"{ocp_path[0]} catalog. Fell back to '{picked}' ({explain}). "
            f"Confirm this matches the subscription.")
        channel = picked
        result['input']['resolved_channel'] = picked

    matched, matches = resolve_version(current_catalog, name, channel, version)
    if matched is None:
        # The version may belong to the channel that was originally requested.
        alt = None
        for chan in current_catalog[name]:
            cand, _ = resolve_version(current_catalog, name, chan, version)
            if cand is not None:
                alt = (chan, cand)
                break
        if alt:
            channel, matched = alt[0], alt[1]
            result['resolved_channel'] = channel
            result['input']['resolved_channel'] = channel
            # The earlier fallback guess is superseded by where the installed
            # version actually lives, so replace it rather than report both.
            result['notes'] = [n for n in result['notes']
                               if not n.startswith(f"Channel '{requested_channel}'")]
            result['notes'].append(
                f"Channel '{requested_channel}' does not exist for '{name}' in "
                f"the {ocp_path[0]} catalog. Version {version} was located in "
                f"'{channel}', which is used instead.")
        else:
            result['verdict'] = 'manual_review'
            result['notes'].append(
                f"Version {version} is not present in channel '{channel}' of "
                f"the {ocp_path[0]} catalog. Check manually.")
            return result

    if matched != normalize_version(version):
        result['resolved_version'] = matched
        extra = (f" ({len(matches)} builds share this version; the newest was "
                 f"used)" if len(matches) > 1 else "")
        result['notes'].append(
            f"Resolved installed version {version} to catalog entry "
            f"{matched}{extra}.")
        version = matched

    result['version_pinned'] = is_version_pinned(catalogs, name)

    gaps = find_monotonicity_gaps(catalogs, ocp_path, name)
    for gap in gaps:
        result['notes'].append(
            f"Non-monotonic catalog: {gap['channel']} {gap['version']} is "
            f"present in {' and '.join(gap['present_in'])} but absent in "
            f"{gap['absent_in']}. Excluded from planning - verify manually.")
    excluded = {(g['channel'], g['version']) for g in gaps}

    current = (channel, version)

    if result['version_pinned']:
        return _plan_pinned(catalogs, ocp_path, name, current, excluded, result)

    return _plan_floating(catalogs, ocp_path, name, current, excluded, result)


def _plan_pinned(catalogs, ocp_path, name, current, excluded, result) -> Dict:
    """
    Plan an operator whose version stream is pinned to the OCP release.

    Such an operator follows the cluster rather than leading it. Each catalog
    carries the previous release's channel as well as its own, so an operator
    sitting at stable-<N> stays valid when the cluster moves to N+1. The upgrade
    therefore happens after each hop: move the cluster, then switch to that
    release's channel and take its latest version. One operator upgrade per OCP
    upgrade.

    A pre-upgrade phase is emitted only when the installed version is too old to
    survive the first hop at all.
    """
    first_hop_catalog = catalogs[ocp_path[1]]

    if current not in tuples_in(first_hop_catalog, name):
        on_ocp, next_ocp = ocp_path[0], ocp_path[1]
        # Same rule as every other release: switch to this release's channel and
        # take its latest version. Anything older cannot survive the hop.
        target = release_channel(catalogs[on_ocp], name, on_ocp,
                                 prefer=current[0])
        phase = {
            'phase': 1, 'kind': 'pre-upgrade', 'on_ocp': on_ocp,
            'satisfies': [on_ocp, next_ocp],
            'from': {'channel': current[0], 'version': current[1]},
        }
        steps = (shortest_path_to(catalogs[on_ocp], name, current, {target})
                 if target and target != current else None)

        if target == current:
            # Already at this release's head. The operator moves once the
            # cluster reaches the next release; nothing to do beforehand.
            pass
        elif target is None or steps is None:
            phase.update({'to': phase['from'], 'hops': 0, 'steps': [],
                          'status': 'blocked'})
            result['phases'].append(phase)
            result['verdict'] = 'blocked'
            result['blocking'] = True
            where = (f"{target[1]} in channel {target[0]}" if target
                     else f"any {on_ocp} channel")
            result['notes'].append(
                f"'{name}' at {current[1]} (channel {current[0]}) is too old to "
                f"survive the move from {on_ocp} to {next_ocp}, and cannot be "
                f"upgraded to {where} using the {on_ocp} catalog.")
            return result
        else:
            current = target
            phase.update({'to': {'channel': current[0], 'version': current[1]},
                          'hops': sum(1 for x in steps
                                      if x['via'] != 'channel-switch'),
                          'steps': steps, 'status': 'upgrade_required'})
            result['phases'].append(phase)
            result['verdict'] = 'operator_upgrade_required'
            result['notes'].append(
                f"The installed version predates the release window, so an "
                f"upgrade on {on_ocp} is required before the cluster can move.")

    # One operator upgrade per OCP release the cluster lands on.
    for ocp in ocp_path[1:]:
        catalog = catalogs[ocp]
        target = release_channel(catalog, name, ocp, prefer=current[0])
        phase = {
            'phase': len(result['phases']) + 1,
            'kind': 'per-release', 'on_ocp': ocp, 'satisfies': [ocp],
            'from': {'channel': current[0], 'version': current[1]},
        }

        if target is None or target == current:
            phase.update({'to': phase['from'], 'hops': 0, 'steps': [],
                          'status': 'no_action'})
            result['phases'].append(phase)
            continue

        steps = shortest_path_to(catalog, name, current, {target})
        if steps is None:
            phase.update({'to': phase['from'], 'hops': 0, 'steps': [],
                          'status': 'unreachable'})
            result['phases'].append(phase)
            result['verdict'] = 'manual_review'
            result['notes'].append(
                f"On OCP {ocp}, '{name}' cannot reach {target[1]} in channel "
                f"{target[0]} from {current[1]} (channel {current[0]}). "
                f"Verify manually.")
            continue

        current = target
        phase.update({'to': {'channel': current[0], 'version': current[1]},
                      'hops': sum(1 for s in steps if s['via'] != 'channel-switch'),
                      'steps': steps, 'status': 'upgrade_required'})
        result['phases'].append(phase)
        result['verdict'] = 'operator_upgrade_required'

        # The version installed here may have been retired by the next release.
        idx = ocp_path.index(ocp)
        if idx + 1 < len(ocp_path):
            nxt = ocp_path[idx + 1]
            if current not in tuples_in(catalogs[nxt], name):
                result['notes'].append(
                    f"{current[1]} (channel {current[0]}) is the head of that "
                    f"channel on {ocp} but is absent from the {nxt} catalog. "
                    f"That is expected for a release-pinned operator, which "
                    f"moves to the {nxt} channel once the cluster arrives.")

    return result


def _plan_floating(catalogs, ocp_path, name, current, excluded, result) -> Dict:
    """
    Plan an operator whose versions are independent of the OCP release.

    Here the operator leads: for each hop it must already sit at a
    (channel, version) present in both the current and next catalogs, so it is
    upgraded before the cluster moves. A final phase takes it to latest once the
    cluster has arrived.
    """
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
