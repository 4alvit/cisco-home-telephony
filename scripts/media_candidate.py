#!/usr/bin/env python3
"""Freeze or verify local, declared media candidate inputs; never run a live test."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import time

MAX_JSON = 8 * 1024 * 1024
MAX_FILE = 512 * 1024 * 1024
MAX_TREE_FILES = 4096
MAX_TREE_BYTES = 1024 * 1024 * 1024
LABEL = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}\Z')
OBJECT_ID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
IMAGE = re.compile(r'(?:(?:containerd|docker-pullable)://)?(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:([0-9a-f]{64})\Z')
HASH_RULES = {
    'files': 'sha256 of exact file bytes',
    'json_sections': 'sha256 of UTF-8 JSON, sorted keys, ASCII escapes, compact separators, no NaN',
    'installed_tree': 'all regular files, relative POSIX paths, exact bytes matched to the source Git tree; no exclusions',
    'images': 'exact sha256 digest from a supplied local observation; no registry lookup',
    'reported_fingerprints': 'collector-reported sha256 and length of raw bytes; no direct access to the remote file',
}


class Refused(ValueError):
    """A static reason safe to report without exposing input contents."""


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'duplicate_json_key')
        value[key] = item
    return value


def decode(raw):
    def finite_number(text):
        value = float(text)
        require(math.isfinite(value), 'invalid_json')
        return value

    def invalid_constant(_):
        raise Refused('invalid_json')

    try:
        return json.loads(raw, object_pairs_hook=unique_object,
                          parse_float=finite_number, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, Refused):
            raise
        raise Refused('invalid_json') from None


def path_from(value, base):
    require(isinstance(value, str) and value and '\x00' not in value, 'invalid_path')
    path = Path(value)
    require('..' not in path.parts, 'path_traversal')
    return Path(os.path.abspath(path if path.is_absolute() else base / path))


@contextmanager
def directory(path):
    """Open every directory component without following symlinks."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def read_descriptor(descriptor, *, limit, keep_bytes=False):
    before = os.fstat(descriptor)
    require(stat.S_ISREG(before.st_mode) and before.st_size <= limit, 'invalid_or_oversized_file')
    digest, length, chunks = hashlib.sha256(), 0, []
    while chunk := os.read(descriptor, min(1024 * 1024, limit + 1 - length)):
        length += len(chunk)
        require(length <= limit, 'invalid_or_oversized_file')
        digest.update(chunk)
        if keep_bytes:
            chunks.append(chunk)
    after = os.fstat(descriptor)
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            and length == before.st_size, 'file_changed_during_read')
    return {'sha256': digest.hexdigest(), 'bytes': length}, b''.join(chunks)


def read_file(path, *, limit=MAX_FILE, keep_bytes=False):
    with directory(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            return read_descriptor(descriptor, limit=limit, keep_bytes=keep_bytes)
        finally:
            os.close(descriptor)


def read_json(path):
    _, raw = read_file(path, limit=MAX_JSON, keep_bytes=True)
    return decode(raw)


def section(value, pointer):
    require(isinstance(pointer, str) and len(pointer) <= 1024
            and (pointer == '' or pointer.startswith('/')), 'invalid_json_pointer')
    if not pointer:
        return value
    for raw in pointer[1:].split('/'):
        require(re.search(r'~(?![01])', raw) is None, 'invalid_json_pointer')
        key = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            require(key in value, 'missing_json_section')
            value = value[key]
        elif isinstance(value, list):
            require(re.fullmatch(r'0|[1-9][0-9]*', key) is not None
                    and int(key) < len(value), 'missing_json_section')
            value = value[int(key)]
        else:
            raise Refused('missing_json_section')
    return value


def json_input(value, base):
    require(isinstance(value, dict) and set(value) == {'file', 'pointer'}, 'invalid_json_input')
    return section(read_json(path_from(value['file'], base)), value['pointer'])


def git(repo, *args, limit=MAX_JSON):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_NO_LAZY_FETCH='1', GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0',
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    command = ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
               '-c', 'protocol.allow=never', '-C', str(repo), *args]
    chunks, length, deadline = [], 0, time.monotonic() + 30
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, env=environment) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    require(remaining > 0 and selector.select(remaining), 'git_source_timeout')
                    chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - length))
                    if not chunk:
                        break
                    length += len(chunk)
                    require(length <= limit, 'git_source_unavailable')
                    chunks.append(chunk)
            require(process.wait(timeout=max(.01, deadline - time.monotonic())) == 0,
                    'git_source_unavailable')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return b''.join(chunks)


