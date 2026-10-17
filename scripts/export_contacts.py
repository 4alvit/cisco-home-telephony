#!/usr/bin/env python3
"""Export a private, read-only gateway directory snapshot as JSON or CSV.

This helper never changes contacts, calls, routes or permissions. It does not
write to Zoiper or any address book; import/synchronization is a separate step.
"""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import sys

import cisco


def contacts(data):
    """Validate a fresh gateway snapshot and retain only contact metadata."""
    directory = cisco.directory_result(data)
    entries = []
    for item in directory['directory']:
        label = item['room'] or ('Cisco phone ' if item['type'] == 'cisco' else 'Softphone ') + item['id']
        entries.append({'id': item['type'] + ':' + item['id'], 'label': label,
                        'kind': item['type'], 'numbers': item['lines']})
    for item in directory['services']:
        entries.append({'id': 'service:' + item['number'], 'label': item['label'],
                        'kind': item['kind'], 'numbers': [item['number']]})
    entries.sort(key=lambda item: int(item['numbers'][0]))
    return {'schema_version': 1, 'source': 'cisco-home-telephony',
            'observed_at': directory['observed_at'], 'contacts': entries}


def encode(snapshot, format):
    if format == 'json':
        return (json.dumps(snapshot, indent=2) + '\n').encode()
    if format != 'csv':
        raise ValueError('unsupported export format')
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['Contact ID', 'Name', 'Phone', 'Kind'])
    for item in snapshot['contacts']:
        for number in item['numbers']:
            writer.writerow([item['id'], item['label'], number, item['kind']])
    return output.getvalue().encode('utf-8')


def write_new(path, data):
    """Use exclusive creation so existing exports and symlinks stay untouched."""
    fd = os.open(Path(path).expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('CISCO_CONFIG', '~/.config/cisco-home/cli.json'))
    parser.add_argument('--output', type=Path, required=True, help='new private file; existing files are never replaced')
    parser.add_argument('--format', choices=('json', 'csv'), default='json')
    parser.add_argument('--timeout', type=int, default=20)
    args = parser.parse_args(argv)
    try:
        cisco.require(1 <= args.timeout <= 60, 'timeout_requires_1_to_60_seconds')
        config = cisco.load_config(args.config)
        data = cisco.Gateway(config, args.timeout).request('/v1/phones')
        snapshot = contacts(data)
        write_new(args.output, encode(snapshot, args.format))
    except cisco.Refused as error:
        print('Contact export refused: ' + error.reason, file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print('Contact export failed; check the private output path and configuration.', file=sys.stderr)
        return 1
    print(f"Exported {len(snapshot['contacts'])} contacts and "
          f"{sum(len(item['numbers']) for item in snapshot['contacts'])} numbers. "
          'No address book was changed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
