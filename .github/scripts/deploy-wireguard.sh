#!/usr/bin/env bash
# Configure a short-lived, host-only WireGuard connection on the CI runner.
set -euo pipefail
umask 077

operation="${1:-}"
service="${2:-}"
case "$service" in backend|frontend) ;; *) echo 'Choose backend or frontend' >&2; exit 2 ;; esac
vpn_dir="${RUNNER_TEMP:?RUNNER_TEMP is required}/semantic-home-vpn"
vpn_config="$vpn_dir/semantic-ci.conf"
ssh_key="$vpn_dir/deploy-key"
known_hosts="$vpn_dir/known_hosts"

ssh_options=(-i "$ssh_key" -o BatchMode=yes -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known_hosts"
  -o HostKeyAlgorithms=ssh-ed25519 -o ConnectTimeout=10
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -p 2222)

case "$operation" in
  up)
    : "${DEPLOY_WIREGUARD_CONFIG:?Set the production DEPLOY_WIREGUARD_CONFIG secret}"
    : "${DEPLOY_SSH_PRIVATE_KEY:?Set the existing deployment SSH key secret}"
    mkdir -p "$vpn_dir"
    chmod 700 "$vpn_dir"
    # Reject hooks, DNS changes and broad routes before wg-quick runs as root.
    python3 - <<'PY'
import base64, ipaddress, os, re
from pathlib import Path
text = os.environ['DEPLOY_WIREGUARD_CONFIG']
allowed = {'Interface': {'PrivateKey', 'Address', 'MTU'},
           'Peer': {'PublicKey', 'AllowedIPs', 'Endpoint', 'PersistentKeepalive'}}
sections = {}
current = None
for raw in text.splitlines():
    line = raw.strip()
    if not line or line.startswith('#'):
        continue
    if line.startswith('[') and line.endswith(']'):
        current = line[1:-1]
        if current not in allowed or current in sections:
            raise SystemExit('Invalid VPN section')
        sections[current] = {}
        continue
    key, sep, value = line.partition('=')
    key, value = key.strip(), value.strip()
    if not sep or current not in allowed or key not in allowed[current] or key in sections[current]:
        raise SystemExit('Unsupported VPN setting; hooks and broad routes are forbidden')
    sections[current][key] = value
if set(sections) != {'Interface', 'Peer'}:
    raise SystemExit('Expected one interface and one peer')
interface, peer = sections['Interface'], sections['Peer']
if set(interface) != allowed['Interface'] or set(peer) != allowed['Peer']:
    raise SystemExit('Incomplete VPN settings')
for value in (interface['PrivateKey'], peer['PublicKey']):
    if len(base64.b64decode(value, validate=True)) != 32:
        raise SystemExit('Invalid VPN key')
address = ipaddress.IPv4Interface(interface['Address'])
private_ranges = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
if address.network.prefixlen != 32 or not any(address.ip in n for n in private_ranges) or address.ip == ipaddress.ip_address('10.8.0.1'):
    raise SystemExit('Expected a dedicated CI /32 address')
if peer['AllowedIPs'] != '10.8.0.1/32' or interface['MTU'] != '1280' or peer['PersistentKeepalive'] != '25':
    raise SystemExit('Expected host-only VPN routing')
if not re.fullmatch(r'[A-Za-z0-9.-]+:51048', peer['Endpoint']):
    raise SystemExit('Expected a WireGuard endpoint on UDP 51048')
path = Path(os.environ['RUNNER_TEMP']) / 'semantic-home-vpn' / 'semantic-ci.conf'
path.write_text(text.rstrip() + '\n')
path.chmod(0o600)
PY
    printf '%s\n' "$DEPLOY_SSH_PRIVATE_KEY" > "$ssh_key"
    chmod 600 "$ssh_key"
    # Pinned through the existing trusted LAN SSH connection, never keyscan.
    printf '%s\n' '[10.8.0.1]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoLv45noKYxev3mst3G3eBIl3zpIBguF0cUqYU/IytU' > "$known_hosts"
    sudo wg-quick up "$vpn_config"
    ;;
  check)
    # Trigger traffic so WireGuard starts its handshake; SSH proves both the
    # route and the pinned server identity. This does not run a deployment.
    for attempt in {1..6}; do
      if ssh "${ssh_options[@]}" deploy@10.8.0.1 \
        "printf '%s\\n' wireguard_ssh_ready; sudo -n -l /opt/home-automation/bin/deploy-$service.sh >/dev/null"; then
        handshake="$(sudo wg show semantic-ci latest-handshakes | awk '{print $2}')"
        if [[ "$handshake" =~ ^[0-9]+$ ]] && ((handshake > 0)); then
          echo 'WireGuard handshake, pinned SSH login and deployment permission verified.'
          exit 0
        fi
      fi
      echo "Waiting for VPN deployment access ($attempt/6)..."
      sleep 2
    done
    echo 'VPN deployment connection could not be verified.' >&2
    exit 1
    ;;
  deploy)
    tag="${DEPLOY_TAG:?DEPLOY_TAG is required}"
    if [[ ! "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
      echo 'Invalid image tag' >&2; exit 2
    fi
    printf '%s\n' "$tag" | ssh "${ssh_options[@]}" deploy@10.8.0.1 \
      "sudo -n /opt/home-automation/bin/deploy-$service.sh"
    ;;
  down)
    if sudo ip link show semantic-ci >/dev/null 2>&1; then
      sudo wg-quick down "$vpn_config"
    fi
    rm -f "$vpn_config" "$ssh_key" "$known_hosts"
    ;;
  *) echo 'Usage: deploy-wireguard.sh up|check|deploy|down backend|frontend' >&2; exit 2 ;;
esac
