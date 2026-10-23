"""Validate the local, firmware-free TFTP profile inputs without network access."""
import copy
import ipaddress
import os
from pathlib import Path
import re
import stat
import xml.etree.ElementTree as ET


RFC1918 = tuple(ipaddress.IPv4Network(value) for value in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 700_000


def _mac(value):
    if not isinstance(value, str):
        raise ValueError("Profile TFTP requires a valid unicast MAC")
    # Match provision.py's canonical naming and duplicate detection.
    mac = re.sub(r"[:-]", "", value).upper()
    if (not re.fullmatch(r"[0-9A-F]{12}", mac) or int(mac[:2], 16) & 1
            or mac == "000000000000"):
        raise ValueError("Profile TFTP requires a valid unicast MAC")
    return mac


def _address(value):
    if not isinstance(value, str):
        raise ValueError("Profile TFTP addresses must be explicit RFC1918 IPv4 literals")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError("Profile TFTP addresses must be explicit RFC1918 IPv4 literals") from None
    if not any(address in network for network in RFC1918):
        raise ValueError("Profile TFTP addresses must be explicit RFC1918 IPv4 literals")
    return address


def merge_addresses(inventory, address_map):
    """Join an exact version-1 MAC/address companion without modifying inventory.

    The companion schema is {"version": 1, "phones": [{"mac": ..., "ipv4": ...}]}.
    Evidence reports with additional metadata must be explicitly projected into
    that schema first. A companion cannot override an existing different IP.
    """
    if (not isinstance(inventory, dict) or not isinstance(inventory.get("phones"), list)
            or not 1 <= len(inventory["phones"]) <= 50):
        raise ValueError("Address merge requires an inventory of 1..50 phones")
    if (not isinstance(address_map, dict) or set(address_map) != {"version", "phones"}
            or type(address_map["version"]) is not int or address_map["version"] != 1
            or not isinstance(address_map["phones"], list)
            or len(address_map["phones"]) != len(inventory["phones"])):
        raise ValueError("Address map requires version 1 and exactly one row per inventoried phone")
    by_mac, addresses = {}, set()
    for row in address_map["phones"]:
        if not isinstance(row, dict) or set(row) != {"mac", "ipv4"}:
            raise ValueError("Each address map row requires exactly mac and ipv4")
        mac, address = _mac(row["mac"]), str(_address(row["ipv4"]))
        if mac in by_mac or address in addresses:
            raise ValueError("Address map MACs and IPv4 addresses must be unique")
        by_mac[mac] = address
        addresses.add(address)
    result, seen = copy.deepcopy(inventory), set()
    for phone in result["phones"]:
        if not isinstance(phone, dict):
            raise ValueError("Each inventoried phone must be an object")
        mac = _mac(phone.get("mac"))
        if mac in seen or mac not in by_mac:
            raise ValueError("Address map must match each inventory MAC exactly once")
        seen.add(mac)
        if "ipv4" in phone and str(_address(phone["ipv4"])) != by_mac[mac]:
            raise ValueError("Address map conflicts with an inventoried IPv4 address")
        phone["ipv4"] = by_mac[mac]
    if seen != set(by_mac):
        raise ValueError("Address map must match each inventory MAC exactly once")
    return result


def _subnets(site):
    """Use known masks to reject network/broadcast addresses, never as an ACL."""
    values = site.get("phone_cidrs", [])
    if not isinstance(values, list):
        raise ValueError("phone_cidrs must be a list")
    values = list(values)
    if "lan_cidr" in site:
        values.append(site["lan_cidr"])
    networks = []
    for value in values:
        if not isinstance(value, str) or "/" not in value:
            raise ValueError("Configured subnets must be explicit IPv4 CIDRs")
        try:
            networks.append(ipaddress.IPv4Network(value, strict=True))
        except ipaddress.AddressValueError:
            raise ValueError("Configured subnets must be explicit IPv4 CIDRs") from None
    return networks


def _host(address, networks):
    matching = [network for network in networks if address in network]
    if matching:
        network = max(matching, key=lambda item: item.prefixlen)
        if network.prefixlen < 31 and address in (network.network_address, network.broadcast_address):
            raise ValueError("Profile TFTP requires host addresses, not network/broadcast addresses")


def _inventory(site, inventory):
    if not isinstance(site, dict) or not isinstance(inventory, dict):
        raise ValueError("Profile TFTP requires site and inventory objects")
    node = _address(site.get("node_ipv4"))
    networks = _subnets(site)
    _host(node, networks)
    phones = inventory.get("phones")
    if not isinstance(phones, list) or not 1 <= len(phones) <= 50:
        raise ValueError("Profile TFTP inventory must contain 1..50 physical phones")
    seen_mac, seen_address, normalized = set(), set(), []
    for phone in phones:
        if (not isinstance(phone, dict) or phone.get("verified") is not True
                or phone.get("model") != "CP-7945G"):
            raise ValueError("Profile TFTP requires verified CP-7945G phones")
        mac = _mac(phone.get("mac"))
        if mac in seen_mac:
            raise ValueError("Profile TFTP requires unique valid unicast MACs")
        address = _address(phone.get("ipv4"))
        _host(address, networks)
        if address == node or address in seen_address:
            raise ValueError("Profile TFTP phone addresses must be unique and distinct from the node")
        load = phone.get("firmware_load")
        if not isinstance(load, str) or not re.fullmatch(r"SIP45\.[A-Za-z0-9-]+", load):
            raise ValueError("firmware_load must be the exact SIP45 load ID without .loads")
        seen_mac.add(mac)
        seen_address.add(address)
        normalized.append({"mac": mac, "ipv4": str(address), "firmware_load": load})
    return str(node), normalized


def policy(site, inventory):
    """Return only explicit inventoried phone hosts; never expand service CIDRs."""
    node, phones = _inventory(site, inventory)
    return {"version": 1, "bind_ipv4": node,
            "phone_ipv4": sorted(phone["ipv4"] for phone in phones)}


def _expected(phones):
    return {"SEP" + phone["mac"] + ".cnf.xml": phone for phone in phones}


def _xml(text):
    # NULs can make Expat auto-detect UTF-16/32 after a permissive UTF-8 decode,
    # bypassing a textual declaration check. This input contract is UTF-8 only.
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ValueError("Profile TFTP XML must not declare a DOCTYPE or entities")
    try:
        return ET.fromstring(text)
    except (ET.ParseError, ValueError):
        raise ValueError("Profile TFTP requires valid XML") from None


def _field(root, path, value):
    elements = root.findall(path)
    # Reject duplicate or misplaced spellings as well as a wrong value.
    if (len(elements) != 1 or len(list(root.iter(path.rsplit("/", 1)[-1]))) != 1
            or elements[0].text != value or elements[0].attrib or len(elements[0])):
        raise ValueError("Profile TFTP XML has a missing, duplicate or mismatched required field")


def validate_profiles(files, site, inventory):
    """Validate an exact profile mapping and return it in stable filename order."""
    _, phones = _inventory(site, inventory)
    expected = _expected(phones)
    if not isinstance(files, dict) or set(files) != set(expected) | {"dialplan.xml"}:
        raise ValueError("Profile TFTP requires exactly the inventoried SEP profiles and dialplan.xml")
    total = 0
    for name, text in files.items():
        if not isinstance(text, str):
            raise ValueError("Profile TFTP XML must be UTF-8 text")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError:
            raise ValueError("Profile TFTP XML must be UTF-8 text") from None
        if not 0 < size <= MAX_FILE_BYTES:
            raise ValueError("Profile TFTP file exceeds its size limit or is empty")
        total += len(name.encode("utf-8")) + size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Profile TFTP inputs exceed the safe Secret size")
        root = _xml(text)
        if name == "dialplan.xml":
            if root.tag != "DIALTEMPLATE":
                raise ValueError("dialplan.xml must have a DIALTEMPLATE root")
        else:
            if root.tag != "device":
                raise ValueError("Phone profiles must have a device root")
            _field(root, "./loadInformation", expected[name]["firmware_load"])
            _field(root, "./vendorConfig/g722CodecSupport", "2")
            _field(root, "./sipProfile/dialTemplate", "dialplan.xml")
    return {name: files[name] for name in sorted(files)}


def _directory(path):
    """Open each directory component with NOFOLLOW, including input ancestors."""
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if ".." in path.parts:
        raise ValueError("Profile TFTP input must not contain parent traversal")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_profiles(directory, site, inventory):
    """Read a bounded flat directory; reject links, extra files and changing inputs."""
    _, phones = _inventory(site, inventory)
    expected = set(_expected(phones)) | {"dialplan.xml"}
    fd = None
    try:
        fd = _directory(directory)
        initial = _identity(os.fstat(fd))
        if set(os.listdir(fd)) != expected:
            raise ValueError("Profile TFTP directory contains missing or unexpected entries")
        files, identities = {}, {}
        for name in sorted(expected):
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_FILE_BYTES:
                raise ValueError("Profile TFTP inputs must be bounded regular files without symlinks")
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if _identity(before) != _identity(opened):
                    raise ValueError("Profile TFTP input changed during validation")
                content = source.read(MAX_FILE_BYTES + 1)
                if len(content) != before.st_size or _identity(os.fstat(source.fileno())) != _identity(before):
                    raise ValueError("Profile TFTP input changed during validation")
            identities[name] = _identity(before)
            files[name] = content.decode("utf-8")
        if set(os.listdir(fd)) != expected or _identity(os.fstat(fd)) != initial:
            raise ValueError("Profile TFTP directory changed during validation")
        if any(_identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != identity
               for name, identity in identities.items()):
            raise ValueError("Profile TFTP input changed during validation")
        return validate_profiles(files, site, inventory)
    except (OSError, UnicodeError):
        raise ValueError("Profile TFTP inputs must be readable UTF-8 regular files without symlink ancestors") from None
    finally:
        if fd is not None:
            os.close(fd)
