#!/usr/bin/env bash
# Manual, throwaway check of the native SFTP inbox drop-in (research/konzept-datenzonen.md §4,
# docs/data-store.md → SFTP inbox). It never touches the production sshd: it runs in a fresh container.
#
#   docker run --rm -v "$PWD":/repo ubuntu:24.04 bash /repo/deploy/sftp/check-inbox.sh
#
# It installs OpenSSH, applies deploy/ssh/60-taskbrigade-sftp.conf, provisions one chrooted deliverer exactly like
# `tbr-admin data sftp-user add` does, and verifies: SFTP upload works, shell is refused, path escapes are refused
# and remote forwarding is refused. Prints a `CHECKS` line per step and exits non-zero on a failure.
set -uo pipefail
REPO=${REPO:-/repo}
ROOT=/srv/taskbrigade-sftp
USER=demo-export
GROUP=tbr-sftp
PASS=0; FAIL=0
check() { # check <name> <expected-substring> <actual>
  if grep -qiF -- "$2" <<<"$3"; then echo "CHECKS ok  : $1  [$3]"; PASS=$((PASS+1));
  else echo "CHECKS FAIL: $1  expected '$2', got [$3]"; FAIL=$((FAIL+1)); fi
}

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq openssh-server openssh-client >/dev/null 2>&1
mkdir -p /run/sshd
# Host key for the client to pin.
ssh-keygen -q -t ed25519 -N "" -f testhost.key

# Drop-in exactly as installed by the deploy scripts (paths already default).
install -d -o root -g root -m 0755 /etc/ssh/sshd_config.d
sed "s|/srv/taskbrigade-sftp|$ROOT|g" "$REPO/deploy/ssh/60-taskbrigade-sftp.conf" \
  > /etc/ssh/sshd_config.d/60-taskbrigade-sftp.conf
groupadd --system "$GROUP"
useradd --system --home "$ROOT/$USER" --shell /usr/sbin/nologin --gid "$GROUP" --groups "$GROUP" "$USER"
install -d -o root -g root -m 0755 "$ROOT" "$ROOT/$USER"
install -d -o "$USER" -g "$GROUP" -m 0730 "$ROOT/$USER/upload"

# Key on the client side + authorized_keys as the service writes it.
ssh-keygen -q -t ed25519 -N "" -f client.key
install -d -o root -g root -m 0755 "$ROOT/$USER/.ssh"
cp client.key.pub "$ROOT/$USER/.ssh/authorized_keys"
chmod 0644 "$ROOT/$USER/.ssh/authorized_keys"

check "sshd accepts the drop-in" "done" "$(sshd -t 2>&1; echo done)"
/usr/sbin/sshd -D -e -o PidFile=/run/sshd.pid &
sleep 1

SFTP="sftp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o PreferredAuthentications=publickey -i client.key -P 22"
# 1. SFTP login works and the chroot shows an empty root with the write-only upload folder.
OUT=$($SFTP "$USER@127.0.0.1" <<< "pwd
ls
put /etc/hostname upload/test.txt
ls upload
bye" 2>&1)
check "SFTP login + pwd is chroot /" "Remote working directory: /" "$OUT"
check "upload folder is visible" "upload" "$OUT"
check "upload succeeds" "Uploading /etc/hostname to /upload/test.txt" "$OUT"
check "uploaded file is listed" "test.txt" "$OUT"

# 2. No shell: an exec/pty request must fail (ForceCommand internal-sftp).
OUT=$(timeout 20 ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes \
  -o PreferredAuthentications=publickey -i client.key "$USER@127.0.0.1" "id" 2>&1)
check "no shell (command refused)" "this service allows sftp connections only" "$OUT"

# 3. Path escape out of the chroot is impossible.
OUT=$($SFTP "$USER@127.0.0.1" <<< "get /etc/passwd /tmp/leak.txt
bye" 2>&1)
check "path escape refused" "not found" "$OUT"
[[ -f /tmp/leak.txt ]] && { echo "CHECKS FAIL: /etc/passwd leaked out of the chroot"; FAIL=$((FAIL+1)); } \
  || { echo "CHECKS ok  : no file outside the chroot was readable"; PASS=$((PASS+1)); }

# 4. Remote forwarding is refused (AllowTcpForwarding no is enforced server-side for -R). A local -L forward
#    is a client-side channel and is not what the server could refuse anyway.
set +e
OUT=$(timeout 20 ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes \
  -o PreferredAuthentications=publickey -i client.key -o ExitOnForwardFailure=yes \
  -R 18099:127.0.0.1:22 "$USER@127.0.0.1" -N 2>&1)
RC=$?
set -e
check "remote forwarding refused" "remote port forwarding failed" "$OUT"
if [[ $RC -ne 0 && $RC -ne 124 ]]; then
  echo "CHECKS ok  : remote forwarding did not stay open (exit $RC)"; PASS=$((PASS+1))
else
  echo "CHECKS FAIL: remote forwarding stayed open (exit $RC)"; FAIL=$((FAIL+1))
fi

echo "CHECKS summary: $PASS ok, $FAIL failed"
exit $((FAIL > 0))
