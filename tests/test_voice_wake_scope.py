"""Check exact Git-event scope and fail-closed wake gate decisions."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('wake_scope', ROOT / 'scripts/voice_wake_scope.py')
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)


class GitScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git('init', '-q')
        self.base = self.commit('README.md', 'base')

    def git(self, *args):
        # Detached Git maintenance can outlive commit and race temporary-repo
        # cleanup. Disable both modern maintenance and older automatic GC.
        return subprocess.check_output([
            'git', '-C', str(self.root), '-c', 'commit.gpgsign=false',
            '-c', 'maintenance.auto=false', '-c', 'gc.auto=0',
            '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Synthetic Test',
            '-c', 'user.email=test@example.invalid', *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, path, content):
        target = self.root / path; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic change')
        return self.git('rev-parse', 'HEAD')

    def push(self, before, after):
        return scope.classify('push', {'before': before, 'after': after, 'deleted': False}, after, self.root)

    def test_documentation_only_push_has_proven_irrelevant_diff(self):
        head = self.commit('docs/guide.md', 'guide')
        result = self.push(self.base, head)
        self.assertFalse(result['required'])
        self.assertEqual(result['reason'], 'proven-irrelevant-diff')
        self.assertEqual(result['changed_paths'], 1)

    def test_synthetic_commit_does_not_spawn_background_maintenance(self):
        trace = self.root / '.git' / 'test-trace.jsonl'
        with patch.dict(os.environ, GIT_TRACE2_EVENT=str(trace)):
            self.commit('docs/guide.md', 'guide')
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertTrue(any(item.get('event') == 'start' and 'commit' in item.get('argv', [])
                            for item in records))
        automatic = [item['argv'] for item in records if item.get('event') == 'child_start'
                     and len(item.get('argv', [])) > 1 and item['argv'][1] in ('maintenance', 'gc')]
        self.assertEqual(automatic, [])

    def test_runtime_change_and_deletion_require_build(self):
        head = self.commit('containers/voice/Dockerfile', 'FROM synthetic')
        self.assertTrue(self.push(self.base, head)['required'])
        (self.root / 'containers/voice/Dockerfile').unlink()
        deleted = self.commit('README.md', 'deletion')
        self.assertTrue(self.push(head, deleted)['required'])

    def test_rename_out_of_runtime_still_requires_build(self):
        head = self.commit('voice_assistant/old.py', 'synthetic')
        self.git('mv', 'voice_assistant/old.py', 'README.md.tmp')
        renamed = self.commit('README.md', 'rename')
        self.assertTrue(self.push(head, renamed)['required'])

    def test_new_branch_checks_the_whole_tree(self):
        head = self.commit('voice_assistant/start.py', 'synthetic')
        self.assertTrue(self.push(scope.ZERO_SHA, head)['required'])

    def test_pull_request_uses_unique_merge_base_not_latest_base_contents(self):
        self.git('checkout', '-qb', 'topic')
        head = self.commit('docs/guide.md', 'topic documentation')
        self.git('checkout', '-qb', 'target', self.base)
        base = self.commit('voice_assistant/base.py', 'already qualified base change')
        self.git('merge', '--no-ff', '-qm', 'synthetic test merge', 'topic')
        merged = self.git('rev-parse', 'HEAD')
        event = {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}
        result = scope.classify('pull_request', event, merged, self.root)
        self.assertFalse(result['required'])
        self.assertEqual(result['comparison_base'], self.base)
        self.assertEqual(result['base'], base)
        self.assertEqual(result['head'], head)

    def test_pull_request_runtime_change_requires_build(self):
        head = self.commit('scripts/voice_wake_scope.py', 'modified gate')
        event = {'pull_request': {'base': {'sha': self.base}, 'head': {'sha': head}}}
        self.assertTrue(scope.classify('pull_request', event, head, self.root)['required'])

    def test_ambiguous_pull_request_merge_base_fails(self):
        event = {'pull_request': {'base': {'sha': self.base}, 'head': {'sha': self.base}}}
        original = scope.git
        def ambiguous(root, *args):
            if args[:2] == ('merge-base', '--all'):
                return (self.base + '\n' + 'a' * 40 + '\n').encode()
            return original(root, *args)
        with patch.object(scope, 'git', side_effect=ambiguous), self.assertRaises(ValueError):
            scope.classify('pull_request', event, self.base, self.root)

    def test_mismatched_checkout_or_event_head_fails(self):
        head = self.commit('README.md', 'later')
        with self.assertRaises(ValueError):
            scope.classify('push', {'before': self.base, 'after': head, 'deleted': False}, self.base, self.root)
        with self.assertRaises(ValueError):
            scope.classify('push', {'before': self.base, 'after': self.base, 'deleted': False}, head, self.root)

    def test_missing_push_history_is_not_an_irrelevant_diff(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.push('a' * 40, self.base)

    def test_unmerged_pull_request_head_fails(self):
        self.git('checkout', '-qb', 'topic')
        head = self.commit('docs/topic.md', 'topic')
        self.git('checkout', '-qb', 'target', self.base)
        base = self.commit('docs/base.md', 'base')
        event = {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}
        with self.assertRaises(subprocess.CalledProcessError):
            scope.classify('pull_request', event, base, self.root)

    def test_dispatch_always_builds_and_unknown_event_fails(self):
        self.assertTrue(scope.classify('workflow_dispatch', {}, self.base, self.root)['required'])
        with self.assertRaises(ValueError):
            scope.classify('schedule', {}, self.base, self.root)

    def test_malformed_commit_or_deleted_push_fails(self):
        for before in ('main', '--help', '', None):
            with self.subTest(before=before), self.assertRaises(ValueError):
                self.push(before, self.base)
        with self.assertRaises(ValueError):
            scope.classify('push', {'before': self.base, 'after': self.base, 'deleted': True}, self.base, self.root)

    def test_incomplete_or_non_utf8_path_evidence_fails(self):
        for raw in (b'README.md', b'../outside\0', b'/absolute\0', b'bad\xff\0'):
            with self.subTest(raw=raw), patch.object(scope, 'git', return_value=raw), self.assertRaises(ValueError):
                scope.changed_paths(self.root, self.base, self.base)


class GateTests(unittest.TestCase):
    def test_only_complete_success_or_proven_irrelevant_skip_passes(self):
        allowed = {('success', 'true', 'success'), ('success', 'false', 'skipped')}
        for result, required, build in itertools.product(
                ('success', 'failure', 'cancelled', 'skipped', '', None),
                ('true', 'false', '', 'unknown', None),
                ('success', 'failure', 'cancelled', 'skipped', '', None)):
            with self.subTest(scope=result, required=required, build=build):
                if (result, required, build) in allowed:
                    scope.verify_gate(result, required, build)
                else:
                    with self.assertRaises(ValueError):
                        scope.verify_gate(result, required, build)

    def test_workflow_always_reports_gate_and_keeps_build_conditional(self):
        workflow = (ROOT / '.github/workflows/voice-wake.yml').read_text()
        self.assertNotIn('paths:', workflow)
        self.assertIn('fetch-depth: 0', workflow)
        self.assertIn("if: needs.scope.outputs.required == 'true'", workflow)
        self.assertIn('name: Wake gate', workflow)
        self.assertIn('needs: [scope, build]\n    if: ${{ always() }}', workflow)
        self.assertIn('run: python3 scripts/voice_wake_scope.py --gate', workflow)


if __name__ == '__main__':
    unittest.main()
