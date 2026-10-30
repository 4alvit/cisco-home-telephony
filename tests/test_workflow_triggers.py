"""Workflow triggers must not run CI twice for one pull-request commit.

A `push` trigger without branch filters and a `pull_request` trigger both fire
for the same branch commit. Their concurrency groups key on different refs, so
neither cancels the other and every commit is built twice. The workflows are
parsed here without PyYAML, whose absence the test environment already relies
on, so only the small `on:` subset this repository uses is understood.
"""
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'
PULL_REQUEST_EVENTS = {'pull_request', 'pull_request_target'}
BRANCH_FILTERS = {'branches', 'branches-ignore'}
ON_KEY = re.compile(r"""^(?:on|'on'|"on")\s*:(.*)$""")
KEY = re.compile(r'^([A-Za-z0-9_-]+)\s*:\s*(.*)$')


def triggers(text):
    """Return {event: {sub-key}} for a workflow's top-level `on` mapping."""
    lines = text.splitlines()
    start = None
    tail = ''
    for index, line in enumerate(lines):
        match = ON_KEY.match(line)
        if match:
            start, tail = index, match.group(1).strip()
            break
    if start is None:
        raise ValueError('workflow declares no top-level on key')
    if tail:
        # Flow style, e.g. `on: [push, pull_request]` or `on: push`.
        body = tail.strip('[]')
        return {name.strip().strip('\'"'): set() for name in body.split(',')
                if name.strip()}
    events = {}
    current = None
    depth = None
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if not line[:1].isspace():
            if stripped.startswith('#'):
                continue
            break
        entry = KEY.match(stripped)
        if not entry:
            continue
        indent = len(line) - len(line.lstrip())
        if depth is None:
            depth = indent
        if indent == depth:
            current = entry.group(1)
            events[current] = set()
        elif current is not None:
            events[current].add(entry.group(1))
    return events


def duplicate_runs(paths):
    """Return one problem per workflow that builds a branch commit twice."""
    problems = []
    for path in paths:
        events = triggers(path.read_text(encoding='utf-8'))
        shared = PULL_REQUEST_EVENTS & set(events)
        if 'push' not in events or not shared:
            continue
        if BRANCH_FILTERS & events['push']:
            continue
        problems.append(
            f'{path.name}: {sorted(shared)[0]} and an unfiltered push both '
            'trigger, so one branch commit runs the workflow twice')
    return problems


def workflow_files():
    """Return the tracked workflow files in a stable order."""
    return sorted(list(WORKFLOWS.glob('*.yml')) + list(WORKFLOWS.glob('*.yaml')))


class WorkflowTriggerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text.strip() + '\n', encoding='utf-8')
        return path

    def test_repository_workflows_never_duplicate_a_pull_request_run(self):
        problems = duplicate_runs(workflow_files())
        self.assertEqual([], problems, '\n'.join(problems))

    def test_repository_workflows_keep_branch_scoped_pushes(self):
        parsed = {path.name: triggers(path.read_text(encoding='utf-8'))
                  for path in workflow_files()}
        self.assertIn('validate.yml', parsed, 'workflow discovery is broken')
        self.assertIn('pull_request', parsed['validate.yml'])
        self.assertIn('branches', parsed['validate.yml']['push'])
        self.assertIn('branches', parsed['voice-wake.yml']['push'])

    def test_unfiltered_push_next_to_pull_request_is_rejected(self):
        path = self.write('duplicate.yml', '''
name: Duplicate
on:
  push:
  pull_request:
jobs:
  check:
    runs-on: ubuntu-24.04
''')
        self.assertEqual(1, len(duplicate_runs([path])))

    def test_flow_style_triggers_are_rejected(self):
        path = self.write('flow.yml', '''
name: Flow
on: [push, pull_request]
jobs:
  check:
    runs-on: ubuntu-24.04
''')
        self.assertEqual(1, len(duplicate_runs([path])))

    def test_path_filtered_push_is_still_a_duplicate(self):
        path = self.write('paths.yml', '''
name: Paths
on:
  push:
    paths: ['docs/**']
  pull_request:
jobs:
  check:
    runs-on: ubuntu-24.04
''')
        self.assertEqual(1, len(duplicate_runs([path])))

    def test_branch_scoped_push_is_accepted(self):
        path = self.write('scoped.yml', '''
name: Scoped
on:
  push:
    branches: [main]
  pull_request:
jobs:
  check:
    runs-on: ubuntu-24.04
''')
        self.assertEqual([], duplicate_runs([path]))

    def test_push_only_workflow_is_accepted(self):
        path = self.write('push-only.yml', '''
name: Push only
on:
  push:
jobs:
  check:
    runs-on: ubuntu-24.04
''')
        self.assertEqual([], duplicate_runs([path]))

    def test_comments_inside_the_trigger_block_are_ignored(self):
        path = self.write('commented.yml', '''
name: Commented
on:
  # Branch pushes are covered by the pull request event.
  push:
    branches:
      - main
  pull_request:
jobs:
  check:
    runs-on: ubuntu-24.04
''')
        self.assertEqual([], duplicate_runs([path]))


if __name__ == '__main__':
    unittest.main()
