#!/usr/bin/env python3
"""Restore explicitly approved node-local OCI images, without network or restarts.

Install as /usr/local/sbin/telephony-image-cache, owned by root. The default
check reads runtime metadata, content readiness and CRI image status.
--verify-archives also hashes/validates archives. --apply imports missing or
unusable configured images
and pins their exact references. It never changes a wrong existing image target.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import time


ROOT_UID = 0
MAX_ARCHIVE = 4 * 1024 ** 3
MAX_JSON = 1024 ** 2
MAX_MEMBERS = 1024
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REFERENCE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
BLOB = re.compile(r"blobs/sha256/[0-9a-f]{64}\Z")
PIN = "io.cri-containerd.pinned"
OWNER = "io.cisco-home-telephony.image-cache"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
MANIFEST_TYPES = {MANIFEST, 'application/vnd.docker.distribution.manifest.v2+json'}
CONFIG_TYPES = {CONFIG, 'application/vnd.docker.container.image.v1+json'}
LAYER_TYPES = {"application/vnd.oci.image.layer.v1.tar",
               "application/vnd.oci.image.layer.v1.tar+gzip",
               "application/vnd.oci.image.layer.v1.tar+zstd",
               "application/vnd.docker.image.rootfs.diff.tar.gzip"}
LOCK_DIRECTORY = Path('/run/cisco-telephony-image-cache')


class Refused(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def parse_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'duplicate_json_key')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, TypeError):
        raise Refused('invalid_json') from None


def trusted_path(path, *, directory=False):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'absolute_path_required')
    for parent in reversed(path.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == ROOT_UID
                and not info.st_mode & 0o022, 'untrusted_parent_directory')
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    require(kind(info.st_mode) and info.st_uid == ROOT_UID
            and not info.st_mode & 0o022, 'untrusted_file')
    if not directory:
        require(info.st_nlink == 1, 'hardlinked_file')
    return path


def open_trusted(path, maximum):
    path = trusted_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    named = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID
            or info.st_mode & 0o022 or info.st_nlink != 1
            or info.st_size > maximum
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
        os.close(fd)
        raise Refused('trusted_file_changed_or_oversized')
    return os.fdopen(fd, 'rb')


def load_config(path):
    with open_trusted(path, MAX_JSON) as stream:
        value = parse_json(stream.read(MAX_JSON + 1))
    require(isinstance(value, dict) and set(value) == {'version', 'platform', 'archives'}
            and type(value['version']) is int and value['version'] == 1,
            'invalid_configuration')
    require(value['platform'] in ('linux/amd64', 'linux/arm64'), 'invalid_platform')
    require(isinstance(value['archives'], list) and 1 <= len(value['archives']) <= 8,
            'invalid_archives')
    paths, refs = set(), set()
    for archive in value['archives']:
        require(isinstance(archive, dict) and set(archive) == {'path', 'sha256', 'images'},
                'invalid_archive_entry')
        require(isinstance(archive['path'], str) and archive['path'].endswith('.tar'),
                'uncompressed_oci_tar_required')
        require(archive['path'] not in paths, 'duplicate_archive')
        paths.add(archive['path'])
        trusted_path(archive['path'])
        require(isinstance(archive['sha256'], str)
                and re.fullmatch('[0-9a-f]{64}', archive['sha256']), 'invalid_archive_hash')
        images = archive['images']
        require(isinstance(images, list) and 1 <= len(images) <= 32, 'invalid_images')
        for ref in images:
            require(isinstance(ref, str) and REFERENCE.fullmatch(ref)
                    and '/' in ref.split('@')[0] and '..' not in ref, 'invalid_image_reference')
            require(ref not in refs, 'duplicate_image_reference')
            refs.add(ref)
    require(len(refs) <= 32, 'too_many_images')
    return value


def fingerprint(stream):
    info = os.fstat(stream.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def verify_archive(stream, archive, platform):
    """Check the entire bounded OCI graph; retain this open inode for import."""
    before = fingerprint(stream)
    require(0 < before[2] <= MAX_ARCHIVE, 'archive_size')
    stream.seek(0)
    checksum = hashlib.sha256()
    while chunk := stream.read(1024 ** 2):
        checksum.update(chunk)
    require(checksum.hexdigest() == archive['sha256'], 'archive_checksum_mismatch')
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode='r:') as tar:
        members = {}
        directories = set()
        for member in tar:
            require(len(members) < MAX_MEMBERS, 'too_many_archive_members')
            name = member.name
            if member.isdir():
                require(name in ('blobs', 'blobs/', 'blobs/sha256', 'blobs/sha256/')
                        and member.size == 0, 'unexpected_archive_directory')
                name = name.rstrip('/')
                directories.add(name)
            require(name not in members, 'duplicate_archive_member')
            require(member.isdir() or (member.isfile() and
                    (name in ('index.json', 'oci-layout') or BLOB.fullmatch(name))),
                    'unexpected_archive_member')
            require(0 <= member.size <= MAX_ARCHIVE, 'archive_member_size')
            members[name] = member

        def read_json(name):
            require(name in members and members[name].size <= MAX_JSON, 'missing_or_large_oci_json')
            return parse_json(tar.extractfile(members[name]).read(MAX_JSON + 1))

        require(read_json('oci-layout') == {'imageLayoutVersion': '1.0.0'}, 'invalid_oci_layout')
        index = read_json('index.json')
        require(isinstance(index, dict) and index.get('schemaVersion') == 2
                and isinstance(index.get('manifests'), list), 'invalid_oci_index')
        expected = set(archive['images'])
        found, referenced = set(), {'index.json', 'oci-layout'}
        verified = set()

        def descriptor(item, media_types):
            require(isinstance(item, dict) and item.get('mediaType') in media_types
                    and isinstance(item.get('digest'), str) and DIGEST.fullmatch(item['digest'])
                    and type(item.get('size')) is int and item['size'] >= 0
                    and not item.get('urls'), 'invalid_oci_descriptor')
            name = 'blobs/sha256/' + item['digest'][7:]
            require(name in members and members[name].size == item['size'], 'missing_oci_blob')
            referenced.add(name)
            if name not in verified:
                content_hash = hashlib.sha256()
                with tar.extractfile(members[name]) as blob:
                    while chunk := blob.read(1024 ** 2):
                        content_hash.update(chunk)
                require(content_hash.hexdigest() == item['digest'][7:], 'oci_blob_checksum_mismatch')
                verified.add(name)
            return name

        for item in index['manifests']:
            name = descriptor(item, MANIFEST_TYPES)
            annotations = item.get('annotations', {})
            require(isinstance(annotations, dict), 'invalid_oci_annotations')
            ref = annotations.get('io.containerd.image.name')
            require(ref in expected and ref not in found, 'unexpected_oci_image')
            # ctr export records the full name above and an @digest suffix here.
            # No unrelated alias is accepted; native import honors the full name.
            require(annotations.get('org.opencontainers.image.ref.name', ref)
                    in (ref, '@' + item['digest']),
                    'ambiguous_oci_image_name')
            require(ref.rsplit('@', 1)[1] == item['digest'], 'oci_image_target_mismatch')
            found.add(ref)
            manifest = read_json(name)
            require(isinstance(manifest, dict) and manifest.get('schemaVersion') == 2
                    and isinstance(manifest.get('layers'), list), 'invalid_oci_manifest')
            config = read_json(descriptor(manifest.get('config'), CONFIG_TYPES))
            require(isinstance(config, dict)
                    and (config.get('os'), config.get('architecture')) == tuple(platform.split('/')),
                    'oci_platform_mismatch')
            for layer in manifest['layers']:
                descriptor(layer, LAYER_TYPES)
        require(found == expected, 'oci_image_set_mismatch')
        require(set(members) - directories == referenced, 'unreferenced_oci_blob')
    require(fingerprint(stream) == before, 'archive_changed_during_verification')
    return before


class Runtime:
    def __init__(self):
        self.deadline = time.monotonic() + 270

    def command(self, args, *, pass_fds=(), family='ctr', allow_failure=False):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'runtime_deadline')
        with tempfile.TemporaryFile() as output:
            try:
                require(family in ('ctr', 'crictl'), 'unsupported_runtime_command')
                prefix = (['ctr', '--namespace', 'k8s.io'] if family == 'ctr' else ['crictl'])
                result = subprocess.run(['/usr/local/bin/k3s', *prefix, *args],
                                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                        timeout=min(90, remaining), check=False, pass_fds=pass_fds)
            except subprocess.TimeoutExpired:
                raise Refused('runtime_command_timeout') from None
            if result.returncode and allow_failure:
                return None
            require(result.returncode == 0, 'runtime_command_failed')
            require(output.tell() <= 2 * MAX_JSON, 'runtime_output_too_large')
            output.seek(0)
            return output.read().decode('utf-8')

    def snapshot(self):
        return parse_images(self.command(['images', 'list']))

    def unusable(self, config, snapshot):
        """Check selected images only; never pull, unpack or hash their layers.

        ctr metadata alone can survive missing content or a broken CRI alias.
        `images check --quiet` reports fully downloaded and unpacked images.
        CRI's selected config and advertised references must also resolve.
        """
        text = self.command(['images', 'check', '--quiet'])
        ready = set(text.splitlines())
        require(all(ref and not any(c.isspace() for c in ref) for ref in ready),
                'invalid_content_readiness')
        failures, configs = {}, {}
        for archive in config['archives']:
            for ref in archive['images']:
                if ref not in snapshot:
                    continue
                if ref not in ready:
                    failures[ref] = 'content_incomplete_or_not_unpacked'
                    continue
                digest = ref.rsplit('@', 1)[1]
                if digest not in configs:
                    raw = self.command(['content', 'get', digest], allow_failure=True)
                    if raw is None:
                        failures[ref] = 'manifest_unavailable'
                        continue
                    manifest = parse_json(raw)
                    descriptor = manifest.get('config', {}) if isinstance(manifest, dict) else {}
                    require(isinstance(descriptor, dict)
                            and isinstance(descriptor.get('digest'), str)
                            and DIGEST.fullmatch(descriptor['digest']), 'invalid_runtime_manifest')
                    configs[digest] = descriptor['digest']
                raw = self.command(['inspecti', '--quiet', '--output', 'json', ref],
                                   family='crictl', allow_failure=True)
                if raw is None:
                    failures[ref] = 'cri_image_unavailable'
                    continue
                value = parse_json(raw)
                status = value.get('status') if isinstance(value, dict) else None
                if not isinstance(status, dict):
                    failures[ref] = 'cri_image_unavailable'
                    continue
                if status.get('id') != configs[digest]:
                    failures[ref] = 'cri_config_mismatch'
                    continue
                aliases = status.get('repoDigests', [])
                require(isinstance(aliases, list)
                        and all(isinstance(alias, str) and REFERENCE.fullmatch(alias)
                                for alias in aliases), 'invalid_cri_image_aliases')
                if normalized_reference(ref) not in {normalized_reference(alias) for alias in aliases}:
                    failures[ref] = 'cri_reference_mismatch'
                    continue
                # CRI may choose any advertised repository digest when creating
                # a container. Only this selected image's aliases are examined;
                # unrelated runtime images need not belong to our allowlist.
                for alias in aliases:
                    current = snapshot.get(normalized_reference(alias))
                    if current is None or current['digest'] != alias.rsplit('@', 1)[1]:
                        failures[ref] = 'cri_alias_unavailable'
                        break
        return failures

    def restore(self, stream, platform):
        self.command(['images', 'import', '--platform', platform,
                      '/proc/self/fd/' + str(stream.fileno())], pass_fds=(stream.fileno(),))

    def pin(self, ref):
        self.command(['images', 'label', ref, PIN + '=pinned', OWNER + '=v1'])


def normalized_reference(ref):
    """Docker/CRI default registry names, without changing the image digest."""
    name, digest = ref.rsplit('@', 1)
    pieces = name.split('/')
    if len(pieces) == 1 or not ('.' in pieces[0] or ':' in pieces[0] or pieces[0] == 'localhost'):
        pieces.insert(0, 'docker.io')
    if pieces[0] == 'index.docker.io':
        pieces[0] = 'docker.io'
    if pieces[0] == 'docker.io' and len(pieces) == 2:
        pieces.insert(1, 'library')
    return '/'.join(pieces) + '@' + digest


def parse_images(text):
    rows = text.splitlines()
    require(rows and rows[0].split() == ['REF', 'TYPE', 'DIGEST', 'SIZE', 'PLATFORMS', 'LABELS'],
            'unrecognized_image_table')
    result = {}
    for row in rows[1:]:
        columns = row.split()
        require(len(columns) >= 6 and DIGEST.fullmatch(columns[2]), 'invalid_image_table_row')
        ref, labels = columns[0], columns[-1]
        require(ref not in result, 'duplicate_runtime_reference')
        pairs = labels.split(',') if labels != '-' else []
        require(all('=' in entry for entry in pairs), 'invalid_image_labels')
        result[ref] = {'digest': columns[2], 'labels': dict(entry.split('=', 1) for entry in pairs)}
    return result


def inspect(config, snapshot):
    missing, unpinned = [], []
    for archive in config['archives']:
        for ref in archive['images']:
            current = snapshot.get(ref)
            if current is None:
                missing.append(ref)
                continue
            require(current['digest'] == ref.rsplit('@', 1)[1], 'existing_image_target_mismatch')
            label = current['labels'].get(OWNER)
            require(label in (None, 'v1'), 'conflicting_image_owner')
            if current['labels'].get(PIN) != 'pinned' or label != 'v1':
                unpinned.append(ref)
    return missing, unpinned


@contextlib.contextmanager
def exclusive_lock():
    LOCK_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
    trusted_path(LOCK_DIRECTORY, directory=True)
    fd = os.open(LOCK_DIRECTORY / 'owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, 'untrusted_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('recovery_already_running') from None
        yield
    finally:
        os.close(fd)


def reconcile(config, runtime, *, apply=False, verify=False):
    def observe():
        snapshot = runtime.snapshot()
        missing, unpinned = inspect(config, snapshot)
        present = any(ref in snapshot for archive in config['archives'] for ref in archive['images'])
        unusable = runtime.unusable(config, snapshot) if present else {}
        return missing, unpinned, unusable

    missing, unpinned, unusable = observe()
    restored = 0
    with contextlib.ExitStack() as stack:
        approved = []
        for archive in config['archives']:
            needed = bool(set(archive['images']) & (set(missing) | set(unusable)))
            if verify or (apply and needed):
                stream = stack.enter_context(open_trusted(archive['path'], MAX_ARCHIVE))
                identity = verify_archive(stream, archive, config['platform'])
                if apply and needed:
                    approved.append((archive, stream, identity))
        for archive, stream, identity in approved:
            latest_missing, _, latest_unusable = observe()
            if not set(archive['images']) & (set(latest_missing) | set(latest_unusable)):
                continue
            require(fingerprint(stream) == identity, 'archive_changed_before_import')
            runtime.restore(stream, config['platform'])
            after_missing, _, after_unusable = observe()
            require(not set(archive['images']) & set(after_missing), 'import_did_not_restore_images')
            require(not set(archive['images']) & set(after_unusable), 'import_did_not_restore_image_usability')
            restored += 1
        if apply:
            missing, unpinned, unusable = observe()
            require(not missing, 'images_still_missing')
            require(not unusable, 'images_still_unusable')
            for ref in unpinned:
                inspect(config, runtime.snapshot())
                runtime.pin(ref)
            missing, unpinned, unusable = observe()
    return {'status': 'ready' if not missing and not unpinned and not unusable else 'unavailable',
            'missing': missing, 'unpinned': unpinned, 'unusable': unusable, 'archives_imported': restored,
            'archives_verified': bool(verify or approved)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/cisco-telephony-image-cache.json')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Read-only availability check (default).')
    mode.add_argument('--apply', action='store_true', help='Restore missing images and pin approved references.')
    parser.add_argument('--verify-archives', action='store_true', help='Also hash and validate all approved OCI archives.')
    args = parser.parse_args(argv)
    try:
        require(os.geteuid() == ROOT_UID, 'root_required')
        config = load_config(args.config)
        with exclusive_lock() if args.apply else contextlib.nullcontext():
            report = reconcile(config, Runtime(), apply=args.apply, verify=args.verify_archives)
        print(json.dumps(report, sort_keys=True))
        return 0 if report['status'] == 'ready' else 1
    except (Refused, OSError, tarfile.TarError, UnicodeError, TypeError, KeyError) as error:
        # Error text from subprocesses/config files can contain private paths.
        reason = str(error) if isinstance(error, Refused) else 'invalid_input_or_runtime_unavailable'
        print(json.dumps({'status': 'refused', 'reason': reason}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
