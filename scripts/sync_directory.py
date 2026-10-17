#!/usr/bin/env python3
"""Render directory metadata into a new gateway file without rotating credentials.

By default only room names and read-only softphone/service metadata may change.
The explicit --sync-services option also reconciles known service permissions.
Physical identities, local-line permissions, callbacks, tokens and API access stay unchanged.
"""
import argparse
import copy
import json
import os
from pathlib import Path

from phone_control import write_new
import provision
import service_catalog
from render_gateway import gateway


def update(site, inventory, existing, *, sync_services=False):
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
    services = service_catalog.build(
        site, has_phones=bool(phones),
        local_numbers=extensions + [client['extension'] for client in clients],
        line_owners={line['extension']: provision.phone_lines(phone)[0]['extension']
                     for phone in phones for line in provision.phone_lines(phone)},
        group_members=[provision.phone_lines(phone)[0]['extension'] for phone in phones]
                      + [client['extension'] for client in clients],
        allowed_numbers=None if sync_services else existing['dial_allowlist'])
    if sync_services:
        known = service_catalog.managed_numbers()
        enabled = {entry['number'] for entry in services}
        # Preserve restrictions on local extensions and unrelated custom routes.
        # Only the explicitly managed service codes may be removed or added.
        result['dial_allowlist'] = [number for number in existing['dial_allowlist']
                                    if number not in known or number in enabled]
        result['dial_allowlist'].extend(sorted(enabled - set(result['dial_allowlist']), key=int))
        result['ring_allowlist'] = [number for number in existing.get('ring_allowlist', ['640'])
                                    if number not in known or number in enabled]
    result['services'] = services
    return gateway.validate(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, required=True)
    parser.add_argument('--phones', type=Path, required=True)
    parser.add_argument('--gateway', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sync-services', action='store_true',
                        help='explicitly reconcile enabled managed service permissions; preserve local-line restrictions')
    args = parser.parse_args()
    os.umask(0o077)
    result = update(json.loads(args.site.read_text()), json.loads(args.phones.read_text()),
                    json.loads(args.gateway.read_text()), sync_services=args.sync_services)
    write_new(args.output, (json.dumps(result, indent=2) + '\n').encode())
    if args.sync_services:
        print('New private gateway directory rendered with synchronized service permissions; '
              'credentials, callbacks and local-line permissions are unchanged.')
    else:
        print('New private gateway directory rendered; credentials, callbacks and dial permissions are unchanged.')


if __name__ == '__main__':
    main()
