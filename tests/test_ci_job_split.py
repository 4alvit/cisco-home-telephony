"""The split between short checks and container builds must not decay.

A pull request gets its first verdict from the short `validate` job, while the
`images` job pays the minutes for building, scanning and smoking the
containers. Two regressions undo that without any visible error: moving one
build step back into the fast job restores the ten-minute wait before any
result, and dropping or renaming the slow job leaves the branch policy waiting
for a check context that never appears.

A third regression is quieter than either: hiding the slow job behind a path
filter. Markdown-only pull requests should not pay for the builds, but dropping
the whole job leaves the branch policy waiting for a required context that is
never reported, so the pull request cannot merge at all. The job therefore
always runs and the repository gates its steps instead; these checks hold that
shape as much as they hold the split.

The workflows are parsed here without PyYAML, whose absence the test
environment already relies on, so only the top-level `jobs:` subset this
repository uses is understood. The live ruleset is external state and is
verified against GitHub separately; these checks prove the repository still
describes the split it claims.
"""
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'
POLICY = ROOT / 'docs' / 'ci-images.md'
JOBS_KEY = re.compile(r'^jobs\s*:\s*$')
JOB_KEY = re.compile(r'^ {2}([A-Za-z0-9_-]+)\s*:\s*$')
JOB_NAME = re.compile(r'^ {4}name\s*:\s*(\S.*?)\s*$', re.MULTILINE)
FAST_JOB = 'validate'
SLOW_JOB = 'images'
# Pulling, building, scanning or smoking a container image is slow work.
CONTAINER_WORK = ('docker', 'trivy', 'build-voice-image.sh', 'build-images.sh',
                  'smoke-containers.sh', 'tools.l16_lab',
                  'verify_voice_security.py', 'test_external_firewall')
SLOW_JOB_WORK = ('build-voice-image.sh', 'build-images.sh', 'smoke-containers.sh')
REQUIRED = ('validate', 'images', 'Wake gate')
STEP_START = re.compile(r'^ {6}- ')
JOB_LEVEL_IF = re.compile(r'^ {4}if\s*:', re.MULTILINE)
TRIGGER_START = re.compile(r'^on\s*:\s*$')
PATH_FILTER = re.compile(r'^\s*paths(?:-ignore)?\s*:', re.MULTILINE)
SCOPE_GATE = "steps.scope.outputs.images == 'true'"
SCOPE_SCRIPT = 'scripts/ci_images_scope.py'
REQUIRED_SENTENCE = re.compile(r'The required contexts are (.+?)\.', re.S)
BACKTICK = re.compile(r'`([^`]+)`')


def job_bodies(text):
    """Return {job key: that job's indented lines} for a workflow's jobs block."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if JOBS_KEY.match(line)), None)
    if start is None:
        raise ValueError('workflow declares no top-level jobs key')
    bodies = {}
    current = None
    for line in lines[start + 1:]:
        if line.strip() and not line[:1].isspace():
            break
        entry = JOB_KEY.match(line)
        if entry:
            current = entry.group(1)
            bodies[current] = []
            continue
        if current is not None:
            bodies[current].append(line)
    return {key: '\n'.join(body) for key, body in bodies.items()}


def job_names(text):
    """Return {job key: display name}, falling back to the key."""
    names = {}
    for key, body in job_bodies(text).items():
        match = JOB_NAME.search(body)
        names[key] = match.group(1).strip().strip('\'"') if match else key
    return names


def executable_lines(body):
    """Return the body without comment-only lines, lowercased."""
    return '\n'.join(line for line in body.splitlines()
                     if not line.strip().startswith('#')).lower()


def fast_job_problems(text):
    """Return one problem per reason the short job is no longer short."""
    bodies = job_bodies(text)
    if FAST_JOB not in bodies:
        return [f'no {FAST_JOB} job remains, so no pull request gets a fast check']
    if SLOW_JOB not in bodies:
        return [f'no {SLOW_JOB} job remains, so the container work is homeless']
    body = executable_lines(bodies[FAST_JOB])
    return [f'{FAST_JOB} runs {marker}, which belongs to {SLOW_JOB}'
            for marker in CONTAINER_WORK if marker in body]


def slow_job_problems(text):
    """Return one problem per piece of container work that disappeared."""
    body = executable_lines(job_bodies(text).get(SLOW_JOB, ''))
    return [f'{SLOW_JOB} no longer runs {marker}'
            for marker in SLOW_JOB_WORK if marker not in body]


def trigger_block(text):
    """Return the workflow's top-level `on:` block."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if TRIGGER_START.match(line)), None)
    if start is None:
        raise ValueError('workflow declares no top-level on key')
    block = []
    for line in lines[start + 1:]:
        if line.strip() and not line[:1].isspace():
            break
        block.append(line)
    return '\n'.join(block)


def job_steps(body):
    """Return one block of text per step in a job body."""
    blocks, current = [], None
    for line in body.splitlines():
        if STEP_START.match(line):
            if current is not None:
                blocks.append('\n'.join(current))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append('\n'.join(current))
    return blocks


