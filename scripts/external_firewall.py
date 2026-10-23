#!/usr/bin/env python3
"""Add stateful outbound-SIP return paths around an exact, captured raw guard.

Capture/migrate/check/status do not change firewall rules. Apply is opt-in.
Rollback closes external traffic and optional profile TFTP; remove also requires
the external UDP listener stopped.
Never installs router forwarding, a public source allowlist or an OUTPUT rule.
"""
import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

CHAIN = 'CISCO_EXTERNAL_SIP'
OWNER = 'cisco-external-sip'
RTP = '10000:10199'
TFTP = ('69', '20000:20031')
LEGACY_KEYS = {'bind_ipv4', 'signaling_port', 'raw_baseline'}
PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def private_network(value):
    network = ipaddress.IPv4Network(value, strict=True)
    require(str(network) == value and any(network.subnet_of(parent) for parent in PRIVATE),
            'Source must be a canonical RFC1918 IPv4 network')
    return network


def raw_rule(chain, owner, proto=None, port=None, source=None, destination=None, verdict='DROP'):
    rule = ['-A', chain]
    if source:
        rule += ['-s', source]
    if destination:
        rule += ['-d', destination]
    if proto:
        rule += ['-p', proto, '-m', proto, '--dport', str(port)]
    return rule + ['-m', 'comment', '--comment', owner, '-j', verdict]


def raw_hook(config):
    raw = config['raw_baseline']
    return ['-A', 'PREROUTING', '-m', 'addrtype', '--dst-type', 'LOCAL',
            '-m', 'comment', '--comment', raw['owner'], '-j', raw['chain']]


def validate_profile_policy(policy):
    require(isinstance(policy, dict) and set(policy) == {'version', 'bind_ipv4', 'phone_ipv4'}
            and type(policy['version']) is int and policy['version'] == 1,
            'Profile TFTP policy requires version 1, bind_ipv4 and phone_ipv4')
    address = ipaddress.IPv4Address(policy['bind_ipv4'])
    require(str(address) == policy['bind_ipv4'] and any(address in n for n in PRIVATE),
            'Profile TFTP bind address must be a canonical RFC1918 host')
    phones = policy['phone_ipv4']
    require(isinstance(phones, list) and 1 <= len(phones) <= 128
            and all(isinstance(value, str) for value in phones),
            'Profile TFTP requires 1..128 inventoried IPv4 hosts')
    for value in phones:
        phone = ipaddress.IPv4Address(value)
        require(str(phone) == value and any(phone in n for n in PRIVATE) and phone != address,
                'Profile TFTP sources must be canonical RFC1918 hosts distinct from the server')
    require(phones == sorted(set(phones)), 'Profile TFTP sources must be unique and sorted')
    return policy


