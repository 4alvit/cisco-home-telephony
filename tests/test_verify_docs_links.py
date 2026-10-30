"""Link-checker behaviour and the repository's own documentation; offline."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import verify_docs_links
finally:
    sys.path.pop(0)


class VerifyDocsLinksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def problems(self, *names):
        files = [self.root / name for name in names]
        return verify_docs_links.check(self.root, files)[0]

    def test_missing_file_is_reported(self):
        self.write('guide.md', 'See [the runbook](missing.md).\n')
        problem = self.problems('guide.md')
        self.assertEqual(len(problem), 1)
        self.assertIn('does not exist', problem[0])

    def test_existing_file_link_is_accepted(self):
        self.write('runbook.md', '# Deploy\n')
        self.write('guide.md', 'Read [the runbook](runbook.md).\n')
        self.assertEqual(self.problems('guide.md', 'runbook.md'), [])

    def test_heading_anchor_is_resolved(self):
        self.write('runbook.md', '## Step 1: apply the Secret\n')
        self.write('guide.md', 'Jump to [the step](runbook.md#step-1-apply-the-secret).\n')
        self.assertEqual(self.problems('guide.md', 'runbook.md'), [])

    def test_unknown_anchor_is_reported(self):
        self.write('runbook.md', '## Deploy\n')
        self.write('guide.md', 'Jump to [the step](runbook.md#export).\n')
        problem = self.problems('guide.md', 'runbook.md')
        self.assertEqual(len(problem), 1)
        self.assertIn('no matching heading', problem[0])

    def test_mixed_case_markdown_files_are_discovered_and_checked(self):
        subprocess.run(['git', '-C', str(self.root), 'init', '-q'], check=True)
        self.write('README.md', '# Readme\n')
        self.write('UPPER.MD', '[Broken](missing.md)\n')
        self.write('mixed.Md', '# Mixed\n')
        self.write('not-markdown.txt', '[Ignored](missing.md)\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        self.write('untracked.md', '[Untracked](missing.md)\n')
        files = verify_docs_links.markdown_files(self.root)
        self.assertEqual({'README.md', 'UPPER.MD', 'mixed.Md'},
                         {path.name for path in files})
        problems, links = verify_docs_links.check(self.root, files)
        self.assertEqual(1, links)
        self.assertEqual(1, len(problems))
        self.assertIn('UPPER.MD:1:', problems[0])
        self.assertIn('does not exist', problems[0])

    def test_filesystem_fallback_discovers_mixed_case_markdown(self):
        expected = self.write('docs/UPPER.MD', '# Upper\n')
        self.write('.local/private.MD', '[Private](missing.md)\n')
        self.write('not-markdown.txt', '[Ignored](missing.md)\n')
        with patch.object(verify_docs_links.subprocess, 'run', side_effect=OSError):
            self.assertEqual([expected], verify_docs_links.markdown_files(self.root))

    def test_mixed_case_markdown_anchor_targets_are_checked(self):
        self.write('runbook.MD', '## Deploy\n')
        self.write('guide.md', '[Valid](runbook.MD#deploy)\n')
        self.assertEqual([], self.problems('guide.md'))
        self.write('guide.md', '[Missing](runbook.MD#missing)\n')
        problems = self.problems('guide.md')
        self.assertEqual(1, len(problems))
        self.assertIn('no matching heading', problems[0])

    def test_repeated_headings_get_numbered_anchors(self):
        self.write('runbook.md', '## Notes\n\n## Notes\n')
        self.write('guide.md',
                   '[first](runbook.md#notes) [second](runbook.md#notes-1)\n')
        self.assertEqual(self.problems('runbook.md', 'guide.md'), [])

    def test_html_anchor_is_resolved(self):
        self.write('runbook.md', '<a name="legacy"></a>\n\n## Section\n')
        self.write('guide.md', '[old anchor](runbook.md#legacy)\n')
        self.assertEqual(self.problems('guide.md', 'runbook.md'), [])

    def test_fenced_and_inline_code_are_ignored(self):
        self.write('guide.md', '```sh\nsee [nothing](nowhere.md)\n```\n'
                               'Inline `[x](nowhere.md)` stays literal.\n')
        self.assertEqual(self.problems('guide.md'), [])

    def test_external_links_are_skipped(self):
        self.write('guide.md', '[site](https://example.com/a#b) '
                               '[mail](mailto:ops@example.com)\n')
        self.assertEqual(self.problems('guide.md'), [])

    def test_reference_definition_is_checked(self):
        self.write('guide.md', '[runbook][rb]\n\n[rb]: missing.md\n')
        problem = self.problems('guide.md')
        self.assertEqual(len(problem), 1)
        self.assertIn('does not exist', problem[0])

    def test_escape_from_repository_is_reported(self):
        self.write('guide.md', '[outside](../elsewhere.md)\n')
        problem = self.problems('guide.md')
        self.assertEqual(len(problem), 1)
        self.assertIn('escapes the repository', problem[0])

    def test_repository_documentation_is_clean(self):
        files = verify_docs_links.markdown_files(ROOT)
        problems, links = verify_docs_links.check(ROOT, files)
        self.assertGreater(len(files), 50)
        self.assertGreater(links, 500)
        self.assertEqual(problems, [])


if __name__ == '__main__':
    unittest.main()
