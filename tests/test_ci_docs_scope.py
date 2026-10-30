"""Only a proven documentation-only pull request may skip the image work.

`validate` and `images` stay required contexts, so both jobs always run and
always report. The decision inside them is what may skip their heavy work, and
it must fail closed: treating container code as documentation would merge
unbuilt images and untested Python.
"""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('docs_scope', ROOT / 'scripts/ci_docs_scope.py')
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)

GIT = ('git', '-c', 'commit.gpgsign=false', '-c', 'maintenance.auto=false',
       '-c', 'gc.auto=0', '-c', 'core.hooksPath=/dev/null',
       '-c', 'user.name=Synthetic Test', '-c', 'user.email=test@example.invalid')


def run(root, *args):
    return subprocess.check_output([*GIT, '-C', str(root), *args],
                                   stderr=subprocess.DEVNULL).decode().strip()


def repository(parent, changed):
    """Return (root, base, head) for a repository whose single change is `changed`."""
    root = Path(parent)
    root.mkdir(parents=True)
    run(root, 'init', '-q')
    (root / 'README.md').write_text('base')
    run(root, 'add', '.')
    run(root, 'commit', '-qm', 'base')
    base = run(root, 'rev-parse', 'HEAD')
    target = root / changed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('changed')
    run(root, 'add', '.')
    run(root, 'commit', '-qm', 'change')
    return root, base, run(root, 'rev-parse', 'HEAD')


def pull_event(base, head):
    return {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}


class ImagesScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def case(self, changed):
        return repository(self.root / changed.replace('/', '_'), changed)

    def test_documentation_only_pull_request_skips_the_builds(self):
        root, base, head = self.case('docs/guide.md')
        result = scope.classify('pull_request', pull_event(base, head), head, root)
        self.assertFalse(result['images'])
        self.assertTrue(result['documentation_only'])
        self.assertEqual('documentation-only-diff', result['reason'])
        self.assertEqual(1, result['changed_paths'])

    def test_diverged_target_changes_do_not_become_pull_request_changes(self):
        for changed, documentation_only in (('docs/guide.md', True),
                                             ('scripts/runtime.py', False)):
            with self.subTest(changed=changed):
                root, common, head = self.case(changed)
                run(root, 'checkout', '-qb', 'target', common)
                (root / 'base.py').write_text('already validated target change\n')
                run(root, 'add', '.')
                run(root, 'commit', '-qm', 'unrelated target change')
                base = run(root, 'rev-parse', 'HEAD')
                run(root, 'merge', '--no-ff', '-qm', 'synthetic test merge', head)
                merged = run(root, 'rev-parse', 'HEAD')
                result = scope.classify('pull_request', pull_event(base, head), merged, root)
                self.assertEqual(documentation_only, result['documentation_only'])
                self.assertEqual(not documentation_only, result['images'])
                self.assertEqual(common, result['comparison_base'])
                self.assertEqual(base, result['base'])
                self.assertEqual(head, result['head'])
                self.assertEqual(1, result['changed_paths'])

    def test_missing_or_ambiguous_merge_base_fails(self):
        root, base, head = self.case('docs/guide.md')
        original_git = scope.git
        for evidence in (b'', f'{base}\n{"a" * 40}\n'.encode()):
            with self.subTest(evidence=evidence):
                def uncertain(root, *args):
                    if args[:2] == ('merge-base', '--all'):
                        return evidence
                    return original_git(root, *args)
                with patch.object(scope, 'git', side_effect=uncertain):
                    with self.assertRaisesRegex(ValueError, 'unique pull-request merge base'):
                        scope.classify('pull_request', pull_event(base, head), head, root)

    def test_any_other_path_keeps_the_builds(self):
        for changed in ('scripts/tool.py', '.github/workflows/validate.yml',
                        'containers/voice/Dockerfile', 'LICENSE',
                        'docs/notes.md.bak', 'container-trivy.json'):
            with self.subTest(changed=changed):
                root, base, head = self.case(changed)
                result = scope.classify('pull_request', pull_event(base, head), head, root)
                self.assertTrue(result['images'], changed)
                self.assertFalse(result['documentation_only'], changed)
                self.assertEqual('changed-paths', result['reason'])

    def test_an_uppercase_markdown_suffix_is_still_documentation(self):
        root, base, head = self.case('docs/NOTES.MD')
        self.assertFalse(scope.classify('pull_request', pull_event(base, head), head, root)['images'])

    def test_pushes_and_dispatches_always_build(self):
        root, base, head = self.case('docs/guide.md')
        pushed = scope.classify('push', {'before': base, 'after': head, 'deleted': False}, head, root)
        dispatched = scope.classify('workflow_dispatch', {}, head, root)
        self.assertTrue(pushed['images'])
        self.assertTrue(dispatched['images'])
        self.assertFalse(pushed['documentation_only'])
        self.assertFalse(dispatched['documentation_only'])
        self.assertEqual('not-a-pull-request', pushed['reason'])

    def test_an_empty_diff_is_not_treated_as_documentation(self):
        root, base, head = self.case('docs/guide.md')
        result = scope.classify('pull_request', pull_event(head, head), head, root)
        self.assertTrue(result['images'])
        self.assertEqual(0, result['changed_paths'])

    def test_a_checkout_that_differs_from_the_event_fails(self):
        root, base, head = self.case('docs/guide.md')
        with self.assertRaises(ValueError):
            scope.classify('pull_request', pull_event(base, head), base, root)

    def test_a_base_commit_outside_the_checkout_fails(self):
        root, base, head = self.case('docs/guide.md')
        _, _, unrelated = self.case('other/notes.md')
        with self.assertRaises(ValueError):
            scope.classify('pull_request', pull_event(base, unrelated), head, root)

    def test_a_zero_sha_fails_instead_of_skipping(self):
        root, base, head = self.case('docs/guide.md')
        with self.assertRaises(ValueError):
            scope.classify('pull_request', pull_event('0' * 40, head), head, root)

    def test_both_event_commits_must_be_contained_in_the_checkout(self):
        root, common, head = self.case('docs/guide.md')
        run(root, 'checkout', '-qb', 'target', common)
        (root / 'base.md').write_text('target documentation\n')
        run(root, 'add', '.')
        run(root, 'commit', '-qm', 'target change')
        base = run(root, 'rev-parse', 'HEAD')
        for checked_out in (base, head):
            with self.subTest(checked_out=checked_out):
                run(root, 'checkout', '--detach', '-q', checked_out)
                with self.assertRaisesRegex(ValueError, 'not contained'):
                    scope.classify('pull_request', pull_event(base, head), checked_out, root)


if __name__ == '__main__':
    unittest.main()
