# Stable phone addresses with the existing DHCP service

Keep DHCP enabled on the Cisco phones and reserve one IPv4 address per physical
phone in the site's existing DHCP service. Both line buttons share that address.
A reservation follows the phone's MAC address when it moves between rooms or switch ports;
it does not change its extensions, room label or SIP credentials.

Use an unused address block outside the dynamic DHCP pool. Follow the site's
existing device grouping, and check reservations, active leases, static hosts
and ARP responses before assigning addresses. A block inside an existing larger
LAN prefix is an administrative grouping, not a new subnet or a security VLAN.
Keep the LAN's real mask, gateway and DNS settings. Do not narrow the phone's
mask to `/24` just because its address block has a distinct third octet.

All addresses and names below are examples. Keep the real MAC-to-extension,
IP-to-room and switch-port mapping in an ignored local file, such as
`.local/phone-addresses.md`. Never publish router exports or configuration backups.

## Identify the DHCP configuration owner

Before editing, identify the process that actually answers DHCPv4 and the
configuration source that owns its reservations. A router may still run dnsmasq
for DNS while a Kea HA pair serves DHCP. UCI/LuCI edits and a dnsmasq reload do
not update that pair. Use the site's private operations record and verify the
active service and a client's DHCP ACK; do not infer ownership from the router
hostname or from the presence of `/etc/config/dhcp` alone.

This project provisions phones and the PBX; it does not deploy DHCP or own the
site's DHCP generator. The examples below are configuration patterns, not
evidence that either service is installed or that a change has been applied.

## OpenWrt/dnsmasq configuration

Use this path only when dnsmasq is the confirmed DHCPv4 owner, including when
maintaining a legacy installation.

Back up `/etc/config/dhcp` privately and check `uci changes` before editing.
Update an existing host section when its MAC is already reserved; do not add a
second reservation for the same phone. Preserve its existing tag, including
the associated Cisco TFTP option 150.

For example, this section reserves a phone address outside a hypothetical
dynamic pool of `192.168.50.100–199`:

```text
config host 'desk_phone_01'
    option name 'desk-phone-01'
    option mac '02:00:00:00:00:01'
    option ip '192.168.50.20'
    option dns '1'
    option leasetime '12h'
    option tag 'cisco_phones'

config tag 'cisco_phones'
    option force '1'
    list dhcp_option '150,192.168.50.10'
```

The sample MAC is a placeholder. Use the MAC displayed by the real phone and
confirm it against the existing inventory. `dns '1'` makes the reserved name
available in local DNS even while the phone is offline. The 12-hour lease
renews normally; expiration does not remove the persistent reservation.

After reviewing the changed sections, commit any UCI edits and reload dnsmasq:

```sh
uci commit dhcp
/etc/init.d/dnsmasq reload
```

Do not restart the entire network or change the switch VLAN merely to update
reservations. Reloading dnsmasq does not force every active phone to immediately
abandon its old lease. Arrange a normal phone restart or an individually verified
PoE cycle after the server configuration is ready. Offline phones acquire their
reserved address when they reconnect.

## Kea HA configuration

When Kea serves DHCP, use the site's maintained DHCP configuration generator and
its coordinated update procedure. Keep its location and real input values in
the private operations record rather than copying a second generator here.

1. Record the current HA roles and state. Save the current configuration and
   active lease export privately; check that the pair is healthy before a routine
   reservation change. Preserve existing reservations for disconnected phones.
2. Update the canonical reservation input for the verified phone MAC, retaining
   its client-identifier policy, reserved address, hostname and lease duration
   unless those are the intended changes. Preserve the actual LAN prefix/mask,
   gateway and all advertised DNS servers. If UCI is an input to this generator,
   editing UCI is only the input step, not a completed deployment.
3. Preserve the phone-specific option 150 assignment and its forced delivery.
   The equivalent Kea option/class must reach the same intended phone even if
   the client does not request that option; do not turn it into a global TFTP
   setting or drop it while translating a dnsmasq tag. The value remains the
   reachable TFTP node's IPv4 address.
4. Generate and validate configurations for every member of the configured HA
   topology, including any passive backup. Review the complete diff and apply
   it through that topology's existing procedure, preserving peer roles,
   authentication and service state. Do not apply only to the currently active
   node or start an independent DHCP server to test the change.
5. Do not replace, clear or reseed lease databases for a reservation update.
   Keep live leases and the HA exchange intact. Coordinate any changed phone
   address with the server migration below, then renew only the selected phone
   after its accepted profile and bounded TFTP access are ready.
6. Verify that the DHCP ACK has the intended address, real mask, gateway, DNS
   list, lease duration and option 150. Confirm the new configuration on all
   peers and recheck HA health. Check the selected phone's boot, TFTP retrieval
   and both line registrations; a generated configuration alone proves none of
   these live results.

## Coordinate the server migration

An IP change needs more than a DHCP reservation in this deployment:

1. Update the private site's `phone_cidrs` and regenerate/review the Asterisk
   `pjsip.conf` ACLs and transport `local_net` entries. Preserve softphone,
   Home Assistant and external-provider settings.
2. Update the matching phone's `ipv4` in private `gateway.json`. Keep its ID,
   line numbers, room, credentials, callback ports and application paths intact.
3. Update the persistent host firewall and its running rules: internal SIP/RTP,
   the external-SIP guard's inherited phone RTP sources, and each phone's exact
   HTTP callback source. Updating only one of these can produce working
   registration with silent audio or broken phone applications.
4. Replace the relevant Kubernetes configuration Secrets and roll out their
   workloads. Preserve the deployed images, persistent state and other settings.
   A transport `local_net` change needs an Asterisk restart; an ordinary reload
   does not apply every transport setting.
5. If the phone needs provisioning at boot, open a bounded TFTP window for the
   affected old/new addresses, verify the served files, and then restart the
   selected phone. Close the temporary window after validation.

The CLI and Home Assistant use the gateway's stable phone IDs. Updating the
gateway's IP mapping preserves their relationship to each phone; there is no
need to recreate a device solely because its DHCP address changed.

## Verify and recover

Check all reservations even if only some phones are plugged in:

- Exactly one reservation per inventoried MAC, with unique names and addresses.
- Forward and reverse DNS match the reserved address and name.
- The effective DHCP configuration still assigns the expected phone-specific
  TFTP option: the dnsmasq tag or its reviewed Kea option/class equivalent.
- Connected phones have the new lease, correct LAN mask, gateway and TFTP server.
- Both lines register in `pjsip show contacts` at the new IP. An offline phone
  cannot pass a live registration test until it is connected.
- Phone web/SSH diagnostics, the CLI and Home Assistant still reach the same
  device. A harmless phone-app screen request verifies the HTTP callback path.
- In an attended test, confirm two-way audio with extension `600`.
- Old phone addresses no longer remain in active PBX or gateway source ACLs.
  Unrelated clients may still legitimately use addresses in the former pool.

Keep the pre-change DHCP configuration, configuration Secrets and exact owned
firewall rules until validation finishes. To roll back, restore only the owned
phone configuration through its current DHCP owner and matching server mappings,
then renew the affected phones' leases. For Kea HA, coordinate the configuration
rollback across all peers and retain the current lease databases.
Do not flush the shared firewall or overwrite unrelated router edits with an
old full-system snapshot.

See the [deployment runbook](k3s-deployment.md),
[phone-control guide](phone-control.md) and
[external SIP firewall guide](external-firewall.md) for the surrounding setup.
