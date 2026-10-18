#!/usr/bin/env python3
"""Create new private persistent-gateway input from verified existing profiles.

This is an explicit migration from bounded commissioning callbacks. It does not
modify a phone, change a callback credential, or start a persistent listener.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import softphones
import service_catalog
from configure_phone_apps import application_path

from phone_control import load_config, write_new
from render_gateway import gateway
from render_k3s import load_site


def prepare(site, inventory, callbacks, ami, clients, api_port=18084):
    require = gateway.require
    require(site.get('gateway', {}).get('enabled') is True, 'enable_site_gateway_first', 400)
    require(site['gateway']['ami_client_ipv4'] == site['node_ipv4'], 'gateway_ami_source_must_be_local_node', 400)
    mapped = {callback['device']: callback for callback in callbacks}
    require(len(mapped) == len(callbacks) == len(inventory['phones']), 'one_callback_per_phone_required', 400)
    phones = []
    for item in inventory['phones']:
        device = 'SEP' + re.sub('[:-]', '', item['mac']).upper()
        require(item.get('verified') is True and item.get('model') == 'CP-7945G', 'verified_7945g_required', 400)
        require(device in mapped, 'callback_device_not_in_inventory', 400)
        callback = mapped[device]
        expected = f'http://{callback["auth_bind_ipv4"]}:{callback["auth_port"]}{callback["auth_path"]}'
        require(item.get('authentication_url') == expected, 'callback_must_match_provisioned_authentication_url', 400)
        require(callback['auth_bind_ipv4'] == site['node_ipv4'], 'callback_bind_must_match_node', 400)
        require(callback['phone_ipv4'] + '/32' in site['phone_cidrs'], 'exact_phone_acl_required', 400)
        lines = [line['extension'] for line in item['lines']] if 'lines' in item else [item['extension']]
        phone = {'id': lines[0], 'lines': lines, 'ipv4': callback['phone_ipv4'], 'device': device,
                       'port': callback['auth_port'], 'auth_path': callback['auth_path'],
                       'username': callback['username'], 'password': callback['password']}
        if 'room' in item:
            phone['room'] = softphones.room_name(item['room'])
        path = application_path(item, phone, site['node_ipv4'])
        if path is not None:
            phone['apps_path'] = path
        phones.append(phone)
    apps_enabled = any('apps_path' in phone for phone in phones)
    require(not apps_enabled or all('apps_path' in phone for phone in phones), 'app_urls_required_for_all_phones', 400)
    clients_metadata = [{'extension': client['extension'], 'room': client.get('room')}
                        for client in softphones.validate(site, [line for phone in phones for line in phone['lines']])]
    local_numbers = ([line for phone in phones for line in phone['lines']]
                     + [client['extension'] for client in clients_metadata])
    services = service_catalog.build(site, has_phones=bool(phones), local_numbers=local_numbers,
                                     line_owners={line: phone['id'] for phone in phones for line in phone['lines']},
                                     group_members=[phone['id'] for phone in phones]
                                     + [client['extension'] for client in clients_metadata])
    numbers = local_numbers + [entry['number'] for entry in services]
    result = {'persistent_enabled': True, 'bind_ipv4': site['node_ipv4'], 'api_port': api_port,
              'api_clients': clients, 'api_token': secrets.token_urlsafe(36),
              'ami': {'host': site['node_ipv4'], 'port': 5038, 'username': ami['username'], 'secret': ami['secret']},
              'phones': phones, 'dial_allowlist': numbers,
              'ring_allowlist': ['640'] if '640' in numbers else [], 'services': services}
    if clients_metadata:
        result['softphones'] = clients_metadata
    if apps_enabled:
        result['applications'] = {'enabled': True}
    return gateway.validate(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--inventory', required=True)
    parser.add_argument('--callback', action='append', required=True)
    parser.add_argument('--ami-secret', required=True)
    parser.add_argument('--ha-source-ip', action='append', required=True)
    parser.add_argument('--api-port', type=int, default=18084)
    parser.add_argument('--enable-persistent', action='store_true', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = prepare(load_site(args.site), json.loads(Path(args.inventory).read_text()),
                     [load_config(path, allow_expired=True) for path in args.callback],
                     json.loads(Path(args.ami_secret).read_text()), args.ha_source_ip, args.api_port)
    write_new(args.output, (json.dumps(result, indent=2) + '\n').encode())
    print('Created private persistent gateway input. Existing phone credentials and callback URLs are unchanged.')


if __name__ == '__main__':
    main()
