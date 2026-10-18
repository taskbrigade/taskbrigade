# Throwaway systemd host for the sandbox isolation test (deploy/sandbox-test/run.sh). Not a product image.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv dbus polkitd python3.12 python3.12-venv python3-cryptography python3-httpx python3-openpyxl python3-defusedxml sudo ca-certificates procps iproute2 && apt-get clean
RUN systemctl mask systemd-logind.service getty.target console-getty.service systemd-udevd.service systemd-networkd.service systemd-resolved.service || true
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
