#!/usr/bin/env python3
"""Play one prepared announcement and write a private completion receipt."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time


def check(condition):
    if not condition:
        raise ValueError('invalid_announcement')


def metadata(path, *, directory=False, maximum=262144):
    info = path.lstat()
    check((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
          and info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077
          and (directory or info.st_size <= maximum))


def environment(source):
    result = {}
    for _ in range(100):
        line = source.readline(4097)
        check(len(line) <= 4096)
        if line in ('\n', '\r\n'):
            return result
        check(line and ': ' in line)
        key, value = line.rstrip('\r\n').split(': ', 1)
        result[key] = value
    raise ValueError('invalid_agi_environment')


def receipt(directory, job_id, line, uniqueid, status):
    target = directory / ('receipt-' + line + '.json')
    temporary = directory / ('receipt-' + line + '.tmp-' + str(os.getpid()))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump({'job_id': job_id, 'line': line, 'uniqueid': uniqueid, 'status': status}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def play(arguments, source=sys.stdin, output=sys.stdout, clock=time.time):
    check(len(arguments) == 3)
    job_id, line, spool = arguments
    check(re.fullmatch(r'[0-9a-f]{32}', job_id) and re.fullmatch(r'2[0-9]{2}', line))
    check(re.fullmatch(r'/[A-Za-z0-9_./-]{1,1023}', spool)
          and '..' not in Path(spool).parts and str(Path(spool)) == spool)
    directory = Path(spool) / job_id
    metadata(Path(spool), directory=True)
    metadata(directory, directory=True)
    journal = directory / 'job.json'
    metadata(journal)
    job = json.loads(journal.read_text())
    check(job['id'] == job_id and line in job['lines'] and job['status'] == 'running')
    selected = [r for r in job['recipients'] if r['line'] == line]
    check(len(selected) == 1 and not selected[0]['cancel'])
    root = 'announce-' + job_id + '-' + line
    check(selected[0]['root'] == root)
    remaining = job['expires_at'] - clock()
    check(0 < remaining <= 180)
    env = environment(source)
    uniqueid = env.get('agi_uniqueid')
    check(uniqueid in (root, root + '-b'))
    check(re.fullmatch(r'Local/' + line + r'@cisco-announcement-[0-9a-fA-F]+;[12]',
                      env.get('agi_channel', '')))
    audio = directory / 'speech.sln16'
    metadata(audio, maximum=120 * 32000)
    check(audio.stat().st_size >= 12800 and audio.stat().st_size % 2 == 0)

    def interrupted(_number, _frame):
        raise InterruptedError('call_ended')

    previous = {number: signal.signal(number, interrupted) for number in (
        signal.SIGHUP, signal.SIGTERM, signal.SIGALRM)}
    signal.alarm(math.ceil(remaining))
    try:
        receipt(directory, job_id, line, uniqueid, 'playing')
        output.write('STREAM FILE ' + str(audio.with_suffix('')) + ' ""\n')
        output.flush()
        response = source.readline(4097)
        check(len(response) <= 4096 and re.fullmatch(r'200 result=0(?: endpos=[0-9]+)?\r?\n', response))
        receipt(directory, job_id, line, uniqueid, 'completed')
        return 0
    except Exception:
        receipt(directory, job_id, line, uniqueid, 'failed')
        return 1
    finally:
        signal.alarm(0)
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        sys.exit(play(sys.argv[1:]))
    except Exception:
        sys.exit(1)
