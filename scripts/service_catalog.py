#!/usr/bin/env python3
"""Derive read-only service labels from the same settings as the dialplan.

This module neither creates routes nor grants dial permission. Generated labels
never include external peer names, credentials, addresses or household names.
"""
import re
import provision


KINDS = {'diagnostic', 'group', 'assistant', 'alert', 'radio', 'external', 'automation'}


def managed_numbers():
    """Codes owned by this repository, including currently disabled services."""
    return ({'600', '601', '620', '621', '630', '640', '641'}
            | {entry['extension'] for entry in provision._radio.catalog().values()}
            | {entry['number'] for entry in provision.external_sip.PROVIDERS.values()})


def validate(entries, local_numbers=(), allowed_numbers=None):
    """Validate portable metadata, rejecting collisions and invented permission."""
    if not isinstance(entries, list) or len(entries) > 128:
        raise ValueError('services must be a list of at most 128 entries')
    seen = set(local_numbers)
    allowed = None if allowed_numbers is None else set(allowed_numbers)
    for item in entries:
        if not isinstance(item, dict) or set(item) != {'number', 'label', 'kind'}:
            raise ValueError('service metadata requires only number, label and kind')
        number, label = item['number'], item['label']
        if not isinstance(number, str) or not re.fullmatch(r'[0-9]{3,8}', number) or number in seen:
            raise ValueError('duplicate or invalid service number')
        if allowed is not None and number not in allowed:
            raise ValueError('service is absent from the existing dial allowlist')
        if (not isinstance(label, str) or label != label.strip()
                or not re.fullmatch(r'[A-Za-z0-9][ -~]{0,79}', label)):
            raise ValueError('service label must be 1..80 printable ASCII characters starting with a letter or digit')
        if not isinstance(item['kind'], str) or item['kind'] not in KINDS:
            raise ValueError('invalid service kind')
        seen.add(number)
    return entries


def build(site, *, has_phones, local_numbers=(), allowed_numbers=None, group_members=None,
          line_owners=None):
    """Return enabled routes, optionally narrowed to existing dial permission.

    The provisioner's validators are reused, including its external-account
    validation. A configured account with no outbound target has no shortcut.
    Missing permission omits a route from this catalog; it never widens access.
    """
    entries = []

    def add(number, label, kind):
        entries.append({'number': number, 'label': label, 'kind': kind})

    add('600', 'Echo test', 'diagnostic')
    add('601', 'Milliwatt test tone', 'diagnostic')
    if has_phones:
        add('620', 'All Cisco phones', 'group')
    ha = provision.ha_settings(site)
    if ha and ha.get('assist'):
        add('630', 'Home Assistant Assist', 'assistant')
    if provision.alarm_settings(site):
        add('640', 'Legacy alarm tone', 'alert')
    if provision.phone_services.group_settings(site, members=group_members):
        add('621', 'Incoming calls group', 'group')
    # Use the complete deployment validator, not just the feature flag. A
    # partial HA setup, missing prompt or absent physical owner must not create
    # a catalog entry or grant the service through --sync-services.
    if provision.phone_services.settings(site, line_owners or {}):
        add('641', 'Toggle saved alarm', 'automation')
    radio = provision.radio_settings(site)
    if radio:
        stations = provision._radio.catalog()
        for station in radio['stations']:
            entry = stations[station]
            add(entry['extension'], entry['name'], 'radio')
    external = provision.external_sip.validate(site)
    if external:
        providers = {'callcentric': 'Callcentric', 'sip2sip': 'SIP2SIP', 'linphone': 'Linphone'}
        for account in external['accounts']:
            if account['target'] is None:
                continue
            label = providers[account['provider']] + ' configured peer'
            if account['provider'] == 'sip2sip' and account['target'] == '4444':
                label = 'SIP2SIP echo test'
            elif account['provider'] == 'linphone' and account['target'] == 'thetestcall':
                label = 'The Test Call (English menu)'
            add(account['number'], label, 'external')
    validate(entries, local_numbers)
    if allowed_numbers is not None:
        allowed = set(allowed_numbers)
        entries = [entry for entry in entries if entry['number'] in allowed]
    return sorted(validate(entries, local_numbers, allowed_numbers), key=lambda item: int(item['number']))
