# Deployment inputs

Copy `site.example.json` to `.local/site.json`; all example values are fictitious and must be
replaced with the private settings of your own cluster. Image placeholders intentionally
fail validation. Do not put real credentials here.

`scripts/render_k3s.py` produces private `.local/rendered/core.yaml` and, only with
`--with-tftp`, `tftp.yaml`. These are JSON, a valid YAML subset accepted by kubectl.
The core contains a Namespace, immutable versioned Secret, and single Asterisk
Deployment. No Service, Ingress, LoadBalancer, DHCP server, or external tunnel is created.
The TFTP file contains an immutable Secret and an expiring one-hour Job.

`firewall.nft.example` is a reviewed template, never automatically applied.
See [the deployment guide](../docs/k3s-deployment.md).

## Plain HTTP registry prerequisite

A k3s `registries.yaml` mirror configures **containerd pulls on that node**; it
never configures the Docker daemon doing a build/push. If you deliberately use a
trusted LAN HTTP registry, the Linux Docker builder also needs the exact
`REGISTRY_HOST:PORT` in `insecure-registries` in its daemon configuration (normally
`/etc/docker/daemon.json`). Merge this one entry with the existing JSON; do not
replace unrelated settings or allow an entire CIDR. Validate the JSON and arrange
a maintenance window before restarting Docker, since a restart can affect other
containers. For Colima/Desktop, configure that specific VM/daemon through its
supported settings. Prefer a TLS registry when possible. These scripts neither
weaken registry security nor restart any daemon automatically.
