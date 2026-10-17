"""Build full-text RSS and Atom feeds for KOReader from reports/**/*.md."""
from datetime import date, datetime, time, timezone
from email.utils import format_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit
import xml.etree.ElementTree as ET

import markdown
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/xiaoj655/ai-research"
RAW = "https://raw.githubusercontent.com/xiaoj655/ai-research/main/"
ATOM = "http://www.w3.org/2005/Atom"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
ET.register_namespace("atom", ATOM)
ET.register_namespace("content", CONTENT)


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time())
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("日期必须包含时区，例如 2026-10-17T18:00:00+08:00")
    return result.astimezone(timezone.utc)


class AbsoluteLinks(HTMLParser):
    """Resolve relative article links and images before embedding in the feed."""
    def __init__(self, path):
        super().__init__(convert_charrefs=False)
        self.path = path
        self.output = []

    def handle_starttag(self, tag, attrs):
        rewritten = []
        for key, value in attrs:
            if value is not None and key in ("href", "src"):
                if not urlsplit(value).scheme and not value.startswith("//"):
                    base = RAW if key == "src" else REPO + "/blob/main/"
                    value = urljoin(base + quote(self.path, safe="/"), value)
            rewritten.append(key if value is None else f'{key}="{escape(value, quote=True)}"')
        suffix = " " + " ".join(rewritten) if rewritten else ""
        self.output.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.output.append(f"</{tag}>")

    def handle_data(self, data):
        self.output.append(data)

    def handle_entityref(self, name):
        self.output.append(f"&{name};")

    def handle_charref(self, name):
        self.output.append(f"&#{name};")


def read_reports(root=ROOT):
    reports = []
    for path in sorted((root / "reports").rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8-sig")
            lines = text.splitlines()
            if not lines or lines[0] != "---":
                raise ValueError("缺少 YAML 元数据")
            end = lines.index("---", 1)
            meta = yaml.safe_load("\n".join(lines[1:end]))
            if not isinstance(meta, dict):
                raise ValueError("元数据必须是键值对")
            if meta.get("draft") is True:
                continue
            title = str(meta.get("title", "")).strip()
            if not title:
                raise ValueError("缺少 title")
            published = timestamp(meta["date"])
            updated = timestamp(meta.get("updated", meta["date"]))
            if updated < published:
                raise ValueError("updated 不能早于 date")
            body = "\n".join(lines[end + 1:]).strip()
            if not body:
                raise ValueError("正文为空")
            html = markdown.markdown(body, extensions=["extra", "sane_lists"])
            links = AbsoluteLinks(relative)
            links.feed(html)
            reports.append(dict(title=title, published=published, updated=updated,
                                author=str(meta.get("author", "AI Research")),
                                url=REPO + "/blob/main/" + quote(relative, safe="/"),
                                html="".join(links.output)))
        except (ValueError, KeyError, TypeError, yaml.YAMLError) as error:
            raise ValueError(f"{relative}: {error}") from error
    return sorted(reports, key=lambda item: (item["published"], item["url"]), reverse=True)


def element(parent, tag, text=None, **attrs):
    node = ET.SubElement(parent, tag, attrs)
    if text is not None:
        node.text = str(text)
    return node


def build(root=ROOT):
    reports = read_reports(root)
    rss = ET.Element("rss", version="2.0")
    channel = element(rss, "channel")
    element(channel, "title", "AI Research · 研究报告")
    element(channel, "link", REPO)
    element(channel, "description", "AI Agent 研究报告全文订阅")
    element(channel, "language", "zh-CN")
    element(channel, f"{{{ATOM}}}link", href=RAW + "feed.xml", rel="self", type="application/rss+xml")
    atom = ET.Element(f"{{{ATOM}}}feed")
    a = lambda parent, tag, text=None, **attrs: element(parent, f"{{{ATOM}}}{tag}", text, **attrs)
    a(atom, "title", "AI Research · 研究报告")
    a(atom, "id", REPO)
    a(atom, "link", href=RAW + "atom.xml", rel="self", type="application/atom+xml")
    a(atom, "link", href=REPO, rel="alternate")
    latest = max((r["updated"] for r in reports), default=datetime(2026, 10, 17, tzinfo=timezone.utc))
    a(atom, "updated", latest.isoformat())
    for report in reports:
        item = element(channel, "item")
        element(item, "title", report["title"])
        element(item, "link", report["url"])
        element(item, "guid", report["url"], isPermaLink="true")
        element(item, "pubDate", format_datetime(report["published"], usegmt=True))
        element(item, "description", report["html"])
        element(item, f"{{{CONTENT}}}encoded", report["html"])
        entry = a(atom, "entry")
        a(entry, "title", report["title"])
        a(entry, "id", report["url"])
        a(entry, "link", href=report["url"], rel="alternate")
        a(entry, "published", report["published"].isoformat())
        a(entry, "updated", report["updated"].isoformat())
        a(a(entry, "author"), "name", report["author"])
        a(entry, "content", report["html"], type="html")
    for name, tree in (("feed.xml", rss), ("atom.xml", atom)):
        ET.indent(tree, space="  ")
        ET.ElementTree(tree).write(root / name, encoding="utf-8", xml_declaration=True)
        ET.parse(root / name)
    print(f"Generated feed.xml and atom.xml: {len(reports)} articles")


if __name__ == "__main__":
    build()
