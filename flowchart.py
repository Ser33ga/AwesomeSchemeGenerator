#!/usr/bin/env python3
"""Render validated JSON flowcharts to SVG and PNG with Graphviz and SVG."""
import argparse
import json
import math
import re
import subprocess
import sys
import textwrap
from pathlib import Path

SHAPES = {"start": "oval", "end": "oval", "process": "box", "decision": "diamond", "input_output": "parallelogram"}
PORTS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}
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
    lines = ['digraph Flowchart {', 'graph [charset="UTF-8", bgcolor="white", pad="0.35", outputorder="edgesfirst", splines="polyline", overlap="false", fontname="DejaVu Sans"];', 'node [fontname="DejaVu Sans", fontsize=13, fontcolor="black", style="filled", fillcolor="#ffffff", color="#000000", penwidth=1.4, margin="0.20,0.14", fixedsize=false];', 'edge [fontname="DejaVu Sans", fontsize=11, fontcolor="black", color="#000000", arrowsize=0.75];']
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


def render_manual(doc, output):
    """Draw exact pixel-positioned nodes and routed arrows to standalone SVG/PNG."""
    try:
        import cairosvg
        from PIL import ImageFont
        from xml.etree import ElementTree as ET
    except ImportError as exc:
        fail(f"Manual mode needs Pillow and CairoSVG: {exc}")
    font_path = r'C:\Windows\Fonts\arial.ttf'
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
    svg = ET.Element('svg', {'xmlns':'http://www.w3.org/2000/svg', 'width':str(width), 'height':str(height), 'viewBox':f'{minx} {miny} {width} {height}'})
    defs = ET.SubElement(svg,'defs')
    marker = ET.SubElement(defs,'marker',{'id':'arrow','viewBox':'0 0 10 10','refX':'9','refY':'5','markerWidth':'8','markerHeight':'8','orient':'auto-start-reverse'})
    ET.SubElement(marker,'path',{'d':'M 0 0 L 10 5 L 0 10 z','fill':'#000000'})
    ET.SubElement(svg,'rect',{'x':str(minx),'y':str(miny),'width':str(width),'height':str(height),'fill':'white'})
    def anchor(node, side):
        x,y,w,h = (node[k] for k in ('x','y','w','h'))
        if node['type'] == 'decision' and side in ('ne','nw','se','sw'):
            return (x+(-1 if 'w' in side else 1)*w/4,y+(-1 if 'n' in side else 1)*h/4)
        dx = {'w':-1,'e':1,'n':0,'s':0,'nw':-0.65,'ne':0.65,'sw':-0.65,'se':0.65}[side]
        dy = {'w':0,'e':0,'n':-1,'s':1,'nw':-0.65,'ne':-0.65,'sw':-0.65,'se':0.65}[side]
        if node['type'] in ('start','end'):
            scale = 1 / math.hypot(dx,dy)
            dx,dy = dx*scale,dy*scale
        return x+dx*w/2,y+dy*h/2
    for edge in doc['edges']:
        a,b = graph[edge['from']],graph[edge['to']]
        dx,dy = b['x']-a['x'],b['y']-a['y']
        default_a = 'e' if abs(dx)>abs(dy) and dx>=0 else 'w' if abs(dx)>abs(dy) else 's' if dy>=0 else 'n'
        default_b = 'w' if default_a=='e' else 'e' if default_a=='w' else 'n' if default_a=='s' else 's'
        x1,y1 = anchor(a,edge.get('from_port',default_a))
        x2,y2 = anchor(b,edge.get('to_port',default_b))
        pts = [(x1, y1)]
        if 'points' in edge:
            for p in edge['points']:
                px = p['x'] if isinstance(p, dict) else p[0]
                py = p['y'] if isinstance(p, dict) else p[1]
                pts.append((float(px), float(py)))
        pts.append((x2, y2))
        d_path = 'M ' + ' L '.join(f'{px:.2f} {py:.2f}' for px, py in pts)
        ET.SubElement(svg,'path',{'d':d_path,'fill':'none','stroke':'#000000','stroke-width':'1.7','marker-end':'url(#arrow)'})
        if edge.get('label'):
            label_lines = edge['label'].splitlines()
            mx,my=(pts[0][0]+pts[1][0])/2,(pts[0][1]+pts[1][1])/2-8
            lw=max(small_font.getlength(line) for line in label_lines)+10
            lh=len(label_lines)*16+5
            ET.SubElement(svg,'rect',{'x':f'{mx-lw/2:.2f}','y':f'{my-lh+5:.2f}','width':f'{lw:.2f}','height':str(lh),'fill':'white'})
            for j,line in enumerate(label_lines):
                t=ET.SubElement(svg,'text',{'x':f'{mx:.2f}','y':f'{my-(len(label_lines)-1-j)*16:.2f}','font-family':'DejaVu Sans','font-size':'12','text-anchor':'middle','fill':'#000000'})
                t.text=line
    for node in graph.values():
        x,y,w,h = (node[k] for k in ('x','y','w','h'))
        attrs={'fill':'#ffffff','stroke':'#000000','stroke-width':'1.6'}
        typ=node['type']
        if typ in ('start','end'):
            ET.SubElement(svg,'ellipse',dict(attrs,cx=str(x),cy=str(y),rx=str(w/2),ry=str(h/2)))
        elif typ == 'decision':
            ET.SubElement(svg,'polygon',dict(attrs,points=f'{x},{y-h/2} {x+w/2},{y} {x},{y+h/2} {x-w/2},{y}'))
        elif typ == 'input_output':
            skew=min(18,w/7)
            ET.SubElement(svg,'polygon',dict(attrs,points=f'{x-w/2+skew},{y-h/2} {x+w/2},{y-h/2} {x+w/2-skew},{y+h/2} {x-w/2},{y+h/2}'))
        else:
            ET.SubElement(svg,'rect',dict(attrs,x=str(x-w/2),y=str(y-h/2),width=str(w),height=str(h)))
        lines=node['lines']
        for j,line in enumerate(lines):
            t=ET.SubElement(svg,'text',{'x':str(x),'y':f'{y-(len(lines)-1)*10+j*20+5:.2f}','font-family':'DejaVu Sans','font-size':'15','text-anchor':'middle','fill':'#000000'})
            t.text=line
    data=ET.tostring(svg,encoding='utf-8',xml_declaration=True)
    out_svg,out_png=output.with_suffix('.svg'),output.with_suffix('.png')
    out_svg.write_bytes(data)
    try:
        cairosvg.svg2png(bytestring=data,write_to=str(out_png))
    except Exception as exc:
        out_png.unlink(missing_ok=True)
        out_svg.unlink(missing_ok=True)
        fail(f'PNG conversion failed: {exc}')
    return out_svg,out_png

def render(doc, output):
    mode = validate(doc)
    source = dot_source(doc, mode)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if mode == 'manual':
        return render_manual(doc, output)
    tool = 'dot'
    args = [tool]
    for fmt in ('svg', 'png'):
        target = output.with_suffix('.' + fmt)
        try:
            result = subprocess.run(
    args + (['-Gdpi=300'] if fmt == 'png' else []) + [
    '-T' + fmt, '-o', str(target)
],
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
    return output.with_suffix('.svg'), output.with_suffix('.png')

def main():
    parser = argparse.ArgumentParser(description="Render flowchart JSON to SVG and PNG (auto: Graphviz dot; manual: Pillow and CairoSVG).")
    parser.add_argument('input', type=Path, help='JSON file in UTF-8')
    parser.add_argument('-o', '--output', type=Path, help='Output basename (default: input basename)')
    args = parser.parse_args()
    try:
        doc = json.loads(args.input.read_text(encoding='utf-8'))
        svg, png = render(doc, args.output or args.input.with_suffix(''))
        print(f'Created: {svg} and {png}')
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Error: {exc}\n')

if __name__ == '__main__':
    main()
