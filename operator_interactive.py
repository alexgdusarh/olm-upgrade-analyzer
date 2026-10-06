#!/usr/bin/env python3
"""
OLM Operator Upgrade Path Analyzer

Analyzes OpenShift operator upgrade paths using skipRange and replaces metadata
from an OLM catalog, and renders a networkx graph inside a self-contained HTML report.

Generic: works for ANY operator in the catalog. No hardcoded operators or versions.

Usage:
    python operator_interactive.py -f catalogs/data-v4.22.json -o loki-operator -v 6.0.0
    python operator_interactive.py -f catalogs/data-v4.22.json -o loki-operator -v 6.5.0 -c stable-6.5
    python operator_interactive.py -f catalogs/data-v4.22.json -o compliance-operator -v 0.1.32 -t 1.9.2
"""

import json
import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from packaging import version as pkg_version
import networkx as nx
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from io import StringIO


# ---------------------------------------------------------------------------
# Version helpers (generic - no operator-specific logic)
# ---------------------------------------------------------------------------

def normalize_version(ver) -> str:
    """Remove 'v' prefix from version."""
    if not ver:
        return ""
    ver = str(ver)
    return ver[1:] if ver.startswith('v') else ver


def extract_version_from_name(full_name) -> Optional[str]:
    """Extract version from 'package.v6.2.0' format - works for any operator."""
    if not full_name:
        return None
    parts = str(full_name).split('.')
    for i, part in enumerate(parts):
        if part.startswith('v') and len(part) > 1 and part[1].isdigit():
            version_str = '.'.join(parts[i:])
            if '-' in version_str:
                version_str = version_str.split('-')[0]
            return normalize_version(version_str)
    return None


def safe_parse_version(ver_str):
    """Safely parse version string - returns None on failure."""
    try:
        if not ver_str:
            return None
        return pkg_version.parse(normalize_version(ver_str))
    except Exception:
        return None


