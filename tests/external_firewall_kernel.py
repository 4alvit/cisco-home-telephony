"""Invoked only inside the disposable network-none NET_ADMIN test container."""
import copy
import shlex
import socket


def seed_pre_mangle(fw, config):
    """Install the deployed raw/filter policy without using the new apply path.

    The raw and filter rule formats are unchanged by the mangle upgrade. A
    foreign unconditional filter ACCEPT intentionally precedes the old hook.
    Full-table restore is safe only in this fixture's isolated namespace.
    """
    raw = config['raw_baseline']
    for family in (4, 6):
        tables = [
            ['*raw', ':PREROUTING ACCEPT [0:0]', ':OUTPUT ACCEPT [0:0]',
             ':' + raw['chain'] + ' - [0:0]', ':FOREIGN_RAW - [0:0]',
             shlex.join(fw.raw_hook(config)), '-A PREROUTING -j FOREIGN_RAW',
             '-A FOREIGN_RAW -p tcp -m tcp --dport 443 -j RETURN',
             *[shlex.join(rule) for rule in fw.raw_body(config, family, True)]],
            ['*mangle', ':PREROUTING ACCEPT [0:0]', ':INPUT ACCEPT [0:0]',
             ':FORWARD ACCEPT [0:0]', ':OUTPUT ACCEPT [0:0]', ':POSTROUTING ACCEPT [0:0]',
             ':FOREIGN_MANGLE - [0:0]', '-A INPUT -j FOREIGN_MANGLE',
             '-A INPUT -p tcp -m tcp --dport 443 -j ACCEPT',
             '-A FOREIGN_MANGLE -p tcp -m tcp --dport 80 -j RETURN',
             '-A FOREIGN_MANGLE -j RETURN'],
            ['*filter', ':INPUT ACCEPT [0:0]', ':FORWARD DROP [0:0]', ':OUTPUT ACCEPT [0:0]',
             ':FOREIGN_FILTER - [0:0]', ':' + fw.CHAIN + ' - [0:0]',
             '-A INPUT -m comment --comment synthetic-foreign-first -j ACCEPT',
             '-A INPUT -j FOREIGN_FILTER', shlex.join(fw.hook()),
             '-A FOREIGN_FILTER -p tcp -m tcp --dport 443 -j RETURN',
             '-A FOREIGN_FILTER -j RETURN',
             *[shlex.join(rule) for rule in fw.filter_body(config, family, True)]],
        ]
        for body in tables:
            fw.run(['iptables-restore' if family == 4 else 'ip6tables-restore', '--wait', '10'],
                   '\n'.join(body + ['COMMIT', '']))


def assert_guard_layers(state, expected):
    for family in (4, 6):
        for table in ('mangle', 'filter'):
            guard = state[f'{table}{family}']
            if expected == 'absent':
                assert not guard['exists'] and not guard['attached'], 'Owned guard remains'
            else:
                assert expected in guard['states'] and guard['attached'], 'Guard has wrong state'
                assert guard['first'] == (table == 'mangle'), 'Foreign INPUT order changed'


def prove_old_filter_bypass(config, udp, source_port):
    """Prove the seeded early ACCEPT reaches sockets before upgrading it."""
    for offset, port in enumerate((10015, config['signaling_port'])):
        with udp(config['bind_ipv4'], port) as receiver, udp('198.51.100.20', source_port + offset) as peer:
            peer.sendto(b'old-filter-bypass', receiver.getsockname())
            assert receiver.recvfrom(4096)[0] == b'old-filter-bypass', 'Old bypass fixture is ineffective'


