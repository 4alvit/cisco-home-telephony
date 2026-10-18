#!/usr/bin/env python3
"""Enable private Cisco XML apps without rotating existing gateway credentials."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import urlsplit

from phone_control import write_new
from provision import phone_lines, private_phone_url
from render_gateway import gateway


def application_path(item, phone, bind_ipv4, *, create=False):
    """Reconcile existing paths exactly; allocate a separate nonce only once."""
    require = gateway.require
    fields = {key for key in ('services_url', 'directory_url') if key in item}
    require(not fields or len(fields) == 2, 'both_app_urls_required', 400)
    path = phone.get('apps_path')
    if fields:
        for field in fields:
            private_phone_url(item[field], field)
        supplied_path = urlsplit(item['services_url']).path
        require(path is None or path == supplied_path, 'app_path_mismatch', 400)
        path = supplied_path
    if path is None:
        if not create:
            return None
        path = '/apps/' + secrets.token_hex(32)
    require(isinstance(path, str) and re.fullmatch(r'/apps/[a-f0-9]{64}', path), 'bad_apps_path', 400)
    require(path.rsplit('/', 1)[1] != phone['auth_path'].rsplit('/', 1)[1], 'separate_app_nonce_required', 400)
    base = f'http://{bind_ipv4}:{phone["port"]}{path}'
    if fields:
        require(item['services_url'] == base and item['directory_url'] == base + '/directory',
                'app_urls_must_match_phone_listener', 400)
    return path


def prepare(config, inventory, home_assistant=None):
    """Return new private inputs; never mutate the existing gateway or inventory."""
    gateway.validate(config)
    require = gateway.require
    result, phones = copy.deepcopy(config), copy.deepcopy(inventory)
    require(isinstance(phones, dict) and isinstance(phones.get('phones'), list), 'bad_phone_inventory', 400)
    mapped = {phone['device']: phone for phone in result['phones']}
    require(len(mapped) == len(result['phones']) == len(phones['phones']), 'one_inventory_per_phone_required', 400)
    seen = set()
    for item in phones['phones']:
        require(isinstance(item, dict) and item.get('verified') is True
                and item.get('model') == 'CP-7945G', 'verified_7945g_required', 400)
        mac = item.get('mac', '')
        require(isinstance(mac, str), 'bad_device', 400)
        device = 'SEP' + re.sub('[:-]', '', mac).upper()
        require(device in mapped and device not in seen, 'inventory_device_mismatch', 400)
        seen.add(device)
        phone = mapped[device]
        require([line['extension'] for line in phone_lines(item)] == phone['lines'], 'inventory_lines_mismatch', 400)
        origin = f'http://{result["bind_ipv4"]}:{phone["port"]}'
        require(item.get('authentication_url') == origin + phone['auth_path'], 'authentication_url_mismatch', 400)
        phone['apps_path'] = application_path(item, phone, result['bind_ipv4'], create=True)
        item['services_url'] = origin + phone['apps_path']
        item['directory_url'] = item['services_url'] + '/directory'
    result.setdefault('applications', {})['enabled'] = True
    if home_assistant is not None:
        result['applications']['home_assistant'] = copy.deepcopy(home_assistant)
    gateway.validate(result)
    return result, phones


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 2 * 1024 * 1024:
            raise ValueError('input must be a regular private file, mode 0600, at most 2 MiB')
        return json.load(source)


def write_outputs(output, config, inventory):
    """Use an exclusive new directory so reruns cannot overwrite either input."""
    root = Path(output)
    root.mkdir(mode=0o700)
    written = []
    try:
        for name, value in (('gateway.json', config), ('phones.json', inventory)):
            target = root / name
            write_new(target, (json.dumps(value, indent=2) + '\n').encode())
            written.append(target)
    except BaseException:
        for path in written:
            path.unlink()
        root.rmdir()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway', required=True, help='existing private gateway JSON')
    parser.add_argument('--inventory', required=True, help='matching private phone inventory JSON')
    parser.add_argument('--home-assistant', help='optional private HA applications configuration JSON')
    parser.add_argument('--output', required=True, help='new directory; must not exist')
    args = parser.parse_args()
    os.umask(0o077)
    config, inventory = prepare(read_private(args.gateway), read_private(args.inventory),
                                read_private(args.home_assistant) if args.home_assistant else None)
    write_outputs(args.output, config, inventory)
    print('Created private gateway.json and phones.json. Existing credentials and app paths are preserved.')


if __name__ == '__main__':
    main()