def validate(config):
    require(isinstance(config, dict) and set(config) in (LEGACY_KEYS, LEGACY_KEYS | {'version', 'profile_tftp'}),
            'Configuration requires the legacy fields or explicit version 2 profile TFTP policy')
    if 'version' in config:
        require(type(config['version']) is int and config['version'] == 2, 'Unsupported configuration version')
        validate_profile_policy(config['profile_tftp'])
        require(config['profile_tftp']['bind_ipv4'] == config['bind_ipv4'],
                'Profile TFTP and external SIP must use the same fixed node address')
    address = ipaddress.IPv4Address(config['bind_ipv4'])
    require(str(address) == config['bind_ipv4'] and any(address in n for n in PRIVATE),
            'Bind address must be an explicit RFC1918 IPv4 host')
    require(type(config['signaling_port']) is int and 5070 <= config['signaling_port'] <= 5099,
            'External signaling port must be 5070..5099')
    raw = config['raw_baseline']
    require(isinstance(raw, dict) and set(raw) == {'chain', 'owner', 'ipv4_rules', 'ipv6_rules'},
            'Raw baseline requires chain, owner and both exact rule lists')
    require(isinstance(raw['chain'], str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,27}', raw['chain'])
            and raw['chain'] not in {'PREROUTING', 'INPUT', 'OUTPUT', 'POSTROUTING', 'FORWARD', CHAIN},
            'Invalid owned raw chain')
    require(isinstance(raw['owner'], str) and re.fullmatch(r'[A-Za-z0-9_./-]{1,128}', raw['owner']),
            'Invalid raw ownership comment')
    for family in (4, 6):
        body = raw[f'ipv{family}_rules']
        require(isinstance(body, list) and 2 <= len(body) <= 256, 'Invalid raw baseline length')
        final = raw_rule(raw['chain'], raw['owner'], verdict='RETURN')
        require(body[-1] == final, 'Raw baseline must end with its owned RETURN')
        rtp_sources, rtp_drops = [], 0
        seen_drop = set()
        for rule in body[:-1]:
            require(isinstance(rule, list) and all(isinstance(t, str) for t in rule), 'Invalid rule tokens')
            try:
                proto, port, verdict = rule[rule.index('-p') + 1], rule[rule.index('--dport') + 1], rule[-1]
                source = rule[rule.index('-s') + 1] if '-s' in rule else None
            except (ValueError, IndexError):
                raise ValueError('Raw baseline contains an unsupported rule') from None
            require(proto in ('tcp', 'udp') and re.fullmatch(r'[0-9]{1,5}(?::[0-9]{1,5})?', port),
                    'Invalid raw protocol or port')
            ports = list(map(int, port.split(':')))
            require(all(1 <= n <= 65535 for n in ports) and ports == sorted(ports), 'Invalid raw port range')
            if 'profile_tftp' in config and proto == 'udp':
                overlaps_tftp = ports[0] <= 69 <= ports[-1] or ports[0] <= 20031 and ports[-1] >= 20000
                require(not overlaps_tftp or port in TFTP,
                        'Profile TFTP needs exact, non-overlapping baseline TFTP port ranges')
            require(not (proto == 'udp' and ports[0] <= config['signaling_port'] <= ports[-1]),
                    'Raw baseline already controls the selected external SIP port')
            require(rule == raw_rule(raw['chain'], raw['owner'], proto, port, source, verdict=verdict),
                    'Raw baseline contains unsupported or unowned tokens')
            if source:
                require(family == 4 and verdict == 'RETURN', 'Only IPv4 source RETURN exceptions are supported')
                private_network(source)
                require(not (proto == 'udp' and (ports[0] <= 69 <= ports[-1]
                        or ports[0] <= 20031 and ports[-1] >= 20000)),
                        'Capture only the normal phone policy with TFTP closed')
                require((proto, port) not in seen_drop, 'Source exception follows its DROP')
            else:
                require(verdict == 'DROP' and (proto, port) not in seen_drop, 'Expected one DROP per protected port')
                seen_drop.add((proto, port))
            if proto == 'udp' and port == RTP:
                if source:
                    rtp_sources.append(source)
                else:
                    rtp_drops += 1
        require(rtp_drops == 1 and (family == 6 or bool(rtp_sources)), 'Baseline must close RTP with exact IPv4 source exceptions')
        require(len(rtp_sources) == len(set(rtp_sources)), 'Duplicate RTP source exception')
        if 'profile_tftp' in config:
            require(all(('udp', port) in seen_drop for port in TFTP),
                    'Profile TFTP requires exact closed TFTP drops in both baseline families')
    return config


def migrate(config, policy):
    """Build a v2 policy offline; never learn permissions from live open rules."""
    validate(config)
    require(set(config) == LEGACY_KEYS, 'Migration requires the original legacy closed-baseline configuration')
    validate_profile_policy(policy)
    # Isolate the new document from later mutations of either input.
    result = json.loads(json.dumps(config))
    result.update(version=2, profile_tftp=json.loads(json.dumps(policy)))
    return validate(result)


def legacy_config(config):
    return {key: config[key] for key in LEGACY_KEYS}


def inherited_sources(config):
    return [r[r.index('-s') + 1] for r in config['raw_baseline']['ipv4_rules']
            if '-s' in r and '-p' in r and r[r.index('-p') + 1] == 'udp'
            and '--dport' in r and r[r.index('--dport') + 1] == RTP]