def exercise(fw, config):
    # Refuse normal host/container networking before any firewall/IP writes.
    # The Linux host runner additionally verifies a fresh unshared namespace.
    assert {name for _, name in socket.if_nameindex()} == {'lo'}, 'Expected network-none namespace'
    for address in (config['bind_ipv4'], '198.51.100.20', '10.20.30.40'):
        fw.run(['ip', 'address', 'add', address + '/32', 'dev', 'lo'])

    def udp(address, port, family=socket.AF_INET):
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.bind((address, port))
        sock.settimeout(0.3)
        return sock

    def must_block(sock):
        try:
            sock.recvfrom(4096)
        except TimeoutError:
            return
        raise RuntimeError('Unexpected packet crossed the guard')

    seed_pre_mangle(fw, config)
    before = fw.state(config)
    for family in (4, 6):
        assert not before[f'mangle{family}']['exists'], 'Seed accidentally contains new guard'
        assert before[f'filter{family}']['attached'] and not before[f'filter{family}']['first']
    prove_old_filter_bypass(config, udp, 29000)
    fw.apply(config, 'check')
    assert fw.state(config) == before, 'Check changed live namespace rules'
    fw.apply(config, 'apply')
    fw.same_foreign(before, fw.state(config))
    assert_guard_layers(fw.state(config), 'open')

    # Prove real conntrack directions, not just iptables syntax. Both addresses
    # are local aliases in this otherwise disconnected network namespace.
    for port in (10010, config['signaling_port']):
        with udp(config['bind_ipv4'], port) as pbx, udp('198.51.100.20', 24000 + port) as remote:
            # A distinct tuple avoids the local sender's OUTPUT confirmation
            # of the intentionally dropped packet influencing the reply test.
            with udp('198.51.100.20', 24200 + port) as unsolicited:
                unsolicited.sendto(b'unsolicited', pbx.getsockname())
                must_block(pbx)
            pbx.sendto(b'outbound-first', remote.getsockname())
            if remote.recvfrom(4096)[0] != b'outbound-first':
                raise RuntimeError('Outbound probe lost')
            remote.sendto(b'established-reply', pbx.getsockname())
            if pbx.recvfrom(4096)[0] != b'established-reply':
                raise RuntimeError('Expected reply lost')
            with udp('198.51.100.20', 24100 + port) as other_tuple:
                other_tuple.sendto(b'new-tuple', pbx.getsockname())
                must_block(pbx)
            if port == config['signaling_port']:
                # Removing while the real 5070 socket exists must retain a
                # closed guard. Then re-apply for the remaining assertions.
                try:
                    fw.apply(config, 'remove')
                except RuntimeError:
                    pass
                else:
                    raise RuntimeError('Removed a guard with a live listener')
                assert_guard_layers(fw.state(config), 'closed')
                remote.sendto(b'closed-even-if-established', pbx.getsockname())
                must_block(pbx)
                fw.apply(config, 'apply')
    for port in (10011, config['signaling_port']):
        with udp('::1', port, socket.AF_INET6) as ipv6_pbx, udp('::1', 25000, socket.AF_INET6) as ipv6_peer:
            ipv6_peer.sendto(b'ipv6-blocked', ipv6_pbx.getsockname())
            must_block(ipv6_pbx)
    with udp(config['bind_ipv4'], 10012) as pbx, udp('10.20.30.40', 25001) as home_assistant:
        home_assistant.sendto(b'local-source-while-open', pbx.getsockname())
        assert pbx.recvfrom(4096)[0] == b'local-source-while-open', 'Open guard changed local RTP'
    fw.apply(config, 'rollback')
    assert_guard_layers(fw.state(config), 'closed')
    with udp(config['bind_ipv4'], 10012) as pbx, udp('10.20.30.40', 25001) as home_assistant:
        home_assistant.sendto(b'old-exact-source', pbx.getsockname())
        if pbx.recvfrom(4096)[0] != b'old-exact-source':
            raise RuntimeError('Existing local RTP exception changed')
    fw.apply(config, 'remove')
    after = fw.state(config)
    fw.same_foreign(before, after)
    assert_guard_layers(after, 'absent')
    print('PASS: kernel parser, UDP reply tuples, unsolicited/IPv6 drops, source preservation, safe rollback/removal')
    exercise_profiles(fw, config, udp, must_block)


