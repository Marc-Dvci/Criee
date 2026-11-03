# Changes following the external competitor review

3 November 2026. Objective: make the existing clearing and PayPal depth easy for judges to experience and verify, while preserving the three clearing mechanisms.

## Decisions

| Review finding | Change | Why it helps judging |
|---|---|---|
| Other projects guide judges and switch roles | Primary home action opens `/tour`; five steps render the real buyer, seller, pickup, console and ledger surfaces | Reduces navigation and setup effort while keeping consent and listing actions explicit |
| Shared wallet hides PayPal | Official v6 Save PayPal button remains visible even when a demo wallet is already saved; direct approval fallback | Judges can see PayPal's own approval without losing the credential-free demo path |
| Competitors name current PayPal products | Implemented v6 Save PayPal, with documented Vault/Orders/Payments/Payouts/Disputes/Toolkit mapping | Adds a current product that directly serves Criée's existing use case |
| Engineering proof is hard to find | GitHub Actions workflow, live CI badge, threat model, source/timing links and a visible 40-hold result | Makes quality claims inspectable instead of asking judges to trust prose |
| Whole-chain check took 21 seconds without feedback | Bounded streaming verification, exact snapshot, progress, elapsed time and a separate recent-200 check | Gives a fast check and a complete audit, with explicit scope for each |
| Setup requires camera or repository download | One-click sample shelf photo in the seller UI, sent through the same upload/read path | Works on desktop without camera permissions or finding a file |

Do not add unrelated subscriptions, Store Sync, Agent Ready or protocol badges solely for name recognition. Current [PayPal v6 Save PayPal documentation](https://developer.paypal.com/sdk/save-without-purchase/paypal/) covers the implemented flow. [Agent Ready](https://developer.paypal.com/agent-ready/overview/) describes separate integration paths; Criée does not claim to implement them. Future distribution integration can be described as future work, with that boundary explicit.

The 40-hold headline uses `data/settlement_timings.json`: 2,627 ms measured sandbox wall time. It is a saved result, not a fresh benchmark or a production SLA. The tour shows its own current burst timing separately. No unverified competitor latency or repository-count claim has been added to the product.

## Audit findings fixed alongside presentation

1. Browser authentication previously accepted the same bearer capability as MCP. An agent could register a new signing key through the HTTP API, undermining the stated signing boundary. Browser endpoints now reject bearer authority, while MCP retains its own authentication.
2. Vault setup completion now checks the initiating session in both callback paths and records completion to avoid duplicate enrollment events. The browser gets no stored payment-token ID or merchant access token.
3. Public ledger output now redacts non-public account/credential events and private wish details. Draft reads require the owning session. Original events remain unchanged for verification.
4. Ledger queries reject negative limits, rather than allowing SQLite's unlimited-read interpretation. Verification checks missing sequences and a fixed snapshot head.
5. Keyless startup now supplies offline wallet and payout references so the advertised local demo actually works. These references are never substituted for real sandbox credentials.
6. Seller photo object URLs are released instead of being recreated and leaked on every render. Signing-link loading waits for session discovery and handles account changes.
7. A generated market exposed a pre-existing weekly-cap violation across a pending group commitment and a closing auction. Long-group commitments now reserve count and budget, quorum excludes expired or otherwise ineligible wishes, and waitlist replacements recheck eligibility at the time the failed hold is replaced. The reservation index is rebuilt from the existing events and avoids scanning historical lots on each eligibility check.
8. Group capture now waits for the target to be backed by successful PayPal holds, including a waitlist replacement after a decline. A group that is still underfunded at its deadline fails and releases the other holds instead of charging those buyers.
9. Fresh Linux CI exposed upstream PayPal Agent Toolkit metadata that pins unused framework adapters, including a CrewAI version incompatible with Python 3.13. Installation now separates the pinned genuine toolkit core from those adapters, declares its shared dependencies explicitly and tests the real invoice/dispute tool declarations. CI, local setup and both Docker builds use the same installation path.

## Validation and practical limits

The automated backend suite covers the clearing rules, generated invariants, settlement, AI guardrails, evidence, consent, SDK token separation, storage corruption, verification snapshots and privacy. The production build checks TypeScript. `scripts/judge_smoke.py` runs a real Chromium browser against the built app: browser-key signature, 40-person group capture, sample photo reading and listing, pickup capture, payout, evidence PDF, both audit scopes, reload/resume and mobile width. Its PayPal v6 test uses an explicitly mocked SDK to check initialization and the approval callback; it does not certify a new live PayPal approval.

Local validation on 3 November 2026:

| Check | Result |
|---|---|
| Backend and generated-property suite | 81 tests; property coverage raised to 500 generated examples per property for release and CI |
| Production TypeScript and Vite build on Node 24 | Passed |
| Chromium journey, interrupted audit recovery, mobile width and reload/resume | Passed; no uncaught browser errors |
| Real PayPal sandbox OAuth client token, v6 SDK loading, vault eligibility and official button | Passed with `--live-paypal-ui`; no wallet approval or payment performed |
| Controlled in-memory 10,000-event ledger | Recent 200: 17.29 ms; full 10,000: 888.71 ms; both valid |

The ledger benchmark measures local cryptographic work with small synthetic event bodies, excluding HTTP and hosted-instance load. It is not a new hosted latency claim. Reproduce the optional real SDK check with `python scripts/judge_smoke.py --live-paypal-ui` using sandbox app credentials; the checker disables settlement, demo actors and webhooks.

The tour is navigation over the real shared market, not an automatically completed script. Other buyers can purchase stock; pickup codes can be scanned only by their lot's seller. Group orders capture at quorum and do not demonstrate counter pickup. The guide identifies those differences and links to the actual purchase evidence. Steps are never labelled complete just because a judge navigated past them.

## Final release polish

The tour's primary action now opens the current task, while continuing to the next role is secondary. Mobile step navigation occupies one scrollable row, and completed group-order details appear only where they are relevant. Leaving the tour restores the page title. Ledger updates coalesce a settlement burst into one refresh rather than sending a request per event.

The deployment script now builds before replacing the running service, backs up SQLite, retains the previous container for rollback, preserves existing server credentials and data, and verifies both the deployed commit and a pre-deployment ledger checkpoint. See [deployment instructions](DEPLOYMENT.md). GitHub CI and public-host verification provide the final release evidence; the badge reflects actual workflow results. No contest outcome can be guaranteed.

The hosted release was checked on 3 November: the recent-200 audit finished in 0.41 s; the full 121,678-event chain verified in 21.96 s with 478 progress frames. Desktop/mobile checks passed for the tour, official PayPal button, sample photo, pickup keypad, console and title cleanup, with no uncaught browser errors. GitHub CI passed on clean Python 3.12/3.13 environments and the production Chromium build. The original ledger checkpoint was preserved, and rollback containers are kept stopped across daemon restarts.
