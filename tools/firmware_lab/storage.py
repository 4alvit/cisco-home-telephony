"""Small filesystem boundary for private, non-executable inspection artifacts."""

import os
from pathlib import Path
import stat


REPOSITORY = Path(__file__).resolve().parents[2]


def read_regular(path, limit=64 * 1024 * 1024):
    """Read one bounded regular file, rejecting links and special files."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("input symlinks are not supported")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("input must be a regular file")
        if before.st_size > limit:
            raise ValueError("input exceeds byte limit")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if len(data) > limit:
        raise ValueError("input exceeds byte limit")
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise ValueError("input changed during read")
    return data


def create_private_output(path, repository=REPOSITORY):
    """Create a fresh output directory below this checkout's literal .local/."""
    repository = Path(os.path.abspath(repository))
    path = Path(os.path.abspath(path))
    private = repository / ".local"
    try:
        relative = path.relative_to(private)
    except ValueError:
        raise ValueError("output must be below repository .local/") from None
    if not relative.parts:
        raise ValueError("output must be a new subdirectory of .local/")
    private = repository.resolve() / ".local"
    path = private / relative
    current = private
    for part in relative.parts[:-1]:
        if current.is_symlink():
            raise ValueError("output parent symlinks are not supported")
        current.mkdir(mode=0o700, exist_ok=True)
        current = current / part
    if current.is_symlink():
        raise ValueError("output parent symlinks are not supported")
    current.mkdir(mode=0o700, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ValueError("output already exists; choose a fresh directory")
    path.mkdir(mode=0o700)
    return path


def write_private(directory, name, payload):
    """Write one generated flat filename once, with no executable mode bits."""
    if Path(name).name != name or name in (".", ".."):
        raise ValueError("artifact filename must be flat")
    destination = Path(directory) / name
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
    return name
