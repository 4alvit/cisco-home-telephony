#!/usr/bin/env python3
"""Own only one INPUT subchain; preserve every unrelated IPv4/IPv6 rule."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import shlex
import subprocess

CHAIN = 'CISCO_PHONE_MGMT'
OWNER = 'cisco-phone-gateway'


def validate(config):
    if set(config) != {'bind_ipv4', 'api_port', 'api_clients', 'phones'}:
        raise ValueError('Unexpected firewall configuration fields')
    for value in [config['bind_ipv4'], *config['api_clients'], *[phone['ipv4'] for phone in config['phones']]]:
        address = ipaddress.IPv4Address(value)
        if not any(address in ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
            raise ValueError('Only explicit private IPv4 hosts are allowed')
    if not isinstance(config['phones'], list) or any(set(phone) != {'ipv4', 'port'} for phone in config['phones']):
        raise ValueError('Each phone requires exactly an address and port')
    if len({phone['ipv4'] for phone in config['phones']}) != len(config['phones']):
        raise ValueError('Phone addresses must be distinct')
    ports = [config['api_port'], *[phone['port'] for phone in config['phones']]]
    if not config['api_clients'] or not config['phones'] or len(ports) != len(set(ports)):
        raise ValueError('Clients/phones are required and ports must be distinct')
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports):
        raise ValueError('Ports must be 1024..65535')
    return config


def hook():
    return ['-A', 'INPUT', '-m', 'comment', '--comment', OWNER, '-j', CHAIN]


def rules(config, family):
    validate(config)
    result = []
    # Kubelet TCP probes originate on this node. They may complete a handshake;
    # the application still rejects their HTTP requests (HA-only source ACL).
    api_sources = list(dict.fromkeys([*config['api_clients'], config['bind_ipv4']]))
    for port, clients in [(config['api_port'], api_sources), *[(p['port'], [p['ipv4']]) for p in config['phones']]]:
        destination = ['-d', config['bind_ipv4'] + '/32'] if family == 4 else []
        protocol = ['-p', 'tcp', '-m', 'tcp', '--dport', str(port)]
        if family == 4:
            for client in clients:
                # Match iptables-save canonical source/destination ordering.
                result.append(['-A', CHAIN, '-s', client + '/32', *destination, *protocol,
                               '-m', 'comment', '--comment', OWNER, '-j', 'RETURN'])
        result.append(['-A', CHAIN, *destination, *protocol, '-m', 'comment', '--comment', OWNER, '-j', 'DROP'])
    result.append(['-A', CHAIN, '-m', 'comment', '--comment', OWNER, '-j', 'RETURN'])
    return result


def run(command, data=None):
    value = subprocess.run(command, input=data, text=True, capture_output=True, timeout=15)
    if value.returncode:
        raise RuntimeError('Firewall command failed: ' + ' '.join(command[:2]))
    return value.stdout


def snapshot(config, family, previous=None):
    binary = 'iptables' if family == 4 else 'ip6tables'
    raw = run([binary + '-save', '-t', 'filter'])
    chains, current = {}, []
    for line in raw.splitlines():
        if line.startswith(':'):
            parts = line.split(); chains[parts[0][1:]] = parts[1]
        elif line.startswith('-A '):
            current.append(shlex.split(line))
    body = [line for line in current if line[1] == CHAIN]
    references = [line for line in current if any(flag in line and line[line.index(flag) + 1] == CHAIN for flag in ('-j', '-g'))]
    if references not in ([], [hook()]):
        raise RuntimeError('Foreign or duplicate gateway-chain reference')
    accepted = [rules(config, family)] + ([rules(previous, family)] if previous else [])
    if CHAIN in chains and (chains[CHAIN] != '-' or body not in accepted):
        raise RuntimeError('Gateway chain differs from the reviewed current/previous configuration')
    if CHAIN not in chains and references:
        raise RuntimeError('Dangling gateway-chain reference')
    unrelated = {'chains': {key: val for key, val in chains.items() if key != CHAIN},
                 'rules': [line for line in current if line[1] != CHAIN and line not in references]}
    inputs = [line for line in current if line[1] == 'INPUT']
    return {'exists': CHAIN in chains, 'attached': bool(references),
            'first': bool(inputs and inputs[0] == hook()), 'matches': body == rules(config, family),
            'foreign_sha256': hashlib.sha256(json.dumps(unrelated, sort_keys=True).encode()).hexdigest()}


def payload(config, family, before, remove=False):
    lines = ['*filter']
    if remove:
        if before['attached']:
            lines.append(shlex.join(['-D'] + hook()[1:]))
        if before['exists']:
            lines.extend(['-F ' + CHAIN, '-X ' + CHAIN])
    else:
        if not before['exists']:
            lines.append(':' + CHAIN + ' - [0:0]')
        elif not before['matches']:
            lines.append('-F ' + CHAIN)
        if not before['exists'] or not before['matches']:
            lines.extend(shlex.join(line) for line in rules(config, family))
        if not before['first']:
            if before['attached']:
                lines.append(shlex.join(['-D'] + hook()[1:]))
            lines.append(shlex.join(['-I', 'INPUT', '1'] + hook()[2:]))
    return '\n'.join(lines + ['COMMIT', ''])


def apply(config, mode, previous=None):
    before = {family: snapshot(config, family, previous) for family in (4, 6)}
    if mode == 'status':
        return before
    plans = {family: payload(config, family, before[family], mode == 'remove') for family in (4, 6)}
    for family in (4, 6):
        run([('iptables' if family == 4 else 'ip6tables') + '-restore', '--noflush', '--test'], plans[family])
    if mode == 'check':
        return {'kernel_validation': True, 'changes_applied': False}
    for family in (4, 6):
        run([('iptables' if family == 4 else 'ip6tables') + '-restore', '--noflush'], plans[family])
    after = {family: snapshot(config, family, previous) for family in (4, 6)}
    for family in (4, 6):
        if after[family]['foreign_sha256'] != before[family]['foreign_sha256']:
            raise RuntimeError('Unrelated filter rules changed concurrently; inspect before continuing')
        if mode != 'remove' and not (after[family]['matches'] and after[family]['first']):
            raise RuntimeError('Gateway firewall did not converge')
    return {'changes_applied': True, 'families': after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['check', 'apply', 'status', 'remove'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--previous-config')
    args = parser.parse_args()
    config = validate(json.loads(Path(args.config).read_text()))
    previous = validate(json.loads(Path(args.previous_config).read_text())) if args.previous_config else None
    print(json.dumps(apply(config, args.mode, previous), indent=2))


if __name__ == '__main__': main()
