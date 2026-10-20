#!/usr/bin/env python3
"""Copy an explicit projected Secret into a fresh, owner-only config volume.

Run as the same UID as the voice process (10001 in the image), before it starts.
This is intentionally not a config updater: any existing destination content
requires a fresh volume. Persistent state belongs in the sibling .local/state.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat


MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024
MARKER = '.voice-stage-in-progress'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.(?:json|txt)\Z')
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class StageError(ValueError):
    """A sanitized staging error; never includes private values or paths."""


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise StageError('absolute_paths_without_traversal_required')
    return path


def _open_directory(path):
    """Walk real directories without following any component symlinks."""
    descriptor = os.open(path.anchor, _DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_source(root, root_fd, path, limit):
    # AtomicWriter's file -> ..data/name -> ..timestamp/name links are allowed,
    # but the resulting file must stay inside the source volume. Open the
    # resolved components relative to the pinned root, rejecting later links.
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise StageError('source_link_escapes_root') from None
    if not relative.parts:
        raise StageError('source_must_be_regular_file')
    directory = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, _DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise StageError('source_not_regular_or_oversized')
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise StageError('source_oversized')
        return data
    finally:
        os.close(directory)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StageError('duplicate_manifest_key')
        result[key] = value
    return result


def _manifest_names(data, manifest_name):
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError):
        raise StageError('invalid_manifest_json') from None
    if (not isinstance(value, dict) or set(value) != {'version', 'files'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['files'], list)
            or not 1 <= len(value['files']) <= MAX_FILES):
        raise StageError('invalid_manifest_shape')
    names = value['files']
    if any(not isinstance(name, str) or not _NAME.fullmatch(name)
           or '..' in name or name == manifest_name for name in names):
        raise StageError('invalid_manifest_filename')
    if len(names) != len(set(names)):
        raise StageError('duplicate_manifest_filename')
    return names


def _remove_owned(directory, name, identity):
    """Rollback only the exact inode that this invocation created."""
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) == identity:
            os.unlink(name, dir_fd=directory)
            return True
    except FileNotFoundError:
        pass
    return False


def stage_private(source, destination, manifest):
    """Return copied file count, or raise StageError without changing old data.

    All arguments are absolute paths. Destination must already be an empty,
    current-UID-owned 0700 real directory whose last components are .local/config.
    Source may contain AtomicWriter links, never links outside its real root.
    """
    source = _absolute(source)
    destination = _absolute(destination)
    manifest = _absolute(manifest)
    if destination.parts[-2:] != ('.local', 'config'):
        raise StageError('destination_must_end_in_local_config')
    try:
        manifest.relative_to(source)
    except ValueError:
        raise StageError('manifest_outside_source') from None
    if source == destination or source in destination.parents or destination in source.parents:
        raise StageError('source_and_destination_must_be_separate')
    source_fd = destination_fd = None
    created = []
    completed = False
    try:
        source_fd = _open_directory(source)
        destination_fd = _open_directory(destination)
        info = os.fstat(destination_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise StageError('destination_requires_owner_and_mode_0700')
        if os.listdir(destination_fd):
            raise StageError('destination_must_be_empty')
        names = _manifest_names(
            _read_source(source, source_fd, manifest, MAX_MANIFEST_BYTES), manifest.name)
        payload = []
        total = 0
        for name in names:
            data = _read_source(source, source_fd, source / name, MAX_FILE_BYTES)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise StageError('source_total_oversized')
            payload.append((name, data))

        # Claim this empty volume before exposing any config files. Another
        # initializer cannot proceed while the marker or completed files exist.
        descriptor = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=destination_fd)
        marker_info = os.fstat(descriptor)
        created.append((MARKER, (marker_info.st_dev, marker_info.st_ino)))
        os.close(descriptor)
        if os.listdir(destination_fd) != [MARKER]:
            raise StageError('destination_changed_during_staging')
        for name, data in payload:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=destination_fd)
            file_info = os.fstat(descriptor)
            created.append((name, (file_info.st_dev, file_info.st_ino)))
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(destination_fd)
        if not _remove_owned(destination_fd, MARKER, created[0][1]):
            raise StageError('destination_changed_during_staging')
        completed = True
        return len(payload)
    except (OSError, RuntimeError) as error:
        raise StageError('private_staging_io_failed') from error
    finally:
        # Any failure (including a
        # partially written file) rolls back only our own inodes, never old data.
        if destination_fd is not None:
            if not completed:
                for name, identity in reversed(created):
                    _remove_owned(destination_fd, name, identity)
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        count = stage_private(args.source, args.destination, args.manifest)
    except StageError as error:
        parser.exit(2, f'{error}\n')
    print(json.dumps({'staged': True, 'files': count}))


if __name__ == '__main__':
    main()
