# Stable phone addresses with OpenWrt and dnsmasq

Keep DHCP enabled on the Cisco phones and reserve one IPv4 address per physical
phone on the existing router. Both line buttons share that address. A reservation
follows the phone's MAC address when it moves between rooms or switch ports;
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

## Router configuration

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
- The generated dnsmasq host entry still assigns the expected TFTP tag.
- Connected phones have the new lease, correct LAN mask, gateway and TFTP server.
- Both lines register in `pjsip show contacts` at the new IP. An offline phone
  cannot pass a live registration test until it is connected.
- Phone web/SSH diagnostics, the CLI and Home Assistant still reach the same
  device. A harmless phone-app screen request verifies the HTTP callback path.
- In an attended test, confirm two-way audio with extension `600`.
- Old phone addresses no longer remain in active PBX or gateway source ACLs.
  Unrelated clients may still legitimately use addresses in the former pool.

Keep the pre-change router sections, configuration Secrets and exact owned
firewall rules until validation finishes. To roll back, restore those phone
sections and matching server mappings, then renew the affected phones' leases.
Do not flush the shared firewall or overwrite unrelated router edits with an
old full-system snapshot.

See the [deployment runbook](k3s-deployment.md),
[phone-control guide](phone-control.md) and
[external SIP firewall guide](external-firewall.md) for the surrounding setup.
