"""Keep credentials confined to the reviewed, immutable metadata workflow."""
import re
import unittest
from pathlib import Path

from test_ci_job_split import job_bodies

CALLER = Path(__file__).resolve().parents[1] / '.github/workflows/auto-approve.yml'
PINNED_CALL = re.compile(
    r'^    uses: victron-venus/venus-os-ci-toolkit/\.github/workflows/'
    r'auto-approve-reusable\.yml@[0-9a-f]{40}(?:[ \t]+#[^\r\n]*)?$', re.MULTILINE)


def boundary_problems(text):
    problems = []
    if set(job_bodies(text)) != {'auto-approve'}:
        problems.append('only the reviewed approval job is allowed')
    if len(PINNED_CALL.findall(text)) != 1:
        problems.append('approval must use an immutable shared workflow')
    if re.search(r'^\s*(?:-\s*)?(?:steps|run|runs-on|container|services):', text,
                 re.MULTILINE):
        problems.append('the credential caller must not execute local steps')
    if re.search(r'^\s*secrets:\s*inherit\s*$', text, re.MULTILINE):
        problems.append('secrets must be forwarded explicitly')
    forwarded = re.findall(r'^      (\w+): \$\{\{ secrets\.(\w+) \}\}$', text,
                           re.MULTILINE)
    if sorted(forwarded) != [('APPROVAL_PAT', 'APPROVAL_PAT'), ('BOT_PAT', 'BOT_PAT')]:
        problems.append('only the documented reviewer secrets may be forwarded')
    return problems


class ApprovalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.text = CALLER.read_text(encoding='utf-8')

    def test_repository_caller_preserves_the_credential_boundary(self):
        self.assertEqual([], boundary_problems(self.text))

    def test_floating_shared_code_is_rejected(self):
        mutated = re.sub(r'(?<=\.yml@)[0-9a-f]{40}', 'main', self.text)
        self.assertIn('approval must use an immutable shared workflow',
                      boundary_problems(mutated))

    def test_version_comment_preserves_the_immutable_reference(self):
        annotated = re.sub(r'(\.yml@[0-9a-f]{40})(?:[ \t]+#[^\r\n]*)?',
                           r'\1 # main', self.text)
        self.assertEqual([], boundary_problems(annotated))

    def test_uncommented_suffix_is_not_part_of_a_valid_reference(self):
        malformed = re.sub(r'(\.yml@[0-9a-f]{40})', r'\1 extra', self.text)
        self.assertIn('approval must use an immutable shared workflow',
                      boundary_problems(malformed))

    def test_executable_pr_steps_are_rejected(self):
        mutated = self.text + '\n    steps:\n      - uses: actions/checkout@main\n'
        self.assertIn('the credential caller must not execute local steps',
                      boundary_problems(mutated))

    def test_inherited_secrets_are_rejected(self):
        mutated = self.text.replace('    secrets:\n', '    secrets: inherit\n')
        self.assertIn('secrets must be forwarded explicitly', boundary_problems(mutated))


if __name__ == '__main__':
    unittest.main()