def raw_body(config, family, opened=False):
    raw = config['raw_baseline']
    body = [list(r) for r in raw[f'ipv{family}_rules']]
    if family == 4 and opened:
        drop = raw_rule(raw['chain'], raw['owner'], 'udp', RTP)
        body.insert(body.index(drop), raw_rule(raw['chain'], raw['owner'], 'udp', RTP,
                    destination=config['bind_ipv4'] + '/32', verdict='RETURN'))
        if 'profile_tftp' in config:
            for port in TFTP:
                drop = raw_rule(raw['chain'], raw['owner'], 'udp', port)
                index = body.index(drop)
                body[index:index] = [raw_rule(raw['chain'], raw['owner'], 'udp', port,
                    source=phone + '/32', destination=config['bind_ipv4'] + '/32', verdict='RETURN')
                    for phone in config['profile_tftp']['phone_ipv4']]
    return body


def hook():
    return ['-A', 'INPUT', '-m', 'comment', '--comment', OWNER, '-j', CHAIN]


def filter_body(config, family, opened):
    result = []
    for port in (str(config['signaling_port']), RTP):
        dst = ['-d', config['bind_ipv4'] + '/32'] if family == 4 else []
        protocol = ['-p', 'udp', '-m', 'udp', '--dport', port]
        tail = ['-m', 'comment', '--comment', OWNER, '-j']
        if family == 4:
            if port == RTP:
                for source in inherited_sources(config):
                    result.append(['-A', CHAIN, '-s', source, *dst, *protocol, *tail, 'RETURN'])
            if opened:
                result.append(['-A', CHAIN, *dst, *protocol, '-m', 'conntrack', '--ctstate', 'ESTABLISHED',
                               '--ctdir', 'REPLY', *tail, 'RETURN'])
        result.append(['-A', CHAIN, *dst, *protocol, *tail, 'DROP'])
    if 'profile_tftp' in config:
        for port in TFTP:
            if family == 4 and opened:
                for phone in config['profile_tftp']['phone_ipv4']:
                    result.append(raw_rule(CHAIN, OWNER, 'udp', port, source=phone + '/32',
                                           destination=config['bind_ipv4'] + '/32', verdict='RETURN'))
            # Also protect other local destinations if another raw writer moves.
            result.append(raw_rule(CHAIN, OWNER, 'udp', port))
    return result + [['-A', CHAIN, '-m', 'comment', '--comment', OWNER, '-j', 'RETURN']]


def run(argv, data=None):
    value = subprocess.run(argv, input=data, text=True, capture_output=True, timeout=20)
    if value.returncode:
        # Do not relay raw firewall output, local addresses or arbitrary stderr.
        raise RuntimeError('Command failed: ' + Path(argv[0]).name)
    return value.stdout


def parse_save(text):
    chains, rules = {}, []
    for line in text.splitlines():
        if line.startswith(':'):
            parts = line.split()
            chains[parts[0][1:]] = parts[1]
        elif line.startswith('-A '):
            rules.append(shlex.split(line))
    return chains, rules


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def inspect(config, family, table):
    binary = 'iptables' if family == 4 else 'ip6tables'
    chains, rules = parse_save(run([binary + '-save', '-t', table]))
    is_raw = table == 'raw'
    chain = config['raw_baseline']['chain'] if is_raw else CHAIN
    expected_hook = raw_hook(config) if is_raw else hook()
    body = [r for r in rules if r[1] == chain]
    refs = [r for r in rules if any(flag in r and r[r.index(flag) + 1] == chain for flag in ('-j', '-g'))]
    if refs not in ([], [expected_hook]):
        raise RuntimeError('Unexpected or duplicate owned-chain reference')
    if chain not in chains and (refs or body):
        raise RuntimeError('Dangling owned chain')
    if chain in chains and chains[chain] != '-':
        raise RuntimeError('Owned chain is not a user chain')
    candidates = ({'closed': raw_body(config, family), 'open': raw_body(config, family, True)} if is_raw
                  else {'closed': filter_body(config, family, False), 'open': filter_body(config, family, True)})
    if 'profile_tftp' in config:
        legacy = legacy_config(config)
        candidates.update({'legacy_closed': raw_body(legacy, family), 'legacy_open': raw_body(legacy, family, True)}
                          if is_raw else {'legacy_closed': filter_body(legacy, family, False),
                                          'legacy_open': filter_body(legacy, family, True)})
    states = [key for key, value in candidates.items() if body == value] if chain in chains else ['absent']
    if not states:
        raise RuntimeError('Owned chain drift: expected exact captured or generated rules')
    parent = 'PREROUTING' if is_raw else 'INPUT'
    parent_rules = [r for r in rules if r[1] == parent]
    first = bool(parent_rules and parent_rules[0] == expected_hook)
    if is_raw and (chain not in chains or not first):
        raise RuntimeError('Captured raw guard must exist with its exact first PREROUTING hook')
    foreign = {'chains': {key: val for key, val in chains.items() if key != chain},
               'rules': [r for r in rules if r[1] != chain and r not in refs]}
    return {'exists': chain in chains, 'attached': bool(refs), 'first': first,
            'states': states, 'foreign_sha256': digest(foreign)}


