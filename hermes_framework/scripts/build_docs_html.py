"""Generate HTML versions of README.md and ARCHITECTURE_VIEW.md.

Self contained HTML files. No runtime external dependencies. PDF
generation is attempted via Microsoft Edge headless mode on Windows.
If Edge is not available, the HTML files have print friendly CSS so
the user can open them in any browser and save as PDF via Ctrl+P.

Usage:
    python scripts/build_docs_html.py

Outputs:
    hermes_framework/README.html
    hermes_framework/ARCHITECTURE_VIEW.html
    hermes_framework/ARCHITECTURE_VIEW.pdf  (best effort)
"""

from __future__ import annotations

import html as html_lib
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _ensure_markdown() -> None:
    try:
        import markdown  # noqa: F401
    except ImportError:
        print("Installing 'markdown' (one time)...", file=sys.stderr)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "markdown"]
        )


_ensure_markdown()
import markdown  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOTS_DIR_REL = "docs/screenshots"


CSS = r"""
:root {
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #555555;
  --border: #d0d0d0;
  --border-soft: #d0d0d099;
  --accent: #1a1a1a;
  --link: #0056b3;
  --code-bg: #f6f6f6;
  --code-fg: #1a1a1a;
  --table-stripe: #fafafa;
  --quote-border: #cccccc;
  --kbd-bg: #f6f6f6;
  --max-w: 920px;
  --mono: ui-monospace, "SF Mono", Consolas, "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
          Helvetica, Arial, sans-serif;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #8d96a0;
    --border: #30363d;
    --border-soft: #30363d99;
    --link: #4493f8;
    --code-bg: #151b23;
    --code-fg: #e6edf3;
    --table-stripe: #151b23;
    --quote-border: #30363d;
    --kbd-bg: #151b23;
  }
}

* { box-sizing: border-box; }

html, body { background: var(--bg); color: var(--fg); }

body {
  margin: 0;
  font-family: var(--sans);
  font-size: 16px;
  line-height: 1.65;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  padding: 12px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  font-size: 14px;
}

.topbar .brand { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.topbar .brand-mark {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  background: #1a1a1a;
  color: white;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 700;
}
.topbar nav { display: flex; gap: 16px; flex-wrap: wrap; }
.topbar a { color: var(--link); text-decoration: none; }
.topbar a:hover { text-decoration: underline; }
.topbar .meta { color: var(--muted); font-size: 12px; }

main {
  max-width: var(--max-w);
  margin: 0 auto;
  padding: 32px 28px 80px;
}

h1, h2, h3, h4, h5, h6 {
  margin-top: 32px;
  margin-bottom: 14px;
  font-weight: 600;
  line-height: 1.3;
  color: var(--fg);
}
h1 {
  font-size: 30px;
  padding-bottom: 0.3em;
  border-bottom: 1px solid var(--border);
  margin-top: 0;
}
h2 {
  font-size: 22px;
  padding-bottom: 0.3em;
  border-bottom: 1px solid var(--border);
}
h3 { font-size: 18px; }
h4 { font-size: 16px; }
h5, h6 { font-size: 14px; color: var(--muted); }

p { margin: 0 0 14px; }

a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }

code {
  font-family: var(--mono);
  font-size: 85%;
  background: var(--code-bg);
  color: var(--code-fg);
  padding: 0.2em 0.4em;
  border-radius: 4px;
}

pre {
  background: var(--code-bg);
  border-radius: 6px;
  padding: 14px 16px;
  overflow-x: auto;
  font-size: 13px;
  line-height: 1.5;
  border: 1px solid var(--border-soft);
  margin: 0 0 16px;
}
pre code {
  background: transparent;
  padding: 0;
  border-radius: 0;
  font-size: inherit;
  color: inherit;
}

blockquote {
  margin: 0 0 16px;
  padding: 4px 16px;
  color: var(--muted);
  border-left: 3px solid var(--quote-border);
  background: var(--code-bg);
}

ul, ol { padding-left: 1.8em; margin: 0 0 16px; }
li { margin-bottom: 4px; }
li > p { margin-bottom: 4px; }

table {
  border-collapse: collapse;
  margin: 0 0 16px;
  display: block;
  width: 100%;
  overflow-x: auto;
  border: 1px solid var(--border);
}
table th, table td {
  padding: 8px 12px;
  border: 1px solid var(--border);
  vertical-align: top;
  font-size: 14px;
  text-align: left;
}
table th {
  background: var(--code-bg);
  font-weight: 600;
}
table tr:nth-child(2n) td { background: var(--table-stripe); }

hr {
  border: 0;
  border-top: 1px solid var(--border);
  margin: 32px 0;
}

img {
  max-width: 100%;
  border: 1px solid var(--border);
  border-radius: 4px;
  margin: 8px 0;
  background: var(--code-bg);
}

.screenshot {
  margin: 16px 0 24px;
}
.screenshot figcaption {
  font-size: 13px;
  color: var(--muted);
  margin-top: 6px;
  text-align: left;
}
.screenshot .missing {
  display: block;
  padding: 32px 16px;
  background: var(--code-bg);
  border: 1px dashed var(--border);
  text-align: center;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 13px;
  border-radius: 4px;
}

kbd {
  background: var(--kbd-bg);
  border: 1px solid var(--border);
  border-bottom-width: 2px;
  border-radius: 3px;
  padding: 2px 6px;
  font-family: var(--mono);
  font-size: 12px;
}

.mermaid {
  background: var(--code-bg);
  padding: 16px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
  margin: 0 0 16px;
  overflow-x: auto;
  text-align: center;
  min-height: 80px;
}
.mermaid:not([data-processed="true"])::before {
  content: 'Rendering diagram...';
  color: var(--muted);
  font-family: var(--mono);
  font-size: 12px;
}

.toc {
  background: var(--code-bg);
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  padding: 14px 20px;
  margin: 0 0 28px;
  font-size: 14px;
}
.toc-title {
  font-weight: 600;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
  margin: 0 0 8px;
}
.toc ul { padding-left: 1.4em; margin: 0; }
.toc > ul { padding-left: 0; list-style: none; }
.toc > ul > li { margin-bottom: 4px; }
.toc a { color: var(--fg); }
.toc a:hover { color: var(--link); }

@media print {
  :root { --bg: #ffffff; --fg: #000000; --muted: #444444; }
  .topbar { display: none; }
  main { max-width: 100%; padding: 0; }
  pre { white-space: pre-wrap; word-wrap: break-word; page-break-inside: avoid; }
  table { page-break-inside: avoid; }
  h1, h2, h3 { page-break-after: avoid; }
  .mermaid { page-break-inside: avoid; }
  a { color: inherit; text-decoration: underline; }
  body { font-size: 11pt; }
}

:target { scroll-margin-top: 70px; }
"""


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>{css}</style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark">A</div>
      <span>Argos, Hermes Framework</span>
    </div>
    <nav>
      <a href="README.html">README</a>
      <a href="ARCHITECTURE_VIEW.html">Architecture View</a>
    </nav>
    <span class="meta">{stamp}</span>
  </header>
  <main>
{content}
  </main>
{mermaid_script}
</body>
</html>
"""


MERMAID_SCRIPT = r"""
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
  <script>
    if (window.mermaid) {
      mermaid.initialize({
        startOnLoad: true,
        theme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'default',
        securityLevel: 'loose',
        flowchart: { htmlLabels: false, useMaxWidth: true },
        sequence: { useMaxWidth: true }
      });
    }
  </script>
