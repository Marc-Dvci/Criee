# Release the existing demo

Run the test suite, build the web app and run `scripts/judge_smoke.py` before committing. Fetch and retain upstream changes, then push the reviewed commit. GitHub Actions checks Python 3.12/3.13 and the production Chromium journey.

From a clean checkout, run `bash deploy/deploy.sh <host>` with the server's address. On Windows, use Git Bash. The script needs the existing SSH key (`~/.ssh/criee.pem`, overridable with `CRIEE_SSH_KEY`) and the server's existing `/opt/criee/criee.env`. It does not transfer credentials or replace that environment file.

The release script packages exactly `git archive HEAD` plus a fresh `web/dist`, builds an image tagged and labelled with the commit, and leaves the current container running until the build succeeds. Source stays under `/opt/criee/releases/<commit>`.

Before replacing the running container, it backs up SQLite with SQLite's backup API into the existing data volume at `/data/backups/<commit>.db`. The data volume, ledger key, wallet references, session secret and uploaded images remain in place. Deployment has no database-reset option.

The previous container is stopped and renamed, then the new container starts against the same volume and existing credentials. Startup checks verify health, sandbox mode, the expected release SHA and the hash of a ledger event recorded before deployment. `/api/status` exposes the deployed commit as `release`. Startup or checkpoint failure restores the previous container; it does not restore an older database snapshot, which could discard payment events.

On success, the script prints the stopped rollback container's name and disables its restart policy so a Docker restart cannot start two services on the same port. If a later rollback is necessary, stop the new `criee` container, rename it out of the way, rename that previous container back to `criee`, restore its `always` restart policy, and start it. Keep the same data volume. Database backups and retained images are private operational artifacts; prune them only after checking retention and available disk space.

Verify the public demo after deployment: home and tour, official PayPal button, sample photo, console, recent ledger verification and a full audit with live progress. The stable GitHub Pages URL resolves the current Cloudflare tunnel; redeploying the application does not require restarting the tunnel.

Cloudflare Quick Tunnels buffer SSE. The app uses regular JSON refreshes on that host, and ledger audit progress uses `POST /api/ledger/verify/jobs` followed by short JSON requests to the returned job. Running audits of the same scope are shared, storage is bounded to eight jobs, and completed jobs expire or are evicted. The direct SSE verification endpoint remains available for compatible clients. Validate visible progress before audit completion with `python scripts/verify_release.py BASE_URL COMMIT`.
