#!/usr/bin/env python3
"""Render validated JSON flowcharts to draw.io source (.drawio), SVG, and PNG."""
import argparse
import html
import json
import math
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

SHAPES = {"start": "oval", "end": "oval", "process": "box", "decision": "diamond", "input_output": "parallelogram"}
PORTS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}
PORTS_MAP = {
    'n': (0.5, 0),
    's': (0.5, 1),
    'w': (0, 0.5),
    'e': (1, 0.5),
    'nw': (0, 0),
    'ne': (1, 0),
    'sw': (0, 1),
    'se': (1, 1),
}
ID_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9-]*$")

class DiagramError(ValueError):
    pass

def fail(msg):
    raise DiagramError(msg)

def wrap_label(value, name, width):
    if not isinstance(value, str) or not value.strip():
        fail(f"{name}: expected a non-empty string")
    if len(value) > 2000:
        fail(f"{name}: text is too long (max 2000 characters)")
    lines = []
    for part in value.splitlines():
        lines.extend(textwrap.wrap(part, width=width, break_long_words=True, break_on_hyphens=False, replace_whitespace=False) or [""])
    return "\\n".join(escape(line) for line in lines)

def escape(value):
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '')

def quote(value):
    return '"' + escape(value) + '"'

def position(value, name):
    if not isinstance(value, dict) or set(value) != {'x', 'y'}:
        fail(f"{name}: expected {{'x': number, 'y': number}}")
    result = []
    for axis in ('x', 'y'):
        x = value[axis]
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 10000:
            fail(f"{name}.{axis}: expected a finite number in [-10000, 10000]")
        result.append(float(x))
    return result

def validate(doc):
    if not isinstance(doc, dict) or set(doc) != {'mode', 'nodes', 'edges'}:
        fail("Top level must contain exactly: mode, nodes, edges")
    mode = doc['mode']
    if not isinstance(mode, str) or mode not in ('auto', 'manual'):
        fail("mode must be 'auto' or 'manual'")
    nodes, edges = doc['nodes'], doc['edges']
    if not isinstance(nodes, list) or not nodes or len(nodes) > 300:
        fail("nodes must contain 1–300 blocks")
    if not isinstance(edges, list) or len(edges) > 900:
        fail("edges must be a list of at most 900 connections")
    ids = set()
    for i, node in enumerate(nodes):
        n = f"nodes[{i}]"
        required = {'id', 'type', 'text'} | ({'position'} if mode == 'manual' else set())
        if not isinstance(node, dict) or set(node) != required:
            fail(f"{n}: expected exactly {', '.join(sorted(required))}")
        ident = node['id']
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or len(ident) > 64 or ident in ids:
            fail(f"{n}.id: invalid or duplicate ID (use letters, digits, _, -; start with letter or _)")
        ids.add(ident)
        if not isinstance(node['type'], str) or node['type'] not in SHAPES:
            fail(f"{n}.type: choose one of {', '.join(SHAPES)}")
        wrap_label(node['text'], f"{n}.text", 24)
        if mode == 'manual':
            position(node['position'], f"{n}.position")
    for i, edge in enumerate(edges):
        n = f"edges[{i}]"
        allowed = {'from', 'to', 'label', 'from_port', 'to_port'}
        if mode == 'manual':
            allowed.add('points')
        if not isinstance(edge, dict) or not {'from', 'to'} <= set(edge) or set(edge) - allowed:
            fail(f"{n}: expected from, to and optional label, from_port, to_port" + (", points" if mode == 'manual' else ""))
        if not isinstance(edge['from'], str) or not isinstance(edge['to'], str) or edge['from'] not in ids or edge['to'] not in ids:
            fail(f"{n}: from/to must reference existing node IDs")
        if edge['from'] == edge['to'] and mode != 'manual':
            fail(f"{n}: self-connections are not supported")
        if 'label' in edge:
            wrap_label(edge['label'], f"{n}.label", 16)
        for key in ('from_port', 'to_port'):
            if key in edge and edge[key] not in PORTS:
                fail(f"{n}.{key}: choose n, s, e, w, ne, nw, se, sw")
        if 'points' in edge:
            if not isinstance(edge['points'], list):
                fail(f"{n}.points: expected list of coordinates")
            for j, p in enumerate(edge['points']):
                if isinstance(p, dict):
                    position(p, f"{n}.points[{j}]")
                elif isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) and math.isfinite(v) and abs(v) <= 10000 for v in p):
                    pass
                else:
                    fail(f"{n}.points[{j}]: expected {{'x': number, 'y': number}} or [x, y]")
    return mode