def state(config):
    return {f'{table}{family}': inspect(config, family, table) for table in ('raw', 'filter') for family in (4, 6)}


def same_foreign(before, after):
    if any(before[key]['foreign_sha256'] != after[key]['foreign_sha256'] for key in before):
        raise RuntimeError('Unrelated firewall rules changed; coordinate other writers before continuing')


def payload(config, family, table, before, opened=False, remove=False):
    chain = config['raw_baseline']['chain'] if table == 'raw' else CHAIN
    lines = ['*' + table]
    if table == 'raw':
        if ('open' if opened else 'closed') not in before['states']:
            lines += ['-F ' + chain] + [shlex.join(r) for r in raw_body(config, family, opened)]
    elif remove:
        if before['attached']:
            lines.append(shlex.join(['-D'] + hook()[1:]))
        if before['exists']:
            lines += ['-F ' + CHAIN, '-X ' + CHAIN]
    else:
        if not before['exists']:
            lines.append(':' + CHAIN + ' - [0:0]')
        elif ('open' if opened else 'closed') not in before['states']:
            lines.append('-F ' + CHAIN)
        if not before['exists'] or ('open' if opened else 'closed') not in before['states']:
            lines += [shlex.join(r) for r in filter_body(config, family, opened)]
        if not before['first']:
            if before['attached']:
                lines.append(shlex.join(['-D'] + hook()[1:]))
            lines.append(shlex.join(['-I', 'INPUT', '1'] + hook()[2:]))
    return '\n'.join(lines + ['COMMIT', ''])


def restore(family, data, test=False):
    argv = [('iptables' if family == 4 else 'ip6tables') + '-restore', '--wait', '10', '--noflush']
    run(argv + (['--test'] if test else []), data)


def transition(config, family, table, opened=False, remove=False):
    before = inspect(config, family, table)
    data = payload(config, family, table, before, opened, remove)
    restore(family, data, test=True)
    restore(family, data)
    after = inspect(config, family, table)
    same_foreign({'table': before}, {'table': after})
    if remove:
        if after['exists']:
            raise RuntimeError('Owned filter chain was not removed')
    elif ('open' if opened else 'closed') not in after['states'] or not after['first']:
        raise RuntimeError('Firewall transition did not converge')


def close(config):
    # RTP and profile TFTP must stop bypassing raw DROPs before INPUT changes.
    transition(config, 4, 'raw', opened=False)
    inspect(config, 6, 'raw')  # Never change IPv6 raw rules.
    for family in (6, 4):
        transition(config, family, 'filter', opened=False)


def listener_stopped(config):
    if run(['ss', '-H', '-lun', 'sport = :' + str(config['signaling_port'])]).strip():
        raise RuntimeError('External UDP listener is still present; keep the closed guard installed')


