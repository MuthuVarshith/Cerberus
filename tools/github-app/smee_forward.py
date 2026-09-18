"""
Forward GitHub webhook deliveries from a smee.io channel to a local URL.

A standard-library stand-in for `npx smee-client`, so no Node.js install is needed:

    python smee_forward.py https://smee.io/<channel> http://127.0.0.1:8000/webhook

smee.io relays each delivery as a Server-Sent Event whose data is a JSON object: the
parsed request body under "body" and the original request headers as the other keys.
The body is re-serialised compactly, as smee-client does, so GitHub's HMAC signature
still verifies on the receiving side.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

FORWARDED_HEADERS = (
    "x-github-event", "x-github-delivery", "x-hub-signature-256", "x-hub-signature",
    "x-github-hook-id", "x-github-hook-installation-target-id", "x-github-hook-installation-target-type",
    "user-agent",
)


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


def forward(target: str, data: dict) -> None:
    body = json.dumps(data.get("body", {}), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    for name in FORWARDED_HEADERS:
        if name in data:
            headers[name] = str(data[name])
    event = data.get("x-github-event", "?")
    action = (data.get("body") or {}).get("action", "")
    request = urllib.request.Request(target, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            log(f"forwarded {event}.{action} -> {response.status} {response.read(300).decode('utf-8', 'replace')}")
    except urllib.error.HTTPError as exc:
        log(f"forwarded {event}.{action} -> {exc.code} {exc.read(300).decode('utf-8', 'replace')}")
    except urllib.error.URLError as exc:
        log(f"could not reach {target}: {exc.reason} (is the Cerberus service running?)")


def listen(channel: str, target: str) -> None:
    request = urllib.request.Request(channel, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=90) as stream:
        log(f"connected to {channel}, forwarding to {target}")
        event, data_lines = "message", []
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line == "":
                if data_lines and event not in ("ready", "ping"):
                    try:
                        forward(target, json.loads("\n".join(data_lines)))
                    except ValueError:
                        log("skipped an event that was not JSON")
                event, data_lines = "message", []


def main() -> int:
    if len(sys.argv) != 3 or not sys.argv[1].startswith("https://smee.io/"):
        print(__doc__)
        return 2
    channel, target = sys.argv[1], sys.argv[2]
    while True:
        try:
            listen(channel, target)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # network drops are normal for long-lived streams; reconnect
            log(f"stream closed ({exc.__class__.__name__}); reconnecting in 3s")
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
