"""Fake SFTP server for tests: asyncssh server with password login in a background thread; the SFTP root is chrooted
to a temporary folder (`root`), so `/agent exchange` on the server is `root / "agent exchange"`."""
import asyncio
import threading
from pathlib import Path

import asyncssh


class FakeSftpServer:
    def __init__(self, root: Path, users: dict[str, str]):
        self.root, self.users = root, users
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.fingerprint = self.key.get_fingerprint("sha256")
        self.logins: list[str] = []
        self.loop = asyncio.new_event_loop()
        self.port = 0
        self._server = None
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def start(self) -> "FakeSftpServer":
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._listen(), self.loop).result(10)
        return self

    async def _listen(self):
        server = self

        class Auth(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                ok = server.users.get(username) == password
                if ok:
                    server.logins.append(username)
                return ok

        self._server = await asyncssh.listen(
            "127.0.0.1", 0, server_host_keys=[self.key], server_factory=Auth,
            sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(self.root)), allow_scp=False)
        self.port = self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        async def close():
            self._server.close()
            await self._server.wait_closed()

        asyncio.run_coroutine_threadsafe(close(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10)