def skipped_build_problems(text):
    """Return one problem per way the slow job could stop reporting its context."""
    problems = []
    if PATH_FILTER.search(trigger_block(text)):
        problems.append('a path filter drops the whole job, so the required context '
                        'is never reported for a filtered pull request')
    body = job_bodies(text).get(SLOW_JOB, '')
    if JOB_LEVEL_IF.search(body):
        problems.append(f'{SLOW_JOB} has a job-level condition, so GitHub may report no context')
    if SCOPE_SCRIPT not in executable_lines(body):
        problems.append(f'{SLOW_JOB} no longer reads the documentation-only decision')
    for step in job_steps(body):
        executable = executable_lines(step)
        if any(marker in executable for marker in CONTAINER_WORK) and SCOPE_GATE not in executable:
            problems.append('container work runs without the scope gate: '
                            + step.strip().splitlines()[0].lstrip('- ').strip())
    return problems


def policy_problems(doc, workflow_directory):
    """Return problems with the documented required contexts."""
    match = REQUIRED_SENTENCE.search(doc)
    if not match:
        return ['the branch policy states no required contexts']
    documented = BACKTICK.findall(match.group(1))
    problems = [f'the policy no longer requires {context}'
                for context in REQUIRED if context not in documented]
    problems += [f'the policy requires {context}, which no workflow declares'
                 for context in documented
                 if context not in declared_contexts(workflow_directory)]
    return problems


def declared_contexts(workflow_directory):
    """Return every check context a tracked workflow can report."""
    declared = set()
    for path in sorted(workflow_directory.glob('*.y*ml')):
        text = path.read_text(encoding='utf-8')
        declared.update(job_names(text).values())
    return declared


def workflow_text(name):
    return (WORKFLOWS / name).read_text(encoding='utf-8')


def policy_text():
    return POLICY.read_text(encoding='utf-8')


class RepositorySplitTests(unittest.TestCase):
    def test_the_fast_job_carries_no_container_work(self):
        problems = fast_job_problems(workflow_text('validate.yml'))
        self.assertEqual([], problems, '\n'.join(problems))

    def test_the_slow_job_still_builds_and_smokes_every_image(self):
        problems = slow_job_problems(workflow_text('validate.yml'))
        self.assertEqual([], problems, '\n'.join(problems))

    def test_a_documentation_only_pull_request_keeps_the_context_reported(self):
        problems = skipped_build_problems(workflow_text('validate.yml'))
        self.assertEqual([], problems, '\n'.join(problems))

    def test_a_path_filter_on_the_slow_job_is_rejected(self):
        text = workflow_text('validate.yml')
        anchor = '  pull_request:\n'
        self.assertIn(anchor, text, 'the pull-request trigger changed')
        mutated = text.replace(anchor, anchor + '    paths-ignore: ["docs/**"]\n', 1)
        self.assertTrue(skipped_build_problems(mutated))

    def test_a_container_step_outside_the_scope_gate_is_rejected(self):
        text = workflow_text('validate.yml')
        gated = ('      - name: Build both PBX images and verify binary versions\n'
                 f'        if: {SCOPE_GATE}\n')
        self.assertIn(gated, text, 'the PBX build is no longer gated by the scope decision')
        mutated = text.replace(gated, '      - name: Build both PBX images and verify binary versions\n', 1)
        problems = skipped_build_problems(mutated)
        self.assertTrue(problems, 'an ungated build step was accepted')

    def test_a_job_level_condition_on_the_slow_job_is_rejected(self):
        text = workflow_text('validate.yml')
        anchor = '  images:\n    runs-on: ubuntu-24.04\n'
        self.assertIn(anchor, text, 'the slow job header changed')
        mutated = text.replace(anchor, '  images:\n    if: false\n    runs-on: ubuntu-24.04\n', 1)
        self.assertTrue(skipped_build_problems(mutated))

    def test_the_policy_requires_the_contexts_the_workflows_declare(self):
        problems = policy_problems(policy_text(), WORKFLOWS)
        self.assertEqual([], problems, '\n'.join(problems))

    def test_a_build_step_moved_into_the_real_fast_job_is_rejected(self):
        text = workflow_text('validate.yml')
        anchor = '      - name: Python and shell checks'
        self.assertIn(anchor, text, 'the fast job no longer starts with that step')
        mutated = text.replace(
            anchor, '      - name: Build the images\n'
                    '        run: bash scripts/build-images.sh cisco-home-ci\n'
                    + anchor, 1)
        self.assertTrue(fast_job_problems(mutated))

    def test_the_real_policy_dropping_the_slow_job_is_rejected(self):
        text = policy_text()
        sentence = '`validate`, `images` and `Wake gate`'
        self.assertIn(sentence, text, 'the required-contexts wording changed')
        mutated = text.replace(sentence, '`validate` and `Wake gate`', 1)
        self.assertTrue(policy_problems(mutated, WORKFLOWS))


class SplitFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text.strip() + '\n', encoding='utf-8')
        return path

    def workflow(self, fast_steps, slow_steps, slow_job=True):
        slow = ''
        if slow_job:
            slow = ('  images:\n    runs-on: ubuntu-24.04\n    steps:\n'
                    + slow_steps)
        return self.write('validate.yml', '''
name: Validate
on:
  pull_request:
jobs:
  validate:
    runs-on: ubuntu-24.04
    steps:
''' + fast_steps + slow)

    def test_a_split_workflow_is_accepted(self):
        path = self.workflow(
            '      - name: Test\n        run: python3 -m unittest discover -s tests\n',
            '      - name: Build\n        run: bash scripts/build-voice-image.sh ci\n'
            '      - name: Images\n        run: bash scripts/build-images.sh ci\n'
            '      - name: Smoke\n        run: bash scripts/smoke-containers.sh ci\n')
        text = path.read_text(encoding='utf-8')
        self.assertEqual([], fast_job_problems(text))
        self.assertEqual([], slow_job_problems(text))

    def test_a_missing_slow_job_is_rejected(self):
        path = self.workflow(
            '      - name: Test\n        run: python3 -m unittest discover -s tests\n',
            '', slow_job=False)
        self.assertTrue(fast_job_problems(path.read_text(encoding='utf-8')))

    def test_build_and_scan_steps_in_the_fast_job_are_rejected(self):
        for step in ('bash scripts/build-voice-image.sh ci',
                     'docker build -t example:ci .',
                     'trivy image example:ci',
                     'python3 -m tools.l16_lab asterisk --image example:ci'):
            with self.subTest(step=step):
                path = self.workflow(
                    f'      - name: Work\n        run: {step}\n',
                    '      - name: Build\n        run: bash scripts/build-voice-image.sh ci\n'
                    '      - name: Images\n        run: bash scripts/build-images.sh ci\n'
                    '      - name: Smoke\n        run: bash scripts/smoke-containers.sh ci\n')
                self.assertTrue(fast_job_problems(path.read_text(encoding='utf-8')))

    def test_a_comment_mentioning_docker_is_not_a_build_step(self):
        path = self.workflow(
            '      # No docker here on purpose; the slow job owns the builds.\n'
            '      - name: Test\n        run: python3 -m unittest discover -s tests\n',
            '      - name: Build\n        run: bash scripts/build-voice-image.sh ci\n'
            '      - name: Images\n        run: bash scripts/build-images.sh ci\n'
            '      - name: Smoke\n        run: bash scripts/smoke-containers.sh ci\n')
        self.assertEqual([], fast_job_problems(path.read_text(encoding='utf-8')))

    def test_requires_an_images_job_that_actually_builds(self):
        path = self.workflow(
            '      - name: Test\n        run: python3 -m unittest discover -s tests\n',
            '      - name: Build\n        run: bash scripts/build-voice-image.sh ci\n')
        problems = slow_job_problems(path.read_text(encoding='utf-8'))
        self.assertEqual(2, len(problems), '\n'.join(problems))

    def test_a_policy_context_no_workflow_declares_is_rejected(self):
        path = self.workflow(
            '      - name: Test\n        run: python3 -m unittest discover -s tests\n',
            '      - name: Build\n        run: bash scripts/build-voice-image.sh ci\n'
            '      - name: Images\n        run: bash scripts/build-images.sh ci\n'
            '      - name: Smoke\n        run: bash scripts/smoke-containers.sh ci\n')
        doc = ('The required contexts are `validate`, `container images` and '
               '`Wake gate`.\n')
        problems = policy_problems(doc, path.parent)
        self.assertTrue(problems)
        self.assertIn('container images', ' '.join(problems))

    def test_a_policy_sentence_without_contexts_is_rejected(self):
        self.assertTrue(policy_problems('Branch policy is external.\n', self.root))

    def test_a_display_name_replaces_its_job_id_context(self):
        path = self.workflow(
            '      - run: python3 -m unittest discover -s tests\n',
            '      - run: bash scripts/build-images.sh ci\n')
        self.write('wake.yml', '''
jobs:
  gate:
    name: Wake gate
    runs-on: ubuntu-24.04
    steps:
      - run: true
''')
        doc = 'The required contexts are `validate`, `images` and `Wake gate`.\n'
        self.assertEqual([], policy_problems(doc, self.root))
        original = path.read_text(encoding='utf-8')
        for name in ('Container build', "'Container build'", '"Container build"'):
            with self.subTest(name=name):
                path.write_text(original.replace(
                    '  images:\n', f'  images:\n    name: {name}\n'),
                    encoding='utf-8')
                self.assertEqual({'validate', 'Container build', 'Wake gate'},
                                 declared_contexts(self.root))
                self.assertEqual(
                    ['the policy requires images, which no workflow declares'],
                    policy_problems(doc, self.root))


if __name__ == '__main__':
    unittest.main()
