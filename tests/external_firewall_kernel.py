"""Invoked only inside the disposable network-none NET_ADMIN test container."""
import copy
import shlex
import socket


def exercise(fw, config):
    raw = config['raw_baseline']
    for family in (4, 6):
        body = ['*raw', ':PREROUTING ACCEPT [0:0]', ':OUTPUT ACCEPT [0:0]',
                ':' + raw['chain'] + ' - [0:0]', ':FOREIGN_RAW - [0:0]',
                shlex.join(fw.raw_hook(config)), '-A PREROUTING -j FOREIGN_RAW',
                '-A FOREIGN_RAW -p tcp -m tcp --dport 443 -j RETURN']
        body += [shlex.join(rule) for rule in raw[f'ipv{family}_rules']]
        fw.restore(family, '\n'.join(body + ['COMMIT', '']))
    for address in (config['bind_ipv4'], '198.51.100.20', '10.20.30.40'):
        fw.run(['ip', 'address', 'add', address + '/32', 'dev', 'lo'])
    before = fw.state(config)
    fw.apply(config, 'check')
    fw.apply(config, 'apply')
    fw.same_foreign(before, fw.state(config))

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
                if 'closed' not in fw.state(config)['filter4']['states']:
                    raise RuntimeError('Remove failure did not close signaling')
                remote.sendto(b'closed-even-if-established', pbx.getsockname())
                must_block(pbx)
                fw.apply(config, 'apply')
    with udp('::1', 10011, socket.AF_INET6) as ipv6_pbx, udp('::1', 25000, socket.AF_INET6) as ipv6_peer:
        ipv6_peer.sendto(b'ipv6-blocked', ipv6_pbx.getsockname())
        must_block(ipv6_pbx)
    fw.apply(config, 'rollback')
    with udp(config['bind_ipv4'], 10012) as pbx, udp('10.20.30.40', 25001) as home_assistant:
        home_assistant.sendto(b'old-exact-source', pbx.getsockname())
        if pbx.recvfrom(4096)[0] != b'old-exact-source':
            raise RuntimeError('Existing local RTP exception changed')
    fw.apply(config, 'remove')
    after = fw.state(config)
    fw.same_foreign(before, after)
    if after['filter4']['exists'] or after['filter6']['exists']:
        raise RuntimeError('Owned chains remain after removal')
    print('PASS: kernel parser, UDP reply tuples, unsolicited/IPv6 drops, source preservation, safe rollback/removal')
    exercise_profiles(fw, config, udp, must_block)


def exercise_profiles(fw, legacy, udp, must_block):
    """Real datagrams through both netfilter tables; all addresses stay local."""
    phones = ['192.168.50.21', '192.168.50.22']
    other_destination = '192.168.50.11'
    outsiders = ['192.168.50.23', '192.168.60.22', '10.20.30.40', '198.51.100.20']
    for address in phones + [other_destination] + outsiders[:2]:
        fw.run(['ip', 'address', 'add', address + '/32', 'dev', 'lo'])
    policy = {'version': 1, 'bind_ipv4': legacy['bind_ipv4'], 'phone_ipv4': phones}
    cfg = fw.migrate(legacy, policy)
    # Migration is exercised from the previously deployed external-only policy.
    fw.apply(legacy, 'apply')
    before = fw.state(cfg)
    assert 'legacy_open' in before['raw4']['states']
    assert not fw.apply(cfg, 'status')['profile_tftp']['effective']
    fw.apply(cfg, 'check')
    assert fw.state(cfg) == before, 'Check changed live namespace rules'
    fw.apply(cfg, 'apply')
    assert fw.apply(cfg, 'status')['profile_tftp']['effective']
    fw.same_foreign(before, fw.state(cfg))
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
    assert not fw.state(legacy)['filter4']['exists']
    # Reinstalling the old policy is supported only after version 2 removal.
    fw.apply(legacy, 'apply')
    with udp(cfg['bind_ipv4'], 69) as server:
        deliver(server, phones[0], False)
    fw.apply(legacy, 'remove')
    print('PASS: version 2 kernel TFTP source/destination/IPv6 gates, migration, rollback and inventory change')
