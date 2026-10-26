"""Bounded, metadata-only selection of music mirrored into HA media sources."""
import os
from fnmatch import fnmatchcase
from pathlib import Path
import re
import stat
from urllib.parse import quote, unquote, urlsplit


MAX_DEPTH = 4
MAX_SCAN_ENTRIES = 20_000
EXTENSIONS = {'.mp3', '.flac'}


class LibraryError(ValueError):
    """Fixed public reason, without a private path or underlying exception."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _require(condition, reason='invalid_music_config'):
    if not condition:
        raise LibraryError(reason)


def _text(value):
    return (isinstance(value, str) and 0 < len(value) <= 4096
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def validate_config(value):
    """Validate private settings without opening or requiring the library."""
    _require(isinstance(value, dict) and
             {'directory', 'media_source_prefix'} <= set(value) <=
             {'directory', 'media_source_prefix', 'max_matches'})
    directory, prefix = value['directory'], value['media_source_prefix']
    _require(_text(directory) and _text(prefix))
    limit = value.get('max_matches', 50)
    _require(type(limit) is int and 1 <= limit <= 50)
    try:
        directory = str(Path(directory).expanduser())
        _require(Path(directory).is_absolute())
        parsed = urlsplit(prefix)
        _require(parsed.scheme == 'media-source' and parsed.netloc == 'media_source'
                 and not parsed.query and not parsed.fragment and parsed.path.startswith('/'))
        _require(not re.search(r'%(?![0-9a-fA-F]{2})|%2f|%5c', parsed.path, re.I))
        path = unquote(parsed.path, errors='strict').rstrip('/')
        _require(path and _text(path) and '\\' not in path
                 and not any(part in ('.', '..') for part in path.split('/')))
        prefix = 'media-source://media_source' + quote(path, safe='/')
    except (ValueError, RuntimeError, UnicodeError):
        raise LibraryError('invalid_music_config') from None
    return {'directory': directory, 'media_source_prefix': prefix, 'max_matches': limit}


def _scan(root):
    """Never open audio files or follow descendant symlinks, including races."""
    tracks, directories, rejected = {}, {''}, {}
    count = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def walk(descriptor, parts, depth):
        nonlocal count
        with os.scandir(descriptor) as iterator:
            entries = []
            for entry in iterator:
                count += 1
                _require(count <= MAX_SCAN_ENTRIES, 'library_scan_limit')
                entries.append(entry)
        for entry in sorted(entries, key=lambda item: (item.name.casefold(), item.name)):
            relative = '/'.join((*parts, entry.name))
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                rejected[relative] = 'library_symlink_not_allowed'
            elif stat.S_ISDIR(info.st_mode):
                if depth == MAX_DEPTH:
                    rejected[relative] = 'library_depth_limit'
                    continue
                child = os.open(entry.name, flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    _require((opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino),
                             'library_changed')
                    directories.add(relative)
                    walk(child, (*parts, entry.name), depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if Path(entry.name).suffix.casefold() in EXTENSIONS:
                    tracks[relative] = info.st_size
            else:
                rejected[relative] = 'library_special_file'

    descriptor = os.open(root, flags)
    try:
        walk(descriptor, (), 0)
    finally:
        os.close(descriptor)
    return tracks, directories, rejected


def select_tracks(config, query):
    """Select exact paths, literal substrings, or case-insensitive * / ? masks.

    Files at directory depths zero through four are eligible. Selection is
    case-sensitive for exact paths and case-insensitive for searches and masks.
    A configured root may be a symlink; descendant links are never traversed.
    OneDrive dataless flags and allocated-block counts are deliberately ignored:
    only names and stat metadata are needed to use the independent NAS mirror.
    """
    config = validate_config(config)
    _require(_text(query) and not query.isspace(), 'invalid_library_query')
    _require('\\' not in query and '..' not in query.split('/'), 'library_path_escape')
    try:
        configured = Path(os.path.abspath(config['directory']))
        root = configured.resolve(strict=True)
        expanded = Path(query).expanduser()
        path_query = expanded.is_absolute() or '/' in query or query == '.'
        if expanded.is_absolute():
            relative = None
            for anchor in (configured, root):
                try:
                    relative = expanded.relative_to(anchor).as_posix()
                    break
                except ValueError:
                    pass
            if relative is None:
                # macOS may spell the same root through /var or /private/var.
                # Stop at the first root ancestor; keep all descendant names
                # intact so a later symlink is still rejected by the scan.
                for anchor in reversed((expanded, *expanded.parents)):
                    try:
                        if anchor.resolve(strict=True) == root:
                            relative = expanded.relative_to(anchor).as_posix()
                            break
                    except OSError:
                        continue
            _require(relative is not None, 'library_path_escape')
        else:
            relative = expanded.as_posix()
        if relative == '.':
            relative = ''
        _require(len(Path(relative).parts) <= MAX_DEPTH + 1, 'library_depth_limit')
        tracks, directories, rejected = _scan(root)
        for excluded, reason in rejected.items():
            if relative == excluded or relative.startswith(excluded + '/'):
                raise LibraryError(reason)
        if relative in tracks:
            selected = [relative]
        elif relative in directories:
            selected = [path for path in tracks if not relative or path.startswith(relative + '/')]
        elif '*' in relative or '?' in relative:
            # Only * and ? are operators. Brackets and regex punctuation stay
            # literal, as they do in ordinary substring searches.
            pattern = relative.casefold().replace('[', '[[]')
            def matches(path):
                return (fnmatchcase(path.casefold(), pattern)
                        or '/' not in relative and fnmatchcase(Path(path).name.casefold(), pattern))
            for excluded, reason in rejected.items():
                if matches(excluded):
                    raise LibraryError(reason)
            selected = [path for path in tracks if matches(path)]
        elif path_query:
            raise LibraryError('library_no_matches')
        else:
            folded = query.casefold()
            for excluded, reason in rejected.items():
                if folded in excluded.casefold():
                    raise LibraryError(reason)
            selected = [path for path in tracks if folded in path.casefold()]
        _require(selected, 'library_no_matches')
        _require(len(selected) <= config['max_matches'], 'library_too_many_matches')
        result = []
        for path in sorted(selected, key=lambda value: (value.casefold(), value)):
            _require(_text(path), 'library_invalid_filename')
            result.append({'relative_path': path, 'size_bytes': tracks[path],
                           'media_content_id': config['media_source_prefix'] + '/' + quote(path, safe='/')})
        return result
    except LibraryError:
        raise
    except UnicodeError:
        raise LibraryError('library_invalid_filename') from None
    except (OSError, RuntimeError, ValueError):
        raise LibraryError('library_unavailable') from None
