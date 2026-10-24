#!/usr/bin/env bash
# Firewall + SYN-flood hardening for the VPS. Run once as root:  sudo deploy/harden_network.sh
#
# Why: a SYN flood on ports 80/443 filled the kernel's half-open connection queue (512), so real
# requests from claude.ai were dropped at random ("server isn't responding").
#   1. HTTPS (443) only accepts Anthropic's outbound range, the only client this server has:
#      https://platform.claude.com/docs/en/api/ip-addresses
#   2. Port 80 stays open to everyone: Let's Encrypt needs it to renew the certificate.
#   3. Bigger SYN queue, SYN cookies on, fewer SYN-ACK retries, so a flood can't crowd out real clients.
# Note: with Docker, published ports bypass UFW; this script is for Caddy running on the host.
set -euo pipefail

ANTHROPIC_V4="160.79.104.0/21"

ufw allow OpenSSH
# Remove any rule that opens 443 to everyone (ignore "rule not found").
for rule in "443/tcp" "443" "80,443/tcp" "443/udp"; do ufw delete allow "$rule" >/dev/null 2>&1 || true; done
ufw allow 80/tcp
ufw allow from "$ANTHROPIC_V4" to any port 443 proto tcp
# Optional: block a flooding network, e.g.  BLOCK_NET=179.51.0.0/16 sudo -E deploy/harden_network.sh
if [ -n "${BLOCK_NET:-}" ]; then
  ufw insert 1 deny from "$BLOCK_NET"
fi
ufw --force enable

cat > /etc/sysctl.d/60-ncert-synflood.conf <<'EOF'
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 4096
net.ipv4.tcp_synack_retries = 2
EOF
sysctl --system >/dev/null

ufw status numbered
echo "Half-open connections now: $(ss -tnH state syn-recv | wc -l)"