def exercise_profiles(fw, legacy, udp, must_block):
    """Real datagrams through all guard tables; all addresses stay local."""
    phones = ['192.168.50.21', '192.168.50.22']
    other_destination = '192.168.50.11'
    outsiders = ['192.168.50.23', '192.168.60.22', '10.20.30.40', '198.51.100.20']
    for address in phones + [other_destination] + outsiders[:2]:
        fw.run(['ip', 'address', 'add', address + '/32', 'dev', 'lo'])
    policy = {'version': 1, 'bind_ipv4': legacy['bind_ipv4'], 'phone_ipv4': phones}
    cfg = fw.migrate(legacy, policy)
    # Both deployed versions really lack mangle: seeding with new apply would
    # conceal the upgrade bug. Check migration from v1 and repair of old v2.
    for index, deployed in enumerate((legacy, cfg)):
        seed_pre_mangle(fw, deployed)
        before = fw.state(cfg)
        assert ('legacy_open' if index == 0 else 'open') in before['raw4']['states']
        for family in (4, 6):
            assert not before[f'mangle{family}']['exists']
        assert not fw.apply(cfg, 'status')['profile_tftp']['effective'], 'Old v2 appears ready without mangle'
        prove_old_filter_bypass(cfg, udp, 29010 + index * 10)
        fw.apply(cfg, 'check')
        assert fw.state(cfg) == before, 'Check changed live namespace rules'
        fw.apply(cfg, 'apply')
        assert fw.apply(cfg, 'status')['profile_tftp']['effective']
        fw.same_foreign(before, fw.state(cfg))
        assert_guard_layers(fw.state(cfg), 'open')
        for offset, port in enumerate((10015, cfg['signaling_port'])):
            with udp(cfg['bind_ipv4'], port) as receiver, udp('198.51.100.20', 29100 + index * 10 + offset) as peer:
                peer.sendto(b'new-tuple-after-upgrade', receiver.getsockname())
                must_block(receiver)
        if index == 0:
            fw.apply(cfg, 'remove')
            assert_guard_layers(fw.state(cfg), 'absent')
    opened = fw.state(cfg)
    fw.apply(cfg, 'apply')
    assert fw.state(cfg) == opened, 'Repeated apply changed exact state'

    def deliver(receiver, source, allowed):
        with udp(source, 0) as sender:
            sender.sendto(b'profile-policy-probe', receiver.getsockname())
            if allowed:
                assert receiver.recvfrom(4096)[0] == b'profile-policy-probe', 'Allowed profile packet lost'
            else:
                must_block(receiver)

    for port in (69, 20000, 20031):
        with udp(cfg['bind_ipv4'], port) as server:
            for phone in phones:
                deliver(server, phone, True)
            # Same-LAN devices, an inherited RTP source and public sources do
            # not gain TFTP permission from those other policies.
            for outsider in outsiders:
                deliver(server, outsider, False)
        with udp(other_destination, port) as other_server:
            deliver(other_server, phones[0], False)
        with udp('::1', port, socket.AF_INET6) as v6_server, udp('::1', 0, socket.AF_INET6) as v6_phone:
            v6_phone.sendto(b'no-ipv6-profile-access', v6_server.getsockname())
            must_block(v6_server)

    # Invalid port overlap and missing baseline protection are rejected before
    # any write even when a valid version 2 policy is already active.
    for family in (4, 6):
        for overlap in ('68:70', '19999:20000', '20031:20032'):
            invalid = copy.deepcopy(cfg)
            raw = invalid['raw_baseline']
            raw[f'ipv{family}_rules'].insert(0, fw.raw_rule(raw['chain'], raw['owner'], 'udp', overlap))
            try:
                fw.apply(invalid, 'apply')
            except ValueError:
                pass
            else:
                raise RuntimeError('Overlapping TFTP port policy accepted')
            assert fw.state(cfg) == opened, 'Rejected overlap changed rules'
        invalid = copy.deepcopy(cfg)
        raw = invalid['raw_baseline']
        raw[f'ipv{family}_rules'].remove(fw.raw_rule(raw['chain'], raw['owner'], 'udp', '69'))
        try:
            fw.apply(invalid, 'apply')
        except ValueError:
            pass
        else:
            raise RuntimeError('Missing TFTP DROP accepted')
        assert fw.state(cfg) == opened, 'Rejected missing protection changed rules'

    # Inventory changes require explicit closure. Neither the new list nor a
    # legacy tool may silently adopt the currently open version 2 rules.
    next_cfg = fw.migrate(legacy, {**policy, 'phone_ipv4': phones[:1]})
    for incompatible in (next_cfg, legacy):
        try:
            fw.apply(incompatible, 'apply')
        except RuntimeError:
            pass
        else:
            raise RuntimeError('Unreviewed inventory/downgrade transition accepted')
        assert fw.state(cfg) == opened, 'Rejected migration changed rules'
    fw.apply(cfg, 'rollback')
    assert_guard_layers(fw.state(cfg), 'closed')
    assert not fw.apply(cfg, 'status')['profile_tftp']['effective']
    for port in (69, 20000, 20031):
        with udp(cfg['bind_ipv4'], port) as server:
            deliver(server, phones[0], False)
    fw.apply(next_cfg, 'apply')
    with udp(cfg['bind_ipv4'], 69) as server:
        deliver(server, phones[0], True)
        deliver(server, phones[1], False)
    fw.apply(next_cfg, 'remove')
    fw.same_foreign(before, fw.state(legacy))
    assert_guard_layers(fw.state(legacy), 'absent')
    # Reinstalling the old policy is supported only after version 2 removal.
    fw.apply(legacy, 'apply')
    with udp(cfg['bind_ipv4'], 69) as server:
        deliver(server, phones[0], False)
    fw.apply(legacy, 'remove')
    assert_guard_layers(fw.state(legacy), 'absent')
    print('PASS: version 2 kernel TFTP source/destination/IPv6 gates, migration, rollback and inventory change')
    print('PASS: early mangle INPUT blocks preceding filter ACCEPT after v1/v2 upgrade')
