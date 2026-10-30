#!/usr/bin/env python3
"""Prove that a pull request changes nothing but documentation.

Markdown cannot reach a container image or a Python, shell or media check, so
`images` may skip its builds and `validate` may skip its heavy steps on such a
pull request. Both jobs are required contexts, so neither may be dropped by a
`paths-ignore` filter: the branch policy would then wait for a check that never
arrives. The decision therefore runs inside the running job and only skips work.

Everything else keeps the full work. Pushes and manual dispatches run
unconditionally, so the default branch and every exported artifact still come
from a real build of the exact commit. An invalid or zero SHA, a checkout that
differs from the triggering commit, a base commit that is not contained in that
checkout, an unexpected path or an empty diff also keeps it: a wrong
"documentation only" answer would merge unbuilt container code and untested
Python, so the decision fails closed rather than assuming.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

ZERO_SHA = '0' * 40
COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
MAX_DIFF_BYTES = 16 * 1024 * 1024


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if len(result.stdout) > MAX_DIFF_BYTES:
        raise ValueError('Unexpectedly large Git evidence')
    return result.stdout


def commit(value):
    if not isinstance(value, str) or not COMMIT.fullmatch(value) or value == ZERO_SHA:
        raise ValueError('Expected an exact nonzero commit SHA')
    return value


def ancestor(root, value, descendant):
    try:
        git(root, 'merge-base', '--is-ancestor', value, descendant)
    except subprocess.CalledProcessError as error:
        raise ValueError('Event commit is not contained in the checked-out merge') from error


def changed_paths(root, base, head):
    raw = git(root, 'diff', '--no-ext-diff', '--no-renames', '--name-only', '-z', base, head, '--')
    if raw and not raw.endswith(b'\0'):
        raise ValueError('Incomplete NUL-delimited Git evidence')
    paths = raw.decode('utf-8').split('\0')[:-1]
    if any(not name or name.startswith('/') or '..' in Path(name).parts for name in paths):
        raise ValueError('Unexpected repository path')
    return paths


def documentation_only(paths):
    """True only for a non-empty change set that is entirely Markdown."""
    return bool(paths) and all(path.lower().endswith('.md') for path in paths)


def classify(event_name, event, github_sha, root):
    checked_out = commit(git(root, 'rev-parse', 'HEAD').decode().strip())
    if checked_out != commit(github_sha):
        raise ValueError('Checkout differs from the triggering commit')
    if event_name != 'pull_request':
        # Pushes keep the default branch honestly built and a dispatch exists to
        # export verified bytes, so neither may skip anything.
        return {'images': True, 'documentation_only': False,
                'reason': 'not-a-pull-request', 'head': checked_out, 'base': None}
    pull = event['pull_request']
    base, head = commit(pull['base']['sha']), commit(pull['head']['sha'])
    for value in (base, head):
        ancestor(root, value, checked_out)
    paths = changed_paths(root, base, head)
    skip = documentation_only(paths)
    return {'images': not skip, 'documentation_only': skip,
            'reason': 'documentation-only-diff' if skip else 'changed-paths',
            'base': base, 'head': head, 'checked_out': checked_out,
            'changed_paths': len(paths)}


def main():
    result = classify(os.environ['GITHUB_EVENT_NAME'],
                      json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()),
                      os.environ['GITHUB_SHA'], Path.cwd())
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
        for key in ('images', 'documentation_only'):
            stream.write(key + '=' + str(result[key]).lower() + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
