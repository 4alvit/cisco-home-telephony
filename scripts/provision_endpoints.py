#!/usr/bin/env python3
"""Add local SIP endpoints to a decoded Asterisk configuration snapshot.

This offline renderer preserves all bytes outside its two managed blocks, except
the optional dial-audio.json local_lines list is reconciled and JSON may be
reformatted. Its other values are preserved. Keep
--secrets at one stable private path across candidate output directories; it is
required even when --config-dir is a previous output. An empty softphones list
removes the owned blocks while retaining saved credentials. The source snapshot
must contain all included configuration files, as regular files in one directory.
"""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import softphones


MARKER = b'managed SIP endpoints (provision_endpoints.py)'
BEGIN = b'; BEGIN ' + MARKER + b'\n'
END = b'; END ' + MARKER + b'\n'
TARGETS = ('pjsip.conf', 'extensions.conf')
SECTION = re.compile(r'^\s*\[([^]\r\n]+)\][^\r\n]*(?:\r?\n|\Z)', re.M)
DIAL_EXTENSION = re.compile(r'^\s*exten\s*(?:=>|=)\s*(2[0-9]{2})(?:/[^,\r\n]+)?\s*,', re.M)


def regular_file(path):
    """Reject links and special files before opening or replacing a path."""
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError('input and output files must be regular files, never symlinks')


def directory(path):
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError('input, output and parent directories must not be symlinks')


def read_bytes(path):
    regular_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('input files must be regular files')
        return stream.read()


def text(data):
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Asterisk configuration files must use UTF-8') from None


def strip_managed(data):
    """Remove one exact owned block, rejecting partial or ambiguous markers."""
    if MARKER not in data:
        return data, None
    if data.count(MARKER) != 2 or data.count(BEGIN) != 1 or data.count(END) != 1:
        raise ValueError('malformed or duplicate managed endpoint markers')
    start, stop = data.index(BEGIN), data.index(END)
    if stop < start or (start and data[start - 1:start] != b'\n') or data[stop - 1:stop] != b'\n':
        raise ValueError('malformed managed endpoint marker boundaries')
    return data[:start] + data[stop + len(END):], data[start + len(BEGIN):stop]


def sections(data):
    value = text(data)
    headers = list(SECTION.finditer(value))
    return [(header.group(1).strip(), value[header.end():headers[i + 1].start() if i + 1 < len(headers) else len(value)])
            for i, header in enumerate(headers)]


def old_credentials(block):
    """Read credentials only from well-formed, explicitly owned auth sections."""
    if block is None:
        return {}
    result, endpoints, aors = {}, set(), set()
    for name, body in sections(block):
        kinds = re.findall(r'^type=(\w+)\s*$', body, re.M)
        auth = re.fullmatch(r'auth-softphone-(2[0-9]{2})', name)
        if auth and kinds == ['auth']:
            extension = auth.group(1)
            passwords = re.findall(r'^password=([^\r\n]*)$', body, re.M)
            users = re.findall(r'^username=([^\r\n]*)$', body, re.M)
            if (extension in result or len(passwords) != 1 or users != [extension]
                    or not re.fullmatch(softphones.SECRET_PATTERN, passwords[0])):
                raise ValueError('invalid authentication in owned endpoint block')
            result[extension] = passwords[0]
        elif re.fullmatch(r'2[0-9]{2}', name) and kinds in (['endpoint'], ['aor']):
            found = endpoints if kinds == ['endpoint'] else aors
            if name in found:
                raise ValueError('duplicate section in owned endpoint block')
            if kinds == ['endpoint'] and f'set_var={softphones.MARKER}\n' not in body:
                raise ValueError('owned endpoint is missing its client marker')
            found.add(name)
        else:
            raise ValueError('unexpected section in owned endpoint block')
    if not result or set(result) != endpoints or endpoints != aors:
        raise ValueError('owned endpoint, auth and AOR sets must match')
    return result


