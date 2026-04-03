#!/usr/bin/env python3
"""Convert markdown with mermaid diagrams to a clean PDF.

Renders mermaid blocks to PNG (avoids weasyprint SVG text loss),
embeds them as base64 data URIs, and produces a styled PDF.
"""
import argparse, base64, re, subprocess, sys, tempfile
from pathlib import Path

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
       max-width: 960px; margin: 0 auto; padding: 20px; font-size: 13px; line-height: 1.6; color: #1a1a1a; }
h1 { color: #232f3e; border-bottom: 3px solid #ff9900; padding-bottom: 8px; font-size: 22px; }
h2 { color: #232f3e; border-bottom: 1px solid #ddd; padding-bottom: 5px; margin-top: 28px; font-size: 17px; }
h3 { color: #545b64; font-size: 14px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 12px; }
th { background: #232f3e; color: white; padding: 8px 10px; text-align: left; }
td { border: 1px solid #ddd; padding: 6px 10px; }
tr:nth-child(even) { background: #f8f9fa; }
code { background: #2d2d2d; color: #f8f8f2; padding: 2px 5px; border-radius: 3px; font-size: 12px; }
pre, div.sourceCode { background: #2d2d2d !important; color: #f8f8f2 !important; padding: 14px; border-radius: 6px; font-size: 11px;
      line-height: 1.5; page-break-inside: avoid; }
pre code { background: none !important; padding: 0; color: inherit !important; }
.diagram { text-align: center; margin: 20px 0; page-break-inside: avoid; }
.diagram img { max-width: 100%; height: auto; }
blockquote { border-left: 4px solid #ff9900; margin: 10px 0; padding: 8px 15px; background: #fff8e1; }
hr { border: none; border-top: 1px solid #ddd; margin: 25px 0; }
strong { color: #232f3e; }
a { color: #1a73e8; text-decoration: underline; }
a:visited { color: #1a73e8; }
@page { size: A4; margin: 15mm; }
"""


# Auto-linkify ticket/case IDs
LINK_PATTERNS = [
    # V-tickets and P-tickets → t.corp.amazon.com
    (re.compile(r'(?<![/\w])([VP]\d{9,10})(?!\w)'), r'[\1](https://t.corp.amazon.com/\1)'),
    # 15-digit case IDs → command-center
    (re.compile(r'(?<![/\w])(\d{15})(?!\d)'),
     r'[\1](https://command-center.support.aws.a2z.com/case-console#/cases/\1)'),
]


def auto_linkify(md: str) -> str:
    """Add markdown links for ticket IDs and case IDs, skipping code blocks and existing links."""
    parts = re.split(r'(```.*?```|`[^`]+`)', md, flags=re.DOTALL)
    for i, part in enumerate(parts):
        if part.startswith('`'):
            continue
        for pattern, repl in LINK_PATTERNS:
            part = _safe_sub(pattern, repl, part)
        parts[i] = part
    return ''.join(parts)


def _safe_sub(pattern, repl, text):
    """Apply regex sub but skip matches inside existing markdown links or URLs."""
    result, last_end = [], 0
    for m in pattern.finditer(text):
        if _inside_link(text, m.start()):
            continue
        result.append(text[last_end:m.start()])
        result.append(m.expand(repl))
        last_end = m.end()
    result.append(text[last_end:])
    return ''.join(result)


def _inside_link(text: str, pos: int) -> bool:
    """Check if position is already inside a markdown link or URL."""
    before = text[max(0, pos - 200):pos]
    # Inside markdown link text [text](...) — unmatched [
    if before.count('[') > before.count(']'):
        return True
    # Inside markdown link URL ](url) — between ( and )
    if '](http' in before and before.count('(') > before.count(')'):
        return True
    # Inside a bare URL
    last_newline = before.rfind('\n')
    line_before = before[last_newline + 1:] if last_newline >= 0 else before
    if re.search(r'https?://\S*$', line_before):
        return True
    return False


def render_mermaid_to_png(mmd_text: str, tmpdir: Path, index: int) -> str | None:
    """Render a mermaid block to PNG, return base64 data URI or None on failure."""
    mmd_file = tmpdir / f"d{index}.mmd"
    png_file = tmpdir / f"d{index}.png"
    mmd_file.write_text(mmd_text)
    r = subprocess.run(
        ["npx", "-y", "@mermaid-js/mermaid-cli", "-i", str(mmd_file), "-o", str(png_file),
         "-b", "white", "--width", "1800", "--scale", "2"],
        capture_output=True, timeout=120,
    )
    if png_file.exists() and png_file.stat().st_size > 0:
        b64 = base64.b64encode(png_file.read_bytes()).decode()
        return f'<div class="diagram"><img src="data:image/png;base64,{b64}"></div>'
    print(f"  Warning: diagram {index} failed to render: {r.stderr.decode()[:200]}", file=sys.stderr)
    return None


def mermaid_to_html_fallback(mmd_text: str) -> str:
    """Convert unsupported mermaid (e.g. timeline) to styled HTML."""
    lines = mmd_text.strip().splitlines()
    # Detect timeline diagrams and render as a styled HTML timeline
    if any(l.strip().startswith("timeline") for l in lines[:3]):
        title = ""
        sections: list[tuple[str, list[str]]] = []
        current_section = None
        for line in lines:
            l = line.strip()
            if l.startswith("title "):
                title = l[6:]
            elif l.startswith("section "):
                current_section = l[8:]
                sections.append((current_section, []))
            elif ":" in l and current_section:
                sections[-1][1].append(l)
        html = f'<div class="timeline-visual"><h4 style="text-align:center;color:#232f3e;margin-bottom:12px">{title}</h4>'
        html += '<div style="display:flex;flex-wrap:wrap;gap:0;justify-content:center;border-top:4px solid #ff9900;padding-top:12px">'
        colors = ["#232f3e", "#3498db", "#27ae60", "#e74c3c"]
        for i, (sec, events) in enumerate(sections):
            c = colors[i % len(colors)]
            html += f'<div style="flex:1;min-width:180px;max-width:240px;padding:8px;border-left:3px solid {c}">'
            html += f'<div style="font-weight:bold;color:{c};font-size:13px;margin-bottom:6px">{sec}</div>'
            for ev in events:
                parts = ev.split(":", 1)
                time_part = parts[0].strip()
                desc = parts[1].strip() if len(parts) > 1 else ""
                html += f'<div style="font-size:11px;margin-bottom:4px"><b>{time_part}</b>'
                if desc:
                    html += f'<br><span style="color:#555">{desc}</span>'
                html += '</div>'
            html += '</div>'
        html += '</div></div>'
        return html
    # Generic fallback
    clean = [l.strip() for l in lines if l.strip() and not l.strip().startswith("style ")]
    return '<div class="diagram"><pre>' + "\n".join(clean) + "</pre></div>"


def _gantt_to_vertical_html(mmd_text: str) -> str | None:
    """Convert a mermaid gantt definition to a vertical HTML timeline. Returns None if not a gantt."""
    lines = mmd_text.strip().splitlines()
    if not any(l.strip().startswith("gantt") for l in lines[:3]):
        return None
    title = ""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    cur_section = None
    date_fmt = "HH:mm"
    for line in lines:
        l = line.strip()
        if l.startswith("title "):
            title = l[6:]
        elif l.startswith("dateFormat "):
            date_fmt = l[11:]
        elif l.startswith("section "):
            cur_section = l[8:]
            sections.append((cur_section, []))
        elif ":" in l and cur_section and not l.startswith(("gantt", "dateFormat", "title", "axisFormat")):
            # "Task name :id, start, duration" — split on FIRST colon only
            first_colon = l.index(":")
            name_part = l[:first_colon].strip()
            rest = l[first_colon + 1:].strip()
            parts = [p.strip() for p in rest.split(",")]
            # parts: [id, start, duration] or [start, duration] or [id, start]
            # Find the time-like value (contains : or looks like a time)
            start = ""
            dur = ""
            for p in parts:
                if ":" in p and not p.startswith(("a", "b", "c", "d", "e", "f")):
                    start = p
                elif p.endswith(("min", "hr", "h", "d")):
                    dur = p
                elif not start and any(ch.isdigit() for ch in p) and not p[0].isalpha():
                    start = p
            sections[-1][1].append((start, f"{name_part} ({dur})" if dur else name_part))
    if not sections:
        return None
    colors = ["#232f3e", "#3498db", "#27ae60", "#e74c3c", "#8e44ad"]
    html = f'<div style="max-width:600px;margin:20px auto;font-family:sans-serif">'
    if title:
        html += f'<h4 style="text-align:center;color:#232f3e;margin-bottom:16px">{title}</h4>'
    html += '<div style="position:relative;padding-left:160px;border-left:3px solid #ff9900;margin-left:40px">'
    for si, (sec, events) in enumerate(sections):
        c = colors[si % len(colors)]
        html += f'<div style="margin:16px 0 8px -160px;padding-left:160px;font-weight:bold;font-size:13px;color:{c};border-bottom:1px solid #eee;padding-bottom:4px">{sec}</div>'
        for time_str, name in events:
            html += (
                f'<div style="position:relative;margin:10px 0;padding:6px 12px;background:#f8f9fa;border-radius:4px;border-left:4px solid {c}">'
                f'<span style="position:absolute;left:-158px;top:6px;width:145px;text-align:right;font-size:11px;font-weight:bold;color:#555;font-family:monospace">{time_str}</span>'
                f'<span style="font-size:12px;color:#1a1a1a">{name}</span>'
                f'</div>'
            )
    html += '</div></div>'
    return html


def convert(md_path: str, out_path: str) -> str:
    md = Path(md_path).read_text()
    md = auto_linkify(md)

    # Collapse bash/shell line continuations so commands render as single lines
    def _collapse_continuations(m):
        lang = m.group(1)
        code = m.group(2)
        if lang in ("bash", "shell", "sh", ""):
            code = re.sub(r'\s*\\\s*\n\s*', ' ', code)
        return f'```{lang}\n{code}```'
    md = re.sub(r'```(\w*)\n(.*?)```', _collapse_continuations, md, flags=re.DOTALL)

    with tempfile.TemporaryDirectory(prefix="md2pdf_") as tmpdir:
        tmpdir = Path(tmpdir)
        idx = [0]

        def replace_block(m):
            mmd = m.group(1)
            # Gantt → vertical HTML timeline (more readable than mermaid's horizontal gantt)
            gantt_html = _gantt_to_vertical_html(mmd)
            if gantt_html:
                return gantt_html
            i = idx[0]; idx[0] += 1
            result = render_mermaid_to_png(mmd, tmpdir, i)
            return result if result else mermaid_to_html_fallback(mmd)

        html_md = re.sub(r"```mermaid\n(.*?)```", replace_block, md, flags=re.DOTALL)

        # Pandoc markdown → HTML (standalone for syntax highlight CSS)
        r = subprocess.run(
            ["pandoc", "-f", "markdown", "-t", "html5", "-s", "--syntax-highlighting=tango"],
            input=html_md, capture_output=True, text=True,
        )
        # Inject our CSS into pandoc's standalone HTML
        html = r.stdout.replace("</style>", CSS + "\n</style>", 1)

        # Weasyprint HTML → PDF
        html_file = tmpdir / "report.html"
        html_file.write_text(html)
        subprocess.run(["weasyprint", str(html_file), out_path], capture_output=True)

    print(f"PDF written to {out_path}")
    return out_path


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Convert markdown+mermaid to PDF")
    p.add_argument("input", help="Input markdown file")
    p.add_argument("-o", "--output", help="Output PDF path")
    args = p.parse_args()
    out = args.output or str(Path(args.input).with_suffix(".pdf"))
    convert(args.input, out)
