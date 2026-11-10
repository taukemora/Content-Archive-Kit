"""The optional publisher must work outside Hermes' injected PYTHONPATH."""
from pathlib import Path
import os
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TestPublishDependencies(unittest.TestCase):
    def test_manifest_declares_complete_direct_runtime_dependencies(self):
        def packages(path):
            result = set()
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('-r '):
                    result.update(packages(path.parent / line[3:].strip()))
                else:
                    result.add(line.split('==', 1)[0].lower().replace('_', '-'))
            return result

        declared = packages(ROOT / 'requirements-publish.txt')
        self.assertTrue({'cryptography', 'markdown-it-py', 'pyyaml', 'requests'} <= declared,
                        f'Incomplete standalone publisher requirements: {sorted(declared)}')

    def test_imports_and_status_work_without_injected_pythonpath(self):
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        code = (
            'from pathlib import Path; import sys; '
            f'sys.path.insert(0, {str(ROOT / "scripts")!r}); '
            'import encrypted_site, publish_site; '
            'print(encrypted_site.PROTOCOL)'
        )
        result = subprocess.run([sys.executable, '-B', '-c', code],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'CA-ENC-1')


if __name__ == '__main__':
    unittest.main()