def source_identity(value, base):
    require(isinstance(value, dict) and set(value) == {'repo', 'commit', 'subdir'}, 'invalid_source')
    commit, subdir = value['commit'], value['subdir']
    require(isinstance(commit, str) and OBJECT_ID.fullmatch(commit), 'full_source_commit_required')
    require(isinstance(subdir, str) and subdir and not subdir.startswith('/')
            and '..' not in PurePosixPath(subdir).parts and '\\' not in subdir
            and (subdir == '.' or PurePosixPath(subdir).as_posix() == subdir), 'invalid_source_subdir')
    repo = path_from(value['repo'], base)
    with directory(repo):
        pass
    actual = git(repo, 'rev-parse', '--verify', commit + '^{commit}').decode().strip()
    require(actual == commit, 'source_commit_mismatch')
    tree = git(repo, 'rev-parse', '--verify', commit + ('^{tree}' if subdir == '.' else ':' + subdir)).decode().strip()
    require(OBJECT_ID.fullmatch(tree) and git(repo, 'cat-file', '-t', tree).strip() == b'tree', 'source_tree_required')
    return {'commit': commit, 'subdir': subdir, 'tree': tree}, repo


def installed_files(path):
    files, total = {}, 0

    def walk(parent, prefix='', depth=0):
        nonlocal total
        require(depth <= 32, 'installed_tree_too_deep')
        before = os.fstat(parent)
        names = sorted(os.listdir(parent))
        for name in names:
            require(name not in ('.', '..') and '\\' not in name
                    and all(ord(c) >= 32 for c in name), 'invalid_installed_path')
            relative = prefix + name
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(not stat.S_ISLNK(info.st_mode), 'installed_symlink_refused')
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    walk(child, relative + '/', depth + 1)
                finally:
                    os.close(child)
            else:
                require(stat.S_ISREG(info.st_mode) and len(files) < MAX_TREE_FILES, 'invalid_installed_file')
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    opened = os.fstat(child)
                    require((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                            == (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns),
                            'installed_tree_changed')
                    files[relative], _ = read_descriptor(child, limit=MAX_FILE)
                    after_file = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    require((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                            == (after_file.st_dev, after_file.st_ino, after_file.st_size,
                                after_file.st_mtime_ns, after_file.st_ctime_ns), 'installed_tree_changed')
                finally:
                    os.close(child)
                total += files[relative]['bytes']
                require(total <= MAX_TREE_BYTES, 'installed_tree_too_large')
        after = os.fstat(parent)
        require(names == sorted(os.listdir(parent))
                and (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns), 'installed_tree_changed')

    with directory(path) as root:
        walk(root)
    require(files, 'empty_installed_tree')
    return files


def compare_installed(files, repo, tree):
    expected = {}
    total = 0
    for row in git(repo, 'ls-tree', '-r', '-z', tree).split(b'\0'):
        if not row:
            continue
        metadata, name = row.split(b'\t', 1)
        mode, kind, object_id = metadata.decode('ascii').split()
        require(mode in ('100644', '100755') and kind == 'blob', 'source_symlink_or_submodule_refused')
        path = name.decode('utf-8')
        require(path in files, 'installed_tree_does_not_match_source')
        size = int(git(repo, 'cat-file', '-s', object_id))
        total += size
        require(size <= MAX_FILE and len(expected) < MAX_TREE_FILES
                and total <= MAX_TREE_BYTES, 'source_tree_too_large')
        require(size == files[path]['bytes'], 'installed_tree_does_not_match_source')
        raw = git(repo, 'cat-file', 'blob', object_id, limit=MAX_FILE)
        expected[path] = {'sha256': sha256(raw), 'bytes': len(raw)}
    require(files == expected, 'installed_tree_does_not_match_source')


def image_identity(image, base):
    require(isinstance(image, dict) and set(image) == {'expected', 'observation'}, 'invalid_image')
    expected, observed = image['expected'], json_input(image['observation'], base)
    require(isinstance(expected, str) and isinstance(observed, str)
            and len(expected) <= 1024 and len(observed) <= 1024, 'immutable_image_required')
    wanted, actual = IMAGE.fullmatch(expected), IMAGE.fullmatch(observed)
    require(wanted and actual and wanted[1] == actual[1], 'image_digest_mismatch')
    return {'reference': expected, 'digest': 'sha256:' + actual[1]}


def input_identity(entry, base):
    require(isinstance(entry, dict) and set(entry) in ({'file', 'kind'}, {'fingerprint', 'kind'})
            and entry['kind'] in ('config', 'secret', 'model', 'other'), 'invalid_input')
    if 'file' in entry:
        digest, _ = read_file(path_from(entry['file'], base))
        return {'kind': entry['kind'], 'mode': 'local_raw_file', **digest}
    digest = json_input(entry['fingerprint'], base)
    require(isinstance(digest, dict) and set(digest) == {'sha256', 'bytes'}
            and isinstance(digest['sha256'], str) and DIGEST.fullmatch(digest['sha256'])
            and type(digest['bytes']) is int and 0 <= digest['bytes'] <= MAX_TREE_BYTES,
            'invalid_reported_fingerprint')
    return {'kind': entry['kind'], 'mode': 'collector_reported_raw_file', **digest}


def snapshot(spec, base):
    require(isinstance(spec, dict) and set(spec) == {
        'schema_version', 'scope', 'excluded_scope', 'required_components', 'components'}, 'invalid_spec')
    require(type(spec['schema_version']) is int and spec['schema_version'] == 1, 'invalid_spec_version')
    require(isinstance(spec['scope'], str) and LABEL.fullmatch(spec['scope']), 'invalid_scope')
    excluded = spec['excluded_scope']
    require(isinstance(excluded, list) and 1 <= len(excluded) <= 32
            and all(isinstance(item, str) and LABEL.fullmatch(item) for item in excluded)
            and len(set(excluded)) == len(excluded), 'explicit_excluded_scope_required')
    components, required = spec['components'], spec['required_components']
    require(isinstance(components, dict) and 1 <= len(components) <= 16
            and all(LABEL.fullmatch(name) for name in components), 'invalid_components')
    require(isinstance(required, list) and required and all(isinstance(name, str) for name in required)
            and len(required) == len(set(required)) and set(required) == set(components), 'missing_or_extra_components')
    result = {}
    for name, item in sorted(components.items()):
        require(isinstance(item, dict) and {'role', 'inputs'} <= set(item)
                and set(item) <= {'role', 'inputs', 'source', 'image', 'controller', 'installed_tree'},
                'invalid_component')
        role = item['role']
        if role == 'source':
            require('source' in item and ('installed_tree' in item or 'image' in item), 'invalid_source_component')
            identity, repo = source_identity(item['source'], base)
            frozen = {'role': role, 'source': identity}
        else:
            require(role == 'runtime_dependency'
                    and set(item) == {'role', 'inputs', 'image', 'controller'}, 'invalid_runtime_dependency')
            frozen = {'role': role}
        require(('image' in item) == ('controller' in item), 'image_controller_pair_required')
        if 'image' in item:
            frozen['image'] = image_identity(item['image'], base)
            controller = json_input(item['controller'], base)
            require(isinstance(controller, dict) and controller, 'controller_spec_required')
            frozen['controller'] = {'pointer': item['controller']['pointer'], 'sha256': sha256(canonical(controller))}
        inputs = item['inputs']
        require(isinstance(inputs, dict) and 1 <= len(inputs) <= 64
                and all(LABEL.fullmatch(key) for key in inputs), 'declared_input_bytes_required')
        frozen_inputs = {}
        for label, entry in sorted(inputs.items()):
            frozen_inputs[label] = input_identity(entry, base)
        frozen['inputs'] = frozen_inputs
        if 'installed_tree' in item:
            files = installed_files(path_from(item['installed_tree'], base))
            compare_installed(files, repo, identity['tree'])
            frozen['installed_tree'] = {'sha256': sha256(canonical(files)), 'files': files}
        result[name] = frozen
    require(any('installed_tree' in item for item in result.values()), 'installed_component_tree_required')
    sources = [item for item in result.values() if item['role'] == 'source']
    require(len(sources) >= 2 and any('image' in item for item in sources), 'source_components_required')
    return {'schema': 'cisco-media-candidate/v1', 'acceptance': False, 'scope': spec['scope'],
            'excluded_scope': sorted(excluded), 'hash_rules': HASH_RULES, 'components': result}


def run(action, spec_path, manifest_path, expected_sha256=None):
    spec_path = path_from(str(spec_path), Path.cwd())
    manifest_path = path_from(str(manifest_path), Path.cwd())
    frozen = snapshot(read_json(spec_path), spec_path.parent)
    raw = canonical(frozen) + b'\n'
    if action == 'freeze':
        require(expected_sha256 is None, 'freeze_cannot_require_manifest_digest')
        with directory(manifest_path.parent) as parent:
            descriptor = os.open(manifest_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        status = 'candidate_frozen'
    else:
        require(action == 'verify', 'invalid_action')
        _, previous = read_file(manifest_path, limit=MAX_JSON, keep_bytes=True)
        if expected_sha256 is not None:
            require(isinstance(expected_sha256, str) and DIGEST.fullmatch(expected_sha256)
                    and sha256(previous) == expected_sha256, 'manifest_digest_mismatch')
        require(previous == raw, 'candidate_drift')
        status = 'candidate_unchanged'
    return {'status': status, 'acceptance': False, 'manifest_sha256': sha256(raw),
            'scope': frozen['scope'], 'components': len(frozen['components'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'verify'))
    parser.add_argument('--spec', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args(argv)
    try:
        result = run(args.action, args.spec, args.manifest, args.manifest_sha256)
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError) as error:
        reason = str(error) if isinstance(error, Refused) else 'local_input_or_output_unavailable'
        print(json.dumps({'status': 'refused', 'acceptance': False, 'reason': reason}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
