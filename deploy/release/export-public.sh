#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: export-public.sh --output DIR --author-name NAME --author-email EMAIL [--source DIR] [--patterns-file FILE]

Export the clean HEAD of a source repository into a new local repository.
The output directory must not exist. Nothing is pushed.
Internal material listed in EXCLUDED_PATHS is left out of the export.
EOF
}

# Internal material that stays in the working repository but is never published.
# Globs relative to the repository root; no whitespace inside a glob (matched names may contain it).
EXCLUDED_PATHS=(
  research
  Marketing
  Produktvideo
  docs/naming
  docs/ui-redesign
  docs/konzept-email.md
  media
  'mediaassets/ChatGPT-Bild*.png'
  tasks
)

source_dir=.
output=
author_name=
author_email=
patterns_file=
while (($#)); do
  case "$1" in
    --source|--output|--author-name|--author-email|--patterns-file)
      if (($# < 2)) || [[ -z $2 ]]; then usage >&2; exit 2; fi
      case "$1" in
        --source) source_dir=$2 ;;
        --output) output=$2 ;;
        --author-name) author_name=$2 ;;
        --author-email) author_email=$2 ;;
        --patterns-file) patterns_file=$2 ;;
      esac
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ -z $output || -z $author_name || -z $author_email ]]; then
  usage >&2
  exit 2
fi
if [[ $author_name == *$'\n'* || $author_email == *$'\n'* || $author_email != *@* ]]; then
  echo 'Invalid author identity' >&2
  exit 2
fi
for command_name in git tar python3 gitleaks; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 2
  fi
done
if [[ -n $patterns_file && ! -f $patterns_file ]]; then
  echo 'Patterns file does not exist' >&2
  exit 2
fi
if [[ -e $output || -L $output ]]; then
  echo 'Output path already exists' >&2
  exit 2
fi
if [[ ! -d $(dirname "$output") ]]; then
  echo 'Output parent directory does not exist' >&2
  exit 2
fi
if [[ $(git -C "$source_dir" rev-parse --is-inside-work-tree) != true ]]; then
  echo 'Source must be a Git worktree' >&2
  exit 2
fi
if ! git -C "$source_dir" diff --quiet HEAD --; then
  echo 'Commit staged and tracked working-tree changes before exporting' >&2
  exit 2
fi

stage=$(mktemp -d "$(dirname "$output")/.public-export.XXXXXXXX")
output_created=0
cleanup() {
  rm -rf -- "$stage"
  if ((output_created)); then rm -rf -- "$output"; fi
}
trap cleanup EXIT

git -C "$source_dir" archive --format=tar HEAD | tar -xf - -C "$stage"
# Remove excluded material before scanning, so the scans cover exactly what is published.
shopt -s nullglob
for excluded in "${EXCLUDED_PATHS[@]}"; do
  for match in "$stage"/$excluded; do
    rm -rf -- "$match"
  done
done
shopt -u nullglob

# The scanner reports locations and categories only; it never prints matched values.
python3 - "$stage" "$patterns_file" <<'PY'
import ipaddress
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
patterns_file = sys.argv[2]
# Documented example ranges: loopback, the example LAN 192.168.1.0/24, 10.0.0.0/24, the Docker default
# bridge and the pinned Compose/smoke-test subnets (incl. their runner networks). Netmasks such as 255.255.255.0 are not addresses.
known_networks = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/32", "127.0.0.0/8", "192.168.1.0/24", "10.0.0.0/24", "172.17.0.0/16",
    "172.31.250.0/24", "172.31.251.0/24", "172.31.252.0/24", "172.31.253.0/24", "255.0.0.0/8",
)]
# Example hosts; `settings.local` is the Claude Code file name `.claude/settings.local.json`.
known_host_suffixes = (".example.internal", ".example.ts.net")
known_hosts = {"example.internal", "example.ts.net", "settings.local"}
ip_pattern = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
host_pattern = re.compile(r"\b[a-z0-9][a-z0-9.-]*\.(?:local|internal|ts\.net)\b", re.I)
custom = []
if patterns_file:
    for line in pathlib.Path(patterns_file).read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            custom.append(re.compile(line))

found = False
for path in sorted(root.rglob("*")):
    if not path.is_file() or path.is_symlink():
        continue
    try:
        content = path.read_bytes()
    except OSError as exc:
        print(f"Cannot scan {path.relative_to(root)}: {exc}", file=sys.stderr)
        sys.exit(2)
    # Search all exported bytes, including files containing NUL. A binary asset
    # can still contain a readable private address that a secret scanner misses.
    for number, line in enumerate(content.decode("utf-8", "replace").splitlines(), 1):
        reasons = set()
        for match in ip_pattern.finditer(line):
            value = match.group()
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.is_private and not any(address in network for network in known_networks):
                reasons.add("private IP address")
        for match in host_pattern.finditer(line):
            host = match.group().lower()
            if host not in known_hosts and not host.endswith(known_host_suffixes):
                reasons.add("internal hostname")
        if any(pattern.search(line) for pattern in custom):
            reasons.add("custom pattern")
        for reason in sorted(reasons):
            print(f"{path.relative_to(root)}:{number}: {reason}", file=sys.stderr)
            found = True
if found:
    sys.exit(1)
PY

gitleaks dir --redact --no-banner -- "$stage"

mv -T -- "$stage" "$output"
output_created=1
git -C "$output" init -q -b main
git -C "$output" add --all
GIT_AUTHOR_NAME=$author_name GIT_AUTHOR_EMAIL=$author_email \
GIT_COMMITTER_NAME=$author_name GIT_COMMITTER_EMAIL=$author_email \
GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
  git -C "$output" -c core.hooksPath=/dev/null commit -q --no-gpg-sign -m 'Initial public release'
output_created=0
echo "Public repository created at $output"
