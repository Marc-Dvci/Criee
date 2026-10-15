#!/bin/bash
# Ship the current commit plus the built web app to the demo instance and restart the container.
# Usage: deploy/deploy.sh <host> [--reset]   (needs ~/.ssh/criee.pem and a local .env)
set -euo pipefail
HOST=${1:?host}; RESET=${2:-}
KEY=~/.ssh/criee.pem; SSH="ssh -o ConnectTimeout=15 -i $KEY ec2-user@$HOST"
cd "$(dirname "$0")/.."
(cd web && npm run build >/dev/null)
rm -f bundle.tar bundle.tar.gz
git archive --format=tar -o bundle.tar HEAD
tar --force-local -rf bundle.tar web/dist
gzip -f bundle.tar
grep -v '^CRIEE_PUBLIC_URL\|^CRIEE_DATA_DIR\|^SANDBOX_BUYER_PASSWORD' .env > deploy/.criee.env
printf 'CRIEE_PUBLIC_URL=auto\nCRIEE_DATA_DIR=/data\nCRIEE_DEMO=1\n' >> deploy/.criee.env
scp -o ConnectTimeout=15 -i $KEY bundle.tar.gz deploy/.criee.env ec2-user@$HOST:~/
rm -f bundle.tar.gz deploy/.criee.env
$SSH "set -e; sudo rm -rf /opt/criee/src && sudo mkdir -p /opt/criee/src && sudo tar -xzf ~/bundle.tar.gz -C /opt/criee/src \
  && sudo install -m 600 ~/.criee.env /opt/criee/criee.env && rm ~/.criee.env ~/bundle.tar.gz \
  && cd /opt/criee/src && sudo docker build -q -f deploy/Dockerfile.runtime -t criee . \
  && sudo docker rm -f criee >/dev/null 2>&1 || true; \
  if [ '$RESET' = '--reset' ]; then sudo rm -f /var/lib/criee/data/criee.db*; fi; \
  sudo docker run -d --name criee --restart always -p 127.0.0.1:8000:8000 -v /var/lib/criee/data:/data \
  --env-file /opt/criee/criee.env criee >/dev/null && sleep 12 && curl -s 127.0.0.1:8000/api/status | head -c 200; echo"
