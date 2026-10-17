#!/usr/bin/env python3
"""Render directory metadata into a new gateway file without rotating credentials.

Only room names and read-only softphone metadata may change. Physical identities,
callbacks, tokens, API access, AMI settings and dial permissions remain unchanged.
"""
import argparse
import copy
import json
import os
from pathlib import Path

from phone_control import write_new
import provision
from render_gateway import gateway


def update(site, inventory, existing):
    phones, _ = provision.validate(site, inventory)
    gateway.validate(existing)
    mapped = {phone['device']: phone for phone in existing['phones']}
    if len(mapped) != len(existing['phones']) or len(phones) != len(mapped):
        raise ValueError('directory sync requires exactly the existing physical phone identities')
    desired = {}
    extensions = []
    for phone in phones:
        device = 'SEP' + phone['mac']
        lines = [line['extension'] for line in provision.phone_lines(phone)]
        current = mapped.get(device)
        if current is None or current['id'] != lines[0] or current['lines'] != lines:
            raise ValueError('directory sync cannot add, remove or remap physical devices or lines')
        desired[device] = phone
        extensions.extend(lines)
    clients = provision.softphones.validate(site, extensions)
    if any(client['extension'] not in existing['dial_allowlist'] for client in clients):
        raise ValueError('softphone destination is absent from the existing dial allowlist; review dial permissions separately')
    result = copy.deepcopy(existing)
    for phone in result['phones']:
        source = desired[phone['device']]
        if 'room' in source:
            phone['room'] = source['room']
        else:
            phone.pop('room', None)
    metadata = [{'extension': client['extension'], 'room': client.get('room')} for client in clients]
    if metadata or 'softphones' in result:
        result['softphones'] = metadata
    return gateway.validate(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, required=True)
    parser.add_argument('--phones', type=Path, required=True)
    parser.add_argument('--gateway', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = update(json.loads(args.site.read_text()), json.loads(args.phones.read_text()),
                    json.loads(args.gateway.read_text()))
    write_new(args.output, (json.dumps(result, indent=2) + '\n').encode())
    print('New private gateway directory rendered; credentials, callbacks and dial permissions are unchanged.')


if __name__ == '__main__':
    main()
