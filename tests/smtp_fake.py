"""A small SMTP submission server for tests (like `FakeApi` for Telegram, but over real sockets and TLS).

Speaks EHLO, STARTTLS (or implicit TLS), AUTH PLAIN, MAIL, RCPT, DATA, RSET, NOOP and QUIT on 127.0.0.1 with a
self-signed certificate; `client_context()` trusts it. `fail_rcpt`/`fail_data` = (code, text) answer the next
RCPT/DATA with that reply; `stop()` closes the port, so a client sees a refused connection.
"""
import base64
import datetime
import email
import email.policy
import ipaddress
import socketserver
import ssl
import tempfile
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_CERT: tuple[str, str] | None = None


def _cert() -> tuple[str, str]:
    """Self-signed certificate for 127.0.0.1/localhost (made once per test run)."""
    global _CERT
    if _CERT is None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=2))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                               critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        folder = Path(tempfile.mkdtemp(prefix="smtp-fake-"))
        cert_path, key_path = folder / "cert.pem", folder / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        _CERT = (str(cert_path), str(key_path))
    return _CERT


def client_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=_cert()[0])
    return ctx


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"

    def _reply(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")
        self.wfile.flush()

    def _tls(self) -> None:
        self.connection = self.request = self.server.fake.server_context.wrap_socket(self.request, server_side=True)
        self.rfile = self.connection.makefile("rb")
        self.wfile = self.connection.makefile("wb")
        self.tls = True

    def handle(self) -> None:
        fake = self.server.fake
        self.tls = False
        if fake.security == "tls":
            self._tls()
        fake.connections += 1
        self._reply("220 fake.example.internal ESMTP")
        mail_from, rcpts, authed = None, [], False
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            cmd = line.split(" ", 1)[0].upper()
            if cmd in ("EHLO", "HELO"):
                caps = ["fake.example.internal", "8BITMIME", "AUTH PLAIN"]
                if fake.security == "starttls" and not self.tls:
                    caps.append("STARTTLS")
                for i, cap in enumerate(caps):
                    self._reply(("250 " if i == len(caps) - 1 else "250-") + cap)
            elif cmd == "STARTTLS":
                self._reply("220 2.0.0 Ready to start TLS")
                self._tls()
            elif cmd == "AUTH":
                parts = line.split(" ")
                try:
                    _z, user, password = base64.b64decode(parts[2]).decode().split("\0")
                except (IndexError, ValueError):
                    self._reply("501 5.5.2 Syntax error")
                    continue
                if not self.tls:
                    self._reply("538 5.7.11 Encryption required")
                elif (user, password) == (fake.user, fake.password):
                    authed = True
                    self._reply("235 2.7.0 Authentication successful")
                else:
                    self._reply("535 5.7.8 Authentication credentials invalid")
            elif cmd == "MAIL":
                if fake.user and not authed:
                    self._reply("530 5.7.0 Authentication required")
                    continue
                mail_from, rcpts = line.split(":", 1)[1].strip().split(" ")[0].strip("<>"), []
                self._reply("250 2.1.0 OK")
            elif cmd == "RCPT":
                if fake.fail_rcpt:
                    code, text = fake.fail_rcpt
                    self._reply(f"{code} {text}")
                    continue
                rcpts.append(line.split(":", 1)[1].strip().strip("<>"))
                self._reply("250 2.1.5 OK")
            elif cmd == "DATA":
                self._reply("354 End data with <CR><LF>.<CR><LF>")
                data = []
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b".\r\n", b""):
                        break
                    data.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                if fake.fail_data:
                    code, text = fake.fail_data
                    self._reply(f"{code} {text}")
                    continue
                fake.messages.append((mail_from, list(rcpts), b"".join(data)))
                self._reply("250 2.0.0 Queued")
            elif cmd in ("RSET", "NOOP"):
                self._reply("250 2.0.0 OK")
            elif cmd == "QUIT":
                self._reply("221 2.0.0 Bye")
                return
            else:
                self._reply("502 5.5.2 Command not recognized")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        """A client that drops the connection (e.g. refuses the certificate) is part of the tests, not an error."""


class FakeSmtp:
    def __init__(self, security: str = "starttls", user: str | None = "taskbrigade",
                 password: str | None = "app-password-123"):
        self.security, self.user, self.password = security, user, password
        self.messages: list[tuple[str, list[str], bytes]] = []
        self.fail_rcpt: tuple[int, str] | None = None
        self.fail_data: tuple[int, str] | None = None
        self.connections = 0
        self.server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.server_context.load_cert_chain(*_cert())
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.fake = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def parsed(self, i: int = -1) -> email.message.EmailMessage:
        return email.message_from_bytes(self.messages[i][2], policy=email.policy.default)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