def dot_source(doc, mode):
    lines = [
        'digraph Flowchart {',
        'graph [charset="UTF-8", bgcolor="white", pad="0.35", outputorder="edgesfirst", splines="polyline", overlap="false", fontname="Arial"];',
        'node [fontname="Arial", fontsize=13, fontcolor="black", style="filled", fillcolor="#ffffff", color="#000000", penwidth=1.4, margin="0.20,0.14", fixedsize=false];',
        'edge [fontname="Arial", fontsize=11, fontcolor="black", color="#000000", arrowsize=0.75];'
    ]
    if mode == 'auto':
        lines.append('graph [rankdir="TB", nodesep=0.55, ranksep=0.8];')
    for node in doc['nodes']:
        attrs = [f'shape={quote(SHAPES[node["type"]])}', f'label="{wrap_label(node["text"], "text", 24)}"']
        if mode == 'manual':
            x, y = position(node['position'], 'position')
            attrs.append(f'pos="{x/72:.6f},{-y/72:.6f}!"')
            attrs.append('pin=true')
        lines.append(f'{quote(node["id"])} [{", ".join(attrs)}];')
    for edge in doc['edges']:
        left, right = quote(edge['from']), quote(edge['to'])
        if 'from_port' in edge:
            left += ':' + edge['from_port']
        if 'to_port' in edge:
            right += ':' + edge['to_port']
        attrs = []
        if 'label' in edge:
            attrs.append(f'label="{wrap_label(edge["label"], "label", 16)}"')
        lines.append(f'{left} -> {right}' + (f' [{", ".join(attrs)}]' if attrs else '') + ';')
    lines.append('}')
    return '\n'.join(lines)

