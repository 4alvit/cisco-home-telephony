#!/usr/bin/env python3
"""Build source-bound CLI and announcement service archives without private files."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
CLI = tuple('scripts/' + name for name in (
    'cisco.py', 'cisco_http.py', 'cisco_player.py', 'cisco_library.py',
    'cisco_media_source.py', 'cisco_lifesize.py', 'cisco_announce.py'))
SERVICE = tuple('containers/asterisk/' + name for name in (
    'phone_announce.py', 'phone_announce_agi.py', 'dial_audio.py', 'phone_gateway.py'))
SHARED = ('docs/cli-announcements.md', 'docs/cli-installation.md',
          'docs/cli-lifesize.md', 'docs/cli.md', 'docs/cli-music.md',
          'docs/existing-endpoints.md', 'docs/phone-gateway.md',
          'docs/diagrams/cli-announcements.svg', 'docs/diagrams/cli-setup-flow.svg',
          'docs/diagrams/cli-routing.svg', 'docs/diagrams/cli-music.svg',
          'docs/diagrams/existing-endpoints.svg',
          'deploy/cisco-cli.example.json', 'deploy/cisco-cli.kubectl.example.json',
          'deploy/cisco-cli.lifesize.example.json', 'deploy/cisco-cli.music.example.json',
          'deploy/cisco-cli.schedule.example.json',
          'deploy/cisco-cli.announcements.example.json', 'RELEASING.md')
RUNTIME = SERVICE + ('scripts/render_announcements.py', 'scripts/package-announcements.py',
    'deploy/announcement-dialplan.conf', 'deploy/announcements.example.json',
    'deploy/dial-audio.example.json', 'docs/dial-audio-services.md') + SHARED


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def archive(path, files, manifest):
    """Stable archive metadata and an explicit path allowlist prevent workspace leaks."""
    with path.open('xb') as output, gzip.GzipFile(fileobj=output, mode='wb', mtime=0,
                                               filename='') as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as tar:
            for name, data in sorted({**files, 'SOURCE.json': manifest}.items()):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                item.mode = 0o755 if name.startswith('scripts/') else 0o644
                item.mtime = 0
                tar.addfile(item, io.BytesIO(data))


def source_manifest(revision, files, scope):
    return json.dumps({'schema': 1, 'source_commit': revision, 'scope': scope,
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}},
        indent=2, sort_keys=True).encode() + b'\n'


def build(revision, destination):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Use a full 40-character source commit, not a moving branch or tag.')
    if git('rev-parse', revision + '^{commit}').decode().strip() != revision:
        raise ValueError('Source commit mismatch.')
    # Read committed blobs only. Untracked inventories and local edits cannot enter an asset.
    names = sorted(set(CLI + RUNTIME))
    tree = {}
    for entry in git('ls-tree', '-r', '-z', revision, '--', *names).split(b'\0'):
        if entry:
            metadata, name = entry.split(b'\t', 1)
            mode, kind, _object = metadata.split(b' ', 2)
            tree[name.decode()] = (mode, kind)
    if set(tree) != set(names) or any(mode not in (b'100644', b'100755') or kind != b'blob'
                                    for mode, kind in tree.values()):
        raise ValueError('Every allowlisted source must be a committed regular file.')
    files = {name: git('show', revision + ':' + name) for name in names}
    for name, data in files.items():
        if name.endswith('.py'):
            compile(data, name, 'exec')
    destination.mkdir(parents=True, exist_ok=False)
    manifest = source_manifest(revision, files,
        'CLI and announcement service source; device adapters are separate releases')
    (destination / 'SOURCE.json').write_bytes(manifest)
    for label, entries in (('cisco-cli', CLI + SHARED), ('announcement-service', RUNTIME)):
        selected = {n: files[n] for n in entries}
        bundle_manifest = source_manifest(revision, selected,
            label + '; device adapters are separate releases')
        archive(destination / (label + '.tar.gz'), selected, bundle_manifest)
    artifacts = sorted(p for p in destination.iterdir() if p.is_file())
    (destination / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in artifacts))
    return revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print('Packaged committed announcement sources: ' + build(args.commit, args.output))


if __name__ == '__main__':
    main()