"""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug or "section"


def _build_toc_html(md_text: str) -> str:
    items = []
    for line in md_text.splitlines():
        m = re.match(r"^##\s+(?!#)(.+?)\s*$", line)
        if m:
            title = m.group(1).strip()
            items.append((title, _slugify(title)))
    if not items:
        return ""
    lis = "\n".join(
        f'    <li><a href="#{slug}">{html_lib.escape(title)}</a></li>'
        for title, slug in items
    )
    return (
        '<div class="toc">\n'
        '  <div class="toc-title">Contents</div>\n'
        f"  <ul>\n{lis}\n  </ul>\n"
        "</div>\n"
    )


def _convert_mermaid_blocks(html: str) -> str:
    pattern = re.compile(
        r'<pre><code class="language-mermaid">(.*?)</code></pre>',
        re.DOTALL,
    )

    def repl(match: re.Match[str]) -> str:
        body = html_lib.unescape(match.group(1))
        return f'<div class="mermaid">\n{body}\n</div>'

    return pattern.sub(repl, html)


def _convert_screenshot_blocks(html: str, docs_dir: Path) -> str:
    """Replace markdown image references that point at docs/screenshots/* with
    a figure that gracefully degrades when the file is missing."""
    pattern = re.compile(
        r'<p><img alt="([^"]*?)" src="(docs/screenshots/[^"]+)"\s*/?></p>'
    )

    def repl(match: re.Match[str]) -> str:
        alt = match.group(1)
        src = match.group(2)
        full = docs_dir / src
        if full.exists():
            return (
                f'<figure class="screenshot">'
                f'<img src="{src}" alt="{alt}" />'
                f'<figcaption>{alt}</figcaption>'
                f'</figure>'
            )
        return (
            f'<figure class="screenshot">'
            f'<span class="missing">screenshot missing: {src}<br>'
            f'(drop the image at the path above to populate this slot)</span>'
            f'<figcaption>{alt}</figcaption>'
            f'</figure>'
        )

    return pattern.sub(repl, html)


