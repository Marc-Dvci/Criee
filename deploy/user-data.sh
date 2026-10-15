#!/bin/bash
# Amazon Linux 2023: Docker, swap, and a Cloudflare quick tunnel that publishes its URL to S3.
set -euxo pipefail
dnf install -y docker
systemctl enable --now docker
if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile; chmod 600 /swapfile; mkswap /swapfile; swapon /swapfile; echo '/swapfile swap swap defaults 0 0' >> /etc/fstab; fi
curl -fsSL -o /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64
chmod +x /usr/local/bin/cloudflared
mkdir -p /opt/criee /var/lib/criee/data
chown 10001 /var/lib/criee/data
cat > /usr/local/bin/criee-tunnel.sh <<'SH'
#!/bin/bash
/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000 2>&1 | while read -r line; do
  echo "$line"
  url=$(echo "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' || true)
  if [ -n "$url" ]; then
    printf '{"url":"%s","since":"%s"}\n' "$url" "$(date -u +%FT%TZ)" > /var/lib/criee/current.json
    aws s3 cp /var/lib/criee/current.json "s3://__BUCKET__/current.json" --content-type application/json --cache-control no-cache || true
  fi
done
SH
chmod +x /usr/local/bin/criee-tunnel.sh
cat > /etc/systemd/system/criee-tunnel.service <<'UNIT'
[Unit]
Description=Criee quick tunnel
After=network-online.target docker.service
Wants=network-online.target
[Service]
ExecStart=/usr/local/bin/criee-tunnel.sh
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now criee-tunnel
