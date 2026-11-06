#!/usr/bin/env python3
"""Preserve required contexts while skipping proven documentation-only work.

The shared classifier checks the complete PR or push diff, including file modes
and protected source locations. Repository-specific checkout/ancestry checks
remain strict. Manual runs always build the exact source and exported images.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

ZERO_SHA = '0' * 40
COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
MAX_DIFF_BYTES = 16 * 1024 * 1024

# Vendored from the shared toolkit; no network or dependency install is needed.
_SPEC = importlib.util.spec_from_file_location(
    'shared_change_scope', Path(__file__).with_name('change_scope.py'))
shared_scope = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(shared_scope)


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
    """Retain the path helper without treating source fixtures as prose."""
    return bool(paths) and all(shared_scope.documentation_path(path, ()) for path in paths)


def classify(event_name, event, github_sha, root):
    checked_out = commit(git(root, 'rev-parse', 'HEAD').decode().strip())
    if checked_out != commit(github_sha):
        raise ValueError('Checkout differs from the triggering commit')
    if event_name == 'push':
        base, head = event.get('before'), commit(event['after'])
        if head != checked_out:
            raise ValueError('Push head differs from the checked-out commit')
        result = shared_scope.classify(root, event_name, event)
        return {'images': result['run'], 'documentation_only': not result['run'],
                'reason': result['reason'], 'head': head, 'base': base,
                'checked_out': checked_out}
    if event_name != 'pull_request':
        # A dispatch can export images and must always verify the exact source.
        return {'images': True, 'documentation_only': False,
                'reason': 'not-a-pull-request', 'head': checked_out, 'base': None}
    pull = event['pull_request']
    base, head = commit(pull['base']['sha']), commit(pull['head']['sha'])
    for value in (base, head):
        ancestor(root, value, checked_out)
    ancestors = git(root, 'merge-base', '--all', base, head).decode().splitlines()
    if len(ancestors) != 1:
        raise ValueError('Cannot prove a unique pull-request merge base')
    comparison_base = commit(ancestors[0])
    # Target-branch changes after this PR branched are not part of its diff.
    # Use the same proven merge-base comparison as the optional wake scope.
    paths = changed_paths(root, comparison_base, head)
    result = shared_scope.classify(root, event_name, event)
    skip = not result['run']
    return {'images': not skip, 'documentation_only': skip,
            'reason': 'documentation-only-diff' if skip else 'changed-paths',
            'shared_reason': result['reason'],
            'base': base, 'comparison_base': comparison_base,
            'head': head, 'checked_out': checked_out,
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
