#!/usr/bin/env python3
"""Render a private, one-account Linphone provisioning file without contacting a server."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import xml.etree.ElementTree as ET

import softphones

NAMESPACE = 'http://www.linphone.org/xsds/lpconfig.xsd'
MAX_JSON_BYTES = 1024 * 1024


def read_json(path, *, private=False):
    """Read a bounded regular file; never include its contents in an error."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('input must be a regular file, not a symlink')
        if private and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
            raise ValueError('secrets must be owned by the current user with mode 0600')
        raw = source.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('input JSON is too large')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('input must contain valid JSON') from None


def render(site, saved_secrets, extension):
    """Reuse the declared transport and existing secret; do not invent or rotate an account."""
    if not isinstance(site, dict):
        raise ValueError('site must be a JSON object')
    clients = softphones.validate(site)
    client = next((item for item in clients if item['extension'] == extension), None)
    if client is None:
        raise ValueError('extension is not a declared softphone')
    value = site.get('node_ipv4')
    try:
        address = ipaddress.IPv4Address(value) if isinstance(value, str) else None
    except ipaddress.AddressValueError:
        address = None
    if address is None or not any(address in network for network in softphones.PRIVATE):
        raise ValueError('node_ipv4 must be a private IPv4 literal')
    if not isinstance(saved_secrets, dict) or any(
        not isinstance(key, str) or not re.fullmatch(r'2[0-9]{2}', key)
        or not isinstance(secret, str) or not re.fullmatch(softphones.SECRET_PATTERN, secret)
        for key, secret in saved_secrets.items()
    ):
        raise ValueError('invalid softphone secrets')
    if extension not in saved_secrets:
        raise ValueError('no existing secret for the declared softphone')
    if len(set(saved_secrets.values())) != len(saved_secrets):
        raise ValueError('softphone secrets must be distinct')
    sections = {
        'proxy_0': {
            'reg_identity': f'"{client["label"]}" <sip:{extension}@{address}>',
            'reg_proxy': f'sip:{address};transport={client["transport"]}',
            'reg_sendregister': '1',
            'reg_expires': '300',
        },
        'auth_info_0': {
            'username': extension,
            'domain': str(address),
            'passwd': saved_secrets[extension],
        },
        'sip': {'default_proxy': '0', 'ipv6': '0'},
        'misc': {'transient_provisioning': '1'},
    }
    ET.register_namespace('', NAMESPACE)
    root = ET.Element(f'{{{NAMESPACE}}}config')
    for name, entries in sections.items():
        section = ET.SubElement(root, f'{{{NAMESPACE}}}section', name=name)
        for key, value in entries.items():
            ET.SubElement(section, f'{{{NAMESPACE}}}entry', name=key, overwrite='true').text = value
    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'


def write_private(path, content):
    """Create once inside a private directory; refuse replacement and symlink outputs."""
    path = Path(path).absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory_fd)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise ValueError('output directory must be owned by the current user with mode 0700')
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, 'wb') as destination:
                os.fchmod(destination.fileno(), 0o600)
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            os.unlink(path.name, dir_fd=directory_fd)
            raise
    finally:
        os.close(directory_fd)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True, type=Path)
    parser.add_argument('--secrets', required=True, type=Path,
                        help='existing softphone-secrets.json (0600); never generated or modified')
    parser.add_argument('--extension', required=True)
    parser.add_argument('--output', required=True, type=Path,
                        help='new XML file inside a private 0700 directory')
    args = parser.parse_args(argv)
    try:
        content = render(read_json(args.site), read_json(args.secrets, private=True), args.extension)
        output = write_private(args.output, content)
    except ValueError as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    except OSError:
        print('Error: cannot read private inputs or create a new private output file', file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
