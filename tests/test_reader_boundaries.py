"""Behavior-based regression probes for reader trust boundaries."""
from pathlib import Path
import json
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / 'scripts'))
from common import write_entry
from build_html import build_html


class TestReaderBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(_support.SCRATCH))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.entry = self.root / 'library/coding/item/entry.md'
        self.row = {'id': 'item', 'path': 'library/coding/item/entry.md', 'title': '标题', 'collection': 'manual', 'url': 'javascript:alert(1)'}

    def data(self, body):
        write_entry(self.entry, {'id': 'item'}, body)
        output = build_html(self.root, [self.row], {'coding': '编程'})
        return json.loads(re.search(r'<script id="archive-data"[^>]*>(.*?)</script>', output, re.S)[1])[0]

    def test_reader_redesign_keeps_reading_controls_and_provenance(self):
        write_entry(self.entry, {'id': 'item'}, '正文')
        output = build_html(self.root, [self.row], {'coding': '编程'})
        self.assertIn('id="focus-reading-btn"', output)
        self.assertIn('aria-pressed="false"', output)
        self.assertIn('setFocusReading(false)', output)
        self.assertIn('class="article-footer"', output)
        self.assertIn('class="archive-notes"', output)
        self.assertIn('class="card-kicker"', output)
        self.assertIn('font-size: 18px; line-height: 1.9;', output)
        self.assertIn('text-wrap: pretty;', output)

    def test_template_tokens_in_articles_stay_literal(self):
        tokens = '{{ARCHIVE_DATA}} {{LABELS_DATA}} {{BUILD_TIME}}'
        self.row['title'] = tokens
        self.row['summary'] = tokens
        data = self.data('代码文字 ' + tokens)
        self.assertEqual(data['title'], tokens)
        self.assertEqual(data['summary'], tokens)
        self.assertIn(tokens, data['body_html'])
        self.assertIn(tokens.lower(), data['search_text'])

    def test_search_includes_fenced_and_inline_code(self):
        data = self.data('正文\n\n```python\nUNIQUE_CODE_NEEDLE\n```\n\n`INLINE_NEEDLE`')
        self.assertIn('unique_code_needle', data['search_text'])
        self.assertIn('inline_needle', data['search_text'])

    def test_metadata_original_url_rejects_active_schemes(self):
        self.assertEqual(self.data('正文')['url'], '')
        write_entry(self.entry, {'id': 'item'}, '正文')
        valid_row = dict(self.row, url='https://example.com/article')
        out_valid = build_html(self.root, [valid_row], {'coding': '编程'})
        data_valid = json.loads(re.search(r'<script id="archive-data"[^>]*>(.*?)</script>', out_valid, re.S)[1])[0]
        self.assertEqual(data_valid['url'], 'https://example.com/article')

        ctrl_row = dict(self.row, url='https://example.com\r\ninvalid')
        out_ctrl = build_html(self.root, [ctrl_row], {'coding': '编程'})
        data_ctrl = json.loads(re.search(r'<script id="archive-data"[^>]*>(.*?)</script>', out_ctrl, re.S)[1])[0]
        self.assertEqual(data_ctrl['url'], '')

    def test_missing_or_outside_article_fails_instead_of_silent_summary(self):
        outside = self.root / 'private.md'
        outside.write_text('SECRET_NEEDLE')
        for path in ('private.md', 'library/coding/missing/entry.md', 'library/coding/item/not_entry.md'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    build_html(self.root, [dict(self.row, path=path)], {})

    def test_article_cannot_link_project_private_files(self):
        data = self.data('[private](../../../.env) [snapshot](raw.html.gz)')
        self.assertNotIn('href=".env"', data['body_html'])
        self.assertNotIn('href="raw.html.gz"', data['body_html'])

        # Relative links to safe images in article dir and page anchors are allowed
        images = self.entry.parent / 'images'
        images.mkdir(parents=True, exist_ok=True)
        (images / 'safe.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        data2 = self.data('[safe image](images/safe.png) [anchor](#heading)')
        self.assertIn('href="library/coding/item/images/safe.png"', data2['body_html'])
        self.assertIn('href="#heading"', data2['body_html'])

    def test_svg_external_resources_and_any_event_are_blocked(self):
        images = self.entry.parent / 'images'
        images.mkdir(parents=True, exist_ok=True)
        for svg in (
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/pixel"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" onbegin="alert(1)"></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><style>circle { fill: red; }</style><circle cx="5" cy="5" r="5"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="x" from="0" to="10" dur="1s"/></svg>',
            '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg xmlns="http://www.w3.org/2000/svg"></svg>',
        ):
            (images / 'bad.svg').write_text(svg)
            self.assertNotIn('<img ', self.data('![chart](images/bad.svg)')['body_html'])

        # svgz should be rejected
        (images / 'bad.svgz').write_bytes(b'fake svgz')
        self.assertNotIn('<img ', self.data('![chart](images/bad.svgz)')['body_html'])

        # Native static SVG with standard geometry and local pattern reference is allowed
        native_svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<defs><pattern id="hatch-m" width="5" height="5"><rect width="5" height="5"/></pattern></defs>'
            '<rect width="10" height="10" style="fill:url(#hatch-m)"/>'
            '</svg>'
        )
        (images / 'native.svg').write_text(native_svg)
        self.assertIn('<img ', self.data('![native](images/native.svg)')['body_html'])

    def test_embedded_book_favicon_survives_generation(self):
        import html
        import urllib.parse
        import xml.etree.ElementTree as ET

        write_entry(self.entry, {'id': 'item'}, '正文')
        output = build_html(self.root, [self.row], {'coding': '编程'})
        icon = re.search(r'<link rel="icon" type="image/svg\+xml" href="([^"]+)"', output)
        self.assertIsNotNone(icon)
        href = html.unescape(icon.group(1))
        self.assertTrue(href.startswith('data:image/svg+xml,'))
        svg = ET.fromstring(urllib.parse.unquote(href.split(',', 1)[1]))
        self.assertEqual(svg.tag, '{http://www.w3.org/2000/svg}svg')
        self.assertEqual(svg.attrib['viewBox'], '0 0 40 40')
        self.assertTrue(svg.findall('{http://www.w3.org/2000/svg}path'))
        self.assertIn("img-src 'self' file: data:;", output)
        self.assertIn("connect-src 'none'", output)
        self.assertNotIn('<script', ET.tostring(svg, encoding='unicode'))
        # Allowing the trusted favicon must not admit data images from article text.
        self.assertNotIn('<img ', self.data('![bad](data:image/svg+xml;base64,PHN2Zy8+)')['body_html'])

    def test_csp_and_build_time(self):
        write_entry(self.entry, {'id': 'item'}, '正文')
        output = build_html(self.root, [self.row], {'coding': '编程'})
        self.assertIn('Content-Security-Policy', output)
        self.assertIn("default-src 'none'", output)
        self.assertIn("connect-src 'none'", output)
        self.assertIn("frame-src 'none'", output)
        self.assertIn("object-src 'none'", output)
        self.assertIn("base-uri 'none'", output)
        self.assertIn("form-action 'none'", output)
        self.assertIn("generator-build-time", output)

    def test_ui_accessibility_and_responsiveness(self):
        write_entry(self.entry, {'id': 'item'}, '正文')
        output = build_html(self.root, [self.row], {'coding': '编程'})
        self.assertIn('min-width: 0;', output)
        self.assertIn('aria-selected="true"', output)
        self.assertIn('aria-selected="false"', output)
        self.assertIn('mobile-reading', output)
        self.assertIn('isValidHttpUrl', output)


if __name__ == '__main__':
    unittest.main()