def validate_site(site):
    if (not isinstance(site, dict) or not {'node_ipv4', 'softphones'} <= set(site)
            or set(site) - {'node_ipv4', 'softphones', 'gateway'}):
        raise ValueError('endpoints JSON requires node_ipv4 and softphones, with optional gateway')
    gateway = site.get('gateway', {'enabled': False})
    if not isinstance(gateway, dict) or set(gateway) != {'enabled'} or not isinstance(gateway['enabled'], bool):
        raise ValueError('gateway must contain only a boolean enabled field')
    try:
        address = ipaddress.IPv4Address(site['node_ipv4']) if isinstance(site['node_ipv4'], str) else None
    except ipaddress.AddressValueError:
        address = None
    if address is None or not any(address in network for network in softphones.PRIVATE):
        raise ValueError('node_ipv4 must be an explicit private IPv4 address')
    return softphones.validate(site)


def managed(content):
    return BEGIN + content + END


def reconcile_dial_audio(data, previous, clients):
    """Allow owned endpoints to use existing audio services without replacing them."""
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate dial-audio JSON field')
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError('non-finite dial-audio JSON value')

    try:
        value = json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        path = Path(__file__).resolve().parents[1] / 'containers/asterisk/dial_audio.py'
        spec = importlib.util.spec_from_file_location('endpoint_dial_audio', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.validate(value)
        lines = value['local_lines']
        if (len(set(lines)) != len(lines)
                or any(not re.fullmatch(r'2[0-9]{2}', line) for line in lines)):
            raise ValueError('invalid local line list')
        wanted = [client['extension'] for client in clients]
        # Keep retained lines in their original order, including an extension
        # that was already allowed before its first managed endpoint render.
        updated = [line for line in lines if line not in previous or line in wanted]
        updated.extend(line for line in wanted if line not in updated)
        if updated == lines:
            return data
        value['local_lines'] = updated
        module.validate(value)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('invalid dial-audio.json; require valid services and a nonempty unique local line list') from None
    return (json.dumps(value, indent=2) + '\n').encode()


def prepare(configs, site, secret_path):
    """Validate the full candidate in memory, without changing any files."""
    clients = validate_site(site)
    if not all(name in configs for name in TARGETS):
        raise ValueError('snapshot requires pjsip.conf and extensions.conf')
    result, owned = dict(configs), {}
    for name, data in configs.items():
        if name in TARGETS:
            result[name], owned[name] = strip_managed(data)
        elif MARKER in data:
            raise ValueError('managed endpoint markers are only allowed in the two target files')
    if (owned['pjsip.conf'] is None) != (owned['extensions.conf'] is None):
        raise ValueError('managed endpoint markers must occur in both target files')
    previous = old_credentials(owned['pjsip.conf'])
    if owned['extensions.conf'] is not None:
        routes = DIAL_EXTENSION.findall(text(owned['extensions.conf']))
        if len(routes) != len(previous) or set(routes) != set(previous):
            raise ValueError('owned dialplan and endpoint sets must match')
    expected = {client['extension'] for client in clients}
    reserved = expected | set(previous)
    if 'dial-audio.json' in result:
        result['dial-audio.json'] = reconcile_dial_audio(result['dial-audio.json'], previous, clients)
    unmanaged_passwords = {}
    for name, data in result.items():
        if not name.endswith('.conf'):
            continue
        value = text(data)
        for section, body in sections(data):
            if section in reserved or section in {'auth-softphone-' + extension for extension in reserved}:
                raise ValueError('unmanaged endpoint or authentication section collides with a managed extension')
        if reserved & set(DIAL_EXTENSION.findall(value)):
            raise ValueError('unmanaged dialplan route collides with a managed extension')
        for password in re.findall(r'^\s*password\s*=\s*([^\r\n]*)', value, re.M):
            unmanaged_passwords[str(len(unmanaged_passwords))] = password
    for transport in {client['transport'] for client in clients}:
        matches = [body for name, body in sections(result['pjsip.conf']) if name == 'transport-' + transport]
        if (len(matches) != 1 or not re.search(r'^\s*type\s*=\s*transport\s*$', matches[0], re.M)
                or not re.search(r'^\s*protocol\s*=\s*' + transport + r'\s*$', matches[0], re.M)):
            raise ValueError('each client transport must already exist in pjsip.conf')
    home = list(re.finditer(rb'^[ \t]*\[home\][ \t]*(?:;[^\r\n]*)?\r?\n', result['extensions.conf'], re.M))
    if clients and len(home) != 1:
        raise ValueError('extensions.conf requires one unambiguous existing [home] header')
    passwords = softphones.prepare_secrets(clients, secret_path.parent, unmanaged_passwords, path=secret_path)
    if any(passwords.get(extension) != password for extension, password in previous.items()):
        raise ValueError('owned credentials must match the stable secrets file; refusing to rotate them')
    if clients:
        # Additive clients can gain gateway controls independently of the old
        # site manifest. Always honor per-extension DND; a missing DB key is off.
        render_site = dict(site, gateway={'enabled': True})
        rendered = [softphones.render_client(render_site, client, passwords[client['extension']]) for client in clients]
        result['pjsip.conf'] = managed(''.join(pair[0] for pair in rendered).encode()) + result['pjsip.conf']
        point = home[0].end()
        data = result['extensions.conf']
        result['extensions.conf'] = data[:point] + managed(''.join(pair[1] for pair in rendered).encode()) + data[point:]
    return result, passwords


def private_write(path, data):
    """Replace a candidate file atomically without following its final path."""
    descriptor, temporary = tempfile.mkstemp(prefix='.endpoints-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def generate(config_dir, site, out, secret_path):
    source, output, secrets = Path(config_dir), Path(out), Path(secret_path)
    directory(source)
    directory(output.parent)
    directory(secrets.parent)
    source_resolved, output_resolved, secret_resolved = source.resolve(), output.resolve(), secrets.resolve()
    if (source_resolved == output_resolved or source_resolved in output_resolved.parents
            or output_resolved in source_resolved.parents):
        raise ValueError('source and output must be separate, non-nested directories')
    if (source_resolved in secret_resolved.parents or output_resolved in secret_resolved.parents
            or secret_resolved in (source_resolved, output_resolved)):
        raise ValueError('stable secrets must be outside the source and output directories')
    configs = {path.name: read_bytes(path) for path in sorted(source.iterdir())}
    if output.exists() or output.is_symlink():
        directory(output)
        for path in output.iterdir():
            regular_file(path)
            if path.name not in configs:
                raise ValueError('output contains a file that is absent from the source snapshot')
    if secrets.exists() or secrets.is_symlink():
        regular_file(secrets)
        if secrets.stat().st_mode & 0o077:
            raise ValueError('stable secrets file must be private (mode 0600)')
    candidate, passwords = prepare(configs, site, secrets)
    # Everything above is read-only. Save credentials before writing configs that
    # use them, so an interrupted candidate build never loses a generated secret.
    private_write(secrets, (json.dumps(passwords, indent=2) + '\n').encode())
    output.mkdir(mode=0o700, exist_ok=True)
    output.chmod(0o700)
    for name, data in candidate.items():
        private_write(output / name, data)
    return len(site['softphones']), len(candidate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', required=True, help='decoded complete current Asterisk config snapshot')
    parser.add_argument('--endpoints', required=True, help='private JSON with node_ipv4, gateway and softphones')
    parser.add_argument('--secrets', required=True, help='stable private JSON file outside source/output; created on first run')
    parser.add_argument('--out', required=True, help='separate private candidate configuration directory')
    args = parser.parse_args()
    try:
        endpoint_path = Path(args.endpoints)
        if endpoint_path.resolve() == Path(args.secrets).resolve():
            raise ValueError('endpoint input and stable secrets must use separate files')
        output_path = Path(args.out).resolve()
        if endpoint_path.resolve() == output_path or output_path in endpoint_path.resolve().parents:
            raise ValueError('endpoint input must be outside the output directory')
        regular_file(endpoint_path)
        if endpoint_path.stat().st_mode & 0o077:
            raise ValueError('endpoint input must be private (mode 0600)')
        site = json.loads(read_bytes(endpoint_path))
        clients, files = generate(args.config_dir, site, args.out, args.secrets)
    except (ValueError, OSError) as error:
        parser.exit(2, f'Cannot prepare endpoint configuration: {error}\n')
    print(f'Prepared {clients} managed SIP endpoints across {files} configuration files.')


if __name__ == '__main__':
    main()
