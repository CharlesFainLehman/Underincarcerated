"""Build content pages: content/*.md -> site/*.html.

The maintainer writes Markdown; this adds the site's nav, footer, a table
of contents from the ## headings, footnotes, and chart placeholders that
the page fills from stats.json at load time. Called by build_exports.
"""

import re
from pathlib import Path

import markdown

from config import REPO_ROOT, SITE_DIR

CONTENT_DIR = REPO_ROOT / "content"
TEMPLATE = (REPO_ROOT / "site" / "_page.html").read_text(encoding="utf-8")

CHART_RE = re.compile(r"<p>\{chart:([a-z_]+)\}</p>")


def _front_matter(text: str) -> tuple[dict, str]:
    meta = {}
    if text.startswith("---"):
        head, _, body = text[3:].partition("\n---")
        for line in head.strip().splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        return meta, body
    return meta, text


def build_page(src: Path) -> Path:
    meta, body = _front_matter(src.read_text(encoding="utf-8"))
    md = markdown.Markdown(extensions=["footnotes", "toc", "tables", "smarty"],
                           extension_configs={"toc": {"toc_depth": "2-3"}})
    html = md.convert(body)
    html = CHART_RE.sub(r'<figure class="chart" data-chart="\1"></figure>', html)
    out = (TEMPLATE
           .replace("{{title}}", meta.get("title", src.stem.title()))
           .replace("{{description}}", meta.get("description", ""))
           .replace("{{nav_here}}", src.stem)
           .replace("{{toc}}", md.toc)
           .replace("{{content}}", html))
    dest = SITE_DIR / f"{src.stem}.html"
    dest.write_text(out, encoding="utf-8")
    return dest


def build_pages() -> list[Path]:
    if not CONTENT_DIR.exists():
        return []
    return [build_page(p) for p in sorted(CONTENT_DIR.glob("*.md"))]


if __name__ == "__main__":
    for p in build_pages():
        print(f"built {p}")
