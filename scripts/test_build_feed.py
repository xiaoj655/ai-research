import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

from build_feed import ATOM, CONTENT, build, read_reports, timestamp


class FeedTests(unittest.TestCase):
    def test_full_text_links_dates_and_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports" / "主题").mkdir(parents=True)
            (root / "reports" / "主题" / "report.md").write_text(
                '---\ntitle: "研究 & 证据"\ndate: "2026-10-17T18:00:00+08:00"\n---\n'
                '## 全文\n\n正文 <https://example.com/?a=1&b=2>\n\n'
                '![图](../assets/a.png)\n\n[引用](../other.md)\n\n'
                '| 项目 | 结果 |\n| --- | --- |\n| A | B |', encoding="utf-8")
            (root / "reports" / "draft.md").write_text(
                '---\ndraft: true\n---\n尚未发布', encoding="utf-8")
            build(root)
            rss = ET.parse(root / "feed.xml").getroot()
            items = rss.findall("channel/item")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].findtext("title"), "研究 & 证据")
            self.assertEqual(items[0].findtext("pubDate"), "Sat, 17 Oct 2026 10:00:00 GMT")
            content = items[0].findtext(f"{{{CONTENT}}}encoded")
            self.assertIn("<h2>全文</h2>", content)
            self.assertIn("<table>", content)
            self.assertIn("https://raw.githubusercontent.com/xiaoj655/ai-research/main/reports/assets/a.png", content)
            self.assertIn("https://github.com/xiaoj655/ai-research/blob/main/reports/other.md", content)
            atom = ET.parse(root / "atom.xml").getroot()
            entry = atom.find(f"{{{ATOM}}}entry")
            self.assertEqual(entry.findtext(f"{{{ATOM}}}content"), content)
            previous = (root / "feed.xml").read_bytes()
            build(root)
            self.assertEqual((root / "feed.xml").read_bytes(), previous)

    def test_reject_invalid_article_and_timezone(self):
        with self.assertRaises(ValueError):
            timestamp("2026-10-17")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "reports" / "bad.md").write_text("没有元数据", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "bad.md"):
                read_reports(root)


if __name__ == "__main__":
    unittest.main()