def apply(config, mode):
    validate(config)
    before = state(config)
    if mode == 'status':
        configured = 'profile_tftp' in config
        effective = configured and all(before[key]['first'] and 'open' in before[key]['states']
                                       for key in ('raw4', 'raw6', 'filter4', 'filter6'))
        return {'changes_applied': False, 'state': before, 'policy_version': config.get('version', 1),
                'profile_tftp': {'configured': configured, 'effective': effective,
                                'phone_count': len(config['profile_tftp']['phone_ipv4']) if configured else 0}}
    require(mode in ('check', 'apply', 'rollback', 'remove'), 'Unknown operation')
    # Kernel-validate both target filter families and the raw change before writes.
    for family in (6, 4):
        restore(family, payload(config, family, 'filter', before[f'filter{family}'],
                                opened=mode in ('check', 'apply')), test=True)
    restore(4, payload(config, 4, 'raw', before['raw4'], opened=mode in ('check', 'apply')), test=True)
    if mode == 'check':
        return {'changes_applied': False, 'kernel_validation': True, 'inherited_rtp_source_count': len(inherited_sources(config))}
    try:
        if mode == 'apply':
            for family in (6, 4):
                transition(config, family, 'filter', opened=True)
            guarded = state(config)
            same_foreign(before, guarded)
            if any(not guarded[f'filter{f}']['first'] or 'open' not in guarded[f'filter{f}']['states'] for f in (4, 6)):
                raise RuntimeError('Both INPUT guards must be verified before raw relaxation')
            transition(config, 4, 'raw', opened=True)
        else:
            close(config)
            if mode == 'remove':
                listener_stopped(config)
                # Recheck immediately before deletion; operator must keep it stopped.
                for family in (4, 6):
                    listener_stopped(config)
                    transition(config, family, 'filter', remove=True)
        after = state(config)
        same_foreign(before, after)
    except Exception as error:
        try:
            close(config)
        except Exception:
            raise RuntimeError('Operation failed; automatic closure could not be verified. Keep service stopped and inspect owned rules') from error
        raise RuntimeError('Operation failed; raw baseline restored and closed INPUT guard retained') from error
    return {'changes_applied': True, 'mode': mode, 'state': after}


def capture(bind_ipv4, signaling_port, chain, owner):
    config = {'bind_ipv4': bind_ipv4, 'signaling_port': signaling_port,
              'raw_baseline': {'chain': chain, 'owner': owner}}
    for family in (4, 6):
        binary = 'iptables' if family == 4 else 'ip6tables'
        _, rules = parse_save(run([binary + '-save', '-t', 'raw']))
        config['raw_baseline'][f'ipv{family}_rules'] = [r for r in rules if r[1] == chain]
    validate(config)
    # The only accepted capture is the closed baseline, never a permissive snapshot.
    for family in (4, 6):
        inspect(config, family, 'raw')
    return config


def read_private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.getuid() and info.st_size <= 262144,
                'Configuration must be owned by this user, regular, mode 0600 and at most 256 KiB')
        return json.load(stream)


def read_private(path):
    return validate(read_private_json(path))


def write_private(path, value):
    # Serialize before creating a new file; never replace existing evidence.
    data = json.dumps(value, indent=2) + '\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


@contextlib.contextmanager
def locked():
    import fcntl  # Linux-only runtime; importable for offline tests elsewhere.
    fd = os.open('/run/lock/cisco-external-sip.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('capture', 'migrate', 'check', 'status', 'apply', 'rollback', 'remove'))
    parser.add_argument('--config', required=True, help='Private JSON; capture exclusively creates this file')
    parser.add_argument('--bind-ipv4')
    parser.add_argument('--signaling-port', type=int, default=5070)
    parser.add_argument('--raw-chain')
    parser.add_argument('--raw-owner')
    parser.add_argument('--profile-tftp-policy', help='Private inventory-generated profile TFTP policy for migration')
    parser.add_argument('--output', help='New private version 2 configuration; never overwritten')
    args = parser.parse_args()
    try:
        if args.mode == 'capture':
            require(all((args.bind_ipv4, args.raw_chain, args.raw_owner)), 'Capture needs bind address, raw chain and owner')
            config = capture(args.bind_ipv4, args.signaling_port, args.raw_chain, args.raw_owner)
            write_private(args.config, config)
            result = {'changes_applied': False, 'private_baseline_written': True,
                      'inherited_rtp_source_count': len(inherited_sources(config))}
        elif args.mode == 'migrate':
            require(args.profile_tftp_policy and args.output, 'Migration needs a private profile policy and new output path')
            config = migrate(read_private(args.config), read_private_json(args.profile_tftp_policy))
            write_private(args.output, config)
            result = {'changes_applied': False, 'private_policy_written': True, 'policy_version': 2,
                      'profile_phone_count': len(config['profile_tftp']['phone_ipv4'])}
        else:
            config = read_private(args.config)
            with locked():
                result = apply(config, args.mode)
        print(json.dumps(result, indent=2))
    except RuntimeError as error:
        # All RuntimeError messages are fixed local diagnostics; child stderr
        # and private configuration values never become part of them.
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        # Detailed stderr from child commands/configs may contain private values.
        print('External firewall operation failed; no private configuration or command output was logged.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
