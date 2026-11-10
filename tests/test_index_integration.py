"""Regression checks for the actual index command and its HTML output."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / 'scripts'))
import common
from common import read_entry, write_entry
import rebuild_index


class TestIndexIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(_support.SCRATCH), prefix='ca-index-integration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts')
        shutil.copy(_support.template('taxonomy.yaml'), self.root / 'taxonomy.yaml')
        for origin in ('manual', 'auto'):
            write_entry(self.root / f'library/tech/{origin}/entry.md', {
                'id': origin, 'title': f'中文标题{origin}', 'category': 'tech', 'source': '测试发布方',
                'collection': origin, 'published': '2026-11-01', 'archived': '2026-11-02T09:00:00+08:00',
                'status': 'archived', 'url': f'https://example.com/{origin}',
            }, f'# 中文标题{origin}\n\n## 摘要\n\n摘要。\n\n## 全文\n\n仅正文关键词{origin}。\n')

    def test_real_cli_builds_all_outputs_without_touching_entries(self):
        baseline = {p: p.read_bytes() for p in (self.root / 'library').rglob('entry.md')}
        result = subprocess.run([sys.executable, '-B', str(self.root / 'scripts/rebuild_index.py')],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['by_collection'], {'manual': 1, 'auto': 1})
        for name in ('index.csv', 'index.md', 'index.html'):
            self.assertTrue((self.root / name).is_file())
        self.assertIn('仅正文关键词manual', (self.root / 'index.html').read_text())
        self.assertEqual(baseline, {p: p.read_bytes() for p in baseline})
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_render_failure_preserves_previous_html(self):
        target = self.root / 'index.html'
        target.write_text('previous complete page')
        with patch.object(rebuild_index, 'ROOT', self.root), \
                patch.object(common, 'ROOT', self.root), \
                patch.object(rebuild_index, 'build_html', side_effect=ValueError('broken input')):
            with self.assertRaises(ValueError):
                rebuild_index.main()
        self.assertEqual(target.read_text(), 'previous complete page')


if __name__ == '__main__':
    unittest.main()
