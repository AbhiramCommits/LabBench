#!/usr/bin/env python3
"""Minimal WinRM/WSMan stub service.

Speaks enough of the WSMan SOAP protocol (``/wsman``) for pywinrm's
``Session.run_cmd`` / ``run_ps`` / ``put_file`` code paths to work unchanged.
Commands are executed with a real shell inside the container -- on the
``win-host`` image ``powershell``/``powershell.exe`` are symlinks to ``pwsh``,
so PowerShell scripts arrive as ``-EncodedCommand`` and run for real.

This is intentionally a *stub*: it does not implement NTLM/Kerberos, message
encryption, or full WSMan enumeration. Use transport ``basic`` or ``plaintext``.
It is a genuine component with real process execution and real timing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "wsman": "http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd",
    "rsp": "http://schemas.microsoft.com/wbem/wsman/1/windows/shell",
    "x": "http://schemas.xmlsoap.org/ws/2004/09/transfer",
}
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)

SHELL_URI = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell"
CMD_RESOURCE = f"{SHELL_URI}/cmd"
DONE = f"{SHELL_URI}/CommandState/Done"


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def envelope(action: str, body_inner: str, relates_to: str | None = None) -> bytes:
    relates = (
        f"<wsa:RelatesTo>{relates_to}</wsa:RelatesTo>"
        if relates_to
        else ""
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:rsp="http://schemas.microsoft.com/wbem/wsman/1/windows/shell" '
        'xmlns:wsman="http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd" '
        'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:x="http://schemas.xmlsoap.org/ws/2004/09/transfer">'
        "<s:Header>"
        f'<wsa:Action mustUnderstand="true">{action}</wsa:Action>'
        f"<wsa:MessageID>uuid:{uuid.uuid4()}</wsa:MessageID>"
        f"{relates}"
        f"<wsman:ResourceURI>{CMD_RESOURCE}</wsman:ResourceURI>"
        "</s:Header>"
        f"<s:Body>{body_inner}</s:Body>"
        "</s:Envelope>"
    ).encode("utf-8")


def fault(reason: str, relates_to: str | None = None) -> bytes:
    body = (
        "<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code>"
        f'<s:Reason><s:Text xml:lang="en-US">{reason}</s:Text></s:Reason></s:Fault>'
    )
    return envelope("http://schemas.dmtf.org/wbem/wsman/1/wsman/fault", body, relates_to)


class Command:
    def __init__(self, command_id: str, command_line: str) -> None:
        self.command_id = command_id
        self.command_line = command_line
        self.stdin = bytearray()
        self.executed = False
        self.stdout = b""
        self.stderr = b""
        self.exit_code = 0
        self.started = time.time()
        self.duration = 0.0
        self.lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self.lock:
            self.stdin.extend(data)

    def execute(self, shell_exe: str) -> None:
        with self.lock:
            if self.executed:
                return
            self.executed = True
            start = time.perf_counter()
            try:
                proc = subprocess.run(
                    self.command_line,
                    shell=True,
                    executable=shell_exe,
                    input=bytes(self.stdin) or None,
                    capture_output=True,
                    timeout=900,
                )
                self.stdout = proc.stdout or b""
                self.stderr = proc.stderr or b""
                self.exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                self.stderr = b"command timed out after 900s"
                self.exit_code = 124
            except Exception as exc:  # pragma: no cover - defensive
                self.stderr = f"exec error: {exc}".encode()
                self.exit_code = 125
            self.duration = time.perf_counter() - start


class Shell:
    def __init__(self, shell_id: str) -> None:
        self.shell_id = shell_id
        self.commands: dict[str, Command] = {}


class WinRMStub:
    def __init__(self, shell_exe: str) -> None:
        self.shell_exe = shell_exe
        self.shells: dict[str, Shell] = {}
        self.lock = threading.Lock()
        self.command_log: list[dict[str, Any]] = []
        self.started = time.time()

    def handle(self, root: ET.Element) -> bytes:
        action = ""
        shell_id = ""
        command_id = ""
        message_id = ""
        for el in root.iter():
            name = local(el.tag)
            if name == "Action" and not action:
                action = (el.text or "").strip()
            elif name == "MessageID":
                message_id = (el.text or "").strip()
            elif name == "Selector":
                selector = el.get("Name")
                value = (el.text or "").strip()
                if selector == "ShellId":
                    shell_id = value
                elif selector == "CommandId":
                    command_id = value
            # CommandId also appears as a bare attribute on DesiredStream /
            # Stream / Signal elements (that is how pywinrm sends it).
            attr_cid = el.get("CommandId")
            if attr_cid and not command_id:
                command_id = attr_cid
        op = action.rsplit("/", 1)[-1] if action else ""
        if os.environ.get("LABBENCH_WINRM_DEBUG"):
            print(f"  op={op} action={action} shell={shell_id} cmd={command_id}", flush=True)

        if op == "Create":
            return self._create(message_id)
        if op == "Command":
            return self._command(root, shell_id)
        if op == "Receive":
            return self._receive(shell_id, command_id)
        if op == "Send":
            return self._send(root, shell_id, command_id)
        if op in ("Delete", "Signal"):
            return self._delete(shell_id, command_id, op, message_id)
        if op in ("Get", "Enumerate", "Pull"):
            return envelope(f"{action}Response", "<wsman:Items/>", message_id)
        return fault(f"unsupported operation: {op or action or 'unknown'}", message_id)

    def _create(self, message_id: str) -> bytes:
        shell_id = str(uuid.uuid4()).upper()
        with self.lock:
            self.shells[shell_id] = Shell(shell_id)
        body = (
            "<x:ResourceCreated>"
            "<wsa:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:Address>"
            "<wsa:ReferenceParameters>"
            f"<wsman:ResourceURI>{CMD_RESOURCE}</wsman:ResourceURI>"
            "<wsman:SelectorSet>"
            f'<wsman:Selector Name="ShellId">{shell_id}</wsman:Selector>'
            "</wsman:SelectorSet>"
            "</wsa:ReferenceParameters>"
            "</x:ResourceCreated>"
        )
        return envelope(f"{SHELL_URI}/CreateResponse", body, message_id)

    def _command(self, root: ET.Element, shell_id: str) -> bytes:
        command = ""
        arguments = ""
        for el in root.iter():
            if local(el.tag) == "Command" and el.text:
                command = el.text
            elif local(el.tag) == "Arguments" and el.text:
                arguments = el.text
        command_line = command
        if arguments:
            command_line = f"{command} {arguments}".strip()
        command_id = str(uuid.uuid4()).upper()
        with self.lock:
            shell = self.shells.setdefault(shell_id or "default", Shell(shell_id or "default"))
            shell.commands[command_id] = Command(command_id, command_line)
        body = (
            f"<rsp:CommandResponse><rsp:CommandId>{command_id}</rsp:CommandId></rsp:CommandResponse>"
        )
        return envelope(f"{SHELL_URI}/CommandResponse", body)

    def _lookup(self, shell_id: str, command_id: str) -> Command | None:
        shell = self.shells.get(shell_id)
        if not shell:
            return None
        return shell.commands.get(command_id)

    def _receive(self, shell_id: str, command_id: str) -> bytes:
        command = self._lookup(shell_id, command_id)
        if command is None:
            return fault("unknown command")
        command.execute(self.shell_exe)
        with self.lock:
            self.command_log.append(
                {
                    "shell_id": shell_id,
                    "command_id": command_id,
                    "command": command.command_line,
                    "exit_code": command.exit_code,
                    "duration_ms": round(command.duration * 1000, 2),
                    "ts": command.started,
                }
            )
        streams = []
        if command.stdout:
            streams.append(
                f'<rsp:Stream Name="stdout" CommandId="{command_id}">'
                f"{base64.b64encode(command.stdout).decode()}</rsp:Stream>"
            )
        if command.stderr:
            streams.append(
                f'<rsp:Stream Name="stderr" CommandId="{command_id}">'
                f"{base64.b64encode(command.stderr).decode()}</rsp:Stream>"
            )
        streams.append(
            f'<rsp:CommandState CommandId="{command_id}" State="{DONE}">'
            f"<rsp:ExitCode>{command.exit_code}</rsp:ExitCode></rsp:CommandState>"
        )
        body = f"<rsp:ReceiveResponse>{''.join(streams)}</rsp:ReceiveResponse>"
        return envelope(f"{SHELL_URI}/ReceiveResponse", body)

    def _send(self, root: ET.Element, shell_id: str, command_id: str) -> bytes:
        command = self._lookup(shell_id, command_id)
        if command is None:
            return fault("unknown command")
        for el in root.iter():
            if local(el.tag) == "Stream" and el.text:
                try:
                    command.feed(base64.b64decode(el.text))
                except Exception:
                    command.feed((el.text or "").encode())
        return envelope(f"{SHELL_URI}/SendResponse", "<rsp:SendResponse/>")

    def _delete(self, shell_id: str, command_id: str, op: str, message_id: str) -> bytes:
        with self.lock:
            if command_id and shell_id in self.shells:
                self.shells[shell_id].commands.pop(command_id, None)
            elif shell_id:
                self.shells.pop(shell_id, None)
        return envelope(f"{SHELL_URI}/{op}Response", "", message_id)


def make_handler(stub: WinRMStub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LabBenchWinRMStub/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            if os.environ.get("LABBENCH_WINRM_DEBUG"):
                super().log_message(fmt, *args)

        def _respond(
            self, code: int, body: bytes, ctype: str = "application/soap+xml;charset=UTF-8"
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") in ("/health", ""):
                info = json.dumps(
                    {
                        "status": "ok",
                        "component": "win-host",
                        "mode": "simulated",
                        "shell": stub.shell_exe,
                        "uptime_s": round(time.time() - stub.started, 3),
                        "commands_executed": len(stub.command_log),
                    }
                ).encode()
                self._respond(200, info, "application/json")
            elif self.path.rstrip("/") == "/commands":
                self._respond(200, json.dumps(stub.command_log).encode(), "application/json")
            else:
                self._respond(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if self.path.rstrip("/") != "/wsman":
                self._respond(404, b"not found", "text/plain")
                return
            if os.environ.get("LABBENCH_WINRM_DEBUG"):
                print(f"POST {self.path} len={len(raw)}", flush=True)
            try:
                root = ET.fromstring(raw)
                body = stub.handle(root)
            except ET.ParseError as exc:
                body = fault(f"XML parse error: {exc}")
            except Exception as exc:  # pragma: no cover - defensive
                body = fault(f"internal error: {exc}")
            self._respond(200, body)

    return Handler


def ensure_powershell_symlink() -> None:
    """On the win-host image ``powershell`` must exist; create a pwsh symlink."""

    pwshexe = "/opt/microsoft/powershell/7/pwsh"
    for name in ("/usr/local/bin/powershell", "/usr/local/bin/powershell.exe"):
        try:
            if os.path.exists(pwshexe) and not os.path.exists(name):
                os.symlink(pwshexe, name)
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="LabBench WinRM stub")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("LABBENCH_WINRM_PORT", "5985"))
    )
    parser.add_argument(
        "--shell",
        default=os.environ.get("LABBENCH_WINRM_SHELL", "/bin/bash"),
        help="Shell used to execute received command lines.",
    )
    args = parser.parse_args()
    ensure_powershell_symlink()
    stub = WinRMStub(args.shell)
    print(
        json.dumps(
            {
                "event": "startup",
                "component": "win-host",
                "mode": "simulated",
                "shell": args.shell,
                "listen": f"{args.host}:{args.port}",
                "endpoint": "/wsman",
            }
        ),
        flush=True,
    )
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(stub))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
