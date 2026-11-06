#!/usr/bin/env python3
"""Exercise real APT transport behavior against an isolated loopback origin.

Run inside an existing image with ``docker run --network none``; this script
never installs packages or contacts a package mirror. It is deliberately not
named test_*.py: ordinary unit discovery must not start transport experiments.
Use --require-apt-version 3.0.3 when qualifying the pinned Trixie runtime.
"""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time


HELPER = '/usr/lib/apt/apt-helper'
PAYLOAD = bytes(range(256)) * (4 * 1024 * 1024 // 256)
SMALL_PAYLOAD = PAYLOAD[:4096]
CHILD_LIMITS = '''\
import os, resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
os.execv(sys.argv[1], sys.argv[1:])
'''


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


class Origin(HTTPServer):
    """One bounded origin, with an independent request record for each case."""

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.requests = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        allowed = {'/baseline', '/limited', '/mismatch', '/retry0', '/retry2'}
        if self.path not in allowed:
            self.send_error(404)
            return
        earlier = sum(item['path'] == self.path for item in self.server.requests)
        retry_after = self.path[-1] if self.path.startswith('/retry') else None
        status = 503 if retry_after is not None and earlier == 0 else 200
        body = (b'Synthetic service unavailable\n' if status == 503 else
                PAYLOAD if self.path in ('/baseline', '/limited') else SMALL_PAYLOAD)
        self.server.requests.append({
            'path': self.path, 'at': time.monotonic(), 'status': status,
            'retry_after': retry_after if status == 503 else None,
        })
        self.send_response(status)
        if status == 503:
            self.send_header('Retry-After', retry_after)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def config_text(directory, limit):
    """Suppress system configuration and put writable APT paths in one tempdir."""
    values = {
        'Dir::Etc::main': directory / 'empty.conf',
        'Dir::Etc::parts': directory / 'parts',
        'Dir::State': directory / 'state',
        'Dir::Cache': directory / 'cache',
        'Dir::Log': directory / 'log',
        'Acquire::Retries': '2',
        'Acquire::Retries::Delay': 'true',
        'Acquire::Retries::HandleRetryAfter': 'true',
    }
    for scheme in ('http', 'https'):
        values.update({
            f'Acquire::{scheme}::Proxy': 'DIRECT',
            f'Acquire::{scheme}::Proxy-Auto-Detect': '',
            f'Acquire::{scheme}::Timeout': '5',
            f'Acquire::{scheme}::Pipeline-Depth': '0',
            f'Acquire::{scheme}::Dl-Limit': str(limit),
            f'Acquire::{scheme}::AllowRedirect': 'false',
        })
    return ''.join(f'{key} {json.dumps(str(value))};\n' for key, value in values.items())


def installed_limits():
    # Capture privately: a complete APT configuration may contain proxy secrets.
    # Only the two public policy values are returned or included in evidence.
    dump = subprocess.check_output(
        ['/usr/bin/apt-config', 'dump'], text=True, timeout=5,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
    limits = {}
    for scheme in ('http', 'https'):
        key = f'Acquire::{scheme}::Dl-Limit'
        matches = re.findall(r'^' + re.escape(key) + r' "([0-9]+)";$',
                             dump, flags=re.MULTILINE | re.IGNORECASE)
        limits[scheme] = int(matches[0]) if len(matches) == 1 else None
    return limits


def run_case(origin, parent, name, limit, bad_hash=False):
    directory = parent / name
    directory.mkdir(mode=0o700)
    for child in ('parts', 'state', 'cache', 'log'):
        (directory / child).mkdir(mode=0o700)
    (directory / 'empty.conf').touch(mode=0o600)
    config = directory / 'apt.conf'
    config.write_text(config_text(directory, limit))
    body = PAYLOAD if name in ('baseline', 'limited') else SMALL_PAYLOAD
    expected = hashlib.sha256(body).hexdigest()
    destination = directory / 'download.bin'
    url = f'http://127.0.0.1:{origin.server_port}/{name}'
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
           'HOME': str(directory), 'TMPDIR': str(directory), 'APT_CONFIG': str(config)}
    command = [sys.executable, '-I', '-c', CHILD_LIMITS, HELPER, 'download-file',
               url, str(destination), 'SHA256:' + ('0' * 64 if bad_hash else expected)]
    start = time.monotonic()
    process = subprocess.Popen(command, env=env, cwd=directory,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=20)
    finally:
        # Also terminate a transport child if apt-helper exits unexpectedly.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    elapsed = time.monotonic() - start
    requests = [{**item, 'at': round(item['at'] - start, 4)}
                for item in origin.requests if item['path'] == '/' + name]
    result = {'case': name, 'limit_kib_s': limit, 'seconds': round(elapsed, 4),
              'exit_code': process.returncode, 'requests': requests}
    if bad_hash:
        require(process.returncode != 0 and 'Hash Sum mismatch' in output,
                'incorrect_sha256_was_not_rejected')
        result['hash_mismatch_rejected'] = True
    else:
        require(process.returncode == 0, f'{name}_download_failed: {output[-2000:]}')
        actual = hashlib.sha256(destination.read_bytes()).hexdigest()
        require(actual == expected and destination.stat().st_size == len(body),
                f'{name}_payload_mismatch')
        result.update({'bytes': len(body), 'sha256': actual})
    if name == 'limited':
        # 4 MiB at 1000 KiB/s takes approximately four seconds. Allow the
        # transport's initial buffer and timer granularity, not an exact rate.
        require(elapsed >= 3.0, 'download_limit_did_not_slow_four_mib_transfer')
    if name.startswith('retry'):
        require(len(requests) == 2 and [r['status'] for r in requests] == [503, 200],
                f'{name}_unexpected_retry_sequence')
        delay = requests[1]['at'] - requests[0]['at']
        # Retry-After uses wall-clock seconds; retain tolerance for rounding.
        require(delay >= (0.8 if name == 'retry0' else 1.8),
                f'{name}_retry_delay_not_observed')
        result['retry_gap_seconds'] = round(delay, 4)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-apt-version', help='Fail before tests on a version mismatch')
    args = parser.parse_args()
    result = {'status': 'FAIL', 'network': 'loopback_only', 'package_install': False,
              'uid': os.geteuid(), 'cases': []}
    try:
        require(os.geteuid() != 0, 'probe_requires_nonroot')
        version_text = subprocess.check_output(
            ['/usr/bin/apt', '--version'], text=True, timeout=5).strip()
        result['apt_version_text'] = version_text
        match = re.fullmatch(r'apt (\S+) \([^)]+\)', version_text)
        require(match is not None, 'unrecognized_apt_version')
        result['apt_version'] = match.group(1)
        if args.require_apt_version:
            require(match.group(1) == args.require_apt_version, 'apt_version_mismatch')
        require(Path(HELPER).is_file(), 'apt_helper_missing')
        result['installed_limit_kib_s'] = installed_limits()
        require(result['installed_limit_kib_s'] == {'http': 1000, 'https': 1000},
                'installed_http_https_limits_must_both_equal_1000')
        with tempfile.TemporaryDirectory(prefix='apt-loopback-probe-') as scratch:
            origin = Origin()
            thread = threading.Thread(target=origin.serve_forever,
                                      kwargs={'poll_interval': 0.1}, daemon=True)
            thread.start()
            try:
                for name, limit, bad_hash in (
                    ('baseline', 0, False), ('limited', 1000, False),
                    ('mismatch', 1000, True), ('retry0', 1000, False),
                    ('retry2', 1000, False),
                ):
                    result['cases'].append(run_case(origin, Path(scratch), name, limit, bad_hash))
                    if name == 'limited':
                        baseline, limited = result['cases'][:2]
                        require(limited['seconds'] >= baseline['seconds'] + 1.5,
                                'limited_transfer_not_demonstrably_slower_than_baseline')
            finally:
                origin.shutdown()
                origin.server_close()
                thread.join(timeout=5)
                require(not thread.is_alive(), 'origin_thread_did_not_stop')
        result['status'] = 'PASS'
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result['error'] = str(exc)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