def _add_heading_anchors(html: str) -> str:
    pattern = re.compile(r"<(h[1-4])>(.*?)</\1>", re.DOTALL)

    def repl(m: re.Match[str]) -> str:
        tag, inner = m.group(1), m.group(2)
        plain = re.sub(r"<[^>]+>", "", inner)
        plain = html_lib.unescape(plain).strip()
        slug = _slugify(plain)
        return f'<{tag} id="{slug}">{inner}</{tag}>'

    return pattern.sub(repl, html)


def _rewrite_md_links(html: str) -> str:
    return re.sub(
        r'href="([^"]+?)\.md(#[^"]*)?"',
        lambda m: f'href="{m.group(1)}.html{m.group(2) or ""}"',
        html,
    )


def render_md_to_html(md_text: str, title: str, docs_dir: Path) -> str:
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )
    body = md.convert(md_text)
    body = _convert_mermaid_blocks(body)
    body = _convert_screenshot_blocks(body, docs_dir)
    body = _add_heading_anchors(body)
    body = _rewrite_md_links(body)
    toc = _build_toc_html(md_text)
    content = toc + body
    stamp = datetime.now(timezone.utc).strftime("Built %Y-%m-%d %H:%M UTC")
    needs_mermaid = '<div class="mermaid">' in content
    mermaid_script = MERMAID_SCRIPT if needs_mermaid else ""
    return TEMPLATE.format(
        title=title,
        css=CSS,
        content=content,
        stamp=stamp,
        mermaid_script=mermaid_script,
    )


def _find_edge() -> str | None:
    """Locate Microsoft Edge for headless PDF generation on Windows."""
    if sys.platform != "win32":
        return None
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return shutil.which("msedge")


def _try_generate_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Try Microsoft Edge headless mode to convert HTML to PDF.
    Returns True on success, False if Edge is unavailable or fails.
    """
    edge = _find_edge()
    if not edge:
        return False
    url = html_path.absolute().as_uri()
    try:
        subprocess.run(
            [
                edge,
                "--headless",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                url,
            ],
            check=True,
            timeout=60,
            capture_output=True,
        )
        return pdf_path.exists() and pdf_path.stat().st_size > 1024
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    pairs = [
        ("README.md", "README - Argos / Hermes Framework"),
        ("ARCHITECTURE_VIEW.md", "Architecture View - Argos / Hermes Framework"),
    ]
    results = []
    for md_name, title in pairs:
        src = REPO_ROOT / md_name
        if not src.exists():
            print(f"skipping: {src} not found", file=sys.stderr)
            continue
        html_out = render_md_to_html(
            src.read_text(encoding="utf-8"), title, REPO_ROOT
        )
        dst = REPO_ROOT / md_name.replace(".md", ".html")
        dst.write_text(html_out, encoding="utf-8")
        results.append((dst, len(html_out)))
        print(f"wrote {dst.relative_to(REPO_ROOT.parent)} ({len(html_out)/1024:.1f} KB)")

    # PDF for the comprehensive view doc
    view_html = REPO_ROOT / "ARCHITECTURE_VIEW.html"
    view_pdf = REPO_ROOT / "ARCHITECTURE_VIEW.pdf"
    if view_html.exists():
        ok = _try_generate_pdf(view_html, view_pdf)
        if ok:
            print(
                f"wrote {view_pdf.relative_to(REPO_ROOT.parent)} "
                f"({view_pdf.stat().st_size/1024:.1f} KB)"
            )
        else:
            print(
                "PDF skipped: Microsoft Edge not found. "
                "Open ARCHITECTURE_VIEW.html in any browser and use Ctrl+P, "
                "Save as PDF for a PDF copy.",
                file=sys.stderr,
            )

    if results:
        print(f"\ndone. {len(results)} HTML file(s) generated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
