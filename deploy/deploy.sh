#!/usr/bin/env bash
# Build committed source before replacing the demo; preserve data and support rollback.
# Usage: bash deploy/deploy.sh <host> (requires SSH key and the existing server env file)
set -euo pipefail
HOST=${1:?Usage: deploy/deploy.sh host}
[[ "$HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo 'Invalid host'; exit 2; }
[[ $# == 1 ]] || { echo 'Deployment preserves the database; unexpected extra argument'; exit 2; }
KEY=${CRIEE_SSH_KEY:-$HOME/.ssh/criee.pem}
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -i "$KEY" "ec2-user@$HOST")
cd "$(dirname "$0")/.."
git diff --quiet && git diff --cached --quiet || { echo 'Commit tracked changes before deploying'; exit 2; }
REVISION=$(git rev-parse HEAD)
(cd web && npm run build)
mkdir -p var/releases
BUNDLE="var/releases/$REVISION.tar"
git archive --format=tar -o "$BUNDLE" HEAD
tar -rf "$BUNDLE" web/dist
gzip -f "$BUNDLE"
scp -o BatchMode=yes -o ConnectTimeout=15 -i "$KEY" "$BUNDLE.gz" "ec2-user@$HOST:criee-release-$REVISION.tar.gz"
"${SSH[@]}" bash -s -- "$REVISION" <<'REMOTE'
set -euo pipefail
revision=$1
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
release_dir="/opt/criee/releases/$revision"
previous="criee-previous-$(date -u +%Y%m%dT%H%M%S)"
sudo test -f /opt/criee/criee.env
sudo install -d -o ec2-user -g ec2-user "$release_dir"
tar --no-same-owner -xzf "$HOME/criee-release-$revision.tar.gz" -C "$release_dir"
echo "Building $revision while the current demo stays available"
sudo docker build --label "org.opencontainers.image.revision=$revision" -f "$release_dir/deploy/Dockerfile.runtime" -t "criee:$revision" "$release_dir"
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8000/api/status > "$release_dir/before.json"
echo 'Backing up the live SQLite database'
sudo docker exec criee python -c 'import pathlib,sqlite3,sys; p=pathlib.Path("/data/backups"); p.mkdir(exist_ok=True); src=sqlite3.connect("/data/criee.db"); dst=sqlite3.connect(p / (sys.argv[1]+".db")); src.backup(dst); dst.close(); src.close()' "$revision"
sudo docker stop --time 30 criee
if ! sudo docker rename criee "$previous"; then
  sudo docker start criee
  exit 1
fi
rollback() {
  echo 'Startup failed; restoring the previous container against the unchanged data volume' >&2
  sudo docker rm -f criee >/dev/null 2>&1 || true
  sudo docker rename "$previous" criee
  sudo docker update --restart=always criee >/dev/null
  sudo docker start criee
}
trap rollback ERR
sudo docker update --restart=no "$previous" >/dev/null
sudo docker run -d --name criee --restart always -p 127.0.0.1:8000:8000 \
  -v /var/lib/criee/data:/data --env-file /opt/criee/criee.env \
  -e "CRIEE_RELEASE_SHA=$revision" --label "org.opencontainers.image.revision=$revision" "criee:$revision"
healthy=0
for attempt in $(seq 1 60); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:8000/api/status > "$release_dir/health.json"; then
    healthy=1
    break
  fi
  if (( attempt % 10 == 0 )); then echo "Waiting for the ledger to replay ($attempt/60)"; fi
  sleep 2
done
if [[ "$healthy" != 1 ]]; then echo 'Application health check failed' >&2; false; fi
sudo docker exec -i criee python - "$revision" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=5) as response:
    status = json.load(response)
assert status['release'] == sys.argv[1], status
assert status['paypal'] == 'sandbox', status['paypal']
print(json.dumps({'release': status['release'], 'ledger_events': status['ledger']['events'], 'paypal': status['paypal']}))
PY
before_seq=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ledger"]["events"])' "$release_dir/before.json")
before_hash=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ledger"]["head"])' "$release_dir/before.json")
sudo docker exec criee python -c 'import sqlite3,sys; db=sqlite3.connect("/data/criee.db"); row=db.execute("SELECT hash FROM events WHERE seq=?", (int(sys.argv[1]),)).fetchone(); assert row and row[0]==sys.argv[2]; print("Original ledger checkpoint preserved at event", sys.argv[1]); db.close()' "$before_seq" "$before_hash"
trap - ERR
sudo docker tag "criee:$revision" criee:latest
echo "Deployed $revision; rollback container: $previous"
REMOTE
