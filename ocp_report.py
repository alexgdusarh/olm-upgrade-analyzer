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
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background:#f5f5f5; margin:0; padding:20px; }
.container { max-width:1400px; margin:0 auto; background:#fff; border-radius:12px;
             box-shadow:0 2px 10px rgba(0,0,0,.1); padding:40px; }
h1 { color:#333; margin:0 0 6px; }
h2 { color:#333; font-size:18px; margin:0 0 16px; }
.subtitle { color:#666; margin-bottom:26px; font-size:14px; }
table.info { width:100%; border-collapse:collapse; margin-bottom:18px;
             border-left:4px solid #667eea; background:#f9f9f9; }
table.info td { padding:10px 15px; border-bottom:1px solid #e6e6e6; font-size:14px; color:#333; }
table.info tr:last-child td { border-bottom:none; }
table.info td.label { font-weight:600; color:#667eea; width:220px; }
.badge { display:inline-block; padding:4px 11px; border-radius:20px; font-size:12px;
         font-weight:600; color:#fff; background:#667eea; margin-right:6px; }
.badge.ok { background:#4caf50; } .badge.warn { background:#f0a020; }
.badge.bad { background:#e05252; } .badge.grey { background:#9e9e9e; }
.phase { border:1px solid #e0e0e0; border-radius:8px; padding:24px; margin-bottom:26px;
         background:#fcfcfc; }
.phase-head { display:flex; align-items:center; gap:12px; margin-bottom:16px; }
.phase-num { background:#667eea; color:#fff; border-radius:50%; width:34px; height:34px;
             display:flex; align-items:center; justify-content:center; font-weight:bold;
             flex-shrink:0; }
.graph { border:1px solid #ddd; border-radius:8px; background:#fafafa; padding:16px;
         text-align:center; overflow-x:auto; margin-bottom:16px; }
.graph svg { max-width:100%; height:auto; }
.step { background:#f4f4f8; border-left:4px solid #667eea; padding:11px 14px;
        border-radius:4px; margin-bottom:9px; font-size:14px; color:#333; }
.none { color:#777; font-style:italic; font-size:14px; padding:8px 0; }
.notes { background:#fff8e1; border-left:4px solid #f0a020; padding:16px;
         border-radius:4px; margin-top:22px; }
.notes h3 { margin:0 0 8px; color:#8a6100; font-size:15px; }
.notes li { color:#8a6100; font-size:13.5px; margin-bottom:6px; }
table.ops { width:100%; border-collapse:collapse; margin-top:10px; }
table.ops th { text-align:left; padding:11px 14px; background:#667eea; color:#fff;
               font-size:13px; }
table.ops td { padding:11px 14px; border-bottom:1px solid #eee; font-size:14px; color:#333; }
table.ops tr:hover td { background:#fafafa; }
table.ops th { vertical-align:top; }
.th-sub { font-weight:400; font-size:11px; opacity:.85; margin-top:3px; }
.cell-sub { font-size:11.5px; color:#777; margin-top:3px; }
.cell-ok { color:#4caf50; } .cell-bad { color:#e05252; }
.cell-none { color:#bbb; text-align:center; }
.cell-meta { color:#555; font-size:12.5px; }
.cell-opt { color:#3f51b5; }
.vendor-row td { background:#fafbff; padding:6px 14px 16px; }
.vendor-box { border:1px solid #d6dbf5; border-radius:8px; padding:12px 14px;
              background:#fff; }
.vendor-head { font-size:14px; color:#333; margin-bottom:6px; }
.vendor-advice { font-size:13px; color:#e05252; font-weight:600; margin:4px 0 8px; }
table.vendor-table { width:100%; border-collapse:collapse; margin-top:6px; }
table.vendor-table th { background:#eef0fb; color:#333; font-size:12px; text-align:left;
                        padding:7px 10px; }
table.vendor-table td { font-size:13px; padding:6px 10px; border-bottom:1px solid #eee; }
table.vendor-table tr.vt-installed td { background:#f3f3f3; }
table.vendor-table tr.vt-recommended td { background:#eef8ee; }
.vt-ok { color:#2e7d32; } .vt-bad { color:#e05252; }
.cell-vendor { font-size:11.5px; margin-top:5px; padding-top:4px;
               border-top:1px dashed #ddd; }
.cell-vendor.ok { color:#4caf50; } .cell-vendor.bad { color:#e05252; }
.cell-vendor.warn { color:#8a6100; }
.cell-warn { color:#8a6100; background:#fff8e1; font-size:12.5px; }
.cell-rule { border:none; border-top:1px dashed #ddd; margin:8px 0; }
.legend { font-size:13px; color:#666; background:#f7f7fa; border-left:3px solid #ccd;
          padding:11px 14px; border-radius:4px; margin-bottom:14px; }
a { color:#667eea; text-decoration:none; } a:hover { text-decoration:underline; }
code { background:#eef; padding:1px 5px; border-radius:3px; font-size:13px; }
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


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{CSS}</style></head>
<body><div class="container">{body}</div></body></html>"""


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

    header = f"""<h1>{pkg}</h1>
<div class="subtitle">Operator upgrade plan for the {_cluster_label(cluster)}OCP
{cluster['current']} &rarr; {cluster['target']} ({cluster['channel']}) cluster upgrade</div>
<table class="info">
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
    path.write_text(_page(f"{pkg} - upgrade plan", body))
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

    body = f"""<h1>Cluster Operator Upgrade Plan{
        f' &mdash; {cluster["name"]}' if cluster.get('name') else ''}</h1>
<div class="subtitle">OCP {cluster['current']} &rarr; {cluster['target']}
({cluster['channel']} channel)</div>
<table class="info">
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
<table class="ops">
  <tr><th>Operator</th><th>Installed</th><th>Verdict</th>{head}</tr>
  {rows}
</table>"""

    body += _mirror_html(plan)

    out = Path(output_dir) / 'html'
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'index.html'
    path.write_text(_page(cluster.get('name') or "Cluster Operator Upgrade Plan",
                          body))
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
