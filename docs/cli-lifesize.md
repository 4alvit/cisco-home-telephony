# LifeSize Phone in the Cisco CLI

A first-generation LifeSize Phone can register directly with Asterisk over SIP.
It uses its own automation SSH interface, not Cisco XML/SEP profiles. Registration
alone does not add it to the Cisco gateway's inventory. Without the optional native
driver, selecting its number returns `line_not_in_cisco_gateway_inventory`.

Install `scripts/cisco_lifesize.py` beside `scripts/cisco.py` from the same revision.
Keep the other CLI modules installed. Merge the `lifesize` object from
[`cisco-cli.lifesize.example.json`](../deploy/cisco-cli.lifesize.example.json) into
your existing private `~/.config/cisco-home/cli.json`; this example is a fragment,
not a replacement for the gateway/token settings. Keep the file mode at `0600`.
Use your phone's registered extension as the dictionary key and its private IPv4
address as `host`. Do not put it in the Cisco SEP/MAC provisioning list.

The phone's `auto` account must already accept the configured SSH public key.
Use a dedicated `known_hosts_file` containing its verified host key. Relative
key paths are resolved beside the CLI configuration. The private key must be
owned by your user and have no group/other permissions. The known-hosts file
must be owned by your user and not writable by other users. Encrypted keys that
require a prompt or agent are not supported by this unattended driver.

For legacy firmware that requires RSA/SHA-1 SSH signatures, explicitly set
`legacy_rsa` to `true`. This changes only these phone connections, not global SSH
settings. After an address change, optional `host_key_alias` can retain the old
verified known-hosts label. No password is stored in the CLI configuration.

```sh
cisco 222                 # SIP registration, active calls and available commands
cisco 222 555             # Dial the local time service from the LifeSize speaker
cisco 222 call 777        # Dial the configured music service
cisco 222 777 --dry-run   # Validate without placing a call
cisco 222 hangup          # End the one observed call by its exact handle
cisco 222 directory      # Include the native phone alongside Cisco phones
```

Dialing still requires a fresh gateway inventory and a destination in its
`dial_allowlist`. Before controlling the phone, the driver reads its SIP username
and requires it to match the selected extension. It refuses dialing a busy or
unregistered phone, and refuses hangup if zero or multiple calls are present.
Local per-device locking prevents concurrent commands from this Mac. It cannot
lock out a person using the phone or another controller.

`accepted` means the phone acknowledged the command, not that the other party
answered or audio was heard. On a lost SSH response the CLI reports
`not_confirmed`; inspect `cisco 222` before trying again. There is no automatic
retry or durable idempotency support; mutation commands reject an explicit
`--idempotency-key`. Directory listings do not probe native phones and show
their reachability as `not checked`; selecting the number reads live state.

This driver currently supports status, directory, destinations, dial/call and
hangup. Cisco screen pushes, timers, intercom, hardware volume, voice sessions
and controlled `play` queues are not exposed for LifeSize. Music service numbers
can still be dialed normally. Installing this local driver does not create a
Home Assistant device or add LifeSize support to the gateway server.

Protocol reference: manufacturer's
[LifeSize Automation Command Line Interface](https://www.dekom.com/fileadmin/pdf/Lifesize_automation_command_line_interface_de.pdf),
`status call active`, `control call dial`, and `control call hangup`.
