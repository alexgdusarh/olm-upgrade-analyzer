#!/usr/bin/env python3
"""
HTML reporting for the OCP upgrade planner.

One report per operator. Each phase is the work done from one catalog - an
intermediate catalog when the target cannot cover the operator, then the target
catalog - with its own info table, graph and steps. A cluster summary page links
them together and lists the catalogs to mirror.
"""

from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use('Agg')
# Same graph, same SVG: fixed element ids, so reruns do not churn reports.
matplotlib.rcParams['svg.hashsalt'] = 'olm-upgrade-analyzer'
import matplotlib.pyplot as plt
import networkx as nx

from ocp_planner import (
    safe_parse_version,
    version_in_skip_range,
    extract_version_from_name,
)

GREEN = '#4caf50'
BLUE = '#e3f2fd'

STATUS_LABEL = {
    'no_action': 'No action required',
    'upgrade_required': 'Operator upgrade required',
}

VERDICT_LABEL = {
    'no_action_required': 'No action required',
    'operator_upgrade_required': 'Operator upgrade required',
    'intermediate_catalog_required': 'CRITICAL: intermediate catalog required',
    'manual_review': 'Manual review required',
    'blocked': 'Blocked',
}

VERDICT_CLASS = {
    'no_action_required': 'ok',
    'operator_upgrade_required': 'warn',
    'intermediate_catalog_required': 'bad',
    'manual_review': 'warn',
    'blocked': 'bad',
}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def _phase_graph(catalog: Dict, pkg: str, phase: Dict) -> Optional[str]:
    """Render one phase as an inline SVG, or None when there is nothing to draw."""
    frm = (phase['from']['channel'], phase['from']['version'])
    to = (phase['to']['channel'], phase['to']['version'])

    channels = {frm[0], to[0]}
    for step in phase['steps']:
        channels.add(step['from_channel'])
        channels.add(step['to_channel'])

    lo = safe_parse_version(frm[1])
    hi = safe_parse_version(to[1])
    if lo is None:
        return None
    if hi is None or hi < lo:
        hi = lo

    path_versions = {frm[1], to[1]}
    for step in phase['steps']:
        path_versions.add(step['from_version'])
        path_versions.add(step['to_version'])

    G = nx.DiGraph()
    colors: Dict[str, str] = {}
    details: Dict[str, Dict] = {}

    for chan in sorted(channels):
        for ver, entry in catalog.get(pkg, {}).get(chan, {}).items():
            obj = safe_parse_version(ver)
            if obj is None or obj < lo or obj > hi:
                continue
            node = f"{ver}\n({chan})"
            if node in G:
                continue
            G.add_node(node)
            colors[node] = GREEN if ver in path_versions else BLUE
            details[node] = {
                'version': ver, 'version_obj': obj, 'channel': chan,
                'skipRange': entry.get('skipRange', '') or '',
                'replaces': extract_version_from_name(entry.get('replaces', '')),
            }

    if not G.nodes():
        return None

    # Edges: skipRange from the path versions, plus the replaces chain.
    for to_node, to_d in details.items():
        if to_d['replaces']:
            for from_node, from_d in details.items():
                if (from_d['version'] == to_d['replaces']
                        and from_d['channel'] == to_d['channel']):
                    G.add_edge(from_node, to_node)

    for from_node, from_d in details.items():
        if from_d['version'] not in path_versions:
            continue
        for to_node, to_d in details.items():
            if to_node == from_node:
                continue
            if to_d['version_obj'] <= from_d['version_obj']:
                continue
            if to_d['skipRange'] and version_in_skip_range(
                    from_d['version_obj'], to_d['skipRange']):
                G.add_edge(from_node, to_node)

    # Guarantee the planned steps are drawn
    for step in phase['steps']:
        a = f"{step['from_version']}\n({step['from_channel']})"
        b = f"{step['to_version']}\n({step['to_channel']})"
        if a in G and b in G and not G.has_edge(a, b):
            G.add_edge(a, b)

    n = G.number_of_nodes()
    fig, ax = plt.subplots(figsize=(max(9, min(22, 3 + n * 0.85)),
                                    max(5, min(14, 3 + n * 0.42))))
    try:
        pos = nx.nx_agraph.graphviz_layout(G, prog='dot')
    except Exception:
        pos = nx.spring_layout(G, k=2.2, iterations=120, seed=42)

    nx.draw_networkx_nodes(G, pos, ax=ax, node_size=3000, linewidths=1.4,
                           edgecolors='#555555',
                           node_color=[colors[x] for x in G.nodes()])
    nx.draw_networkx_edges(G, pos, ax=ax, edge_color='#999999', arrows=True,
                           arrowsize=15, width=1.2, node_size=3000,
                           connectionstyle='arc3,rad=0.06')
    nx.draw_networkx_labels(G, pos, ax=ax, font_size=7.5, font_weight='bold')
    ax.axis('off')

    handles = [
        plt.Line2D([0], [0], marker='o', color='w', label='Upgrade path',
                   markerfacecolor=GREEN, markersize=11, markeredgecolor='#555'),
        plt.Line2D([0], [0], marker='o', color='w', label='Other available version',
                   markerfacecolor=BLUE, markersize=11, markeredgecolor='#555'),
    ]
    ax.legend(handles=handles, loc='upper left', fontsize=8, frameon=True)
    plt.tight_layout()

    buf = StringIO()
    fig.savefig(buf, format='svg', bbox_inches='tight',
                metadata={'Date': None})
    plt.close(fig)
    svg = buf.getvalue()
    return svg[svg.find('<svg'):]


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
  /* PatternFly 6 look (OpenShift console style), the same values as the
     ocp_preupgrade_health_check report: hand-picked from the
     @patternfly/patternfly 6.6.1 tokens rather than its ~2MB stylesheet.
     The Red Hat fonts are served from html/fonts/; without them the stacks
     fall back to system fonts. */
  :root {
    --font-body: "Red Hat Text", "RedHatText", Helvetica, Arial, sans-serif;
    --font-heading: "Red Hat Display", "RedHatDisplay", Helvetica, Arial, sans-serif;
    --font-mono: "Red Hat Mono", "RedHatMono", "Courier New", Courier, monospace;
    --bg: #f2f2f2; --panel: #ffffff; --text: #151515; --muted: #4d4d4d;
    --border: #c7c7c7; --border-subtle: #e0e0e0; --hover: rgba(199,199,199,.25);
    --link: #0066cc; --brand: #0066cc; --masthead: #151515; --masthead-text: #ffffff; --accent: #ee0000;
    --crit: #b1380b; --crit-bg: #ffe3d9; --crit-text: #731f00;
    --warn: #dca614; --warn-bg: #fff4cc; --warn-text: #73480b;
    --ok: #3d7317; --ok-bg: #e9f7df; --ok-text: #204d00;
    --info: #5e40be; --info-bg: #ece6ff; --info-text: #3d2785;
    --unknown-bg: #f2f2f2; --unknown-text: #4d4d4d;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #151515; --panel: #1f1f1f; --text: #ffffff; --muted: #c7c7c7;
      --border: #4d4d4d; --border-subtle: #383838; --hover: rgba(199,199,199,.15);
      --link: #92c5f9; --brand: #92c5f9; --masthead: #000000;
      --crit: #f4784a; --crit-bg: #4c1405; --crit-text: #fbbea8;
      --warn: #ffcc17; --warn-bg: #54330b; --warn-text: #ffe072;
      --ok: #87bb62; --ok-bg: #183301; --ok-text: #d1f1bb;
      --info: #b6a6e9; --info-bg: #21134d; --info-text: #d0c5f4;
      --unknown-bg: #383838; --unknown-text: #e0e0e0;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--font-body); font-size: 14px; line-height: 1.5; }
  a { color: var(--link); text-decoration: none; } a:hover { text-decoration: underline; }
  .masthead { background: var(--masthead); color: var(--masthead-text); border-top: 4px solid var(--accent); }
  .masthead .inner { max-width: 1400px; margin: 0 auto; padding: 14px 24px; display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px 16px; }
  .masthead h1 { font-family: var(--font-heading); font-size: 1.25rem; font-weight: 500; margin: 0; }
  .masthead .sub { color: #c7c7c7; font-size: .85rem; }
  .container { max-width: 1400px; margin: 0 auto; padding: 24px 24px 60px; }
  h2 { font-family: var(--font-heading); font-size: 1.25rem; font-weight: 500; margin: 40px 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border-subtle); }
  h3 { font-family: var(--font-heading); font-size: 1rem; font-weight: 500; margin: 0 0 8px; }
  code, pre { font-family: var(--font-mono); font-size: .85rem; }
  code { background: var(--bg); border: 1px solid var(--border-subtle); border-radius: 4px; padding: 0 5px; }

  /* card with label/value rows (PatternFly description list) */
  table.info { width: 100%; border-collapse: separate; border-spacing: 0; background: var(--panel); border: 1px solid var(--border-subtle); border-radius: 16px; margin-bottom: 16px; overflow: hidden; }
  table.info td { padding: 9px 20px; border-bottom: 1px solid var(--border-subtle); vertical-align: top; }
  table.info tr:last-child td { border-bottom: none; }
  table.info td.label { color: var(--muted); font-weight: 500; width: 240px; }

  /* PatternFly filled label: pill, status tint, no border */
  .badge { display: inline-block; padding: 1px 10px; border-radius: 999px; font-size: .8rem; font-weight: 500; white-space: nowrap; margin: 1px 6px 1px 0; color: var(--info-text); background: var(--info-bg); }
  .badge::before { content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; background: var(--info); }
  .badge.ok { color: var(--ok-text); background: var(--ok-bg); } .badge.ok::before { background: var(--ok); }
  .badge.warn { color: var(--warn-text); background: var(--warn-bg); } .badge.warn::before { background: var(--warn); }
  .badge.bad { color: var(--crit-text); background: var(--crit-bg); } .badge.bad::before { background: var(--crit); }
  .badge.grey { color: var(--unknown-text); background: var(--unknown-bg); } .badge.grey::before { background: var(--muted); }

  /* phase card (per-operator report) */
  .phase { background: var(--panel); border: 1px solid var(--border-subtle); border-radius: 16px; padding: 20px 24px; margin-bottom: 20px; }
  .phase .info { border-radius: 8px; }
  .phase-head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
  .phase-num { background: var(--brand); color: var(--panel); border-radius: 50%; width: 32px; height: 32px; display: flex; align-items: center; justify-content: center; font-family: var(--font-heading); font-weight: 600; flex-shrink: 0; }
  .graph { background: #ffffff; border: 1px solid var(--border-subtle); border-radius: 8px; padding: 16px; text-align: center; overflow-x: auto; margin-bottom: 14px; }
  .graph svg { max-width: 100%; height: auto; }
  .step { border-left: 3px solid var(--brand); background: var(--bg); padding: 8px 14px; border-radius: 0 6px 6px 0; margin-bottom: 8px; }
  .none { color: var(--muted); font-style: italic; padding: 6px 0; }

  /* PatternFly inline alert, warning variant */
  .notes { background: var(--warn-bg); color: var(--warn-text); border-top: 2px solid var(--warn); border-radius: 0 0 8px 8px; padding: 14px 20px; margin-top: 20px; }
  .notes h3 { margin: 0 0 6px; color: inherit; }
  .notes ul { margin: 0; padding-left: 20px; } .notes li { margin-bottom: 4px; }

  /* the summary matrix: compact table in a card */
  .tblwrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--border-subtle); border-radius: 16px; margin-top: 8px; }
  table.ops { width: 100%; border-collapse: collapse; }
  table.ops th { text-align: left; font-weight: 600; font-size: .85rem; padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
  table.ops td { padding: 10px 12px; border-bottom: 1px solid var(--border-subtle); vertical-align: top; }
  table.ops tr:last-child td { border-bottom: none; }
  table.ops tr:hover td { background: var(--hover); }
  .th-sub { font-weight: 400; font-size: .75rem; color: var(--muted); margin-top: 2px; }
  .cell-sub { font-size: .75rem; color: var(--muted); margin-top: 2px; }
  .cell-ok { color: var(--ok); font-weight: 500; } .cell-bad { color: var(--crit); font-weight: 500; }
  .cell-none { color: var(--muted); text-align: center; }
  .cell-meta { color: var(--muted); font-size: .85rem; }
  .cell-opt { color: var(--link); }
  table.ops td.cell-warn { background: var(--warn-bg); color: var(--warn-text); font-size: .85rem; }
  table.ops td.cell-warn .cell-sub { color: var(--warn-text); }
  .cell-vendor { font-size: .75rem; margin-top: 6px; padding-top: 4px; border-top: 1px dashed var(--border-subtle); }
  .cell-vendor.ok { color: var(--ok); } .cell-vendor.bad { color: var(--crit); } .cell-vendor.warn { color: var(--warn-text); }
  .cell-rule { border: none; border-top: 1px dashed var(--border-subtle); margin: 8px 0; }
  .legend { color: var(--muted); font-size: .9rem; margin: 0 0 8px; }

  /* vendor support matrix: a card under its operator's row */
  table.ops tr.vendor-row td { background: var(--bg); padding: 4px 12px 16px; }
  table.ops tr.vendor-row:hover td { background: var(--bg); }
  .vendor-box { background: var(--panel); border: 1px solid var(--border-subtle); border-radius: 16px; padding: 14px 18px; }
  .vendor-head { margin-bottom: 4px; } .vendor-head strong { font-family: var(--font-heading); font-weight: 500; font-size: 1rem; }
  .vendor-advice { color: var(--crit-text); background: var(--crit-bg); border-radius: 6px; padding: 6px 12px; font-weight: 500; margin: 6px 0 10px; }
  table.vendor-table { width: 100%; border-collapse: collapse; }
  table.vendor-table th { text-align: left; font-weight: 600; font-size: .8rem; padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  table.vendor-table td { padding: 6px 10px; border-bottom: 1px solid var(--border-subtle); }
  table.vendor-table tr:last-child td { border-bottom: none; }
  table.vendor-table tr.vt-installed td { background: var(--unknown-bg); }
  table.vendor-table tr.vt-recommended td { background: var(--ok-bg); }
  .vt-ok { color: var(--ok); } .vt-bad { color: var(--crit); font-weight: 500; }
  footer { margin-top: 48px; color: var(--muted); font-size: .8rem; text-align: center; }
"""


def _cluster_label(cluster: Dict) -> str:
    return f"{cluster['name']} " if cluster.get('name') else ""


def _vendor_table(vendor: Dict, ocp_path: List[str]) -> str:
    """
    The vendor's support matrix for this cluster's path: every release, its
    certified z-stream on each OpenShift release of the path, and whether it
    covers the path, with the installed and recommended releases marked.
    """
    need = vendor.get('required') or {}
    head = ''
    for i, ocp in enumerate(ocp_path):
        req = need.get(ocp)
        sub = f'&ge; {req}' if req else 'listed'
        role = ('current' if i == 0 else 'target' if i == len(ocp_path) - 1
                else 'intermediate')
        head += (f'<th>OCP {ocp}<div class="th-sub">{role}, {sub}</div></th>')
    rows = ''
    for rel in vendor['table']:
        tags = ''
        if rel['installed']:
            tags += '<span class="badge grey">installed</span>'
        if rel.get('possible') and not vendor['installed']:
            tags += '<span class="badge warn">possible</span>'
        if rel['recommended'] and not rel['installed']:
            tags += '<span class="badge ok">recommended</span>'
        cells = ''
        for ocp in ocp_path:
            c = rel['openshift'][ocp]
            if c['certified'] is None:
                cells += '<td class="vt-bad">&#10007; not certified</td>'
            elif c['ok']:
                cells += f'<td class="vt-ok">&#10003; {c["certified"]}</td>'
            else:
                cells += f'<td class="vt-bad">&#10007; {c["certified"]}</td>'
        result = ('<span class="vt-ok">covers the path</span>' if rel['covers']
                  else '<span class="vt-bad">does not cover</span>')
        cls = (' class="vt-installed"' if rel['installed'] else
               ' class="vt-recommended"' if rel['recommended'] else '')
        rows += (f'<tr{cls}><td><strong>{rel["version"]}</strong> {tags}</td>'
                 f'<td>{rel["operator_min"]}+</td>{cells}<td>{result}</td></tr>')

    status = {'ok': ('ok', 'certified for this path'),
              'blocked': ('bad', 'blocks the upgrade'),
              'inferred': ('warn', 'release inferred, confirm it'),
              'unknown': ('warn', 'installed release unknown')}[vendor['status']]
    installed = vendor['installed'] or (
        f"not given ({', '.join(vendor['possible'])} possible with operator "
        f"{vendor['operator_version']})" if vendor.get('possible')
        else 'unknown')
    advice = ''
    rec = vendor.get('recommended')
    if vendor['status'] == 'blocked' and rec:
        if rec['version'] == vendor['installed']:
            advice = (f"Upgrade the operator to {rec['operator_min']} or later "
                      f"before the cluster upgrade.")
        else:
            advice = (f"Upgrade {vendor['label']} to {rec['version']} with "
                      f"operator {rec['operator_min']} or later before the "
                      f"cluster upgrade.")
    elif vendor['status'] == 'blocked' and not vendor['installed']:
        advice = (f"None of the releases operator {vendor['operator_version']} "
                  f"can run is certified for this path.")
    elif vendor['status'] in ('unknown', 'inferred'):
        advice = ("Confirm the installed release, or add it to the input "
                  "(component_versions).")
    return f"""<div class="vendor-box">
  <div class="vendor-head"><strong>{vendor['label']} support matrix</strong>
    &mdash; installed {installed}, operator {vendor['operator_version']}
    <span class="badge {status[0]}">{status[1]}</span>
    <a href="{vendor['source']}">source</a></div>
  {f'<div class="vendor-advice">{advice}</div>' if advice else ''}
  <table class="vendor-table">
    <tr><th>{vendor['label']}</th><th>Operator</th>{head}<th>Result</th></tr>
    {rows}
  </table>
</div>"""


def _vendor_row(vendor: Optional[Dict]) -> str:
    """The vendor support matrix row of an operator report."""
    if not vendor:
        return ''
    status = {'ok': ('ok', 'certified for the path'),
              'blocked': ('bad', 'blocks the upgrade'),
              'inferred': ('warn', 'release inferred'),
              'unknown': ('warn', 'not checked')}[vendor['status']]
    rec = vendor.get('recommended')
    rec_txt = (f" &rarr; upgrade to {rec['version']} with operator "
               f"{rec['operator_min']}+" if rec and vendor['status'] != 'ok'
               else '')
    return (f'<tr><td class="label">{vendor["label"]}</td><td>'
            f'{vendor["installed"] or "unknown"} '
            f'<span class="badge {status[0]}">{status[1]}</span>{rec_txt} '
            f'<a href="{vendor["source"]}">support matrix</a></td></tr>')


FONT_DIR = Path(__file__).resolve().parent / 'fonts'
FONTS = (('Red Hat Display', 'RedHatDisplayVF.woff2'),
         ('Red Hat Text', 'RedHatTextVF.woff2'),
         ('Red Hat Mono', 'RedHatMonoVF.woff2'))


def _install_fonts(html_dir: Path):
    """
    Copy the Red Hat fonts once into html/fonts/, shared by every page of
    the cluster, so the reports keep their look offline. Unchanged files are
    left alone; missing sources fall back to system fonts.
    """
    dest = html_dir / 'fonts'
    for name in [f for _, f in FONTS] + ['OFL.txt']:
        src = FONT_DIR / name
        if not src.is_file():
            continue
        target = dest / name
        if target.is_file() and target.read_bytes() == src.read_bytes():
            continue
        dest.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())


def _font_faces(root: str) -> str:
    return "".join(
        f'@font-face {{ font-family: "{family}"; src: url({root}fonts/{name}) '
        f'format("woff2"); font-weight: 300 900; font-style: normal; '
        f'font-display: swap; }}\n'
        for family, name in FONTS if (FONT_DIR / name).is_file())


def _page(title: str, body: str, heading: str, sub: str,
          root: str = '') -> str:
    """A page with the PatternFly masthead; root leads back to html/."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{_font_faces(root)}{CSS}</style></head>
<body>
<header class="masthead"><div class="inner"><h1>{heading}</h1>
<div class="sub">{sub}</div></div></header>
<div class="container">{body}
<footer>OLM operator upgrade path analyzer</footer></div></body></html>"""


def _phase_html(catalog: Dict, pkg: str, phase: Dict) -> str:
    status = phase['status']
    label = STATUS_LABEL.get(status, status)
    cls = {'no_action': 'ok', 'upgrade_required': 'warn'}.get(status, '')

    frm, to = phase['from'], phase['to']
    if phase['kind'] == 'current':
        kind = 'Current catalog, before the cluster upgrade'
        purpose = ("The installed bundle's maxOpenShiftVersion is below the "
                   "target. Upgrade to a version present in every catalog on "
                   "the path whose metadata supports the target, so the "
                   "operator stays valid through the whole upgrade.")
    elif phase['kind'] == 'intermediate':
        kind = 'Intermediate catalog'
        if phase.get('reason') == 'release_pinned':
            purpose = ("Release-pinned operator: upgraded with the cluster to "
                       "this release's version, as the strict EUS path "
                       "requires. Mirror and deploy this catalog.")
        else:
            purpose = ("The target catalog does not cover the installed "
                       "version. Mirror and deploy this catalog, and upgrade "
                       "from it first.")
    else:
        kind = 'Target catalog'
        purpose = "The catalog every operator is checked against first."
        if phase.get('done_on'):
            purpose += (f" Upgrade while the cluster is on {phase['done_on']}"
                        f": the installed version is no longer in that "
                        f"release's catalog.")

    info = f"""<table class="info">
  <tr><td class="label">Catalog</td><td><strong>OCP {phase['on_ocp']}</strong></td></tr>
  <tr><td class="label">Current channel</td><td>{frm['channel']}</td></tr>
  <tr><td class="label">Current version</td><td><strong>{frm['version']}</strong></td></tr>
  <tr><td class="label">Target channel</td><td>{to['channel']}</td></tr>
  <tr><td class="label">Target version</td><td><strong>{to['version']}</strong></td></tr>
  <tr><td class="label">Operator upgrades</td><td>{phase['hops']}{
      ' (channel switch only, no version change)'
      if not phase['hops'] and phase['steps'] else ''}</td></tr>
</table>"""

    if phase['steps']:
        steps = ""
        for i, s in enumerate(phase['steps'], 1):
            if s['via'] == 'channel-switch':
                steps += (f'<div class="step">{i}. Switch subscription channel '
                          f'<code>{s["from_channel"]}</code> &rarr; '
                          f'<code>{s["to_channel"]}</code> '
                          f'(stays on {s["to_version"]}, no upgrade)</div>')
            else:
                steps += (f'<div class="step">{i}. Upgrade to '
                          f'<strong>{s["to_version"]}</strong> in channel '
                          f'<code>{s["to_channel"]}</code> (via {s["via"]})</div>')
    else:
        steps = ('<div class="none">Nothing to do &mdash; this catalog still '
                 'ships the installed version.</div>')

    svg = _phase_graph(catalog, pkg, phase)
    graph = f'<div class="graph">{svg}</div>' if svg else ''

    return f"""<div class="phase">
  <div class="phase-head">
    <div class="phase-num">{phase['phase']}</div>
    <div><h2 style="margin:0">{kind} &mdash; OCP {phase['on_ocp']}</h2>
    <div style="font-size:13px;color:#666">{purpose}</div></div>
    <div style="margin-left:auto"><span class="badge {cls}">{label}</span></div>
  </div>
  {info}
  {graph}
  {steps}
</div>"""


def generate_operator_report(catalogs: Dict[str, Dict], result: Dict,
                             cluster: Dict, output_dir: str) -> str:
    """Write one HTML report for a single operator."""
    pkg = result['operator']
    ocp_path = cluster['ocp_path']

    verdict = result['verdict']
    badge = (f'<span class="badge {VERDICT_CLASS.get(verdict, "")}">'
             f'{VERDICT_LABEL.get(verdict, verdict)}</span>')
    pinned = ('<span class="badge grey">Version-pinned to OCP</span>'
              if result['version_pinned'] else '')

    total = sum(p['hops'] for p in result['phases'])
    inp = result['input']

    heading = pkg
    sub = (f"Operator upgrade plan &middot; {_cluster_label(cluster)}OCP "
           f"{cluster['current']} &rarr; {cluster['target']} "
           f"({cluster['channel']})")
    header = f"""<table class="info">
  <tr><td class="label">Verdict</td><td>{badge}{pinned}</td></tr>
  <tr><td class="label">Cluster path</td><td>{' &rarr; '.join(cluster.get('upgrade_path') or ocp_path)}</td></tr>
  <tr><td class="label">Catalog image</td><td><code>{result.get('catalog_image', '')}</code></td></tr>
  <tr><td class="label">Installed channel</td><td>{inp.get('resolved_channel', inp['channel'])}</td></tr>
  <tr><td class="label">Installed version</td><td><strong>{inp.get('resolved_version', inp['version'])}</strong></td></tr>
  <tr><td class="label">Max OCP version</td><td>{result.get('max_ocp_version') or '&mdash;'}</td></tr>
  {_vendor_row(result.get('vendor'))}
  <tr><td class="label">Catalogs needed</td><td>{', '.join(result['catalogs']) or '&mdash;'}</td></tr>
  <tr><td class="label">Operator upgrades</td><td>{total}</td></tr>
</table>"""

    if result['phases']:
        phases = "".join(
            _phase_html(catalogs[p['on_ocp']], pkg, p) for p in result['phases'])
    else:
        phases = '<div class="none">No phases planned.</div>'

    if result.get('vendor'):
        header += ('<h2 style="margin-top:24px">Vendor support matrix</h2>'
                   + _vendor_table(result['vendor'], ocp_path))

    notes = ""
    if result['notes']:
        items = "".join(f"<li>{n}</li>" for n in result['notes'])
        notes = f'<div class="notes"><h3>Notes</h3><ul>{items}</ul></div>'

    body = (header + '<h2 style="margin-top:30px">Catalogs</h2>' + phases + notes
            + '<p style="margin-top:26px"><a href="../index.html">'
              '&larr; Back to cluster summary</a></p>')

    out = Path(output_dir) / 'html' / pkg
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'index.html'
    _install_fonts(Path(output_dir) / 'html')
    path.write_text(_page(f"{pkg} - upgrade plan", body, heading, sub,
                          root='../'))
    return str(path)


def generate_summary_report(plan: Dict, output_dir: str) -> str:
    """Write the cluster-level summary page linking each operator report."""
    cluster = plan['cluster']
    ocp_path = cluster['ocp_path']
    verdict = plan['verdict']
    badge = (f'<span class="badge {VERDICT_CLASS.get(verdict, "")}">'
             f'{VERDICT_LABEL.get(verdict, verdict)}</span>')

    # Columns are catalogs, oldest first. The target catalog is always
    # checked; earlier ones only appear when the target cannot cover an
    # operator.
    head = ""
    for ocp in ocp_path:
        sub = ('target catalog' if ocp == cluster['target']
               else 'current catalog, before the upgrade' if ocp == ocp_path[0]
               else 'intermediate, only if needed')
        head += (f'<th>From the {ocp} catalog'
                 f'<div class="th-sub">{sub}</div></th>')

    rows = ""
    for op in plan['operators']:
        v = op['verdict']
        cells = "".join(_matrix_cell(c, ocp_path)
                        for c in op.get('matrix') or [])

        inp = op['input']
        rows += (f'<tr><td><a href="{op["operator"]}/index.html">'
                 f'{op["operator"]}</a></td>'
                 f'<td>{inp.get("resolved_channel", inp["channel"])}<br>'
                 f'<strong>{inp.get("resolved_version", inp["version"])}'
                 f'</strong></td>'
                 f'<td><span class="badge {VERDICT_CLASS.get(v, "")}">'
                 f'{VERDICT_LABEL.get(v, v)}</span></td>{cells}</tr>')
        if op.get('vendor'):
            rows += (f'<tr class="vendor-row"><td colspan="{3 + len(ocp_path)}">'
                     f'{_vendor_table(op["vendor"], ocp_path)}</td></tr>')

    def listing(names, label, cls):
        if not names:
            return ""
        return (f'<tr><td class="label">{label}</td><td>'
                + "".join(f'<span class="badge {cls}">{n}</span>' for n in names)
                + '</td></tr>')

    heading = ("Cluster Operator Upgrade Plan"
               + (f" &mdash; {cluster['name']}" if cluster.get('name') else ''))
    sub = (f"OCP {cluster['current']} &rarr; {cluster['target']} "
           f"({cluster['channel']} channel)")
    body = f"""<table class="info">
  <tr><td class="label">Overall verdict</td><td>{badge}</td></tr>
  <tr><td class="label">Cluster path</td><td>{' &rarr; '.join(cluster.get('upgrade_path') or ocp_path)}</td></tr>
  <tr><td class="label">Operators analysed</td><td>{len(plan['operators'])}</td></tr>
  {listing(plan['blocking_operators'], 'Blocked', 'bad')}
  {listing(plan['intermediate_catalog_operators'], 'Intermediate catalog required', 'bad')}
  {listing(plan['manual_review_operators'], 'Manual review', 'warn')}
  {listing(plan['operators_requiring_upgrade'], 'Upgrade required', 'warn')}
</table>

<h2 style="margin-top:30px">Operators</h2>
<div class="legend">Every operator is checked against the target catalog first:
it is covered when that catalog still ships the installed version, or has a
skipRange, replaces or skips edge from it. Only when it is not covered does an
earlier catalog appear, working backwards from the target, and that catalog
must be mirrored and deployed too.</div>
<div class="tblwrap"><table class="ops">
  <tr><th>Operator</th><th>Installed</th><th>Verdict</th>{head}</tr>
  {rows}
</table></div>"""

    body += _mirror_html(plan)

    out = Path(output_dir) / 'html'
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'index.html'
    _install_fonts(out)
    path.write_text(_page(cluster.get('name') or "Cluster Operator Upgrade Plan",
                          body, heading, sub))
    return str(path)


def _vendor_line(v: Optional[Dict]) -> str:
    """The vendor support matrix's verdict for one OpenShift release."""
    if not v:
        return ''
    name = (f"{v['label']} {v['installed']}" if v['installed'] else
            f"{v['label']} {v['inferred']} (inferred)" if v.get('inferred')
            else v['label'])
    if v['ok'] is None:
        return (f'<div class="cell-vendor warn">&#9888; {v["label"]} '
                f'version unknown</div>')
    short = v.get('operator_short')
    op = (f'<div class="cell-vendor bad">&#10007; operator '
          f'{short["installed"]}, {name} needs {short["required"]}+</div>'
          if short else '')
    if v['ok']:
        return (f'<div class="cell-vendor ok">&#10003; {name} certified '
                f'{v["certified"]}</div>{op}')
    if v['certified'] is None:
        return (f'<div class="cell-vendor bad">&#10007; {name} not '
                f'certified on this release</div>')
    return (f'<div class="cell-vendor bad">&#10007; {name} certified '
            f'{v["certified"]}, needs {v["required"]}</div>')


def _matrix_cell(cell: Dict, ocp_path: List[str]) -> str:
    """One cell of the summary matrix, with the vendor matrix's verdict."""
    html = _matrix_cell_body(cell, ocp_path)
    line = _vendor_line(cell.get('vendor'))
    return html[:-len('</td>')] + line + '</td>' if line else html


def _matrix_cell_body(cell: Dict, ocp_path: List[str]) -> str:
    kind = cell['kind']
    if kind == 'upgrade':
        hops = cell['hops']
        what = (f"{hops} upgrade" + ('s' if hops != 1 else '')
                if hops else 'channel switch only')
        if cell.get('replaces_missing'):
            what += (f" on this release; {cell['replaces_missing']} not in "
                     f"this catalog")
        return (f'<td>{cell["channel"]}<br><strong>{cell["version"]}</strong>'
                f'<div class="cell-sub">{what}</div></td>')
    if kind == 'newer':
        return (f'<td>{cell["channel"]}<br><strong>{cell["version"]}</strong>'
                f'<div class="cell-sub">upgrade available; '
                f'{cell["from_version"]} not in this catalog</div></td>')
    if kind == 'no_action':
        when = (f'<div class="cell-sub">upgraded on {cell["upgraded_on"]}'
                f'</div>' if cell.get('upgraded_on') else '')
        latest = cell.get('available')
        if latest:
            # nothing required here; say what can optionally be taken
            chan = (f'{latest["channel"]}<br>' if latest['channel_changes']
                    else '')
            return (f'<td class="cell-opt">{chan}<strong>{latest["version"]}'
                    f'</strong> available<div class="cell-sub">optional'
                    f'</div></td>')
        return f'<td><span class="cell-ok">&#10003; no action</span>{when}</td>'
    if kind == 'max':
        top = cell['max_ocp_version']
        return (f'<td class="cell-meta">metadata maxOpenShiftVersion {top}'
                f'<div class="cell-sub">supports {ocp_path[0]} to {top}'
                f'</div></td>')
    if kind == 'present':
        # nothing to do here; the optional latest is shown at the target
        return '<td><span class="cell-ok">&#10003; no action</span></td>'
    if kind == 'missing':
        return ('<td class="cell-warn">&#9888; not in this catalog'
                '<div class="cell-sub">no maxOpenShiftVersion in metadata'
                '</div></td>')
    if kind == 'unknown':
        return ('<td class="cell-warn">&#9888; bundle metadata not available'
                '<div class="cell-sub">supported releases unknown</div></td>')
    return ('<td class="cell-warn">&#9888; not found'
            '<div class="cell-sub">see the operator notes</div></td>')


def _mirror_html(plan: Dict) -> str:
    """The catalog images to mirror, per release."""
    rows = "".join(
        f'<tr><td class="label">OCP {ocp}</td><td>'
        + "<br>".join(f"<code>{img}</code>" for img in images) + '</td></tr>'
        for ocp, images in plan['catalogs_to_mirror'].items())
    return f"""
<h2 style="margin-top:30px">Catalogs to mirror</h2>
<div class="legend">The oc-mirror configuration listing the packages, channels
and versions needed from each is written to
<code>{plan['imageset_config']}</code>.</div>
<table class="info">{rows}</table>"""
