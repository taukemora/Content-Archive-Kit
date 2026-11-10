"""Real index/daily stages + encryption + local bare Git, never production I/O."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / 'scripts'))
import encrypted_site
import publish_site
from common import count_summary_chars, iso, write_entry


class TestPublishPipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(_support.SCRATCH), prefix='ca-publish-pipeline-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'project'
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy(_support.template('taxonomy.yaml'), self.root / 'taxonomy.yaml')
        self.secrets = self.base / 'secrets'
        self.secrets.mkdir(mode=0o700)
        encrypted_site.initialize_key(self.secrets / 'key.json', 'synthetic pipeline passphrase only')
        (self.secrets / 'ssh').write_text('SYNTHETIC-TEST-NOT-A-PRIVATE-KEY')
        (self.secrets / 'ssh').chmod(0o600)
        (self.secrets / 'hosts').write_text('github.com ssh-ed25519 U1lOVEhFVElDLVRFU1QtS0VZ\n')
        # Install an SSH sink in PATH as defense-in-depth: even a regressed
        # nested hook cannot contact GitHub. The intended transport is bare Git.
        self.guard = self.base / 'guard'
        self.guard.mkdir()
        ssh = self.guard / 'ssh'
        ssh.write_text('#!/bin/sh\nexit 99\n')
        ssh.chmod(0o700)
        self.bare = self.base / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(self.bare)],
                       capture_output=True, check=True)
        config = {
            'enabled': True,
            'repository': getattr(publish_site, 'ALLOWED_REPOSITORY', 'archive-owner/content-archive-site'),
            'branch': 'main',
            'site_url': getattr(publish_site, 'ALLOWED_SITE_URL', 'https://archive-owner.github.io/content-archive-site/'),
            'key_file': str(self.secrets / 'key.json'), 'ssh_key_file': str(self.secrets / 'ssh'),
            'known_hosts_file': str(self.secrets / 'hosts'), 'workspace': str(self.base / 'workspace'),
            'git_name': 'Pipeline Test', 'git_email': 'test@example.invalid',
        }
        (self.root / 'publish.yaml').write_text(json.dumps(config))
        summary = '用于隔离流水线验证的合成摘要。' * 8
        entry = self.root / 'library/coding/synthetic/entry.md'
        write_entry(entry, {
            'id': 'synthetic', 'title': '中文流水线合成样本', 'category': 'coding',
            'context': 'work', 'collection': 'auto', 'source': '合成测试', 'source_type': 'other',
            'published': '2026-11-01', 'archived': iso(), 'status': 'archived',
            'url': 'https://example.invalid/synthetic', 'summary_chars': count_summary_chars(summary),
        }, '# 中文流水线合成样本\n\n## 摘要\n\n' + summary + '\n\n## 全文\n\n仅用于隔离验收的合成正文。\n')
        self.entry = entry
        self.entry_bytes = entry.read_bytes()
        runs = self.root / '.state/runs'
        runs.mkdir(parents=True)
        (self.root / '.state/seen.json').write_text(json.dumps({'entries': {}}))
        generated = iso()
        (runs / 'daily-gate.json').write_text(json.dumps({
            'status': 'running', 'started_at': generated, 'fetch_generated_at': generated,
        }))
        (runs / 'latest-fetch.json').write_text(json.dumps({
            'generated_at': generated, 'candidates': [], 'sources': [],
        }))

    def exercise(self, stage):
        # Import from the disposable project in a fresh interpreter. Rebind only
        # Git transport / Pages fetch, leaving index, audit, digest and crypto real.
        code = r'''
import contextlib, io, json, sys
from pathlib import Path
from unittest import mock
root, bare, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(root / 'scripts'))
import publish_site as ps
import encrypted_site as es
import daily_gate, rebuild_index
builds = []
checkpoints = []
real_build = es.build_site
def build(*args, **kwargs):
    builds.append(True)
    return real_build(*args, **kwargs)
def online(url, timeout):
    gate = json.loads((root / '.state/runs/daily-gate.json').read_text())
    checkpoints.append(gate.get('archive_status'))
    return json.loads((root.parent / 'workspace/build/site.json').read_text())
with mock.patch.object(ps, 'production_target', return_value=ps.GitTarget(str(bare), {})), \
     mock.patch.object(ps, '_fetch_pages_site_json', side_effect=online), \
     mock.patch.object(es, 'build_site', side_effect=build):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        result = daily_gate.run_finish(False) if stage == 'daily' else rebuild_index.main()
    output = stream.getvalue()
state = json.loads((root.parent / 'workspace/state.json').read_text())
print(json.dumps({'code': result, 'builds': len(builds), 'checkpoints': checkpoints,
                  'state': state, 'output': output}, ensure_ascii=False))
'''
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env.pop('CA_SKIP_AUTO_PUBLISH', None)
        env['PATH'] = str(self.guard) + os.pathsep + env.get('PATH', '')
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        proc = subprocess.run([sys.executable, '-B', '-c', code, str(self.root), str(self.bare), stage],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        result = json.loads(proc.stdout)
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['builds'], 1, result)
        self.assertEqual(result['state']['status'], 'success', result)
        self.assertTrue(result['state']['verified'], result)
        self.assertEqual(self.entry.read_bytes(), self.entry_bytes)
        for name in ('index.csv', 'index.md', 'index.html'):
            self.assertTrue((self.root / name).is_file(), name)
        count = subprocess.run(['git', '-C', str(self.bare), 'rev-list', '--count', 'main'],
                               capture_output=True, text=True, check=True)
        self.assertEqual(count.stdout.strip(), '1')
        tree = subprocess.run(['git', '-C', str(self.bare), 'ls-tree', '-r', '--name-only', 'main'],
                              capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertTrue(all(p in {'index.html', 'site.json', 'reader.js', '.nojekyll'}
                            or (p.startswith('objects/') and p.endswith('.bin')) for p in tree))
        return result

    def test_daily_finish_rebuilds_audits_digests_then_publishes_once(self):
        result = self.exercise('daily')
        self.assertEqual(result['checkpoints'], ['ok'])
        gate = json.loads((self.root / '.state/runs/daily-gate.json').read_text())
        self.assertEqual(gate['archive_status'], 'ok')
        self.assertEqual(gate['publication']['status'], 'success')
        self.assertTrue(gate['publication']['verified'])

    def test_manual_index_publishes_after_all_local_outputs(self):
        result = self.exercise('index')
        index_report = json.loads(result['output'])
        self.assertEqual(index_report['publication']['status'], 'success')
        self.assertTrue(index_report['publication']['verified'])


if __name__ == '__main__':
    unittest.main()
