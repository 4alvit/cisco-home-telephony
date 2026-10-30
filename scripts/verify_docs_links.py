"""Fail on repository-local Markdown links that no longer resolve.

The docs tree is the operator-facing interface: renaming a file or rewording a
heading leaves runbooks pointing at nothing, and no build step notices. This
check resolves every relative link and every fragment against the working
tree. External URLs are skipped because checking them needs network access and
would make the gate depend on third-party availability.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

SKIP_DIRECTORIES = {".git", ".venv", ".local", ".build", "node_modules",
                    "__pycache__"}
LINK = re.compile(r'!?\[[^\]\n]*\]\(\s*(?:<(?P<angle>[^<>\n]*)>|(?P<plain>[^)\s<>]+))'
                  r'(?:\s+(?:"[^"]*"|\'[^\']*\'))?\s*\)')
REFERENCE = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*(?:<(?P<angle>[^<>\n]*)>|(?P<plain>[^\s<>]+))")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_CODE = re.compile(r"(`+)(.+?)\1")
HTML_ANCHOR = re.compile(r'<a\s[^>]*(?:id|name)="([^"]+)"')
HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")


def slug(text):
    """Return the GitHub heading slug for one heading's text."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"<[^>]+>", "", text)
    text = text.lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def prose_lines(path):
    """Yield numbered lines outside fenced code, preserving fence run lengths."""
    fence = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        marker = FENCE.match(line)
        if fence is not None:
            if (marker and marker.group(1)[0] == fence[0]
                    and len(marker.group(1)) >= len(fence)
                    and not marker.group(2).strip(" \t")):
                fence = None
            continue
        if marker and (marker.group(1)[0] != "`" or "`" not in marker.group(2)):
            fence = marker.group(1)
            continue
        yield number, line


def anchors(path):
    """Return every fragment a link may target in one Markdown file."""
    found = set()
    seen = {}
    for _, line in prose_lines(path):
        found.update(HTML_ANCHOR.findall(line))
        heading = HEADING.match(line)
        if heading:
            base = slug(heading.group(1))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def markdown_files(root):
    """Return tracked Markdown files, falling back to a filesystem walk."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, check=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return sorted(path for path in root.rglob("*")
                      if path.is_file() and path.name.lower().endswith(".md")
                      and not SKIP_DIRECTORIES.intersection(path.parts))
    return sorted(root / name for name in result.stdout.split("\0")
                  if name and name.lower().endswith(".md"))


def check(root, files):
    """Return (problems, links checked) for the given Markdown files."""
    problems = []
    links = 0
    known = {}
    for path in files:
        rel = path.relative_to(root)
        for number, line in prose_lines(path):
            masked = INLINE_CODE.sub(lambda match: " " * len(match.group(0)),
                                     line)
            matches = list(LINK.finditer(masked))
            reference = REFERENCE.match(masked)
            if reference:
                matches.append(reference)
            for match in matches:
                target = match.group("angle")
                if target is None:
                    target = match.group("plain")
                links += 1
                problem = resolve(root, path, target, known)
                if problem:
                    problems.append(f"{rel}:{number}: {target!r} {problem}")
    return problems, links


def resolve(root, path, target, known):
    """Return a problem description, or None when the target resolves."""
    split = urlsplit(target)
    if split.scheme or split.netloc or target.startswith("//"):
        return None
    relative = unquote(split.path)
    destination = path if not relative else path.parent / relative
    try:
        destination = destination.resolve()
    except OSError:
        return "is not a usable path"
    if root not in destination.parents and destination != root:
        return "escapes the repository"
    if not destination.exists():
        return "does not exist"
    fragment = unquote(split.fragment)
    if not fragment or destination.is_dir():
        return None
    if not destination.name.lower().endswith(".md"):
        return "uses a fragment on a non-Markdown file"
    if destination not in known:
        known[destination] = anchors(destination)
    if fragment not in known[destination]:
        return "has no matching heading or anchor"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify local Markdown links and anchors.")
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="Markdown files or directories (default: tracked Markdown)")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    files = [path.resolve() for path in args.paths] or markdown_files(root)
    files = [path for entry in files
             for path in ([entry] if entry.is_file() else markdown_files(entry))]
    problems, links = check(root, files)
    print(f"Checked {links} local links in {len(files)} Markdown files.")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