def generate_drawio(doc, mode):
    """Generate genuine editable draw.io (diagrams.net) uncompressed XML source."""
    graph = {}
    shift_x, shift_y = 0, 0

    if mode == 'manual':
        font_path = r'C:\Windows\Fonts\arial.ttf'
        font = None
        try:
            from PIL import ImageFont
            if Path(font_path).is_file():
                font = ImageFont.truetype(font_path, 15)
        except Exception:
            font = None

        for node in doc['nodes']:
            x = float(node['position']['x'])
            y = float(node['position']['y'])
            text = node['text']
            lines = text.splitlines()
            if font:
                max_width = max((font.getlength(line) for line in lines), default=50)
            else:
                max_width = max((len(line) * 9 for line in lines), default=50)
            pad = 36 if node['type'] == 'decision' else 25 if node['type'] == 'input_output' else 18
            w = max(88, max_width + 2 * pad)
            h = max(48, len(lines) * 20 + (62 if node['type'] == 'decision' else 25))
            if node['type'] == 'decision':
                w = max(w, max_width * 2 + 64)

            w = int(math.ceil(w / 10.0) * 10)
            h = int(math.ceil(h / 10.0) * 10)
            graph[node['id']] = {
                'x': x, 'y': y, 'w': w, 'h': h,
                'top_left_x': int(round(x - w / 2)),
                'top_left_y': int(round(y - h / 2)),
                'type': node['type'],
                'text': text
            }

        all_x = [n['top_left_x'] for n in graph.values()] + [n['top_left_x'] + n['w'] for n in graph.values()]
        all_y = [n['top_left_y'] for n in graph.values()] + [n['top_left_y'] + n['h'] for n in graph.values()]
        for edge in doc['edges']:
            if 'points' in edge:
                for p in edge['points']:
                    px = p['x'] if isinstance(p, dict) else p[0]
                    py = p['y'] if isinstance(p, dict) else p[1]
                    all_x.append(px)
                    all_y.append(py)

        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
        shift_x = max(0, 40 - min_x) if min_x < 40 else 0
        shift_y = max(0, 40 - min_y) if min_y < 40 else 0
        page_w = max(827, int(max_x + shift_x + 80))
        page_h = max(1169, int(max_y + shift_y + 80))

    else:
        source = dot_source(doc, 'auto')
        try:
            proc = subprocess.run(
                ['dot', '-Tjson'],
                input=source,
                text=True,
                encoding='utf-8',
                errors='replace',
                capture_output=True,
                timeout=30,
                check=True
            )
            dot_data = json.loads(proc.stdout)
            bb = [float(v) for v in dot_data['bb'].split(',')]
            graph_h = bb[3]
            for obj in dot_data.get('objects', []):
                nid = obj.get('name')
                matching_node = next((n for n in doc['nodes'] if n['id'] == nid), None)
                if not matching_node:
                    continue
                pos = [float(v) for v in obj['pos'].split(',')]
                w = int(math.ceil(float(obj['width']) * 72 / 10.0) * 10)
                h = int(math.ceil(float(obj['height']) * 72 / 10.0) * 10)
                cx = pos[0] + 40
                cy = (graph_h - pos[1]) + 40
                graph[nid] = {
                    'x': cx, 'y': cy, 'w': w, 'h': h,
                    'top_left_x': int(round(cx - w / 2)),
                    'top_left_y': int(round(cy - h / 2)),
                    'type': matching_node['type'],
                    'text': matching_node['text']
                }
            page_w = max(827, int(bb[2] + 100))
            page_h = max(1169, int(bb[3] + 100))
        except Exception as exc:
            fail(f"Graphviz dot JSON layout failed: {exc}")

    mxfile = ET.Element('mxfile', {
        'host': 'app.diagrams.net',
        'modified': '2026-11-06T08:00:00.000Z',
        'agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'version': '24.7.17',
        'type': 'device'
    })
    diagram = ET.SubElement(mxfile, 'diagram', {'id': 'flowchart', 'name': 'Страница-1'})
    model = ET.SubElement(diagram, 'mxGraphModel', {
        'dx': '1200', 'dy': '800', 'grid': '1', 'gridSize': '10', 'guides': '1',
        'tooltips': '1', 'connect': '1', 'arrows': '1', 'fold': '1', 'page': '1',
        'pageScale': '1', 'pageWidth': str(page_w), 'pageHeight': str(page_h),
        'math': '0', 'shadow': '0'
    })
    root = ET.SubElement(model, 'root')
    ET.SubElement(root, 'mxCell', {'id': '0'})
    ET.SubElement(root, 'mxCell', {'id': '1', 'parent': '0'})

    base_font = 'fontFamily=Arial;fontSize=14;fontColor=#000000;'
    styles = {
        'start': f'rounded=1;whiteSpace=wrap;html=1;arcSize=50;strokeColor=#000000;strokeWidth=1.5;fillColor=#ffffff;{base_font}',
        'end': f'rounded=1;whiteSpace=wrap;html=1;arcSize=50;strokeColor=#000000;strokeWidth=1.5;fillColor=#ffffff;{base_font}',
        'process': f'rounded=0;whiteSpace=wrap;html=1;strokeColor=#000000;strokeWidth=1.5;fillColor=#ffffff;{base_font}',
        'decision': f'rhombus;whiteSpace=wrap;html=1;strokeColor=#000000;strokeWidth=1.5;fillColor=#ffffff;{base_font}',
        'input_output': f'shape=parallelogram;perimeter=parallelogramPerimeter;whiteSpace=wrap;html=1;fixedSize=1;size=20;strokeColor=#000000;strokeWidth=1.5;fillColor=#ffffff;{base_font}'
    }

    for nid, node in graph.items():
        val = html.escape(node['text']).replace('\n', '<br>')
        cell = ET.SubElement(root, 'mxCell', {
            'id': nid,
            'value': val,
            'style': styles.get(node['type'], styles['process']),
            'vertex': '1',
            'parent': '1'
        })
        ET.SubElement(cell, 'mxGeometry', {
            'x': str(node['top_left_x'] + shift_x),
            'y': str(node['top_left_y'] + shift_y),
            'width': str(node['w']),
            'height': str(node['h']),
            'as': 'geometry'
        })

    for i, edge in enumerate(doc['edges']):
        src = graph.get(edge['from'])
        dst = graph.get(edge['to'])
        if not src or not dst:
            continue
        edge_id = f'e_{edge["from"]}_{edge["to"]}_{i}'

        dx = dst['x'] - src['x']
        dy = dst['y'] - src['y']
        default_a = 'e' if abs(dx) > abs(dy) and dx >= 0 else 'w' if abs(dx) > abs(dy) else 's' if dy >= 0 else 'n'
        default_b = 'w' if default_a == 'e' else 'e' if default_a == 'w' else 'n' if default_a == 's' else 's'

        from_p = edge.get('from_port', default_a)
        to_p = edge.get('to_port', default_b)

        px1, py1 = PORTS_MAP.get(from_p, (0.5, 1))
        px2, py2 = PORTS_MAP.get(to_p, (0.5, 0))

        edge_style = (
            f'edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;'
            f'exitX={px1};exitY={py1};exitDx=0;exitDy=0;'
            f'entryX={px2};entryY={py2};entryDx=0;entryDy=0;'
            f'strokeColor=#000000;strokeWidth=1.5;endArrow=classic;endFill=1;'
            f'fontFamily=Arial;fontSize=12;fontColor=#000000;labelBackgroundColor=#ffffff;'
        )

        label_val = edge.get('label', '')
        if label_val:
            label_val = html.escape(label_val).replace('\n', '<br>')

        edge_cell = ET.SubElement(root, 'mxCell', {
            'id': edge_id,
            'value': label_val,
            'style': edge_style,
            'edge': '1',
            'parent': '1',
            'source': edge['from'],
            'target': edge['to']
        })

        geom = ET.SubElement(edge_cell, 'mxGeometry', {'relative': '1', 'as': 'geometry'})
        if label_val:
            ET.SubElement(geom, 'mxPoint', {'as': 'offset', 'y': '-10'})

        if 'points' in edge and edge['points']:
            arr = ET.SubElement(geom, 'Array', {'as': 'points'})
            for pt in edge['points']:
                px = pt['x'] if isinstance(pt, dict) else pt[0]
                py = pt['y'] if isinstance(pt, dict) else pt[1]
                ET.SubElement(arr, 'mxPoint', {
                    'x': str(int(round(px + shift_x))),
                    'y': str(int(round(py + shift_y)))
                })

    rough_string = ET.tostring(mxfile, encoding='utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode('utf-8')

def render_manual(doc, output, drawio_xml=None):
    """Draw exact pixel-positioned nodes and routed arrows to draw.io, standalone SVG and PNG."""
    try:
        import cairosvg
        from PIL import ImageFont
    except ImportError as exc:
        fail(f"Manual mode needs Pillow and CairoSVG: {exc}")

    font_path = r'C:\Windows\Fonts\arial.ttf'
    if not Path(font_path).is_file():
        alt_paths = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/System/Library/Fonts/Supplemental/Arial.ttf',
            '/Library/Fonts/Arial.ttf'
        ]
        for p in alt_paths:
            if Path(p).is_file():
                font_path = p
                break
        if not Path(font_path).is_file():
            fail(f"Font not found: {font_path}")

    font = ImageFont.truetype(font_path, 15)
    small_font = ImageFont.truetype(font_path, 12)
    graph = {}
    for node in doc['nodes']:
        x, y = position(node['position'], 'position')
        text = node['text']
        lines = []
        for paragraph in text.splitlines():
            lines.extend(textwrap.wrap(paragraph, 24, break_long_words=True, break_on_hyphens=False) or [''])
        max_width = max(font.getlength(line) for line in lines)
        pad = 36 if node['type'] == 'decision' else 25 if node['type'] == 'input_output' else 18
        w = max(88, max_width + 2 * pad)
        h = max(48, len(lines) * 20 + (62 if node['type'] == 'decision' else 25))
        if node['type'] == 'decision':
            w = max(w, max_width * 2 + 64)
        graph[node['id']] = dict(x=x, y=y, w=w, h=h, lines=lines, type=node['type'])

    left = min(n['x'] - n['w']/2 for n in graph.values())
    right = max(n['x'] + n['w']/2 for n in graph.values())
    top = min(n['y'] - n['h']/2 for n in graph.values())
    bottom = max(n['y'] + n['h']/2 for n in graph.values())
    for edge in doc['edges']:
        if 'points' in edge:
            for p in edge['points']:
                px = p['x'] if isinstance(p, dict) else p[0]
                py = p['y'] if isinstance(p, dict) else p[1]
                left = min(left, px)
                right = max(right, px)
                top = min(top, py)
                bottom = max(bottom, py)

    minx, miny = min(0, left - 80), min(0, top - 80)
    width, height = math.ceil(right - minx + 100), math.ceil(bottom - miny + 100)
    if width > 16000 or height > 16000 or width * height > 40000000:
        fail('Manual image too large; reduce coordinate spread')
    for edge in doc['edges']:
        if edge.get('label') and len(edge['label']) > 120:
            fail('Manual edge labels must be at most 120 characters')

    svg_attrs = {
        'xmlns': 'http://www.w3.org/2000/svg',
        'width': str(width),
        'height': str(height),
        'viewBox': f'{minx} {miny} {width} {height}'
    }
    if drawio_xml:
        svg_attrs['content'] = drawio_xml

    svg = ET.Element('svg', svg_attrs)
    defs = ET.SubElement(svg, 'defs')
    marker = ET.SubElement(defs, 'marker', {
        'id': 'arrow', 'viewBox': '0 0 10 10', 'refX': '9', 'refY': '5',
        'markerWidth': '8', 'markerHeight': '8', 'orient': 'auto-start-reverse'
    })
    ET.SubElement(marker, 'path', {'d': 'M 0 0 L 10 5 L 0 10 z', 'fill': '#000000'})
    ET.SubElement(svg, 'rect', {'x': str(minx), 'y': str(miny), 'width': str(width), 'height': str(height), 'fill': 'white'})

    def anchor(node, side):
        x, y, w, h = (node[k] for k in ('x', 'y', 'w', 'h'))
        if node['type'] == 'decision' and side in ('ne', 'nw', 'se', 'sw'):
            return (x + (-1 if 'w' in side else 1) * w / 4, y + (-1 if 'n' in side else 1) * h / 4)
        dx = {'w': -1, 'e': 1, 'n': 0, 's': 0, 'nw': -0.65, 'ne': 0.65, 'sw': -0.65, 'se': 0.65}[side]
        dy = {'w': 0, 'e': 0, 'n': -1, 's': 1, 'nw': -0.65, 'ne': -0.65, 'sw': -0.65, 'se': 0.65}[side]
        if node['type'] in ('start', 'end'):
            scale = 1 / math.hypot(dx, dy)
            dx, dy = dx * scale, dy * scale
        return x + dx * w / 2, y + dy * h / 2

    for edge in doc['edges']:
        a, b = graph[edge['from']], graph[edge['to']]
        dx, dy = b['x'] - a['x'], b['y'] - a['y']
        default_a = 'e' if abs(dx) > abs(dy) and dx >= 0 else 'w' if abs(dx) > abs(dy) else 's' if dy >= 0 else 'n'
        default_b = 'w' if default_a == 'e' else 'e' if default_a == 'w' else 'n' if default_a == 's' else 's'
        x1, y1 = anchor(a, edge.get('from_port', default_a))
        x2, y2 = anchor(b, edge.get('to_port', default_b))
        pts = [(x1, y1)]
        if 'points' in edge:
            for p in edge['points']:
                px = p['x'] if isinstance(p, dict) else p[0]
                py = p['y'] if isinstance(p, dict) else p[1]
                pts.append((float(px), float(py)))
        pts.append((x2, y2))
        d_path = 'M ' + ' L '.join(f'{px:.2f} {py:.2f}' for px, py in pts)
        ET.SubElement(svg, 'path', {'d': d_path, 'fill': 'none', 'stroke': '#000000', 'stroke-width': '1.7', 'marker-end': 'url(#arrow)'})
        if edge.get('label'):
            label_lines = edge['label'].splitlines()
            mx, my = (pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2 - 8
            lw = max(small_font.getlength(line) for line in label_lines) + 10
            lh = len(label_lines) * 16 + 5
            ET.SubElement(svg, 'rect', {'x': f'{mx-lw/2:.2f}', 'y': f'{my-lh+5:.2f}', 'width': f'{lw:.2f}', 'height': str(lh), 'fill': 'white'})
            for j, line in enumerate(label_lines):
                t = ET.SubElement(svg, 'text', {
                    'x': f'{mx:.2f}', 'y': f'{my-(len(label_lines)-1-j)*16:.2f}',
                    'font-family': 'Arial, Helvetica, sans-serif', 'font-size': '12',
                    'text-anchor': 'middle', 'fill': '#000000'
                })
                t.text = line

    for node in graph.values():
        x, y, w, h = (node[k] for k in ('x', 'y', 'w', 'h'))
        attrs = {'fill': '#ffffff', 'stroke': '#000000', 'stroke-width': '1.6'}
        typ = node['type']
        if typ in ('start', 'end'):
            ET.SubElement(svg, 'rect', dict(attrs, x=str(x - w / 2), y=str(y - h / 2), width=str(w), height=str(h), rx=str(h / 2), ry=str(h / 2)))
        elif typ == 'decision':
            ET.SubElement(svg, 'polygon', dict(attrs, points=f'{x},{y-h/2} {x+w/2},{y} {x},{y+h/2} {x-w/2},{y}'))
        elif typ == 'input_output':
            skew = min(18, w / 7)
            ET.SubElement(svg, 'polygon', dict(attrs, points=f'{x-w/2+skew},{y-h/2} {x+w/2},{y-h/2} {x+w/2-skew},{y+h/2} {x-w/2},{y+h/2}'))
        else:
            ET.SubElement(svg, 'rect', dict(attrs, x=str(x - w / 2), y=str(y - h / 2), width=str(w), height=str(h)))
        lines = node['lines']
        for j, line in enumerate(lines):
            t = ET.SubElement(svg, 'text', {
                'x': str(x), 'y': f'{y-(len(lines)-1)*10+j*20+5:.2f}',
                'font-family': 'Arial, Helvetica, sans-serif', 'font-size': '15',
                'text-anchor': 'middle', 'fill': '#000000'
            })
            t.text = line

    data = ET.tostring(svg, encoding='utf-8', xml_declaration=True)
    out_drawio = output.with_suffix('.drawio')
    out_svg = output.with_suffix('.svg')
    out_png = output.with_suffix('.png')

    if drawio_xml:
        out_drawio.write_text(drawio_xml, encoding='utf-8')

    out_svg.write_bytes(data)
    try:
        cairosvg.svg2png(bytestring=data, write_to=str(out_png))
    except Exception as exc:
        out_png.unlink(missing_ok=True)
        out_svg.unlink(missing_ok=True)
        fail(f'PNG conversion failed: {exc}')
    return out_drawio, out_svg, out_png

def render(doc, output):
    mode = validate(doc)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    drawio_xml = generate_drawio(doc, mode)
    out_drawio = output.with_suffix('.drawio')
    out_drawio.write_text(drawio_xml, encoding='utf-8')

    if mode == 'manual':
        return render_manual(doc, output, drawio_xml=drawio_xml)

    source = dot_source(doc, mode)
    tool = 'dot'
    args = [tool]
    out_svg = output.with_suffix('.svg')
    out_png = output.with_suffix('.png')
    for fmt in ('svg', 'png'):
        target = output.with_suffix('.' + fmt)
        try:
            result = subprocess.run(
                args + (['-Gdpi=300'] if fmt == 'png' else []) + ['-T' + fmt, '-o', str(target)],
                input=source,
                text=True,
                encoding='utf-8',
                errors='replace',
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            fail(f"Graphviz ({tool}) failed: {exc}")
        if result.returncode or not target.exists() or not target.stat().st_size:
            target.unlink(missing_ok=True)
            fail(f"Graphviz ({tool}) failed: {result.stderr.strip()}")

    # Inject embedded draw.io source into auto-generated SVG
    try:
        svg_tree = ET.parse(str(out_svg))
        svg_root = svg_tree.getroot()
        svg_root.attrib['content'] = drawio_xml
        svg_tree.write(str(out_svg), encoding='utf-8', xml_declaration=True)
    except Exception:
        pass

    return out_drawio, out_svg, out_png

def main():
    parser = argparse.ArgumentParser(description="Render flowchart JSON to draw.io source (.drawio), SVG, and PNG.")
    parser.add_argument('input', type=Path, help='JSON file in UTF-8')
    parser.add_argument('-o', '--output', type=Path, help='Output basename (default: input basename)')
    args = parser.parse_args()
    try:
        doc = json.loads(args.input.read_text(encoding='utf-8'))
        drawio, svg, png = render(doc, args.output or args.input.with_suffix(''))
        print(f'Created: {drawio}, {svg} and {png}')
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Error: {exc}\n')

if __name__ == '__main__':
    main()
