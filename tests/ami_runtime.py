"""Exercise a real AMI socket in the isolated synthetic Docker fixture only."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    container, secret_path = sys.argv[1:]
    credentials = json.loads(Path(secret_path).read_text())
    actions = [
        {"Action": "Login", "ActionID": "login", "Username": credentials["username"], "Secret": credentials["secret"], "Events": "off"},
        {"Action": "CoreSettings", "ActionID": "settings"},
        {"Action": "PJSIPShowEndpoints", "ActionID": "endpoints"},
        {"Action": "Command", "ActionID": "denied", "Command": "core show version"},
        # An in-process Local channel only; cannot call a real phone or network.
        {"Action": "Originate", "ActionID": "originate", "Channel": "Local/600@home", "Application": "Wait", "Data": "0.1", "Async": "false", "Timeout": "1000"},
        {"Action": "Hangup", "ActionID": "hangup", "Channel": "PJSIP/nonexistent-synthetic-channel"},
        {"Action": "Logoff", "ActionID": "logoff"},
    ]
    wire = "".join("".join(f"{k}: {v}\r\n" for k, v in action.items()) + "\r\n" for action in actions)
    # Stream the synthetic secret over stdin, never through command arguments.
    result = subprocess.run(["docker", "exec", "-i", container, "timeout", "15", "bash", "-c",
        'exec 3<>/dev/tcp/127.0.0.1/5038; exec 4<&0; cat <&4 >&3 & sender=$!; cat <&3; wait "$sender"'],
        input=wire, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError("AMI fixture transport failed (response omitted)")
    frames = []
    for chunk in result.stdout.replace("\r\n", "\n").split("\n\n"):
        frame = {}
        for line in chunk.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                frame[key] = value
        frames.append(frame)
    responses = {f["ActionID"]: f for f in frames if "Response" in f and "ActionID" in f}
    for action in ("login", "settings", "endpoints", "originate"):
        if responses.get(action, {}).get("Response") != "Success":
            raise RuntimeError(f"AMI action {action} failed (response omitted)")
    if not any(f.get("Event") == "EndpointList" and f.get("ObjectName") == "201" for f in frames):
        raise RuntimeError("AMI did not discover synthetic endpoint 201")
    if responses.get("denied", {}).get("Message") != "Permission denied":
        raise RuntimeError("AMI account unexpectedly permits CLI Command")
    hangup = responses.get("hangup", {})
    if hangup.get("Response") != "Error" or hangup.get("Message") == "Permission denied":
        raise RuntimeError("AMI Hangup permission check failed")
    print("AMI runtime passed: discovery, CoreSettings, local Originate, Hangup permission, CLI denied.")


if __name__ == "__main__":
    main()
