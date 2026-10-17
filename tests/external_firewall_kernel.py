"""Invoked only inside the disposable network-none NET_ADMIN test container."""
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
