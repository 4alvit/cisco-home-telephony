#!/usr/bin/env python3
"""Prove whether an exact GitHub event changes the optional wake image inputs."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess

RELEVANT_PATHS = (
    '.github/workflows/voice-wake.yml',
    'containers/voice-wake/**', 'containers/voice/**',
    'containers/asterisk/phone_*.py', 'voice_assistant/**',
    'scripts/build-voice-wake-image.sh', 'scripts/build-voice-image.sh',
    'scripts/verify_voice_security.py', 'scripts/voice_wake_scope.py',
    'tests/test_voice_installer.py', 'tests/test_voice_image_security.py',
    'scripts/voice_session.py', 'scripts/voice_session_sip.py',
    'scripts/render_voice_job.py', 'scripts/render_voice_wake_job.py',
    'tests/test_voice_wake_*.py', 'tests/test_render_voice_wake_job.py',
)
ZERO_SHA = '0' * 40


def commit(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value) or value == ZERO_SHA:
        raise ValueError('Expected an exact nonzero commit SHA')
    return value


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if len(result.stdout) > 16 * 1024 * 1024:
        raise ValueError('Unexpectedly large Git evidence')
    return result.stdout


def changed_paths(root, base, head):
    raw = (git(root, 'ls-tree', '-r', '--name-only', '-z', head) if base is None else
           git(root, 'diff', '--no-ext-diff', '--no-renames', '--name-only', '-z', base, head, '--'))
    if raw and not raw.endswith(b'\0'):
        raise ValueError('Incomplete NUL-delimited Git evidence')
    paths = raw.decode('utf-8').split('\0')[:-1]
    if any(not name or name.startswith('/') or '..' in Path(name).parts for name in paths):
        raise ValueError('Unexpected repository path')
    return paths


def classify(event_name, event, github_sha, root):
    checked_out = commit(git(root, 'rev-parse', 'HEAD').decode().strip())
    if checked_out != commit(github_sha):
        raise ValueError('Checkout differs from the triggering commit')
    if event_name == 'workflow_dispatch':
        return {'required': True, 'reason': 'explicit-dispatch', 'head': checked_out, 'base': None}
    if event_name == 'pull_request':
        pull = event['pull_request']
        base, head = commit(pull['base']['sha']), commit(pull['head']['sha'])
        # The checked-out GitHub test merge must include both exact event inputs.
        for sha in (base, head):
            git(root, 'merge-base', '--is-ancestor', sha, checked_out)
        ancestors = git(root, 'merge-base', '--all', base, head).decode().splitlines()
        if len(ancestors) != 1:
            raise ValueError('Cannot prove a unique pull-request merge base')
        comparison_base = commit(ancestors[0])
    elif event_name == 'push':
        if event.get('deleted') is not False or commit(event['after']) != checked_out:
            raise ValueError('Push event does not identify the checked-out commit')
        head = checked_out
        base = event['before']
        comparison_base = None if base == ZERO_SHA else commit(base)
    else:
        raise ValueError('Unsupported event cannot bypass the wake build')
    paths = changed_paths(root, comparison_base, head)
    required = any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in RELEVANT_PATHS)
    return {'required': required, 'reason': 'relevant-diff' if required else 'proven-irrelevant-diff',
            'base': base, 'comparison_base': comparison_base, 'head': head,
            'checked_out': checked_out, 'changed_paths': len(paths)}


def verify_gate(scope_result, required, build_result):
    if scope_result != 'success' or required not in ('true', 'false'):
        raise ValueError('Wake scope did not complete with a recognized decision')
    expected = 'success' if required == 'true' else 'skipped'
    if build_result != expected:
        raise ValueError('Wake build did not match the proven scope decision')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gate', action='store_true')
    args = parser.parse_args()
    if args.gate:
        verify_gate(os.environ.get('SCOPE_RESULT'), os.environ.get('WAKE_REQUIRED'),
                    os.environ.get('BUILD_RESULT'))
        print('Wake scope and required build verified.')
        return
    result = classify(os.environ['GITHUB_EVENT_NAME'],
                      json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()),
                      os.environ['GITHUB_SHA'], Path.cwd())
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
        stream.write('required=' + str(result['required']).lower() + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
