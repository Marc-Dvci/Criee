# Criée threat model

Updated 3 November 2026. Scope: this repository and the sandbox demo. This is an engineering threat model, not an external security certification or a claim of production readiness.

## Assets and boundaries

- A person's signed wish defines the allowed item category, facts, price, quantity, budget, timing and location. A model can propose those terms; only the browser's signing key or passkey can approve them.
- Browser authority comes from an HttpOnly, SameSite=Lax session cookie, marked Secure on HTTPS. An MCP agent token is a separate, narrower capability. The MCP mount authenticates it; browser APIs reject bearer authentication.
- PayPal merchant secrets, saved payment tokens, agent-token mappings, unrevealed lot seeds and the Ed25519 signing key remain on the server. The v6 SDK gets only the browser-safe OAuth `response_type=client_token`, never the merchant access token.
- The deterministic clearing engine consumes typed inputs, not prompts, photos, model prose or payment credentials. The settlement worker translates its decisions into PayPal operations.
- The signed ledger records decisions and payment outcomes. Public event bodies are redacted; the verifier checks original records on the server.

## Threats, controls and evidence

| Threat | Implemented control | Regression evidence |
|---|---|---|
| Agent turns a draft into spending authority | No signing tool in MCP; agent bearer tokens cannot call browser credential registration, wallet setup or signing APIs | `test_agent_drafts_but_cannot_sign`, `test_agent_bearer_cannot_register_a_signing_key_or_use_browser_authority` |
| Terms change after a person signs | Canonical SHA-256 mandate digest is the signature input; signing verifies draft ownership and digest | `test_wish_is_signed_over_its_exact_terms` |
| Wallet approval is attached to another account | Setup token is bound to its initiating browser account; callback and v6 completion enforce ownership | `test_vault_approval_is_owned_by_session_and_completion_is_idempotent` |
| SDK endpoint exposes privileged merchant credentials | Authenticated, no-store endpoint mints a separate restricted client token using Basic authentication server-side | `test_browser_client_token_never_returns_server_credentials`, `test_v6_token_uses_the_browser_safe_oauth_response_type` |
| Retried approvals duplicate enrollment | Completion is serialized and recorded; PayPal token exchange uses the setup token as its stable idempotency reference | `test_vault_approval_is_owned_by_session_and_completion_is_idempotent` |
| Network retries duplicate charges | Stable `PayPal-Request-Id` per settlement action; settlement facts folded from the ledger | `tests/test_settlement.py` |
| Purchase exceeds budget, violates a constraint or matches after revocation | Typed deterministic eligibility checks; reserved commitments count against caps | Generated invariants in `tests/test_invariants.py`, rules in `tests/test_engine.py` |
| Group commitment and another clearing mode reuse the same weekly authority | Pending long-group commitments reserve count and budget; quorum and waitlist filling recheck eligibility | `test_pending_group_commitment_reserves_weekly_count_and_budget`, `test_group_refill_rechecks_a_waitlisted_wish_after_another_purchase` |
| One declined hold makes other group buyers pay below the funded target | Capture waits for enough successful holds; an underfunded group closes as failed and releases the remaining holds | `test_failed_group_funding_never_captures_the_other_buyers`, `test_declined_hold_is_replaced_from_the_waitlist` |
| Model invents a dietary fact or advice number | Unknown facts fail constraints; shelf facts need evidence; advice numbers are checked | `tests/test_ai.py`, `test_unknown_fact_never_satisfies_a_constraint` |
| Seller scans another stall's pickup or reads another seller's sale | Seller ownership checks on pickup, refund, console, disputes and evidence; buyer can view their own evidence | `tests/test_api.py`, `scripts/judge_smoke.py` |
| Ledger contents or ordering change | SQLite append-only triggers, hash links and Ed25519 signatures; verification checks sequence continuity and a fixed head | `tests/test_ledger.py` |
| A live market invalidates a long audit | Snapshot target captured once; batch reads stop at that target; appends continue during signature work | `test_progress_binds_to_head_even_when_market_keeps_appending` |
| Quick verification gives false assurance about all history | Explicit recent scope, sequence bounds and signed predecessor; UI says earlier history was not checked | `test_recent_check_does_not_claim_to_certify_old_history` |
| Public audit view leaks private account or wish data | Non-public event types have redacted bodies; wish signature, full terms, readback, buyer and place are hidden | `test_public_ledger_hides_account_data_and_private_wish_details` |
| Forged or duplicated webhook changes settlement | PayPal signature verification and deduplication by event ID | `test_webhook_twice_changes_nothing` |
| Judge-facing changes break the working product | Backend/property suites, production TypeScript build, browser flow on an isolated keyless server | `.github/workflows/ci.yml` |

## Limits and residual risks

The host, signing key and SQLite storage are trusted. A compromised host or stolen signing key could forge a replacement history; there is no independent timestamp authority or externally pinned checkpoint. Verification is server-side: redacted public bodies cannot independently reproduce original hashes. A recent check authenticates its predecessor and its selected segment; it does not establish integrity before that anchor.

This is a guest-session sandbox application. A stolen browser cookie has the browser's enrollment authority; there is no production identity-recovery, step-up enrollment or hardened session-management system. MCP bearer tokens remain useful secrets: they allow drafting, viewing pickups and managing existing wishes through MCP. Token expiry and revocation management should be added before production use.

Sandbox money, shared demo wallets and simulated actors are labelled. Recorded AI answers and the offline PayPal double are fallbacks, not proof of a fresh provider call. CI uses the double and mocks the SDK callback contract; real PayPal UI approval and live model outputs require separate sandbox validation. The public demo is not hardened against sustained abuse, and full-chain audits consume CPU. Edge rate limits, payment risk controls, backups, observability and account-level operational policy remain deployment concerns.

PayPal stores the wallet; Criée stores its reference. That fact alone is not a PCI, AP2, ACP, UCP or PayPal Agent Ready compliance claim. Sandbox evidence acceptance is not a promise of winning a production dispute.
