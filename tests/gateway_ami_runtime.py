"""Verify gateway AMI privileges and DND rejection in the isolated PBX fixture."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    container, path = sys.argv[1:]
    secret = json.loads(Path(path).read_text())
    actions = [
        {"Action": "Login", "ActionID": "login", "Username": secret["username"], "Secret": secret["secret"], "Events": "on"},
        {"Action": "CoreStatus", "ActionID": "core-status"},
        {"Action": "DBPut", "ActionID": "put", "Family": "cisco-dnd", "Key": "201", "Val": "1"},
        {"Action": "DBGet", "ActionID": "get", "Family": "cisco-dnd", "Key": "201"},
        {"Action": "CoreShowChannels", "ActionID": "channels"},
        {"Action": "Originate", "ActionID": "direct", "Channel": "Local/201@home", "Application": "Wait", "Data": "0.1", "Async": "false", "Timeout": "5000"},
        {"Action": "Originate", "ActionID": "group", "Channel": "Local/620@home", "Application": "Wait", "Data": "0.1", "Async": "false", "Timeout": "5000"},
        {"Action": "DBDel", "ActionID": "delete", "Family": "cisco-dnd", "Key": "201"},
        {"Action": "Command", "ActionID": "command", "Command": "core show version"},
        {"Action": "Hangup", "ActionID": "hangup", "Channel": "PJSIP/nonexistent-synthetic-channel"},
        {"Action": "Logoff", "ActionID": "logoff"},
    ]
    # Originate can return before the channel's final asynchronous Hangup event.
    # Keep the AMI session open until both events arrive, rather than racing Logoff.
    client = r'''
import json, socket, sys, time
actions = json.load(sys.stdin)
wire = ''.join(''.join(f'{key}: {value}\r\n' for key,value in action.items())+'\r\n' for action in actions[:-1])
data = b''
with socket.create_connection(('127.0.0.1',5038),timeout=3) as sock:
    sock.settimeout(.2)
    sock.sendall(wire.encode())
    deadline = time.monotonic()+15
    expected = {a['ActionID'] for a in actions[:-1]}
    while time.monotonic()<deadline and len(data)<1000000:
        try:
            part=sock.recv(65536)
        except socket.timeout:
            continue
        if not part: break
        data+=part
        frames=[dict(line.split(': ',1) for line in chunk.splitlines() if ': ' in line)
                for chunk in data.decode().replace('\r\n','\n').split('\n\n')[:-1]]
        replied={f.get('ActionID') for f in frames if 'Response' in f}
        done=all(any(f.get('Event')=='Hangup' and f.get('Channel','').startswith('Local/'+ext+'@home-')
                     and f.get('Cause')=='17' for f in frames) for ext in ('201','620'))
        if expected<=replied and done: break
    sock.sendall(b'Action: Logoff\r\nActionID: logoff\r\n\r\n')
sys.stdout.write(data.decode())
'''
    result = subprocess.run(["docker", "exec", "-i", container, "python3", "-c", client],
        input=json.dumps(actions), text=True, capture_output=True, timeout=22)
    if result.returncode:
        raise RuntimeError("Gateway AMI transport failed; raw output omitted")
    frames = []
    for chunk in result.stdout.replace('\r\n', '\n').split('\n\n'):
        frames.append(dict(line.split(': ', 1) for line in chunk.splitlines() if ': ' in line))
    responses = {f['ActionID']: f for f in frames if 'ActionID' in f and 'Response' in f}
    for action in ('login', 'core-status', 'put', 'get', 'channels', 'delete'):
        if responses.get(action, {}).get('Response') != 'Success':
            raise RuntimeError(f'Gateway AMI {action} failed; raw output omitted')
    if not all(responses['core-status'].get(key) for key in ('CoreStartupDate', 'CoreStartupTime')):
        raise RuntimeError('Gateway CoreStatus restart fingerprint is missing')
    if not any(f.get('Event') == 'DBGetResponse' and f.get('Val') == '1' for f in frames):
        raise RuntimeError('Gateway DBGet value mismatch')
    for action in ('direct', 'group'):
        if responses.get(action, {}).get('Response') != 'Error':
            raise RuntimeError('DND call was not rejected')
    for extension in ('201', '620'):
        if not any(f.get('Event') == 'Hangup' and f.get('Channel', '').startswith(f'Local/{extension}@home-')
                   and f.get('Cause') == '17' for f in frames):
            raise RuntimeError(f'DND {extension} did not yield user-busy hangup')
    if responses.get('command', {}).get('Message') != 'Permission denied':
        raise RuntimeError('Gateway unexpectedly permits CLI commands')
    if responses.get('hangup', {}).get('Message') == 'Permission denied':
        raise RuntimeError('Gateway cannot hang up calls')
    print('Gateway AMI passed: restart fingerprint, DBPut/Get/Del, channel query, direct/group DND busy, Hangup, CLI denied.')


if __name__ == '__main__':
    main()
