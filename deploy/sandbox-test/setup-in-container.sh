#!/usr/bin/env bash
# Inside the throwaway systemd container of deploy/sandbox-test/run.sh: lay out users, folders, key, runner
# virtualenv, units and polkit rule like `TBR_DATA_ZONES=1 deploy/install.sh` (concept §9), from the checkout /src.
set -euo pipefail
SRC=/src
DATA_DIR=/srv/taskbrigade-data
getent group tbr-data >/dev/null || groupadd --system tbr-data
for u in tbr-runner tbr-job; do
  id "$u" >/dev/null 2>&1 || useradd --system --user-group --shell /usr/sbin/nologin --home-dir /nonexistent "$u"
done
usermod -aG tbr-data,tbr-job tbr-runner
install -d -o root -g tbr-data -m 0750 "$DATA_DIR"
install -d -o root -g tbr-data -m 0750 "$DATA_DIR/store"
install -d -o tbr-runner -g tbr-data -m 0750 "$DATA_DIR/quarantine"
install -d -o root -g root -m 0755 /etc/taskbrigade /var/lib/taskbrigade
install -d -o tbr-runner -g tbr-runner -m 0700 /var/lib/taskbrigade-runner
[[ -f /etc/taskbrigade/data.key ]] || python3 -c 'import base64,secrets,sys; open(sys.argv[1],"wb").write(base64.b64encode(secrets.token_bytes(32))+b"\n")' /etc/taskbrigade/data.key
chown root:tbr-data /etc/taskbrigade/data.key; chmod 0640 /etc/taskbrigade/data.key
echo "TBR_DB_PASSWORD=secret-app-state" > /var/lib/taskbrigade/app-secret; chmod 0600 /var/lib/taskbrigade/app-secret
# Test image supplies openpyxl/defusedxml through apt; production uses pinned runner-requirements.txt.
[[ -x /opt/taskbrigade/runner-venv/bin/python ]] || python3.12 -m venv --system-site-packages /opt/taskbrigade/runner-venv
install -d -m 0755 /opt/taskbrigade/app
for unit in taskbrigade-runner.service taskbrigade-job@.service; do
  sed "s|/srv/taskbrigade-data|$DATA_DIR|g" "$SRC/deploy/systemd/$unit" > "/etc/systemd/system/$unit"
done
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$SRC/deploy/polkit/60-taskbrigade-runner.rules" /etc/polkit-1/rules.d/
if [[ -n "${LIMITS:-}" ]]; then
  install -d -m 0755 /etc/systemd/system/taskbrigade-job@.service.d
  printf '%b' "$LIMITS" > /etc/systemd/system/taskbrigade-job@.service.d/50-limits.conf
fi
systemctl daemon-reload
systemctl restart polkit 2>/dev/null || true
# The runner service's RuntimeDirectory; created here because the self-test runs outside the service.
install -d -o tbr-runner -g tbr-runner -m 0750 /run/taskbrigade-runner
echo "setup done"