def parse_skip_range(skip_range: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse skipRange like '>=6.0.0-0 <6.2.0' into (min_ver, max_ver)."""
    if not skip_range:
        return None, None

    min_match = re.search(r'(>=|>)\s*([\d\.\-\w]+)', skip_range)
    max_match = re.search(r'(<=|<)\s*([\d\.\-\w]+)', skip_range)

    min_ver = None
    max_ver = None

    if min_match:
        min_ver = re.sub(r'-.*$', '', min_match.group(2))
    if max_match:
        max_ver = re.sub(r'-.*$', '', max_match.group(2))

    return min_ver, max_ver


def version_in_skip_range(version_obj, skip_range: str) -> bool:
    """Check if a version satisfies a skipRange expression."""
    if not skip_range or version_obj is None:
        return False

    min_ver, max_ver = parse_skip_range(skip_range)
    if not min_ver and not max_ver:
        return False

    if min_ver:
        min_obj = safe_parse_version(min_ver)
        if min_obj is not None and version_obj < min_obj:
            return False

    if max_ver:
        max_obj = safe_parse_version(max_ver)
        if max_obj is not None and version_obj >= max_obj:
            return False

    return True


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------

def load_operator_channels(json_file: str, operator_name: str) -> Dict[str, List[Dict]]:
    """Load all channels for an operator from an OLM JSON catalog."""
    path = Path(json_file)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {json_file}")

    with open(path, 'r') as f:
        content = f.read()

    if not content.strip():
        raise ValueError("JSON file is empty")

    json_blocks = content.strip().split('}\n{')
    channels_data: Dict[str, List[Dict]] = {}
    skipped = 0

    for i, block in enumerate(json_blocks):
        try:
            if i == 0:
                if not block.startswith('{'):
                    block = '{' + block
            else:
                block = '{' + block

            if i < len(json_blocks) - 1:
                block = block + '}'
            elif not block.endswith('}'):
                block = block + '}'

            data = json.loads(block)
        except json.JSONDecodeError:
            skipped += 1
            continue

        if data.get('package') != operator_name:
            continue

        ch_name = data.get('name', '')
        entries = data.get('entries', [])
        if ch_name and entries:
            channels_data[ch_name] = entries

    if skipped:
        print(f"   ⚠️  Skipped {skipped} malformed JSON block(s)")

    return channels_data


def build_channel_index(channels_data: Dict[str, List[Dict]]) -> List[Dict]:
    """Build per-channel metadata sorted by MAX version DESC."""
    index = []

    for ch_name, entries in channels_data.items():
        versions = []
        for entry in entries:
            ver = extract_version_from_name(entry.get('name', ''))
            ver_obj = safe_parse_version(ver)
            if ver is None or ver_obj is None:
                continue
            versions.append({
                'version': ver,
                'version_obj': ver_obj,
                'skipRange': entry.get('skipRange', '') or '',
                'replaces': extract_version_from_name(entry.get('replaces', '')),
                'entry': entry,
                'channel': ch_name,
            })

        if not versions:
            continue

        # Deduplicate by version, keeping the entry that carries a skipRange
        dedup: Dict[str, Dict] = {}
        for v in versions:
            existing = dedup.get(v['version'])
            if existing is None or (not existing['skipRange'] and v['skipRange']):
                dedup[v['version']] = v
        versions = sorted(dedup.values(), key=lambda x: x['version_obj'], reverse=True)

        index.append({
            'channel': ch_name,
            'versions': versions,
            'max_version': versions[0]['version'],
            'max_version_obj': versions[0]['version_obj'],
        })

    index.sort(key=lambda c: c['max_version_obj'], reverse=True)
    return index


def can_reach(from_ver_obj, target: Dict) -> bool:
    """True if from_ver can upgrade to target, via skipRange or replaces."""
    if target['skipRange'] and version_in_skip_range(from_ver_obj, target['skipRange']):
        return True
    if target['replaces']:
        rep_obj = safe_parse_version(target['replaces'])
        if rep_obj is not None and rep_obj == from_ver_obj:
            return True
    return False


# ---------------------------------------------------------------------------
# Upgrade path calculation
# ---------------------------------------------------------------------------

def calculate_upgrade_path(channels_data: Dict[str, List[Dict]],
                           start_version: str,
                           target_channel: Optional[str] = None,
                           target_version: Optional[str] = None
                           ) -> List[Tuple[str, str, str]]:
    """
    Calculate the upgrade path from START to TARGET.

    Generic algorithm, top-down over channels sorted by MAX version DESC:
      1. Find the highest channel whose versions accept the current version
         (via skipRange, or a direct replaces link).
      2. Jump to the highest reachable version in that channel.
      3. Repeat until the target is reached or no further progress is possible.

    Optional target_channel / target_version constrain where the path stops,
    which allows staying inside a single channel.

    Returns a list of (channel_name, from_version, to_version) steps.
    """
    start_ver = normalize_version(start_version)
    start_obj = safe_parse_version(start_ver)
    if start_obj is None:
        raise ValueError(f"Cannot parse start version: {start_version}")

    index = build_channel_index(channels_data)
    if not index:
        raise ValueError("No usable channels found for this operator")

    # Restrict the search space when a target channel is given
    if target_channel:
        index = [c for c in index if c['channel'] == target_channel]
        if not index:
            available = ', '.join(sorted(channels_data.keys()))
            raise ValueError(
                f"Channel '{target_channel}' not found. Available: {available}")

    # Resolve the target version
    if target_version:
        target_ver = normalize_version(target_version)
        target_obj = safe_parse_version(target_ver)
        if target_obj is None:
            raise ValueError(f"Cannot parse target version: {target_version}")
        found = any(v['version'] == target_ver
                    for c in index for v in c['versions'])
        if not found:
            scope = f"channel '{target_channel}'" if target_channel else "any channel"
            raise ValueError(f"Version {target_ver} not found in {scope}")
    else:
        target_ver = index[0]['max_version']
        target_obj = index[0]['max_version_obj']

    if start_obj >= target_obj:
        raise ValueError(
            f"Start version {start_ver} is not older than target {target_ver}")

    print(f"\n🔍 Calculating upgrade path: {start_ver} → {target_ver}")
    print(f"   Channels in scope: {len(index)}")
    if target_channel:
        print(f"   Target channel: {target_channel}")

    path: List[Tuple[str, str, str]] = []
    current_ver = start_ver
    current_obj = start_obj
    visited = {start_ver}

    while current_obj < target_obj:
        best = None  # (channel, version dict)

        for ch in index:
            # Highest version in this channel that the current version can reach
            # and that does not overshoot the target.
            for v in ch['versions']:
                if v['version'] in visited:
                    continue
                if v['version_obj'] <= current_obj:
                    continue
                if v['version_obj'] > target_obj:
                    continue
                if not can_reach(current_obj, v):
                    continue
                if best is None or v['version_obj'] > best[1]['version_obj']:
                    best = (ch['channel'], v)
                break  # only the highest candidate per channel matters

        if best is None:
            # No skipRange/replaces hop available: walk the replaces chain upward
            best = _next_replaces_step(index, current_obj, target_obj, visited)

        if best is None:
            raise ValueError(
                f"No upgrade path from {current_ver} to {target_ver}. "
                f"Stuck at {current_ver} - no channel accepts this version.")

        ch_name, ver = best
        print(f"     {current_ver} → {ver['version']} ({ch_name})")
        path.append((ch_name, current_ver, ver['version']))
        visited.add(ver['version'])
        current_ver = ver['version']
        current_obj = ver['version_obj']

    print("   ✓ Path complete")
    return path


def _next_replaces_step(index, current_obj, target_obj, visited):
    """Fallback: follow the replaces chain one step up (operators with no skipRange)."""
    best = None
    for ch in index:
        for v in reversed(ch['versions']):  # ascending
            if v['version'] in visited:
                continue
            if v['version_obj'] <= current_obj or v['version_obj'] > target_obj:
                continue
            if best is None or v['version_obj'] < best[1]['version_obj']:
                best = (ch['channel'], v)
            break
    return best


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def plot_combined_channel_graph(channels_data: Dict[str, List[Dict]],
                                operator_name: str,
                                upgrade_path: List[Tuple[str, str, str]],
                                start_version: str) -> str:
    """Build a networkx graph of the upgrade path and return it as inline SVG."""
    start_ver = normalize_version(start_version)
    start_obj = safe_parse_version(start_ver)
    target_ver = upgrade_path[-1][2]
    target_obj = safe_parse_version(target_ver)

    upgrade_versions = {start_ver} | {to_v for _, _, to_v in upgrade_path}
    upgrade_channels = []
    for ch, _, _ in upgrade_path:
        if ch not in upgrade_channels:
            upgrade_channels.append(ch)

    index = {c['channel']: c for c in build_channel_index(channels_data)}

    G = nx.DiGraph()
    node_colors_map: Dict[str, str] = {}
    node_details: Dict[str, Dict] = {}

    # Synthetic START node (the current version may not exist in any channel)
    start_node = f"{start_ver}\n(START)"
    G.add_node(start_node)
    node_colors_map[start_node] = '#4caf50'
    node_details[start_node] = {
        'version': start_ver,
        'version_obj': start_obj,
        'synthetic': True,
        'skipRange': '',
        'replaces': None,
    }
    print(f"   Added synthetic START node: {start_ver}")

    # Nodes: every version in the channels the path touches, bounded by START/TARGET
    for ch_name in upgrade_channels:
        ch = index.get(ch_name)
        if not ch:
            continue
        for v in ch['versions']:
            if v['version'] == start_ver:
                continue
            if v['version_obj'] <= start_obj or v['version_obj'] > target_obj:
                continue
            node_id = f"{v['version']}\n({ch_name})"
            if node_id in G:
                continue
            G.add_node(node_id)
            node_colors_map[node_id] = (
                '#4caf50' if v['version'] in upgrade_versions else '#e3f2fd')
            node_details[node_id] = {
                'version': v['version'],
                'version_obj': v['version_obj'],
                'synthetic': False,
                'skipRange': v['skipRange'],
                'replaces': v['replaces'],
                'channel': ch_name,
            }

    # Edges. Three sources, matching the validated model:
    #   1. START  -> every node whose skipRange covers the start version
    #   2. replaces chain, within a channel
    #   3. each upgrade-path version -> every node whose skipRange covers it
    #      (this is what draws e.g. 1.21.0 -> 1.21.4)
    print("   Adding skipRange / replaces connections...")

    def add_skiprange_edges(from_id, from_d):
        for to_id, to_d in node_details.items():
            if to_id == from_id or to_d['synthetic']:
                continue
            if to_d['version_obj'] <= from_d['version_obj']:
                continue
            if to_d['skipRange'] and version_in_skip_range(
                    from_d['version_obj'], to_d['skipRange']):
                G.add_edge(from_id, to_id, reason='skipRange')

    # 1. from the START node
    add_skiprange_edges(start_node, node_details[start_node])

    # 2. replaces chain (within the same channel)
    for to_id, to_d in node_details.items():
        if to_d['synthetic'] or not to_d['replaces']:
            continue
        for from_id, from_d in node_details.items():
            if from_id == to_id or from_d['version'] != to_d['replaces']:
                continue
            if from_d['synthetic'] or from_d.get('channel') == to_d.get('channel'):
                G.add_edge(from_id, to_id, reason='replaces')

    # 3. from each version on the upgrade path
    for _, _, to_v in upgrade_path:
        from_id = _find_node(G, to_v)
        if from_id:
            add_skiprange_edges(from_id, node_details[from_id])

    # Guarantee the chosen path is drawn even when metadata is sparse
    for ch_name, from_v, to_v in upgrade_path:
        from_id = start_node if from_v == start_ver else _find_node(G, from_v)
        to_id = _find_node(G, to_v)
        if from_id and to_id and not G.has_edge(from_id, to_id):
            G.add_edge(from_id, to_id, reason='path')

    print(f"   Graph complete: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    return _render_svg(G, node_colors_map, operator_name, start_ver, upgrade_path)


def _find_node(G, version: str) -> Optional[str]:
    for node in G.nodes():
        if node.startswith(version + '\n'):
            return node
    return None


def _render_svg(G, node_colors_map, operator_name, start_ver, upgrade_path) -> str:
    """Render the networkx graph to an inline SVG string."""
    n = G.number_of_nodes()
    width = max(12, min(24, 3 + n * 0.9))
    height = max(7, min(16, 4 + n * 0.35))

    fig, ax = plt.subplots(figsize=(width, height))

    try:
        pos = nx.nx_agraph.graphviz_layout(G, prog='dot')
    except Exception:
        pos = nx.spring_layout(G, k=2.2, iterations=120, seed=42)

    colors = [node_colors_map.get(node, '#e3f2fd') for node in G.nodes()]

    nx.draw_networkx_nodes(G, pos, ax=ax, node_color=colors,
                           node_size=3200, edgecolors='#555555', linewidths=1.4)
    nx.draw_networkx_edges(G, pos, ax=ax, edge_color='#999999',
                           arrows=True, arrowsize=16, width=1.3,
                           node_size=3200, connectionstyle='arc3,rad=0.06')
    nx.draw_networkx_labels(G, pos, ax=ax, font_size=7.5, font_weight='bold')

    path_str = start_ver + ' → ' + ' → '.join(to_v for _, _, to_v in upgrade_path)
    ax.set_title(f"{operator_name}\nUpgrade Path: {path_str}",
                 fontsize=13, fontweight='bold', pad=18)
    ax.axis('off')

    legend = [
        plt.Line2D([0], [0], marker='o', color='w', label='Upgrade path version',
                   markerfacecolor='#4caf50', markersize=12, markeredgecolor='#555555'),
        plt.Line2D([0], [0], marker='o', color='w', label='Other available version',
                   markerfacecolor='#e3f2fd', markersize=12, markeredgecolor='#555555'),
    ]
    ax.legend(handles=legend, loc='upper left', fontsize=9, frameon=True)

    plt.tight_layout()

    buf = StringIO()
    fig.savefig(buf, format='svg', bbox_inches='tight')
    plt.close(fig)

    svg = buf.getvalue()
    return svg[svg.find('<svg'):]


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def generate_html_report(operator_name: str,
                         start_version: str,
                         path: List[Tuple[str, str, str]],
                         channels_data: Dict[str, List[Dict]],
                         output_dir: str = ".") -> str:
    """Generate a self-contained HTML report with the embedded networkx graph."""
    start_ver = normalize_version(start_version)
    target_version = path[-1][2]

    channels_involved = []
    for ch, _, _ in path:
        if ch not in channels_involved:
            channels_involved.append(ch)

    svg = plot_combined_channel_graph(channels_data, operator_name, path, start_ver)

    badges = "".join(
        f'<span class="channel-badge">{ch}</span>' for ch in channels_involved)

    steps_html = ""
    for i, (channel, from_ver, to_ver) in enumerate(path, 1):
        verb = "Subscribe to" if i == 1 else "Switch to"
        steps_html += (
            f'<div class="step"><div class="step-number">{i}</div>'
            f'<div class="step-text">{verb} channel <strong>{channel}</strong> '
            f'and upgrade {from_ver} → <strong>{to_ver}</strong></div></div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upgrade Path - {operator_name}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f5f5f5; margin: 0; padding: 20px; }}
  .container {{ max-width: 1400px; margin: 0 auto; background: #fff;
               border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.1); padding: 40px; }}
  h1 {{ color: #333; text-align: center; margin-bottom: 6px; }}
  .subtitle {{ text-align: center; color: #666; margin-bottom: 30px; font-size: 14px; }}
  h2 {{ color: #333; font-size: 18px; margin-top: 34px; }}
  table.info {{ width: 100%; border-collapse: collapse; margin-bottom: 10px;
                border-left: 4px solid #667eea; background: #f9f9f9; }}
  table.info td {{ padding: 12px 16px; border-bottom: 1px solid #e6e6e6; color: #333; font-size: 14px; }}
  table.info tr:last-child td {{ border-bottom: none; }}
  table.info td.label {{ font-weight: 600; color: #667eea; width: 200px; }}
  .channel-badge {{ display: inline-block; background: #667eea; color: #fff;
                    padding: 5px 12px; border-radius: 20px; margin-right: 8px;
                    font-size: 12px; font-weight: 600; }}
  .graph {{ border: 1px solid #ddd; border-radius: 8px; background: #fafafa;
            padding: 20px; text-align: center; overflow-x: auto; }}
  .graph svg {{ max-width: 100%; height: auto; }}
  .step {{ display: flex; align-items: center; background: #f9f9f9; padding: 14px;
           border-radius: 4px; border-left: 4px solid #667eea; margin-bottom: 12px; }}
  .step-number {{ background: #667eea; color: #fff; border-radius: 50%; width: 30px;
                  height: 30px; display: flex; align-items: center; justify-content: center;
                  font-weight: bold; margin-right: 14px; flex-shrink: 0; font-size: 14px; }}
  .step-text {{ color: #333; font-size: 14px; }}
  .summary {{ background: #e8f5e9; border-left: 4px solid #4caf50; padding: 18px;
              border-radius: 4px; margin-top: 26px; }}
  .summary h3 {{ color: #2e7d32; margin: 0 0 8px; font-size: 16px; }}
  .summary p {{ color: #558b2f; margin: 0; font-size: 14px; }}
</style>
</head>
<body>
<div class="container">
  <h1>Upgrade Path: {operator_name}</h1>
  <div class="subtitle">Upgrade analysis based on OLM skipRange and replaces metadata</div>

  <table class="info">
    <tr><td class="label">Operator</td><td>{operator_name}</td></tr>
    <tr><td class="label">Current Version</td><td><strong>{start_ver}</strong></td></tr>
    <tr><td class="label">Channel(s)</td><td>{badges}</td></tr>
    <tr><td class="label">Target Version</td><td><strong>{target_version}</strong></td></tr>
  </table>

  <h2>Upgrade Path Graph</h2>
  <div class="graph">{svg}</div>

  <h2>Step-by-Step Instructions</h2>
  {steps_html}

  <div class="summary">
    <h3>Summary</h3>
    <p>After completing all steps, {operator_name} will be upgraded
       from {start_ver} to {target_version}.</p>
  </div>
</div>
</body>
</html>
"""

    out = Path(output_dir) / "html" / operator_name
    out.mkdir(parents=True, exist_ok=True)
    html_file = out / "index.html"
    html_file.write_text(html)
    return str(html_file)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze OLM operator upgrade paths and render a networkx graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Upgrade to the latest version across channels
  %(prog)s -f catalogs/data-v4.22.json -o loki-operator -v 6.0.0

  # Stay inside one channel
  %(prog)s -f catalogs/data-v4.22.json -o loki-operator -v 6.5.0 -c stable-6.5

  # Target a specific version
  %(prog)s -f catalogs/data-v4.22.json -o compliance-operator -v 0.1.32 -t 1.9.2

  # Target a specific channel and version
  %(prog)s -f catalogs/data-v4.22.json -o openshift-gitops-operator -v 1.14.1 -c gitops-1.21 -t 1.21.4
""")
    parser.add_argument('-f', '--file', required=True, help='OLM catalog JSON file')
    parser.add_argument('-o', '--operator', required=True, help='Operator (package) name')
    parser.add_argument('-v', '--version', required=True, help='Current/start version')
    parser.add_argument('-c', '--target-channel', default=None,
                        help='Optional: restrict the path to this channel')
    parser.add_argument('-t', '--target-version', default=None,
                        help='Optional: stop at this version instead of the latest')
    parser.add_argument('-d', '--output-dir', default='.', help='Output directory (default: .)')

    args = parser.parse_args()

    try:
        channels_data = load_operator_channels(args.file, args.operator)
    except Exception as e:
        print(f"❌ {e}")
        return 1

    if not channels_data:
        print(f"❌ No channels found for operator '{args.operator}'")
        print("   Check the operator name matches the 'package' field in the catalog.")
        return 1

    print(f"✓ Found {len(channels_data)} channel(s) for {args.operator}")

    try:
        path = calculate_upgrade_path(
            channels_data, args.version,
            target_channel=args.target_channel,
            target_version=args.target_version)
    except Exception as e:
        print(f"\n❌ {e}")
        return 1

    print("\n" + "=" * 78)
    print(f"✅ UPGRADE PATH - {args.operator}")
    print("=" * 78)
    for i, (channel, from_ver, to_ver) in enumerate(path, 1):
        print(f"  Step {i}: {from_ver} → {to_ver} ({channel})")
    print("=" * 78)

    print("\n📊 Building graph...")
    try:
        html_file = generate_html_report(
            args.operator, args.version, path, channels_data, args.output_dir)
    except Exception as e:
        print(f"❌ Failed to generate report: {e}")
        return 1

    print(f"\n✅ HTML report: {html_file}")
    print("   Open in a browser to view the graph.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
