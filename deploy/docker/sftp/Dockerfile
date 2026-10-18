# syntax=docker/dockerfile:1
# SFTP inbox of the data zones for Docker installations (compose profile `data-sftp`, docs/docker.md → Data zones).
# OpenSSH `internal-sftp` with the same drop-in as the native inbox (deploy/ssh/60-taskbrigade-sftp.conf): per-user
# chroot, no shell, no forwarding, keys only. The deliverer accounts come from the app's spool file
# (TBR_SFTP_SPOOL, written by `tbr-admin data sftp-user …`); sftp_sync.py applies it and supervises sshd.
# Build context: the repository root (compose.yaml).
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ARG TBR_DATA_GID=10004
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
    && groupadd --system --gid ${TBR_DATA_GID} tbr-data \
    && groupadd --system tbr-sftp \
    && install -d -m 0755 /run/sshd /etc/ssh/keys /etc/ssh/sshd_config.d
COPY deploy/docker/sftp/sshd_config /etc/ssh/sshd_config
COPY deploy/ssh/60-taskbrigade-sftp.conf /etc/ssh/sshd_config.d/60-taskbrigade-sftp.conf
COPY deploy/docker/sftp/sftp_sync.py /usr/local/lib/taskbrigade/sftp_sync.py
EXPOSE 22
# Process check, not a connection: a probe connection would log a key-exchange reset every time.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import os; assert any(open(f'/proc/{p}/comm').read().strip() == 'sshd' for p in os.listdir('/proc') if p.isdigit())"]
ENTRYPOINT ["python3", "-I", "/usr/local/lib/taskbrigade/sftp_sync.py"]
