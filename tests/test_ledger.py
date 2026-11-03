"""Verification must detect tampering, keep snapshot boundaries, and report its actual scope."""
import pytest

from helpers import Harness, T0


def ledger_with_events(n):
    h = Harness()
    for i in range(n):
        h.ledger.append("test", {"n": i}, T0)
    return h.ledger


def tamper(ledger, sql):
    ledger._db.execute("DROP TRIGGER events_no_update")
    ledger._db.execute("DROP TRIGGER events_no_delete")
    ledger._db.execute(sql)


def test_progress_binds_to_head_even_when_market_keeps_appending():
    ledger = ledger_with_events(6)
    audit = ledger.verify_progress(batch_size=2)
    first = next(audit)
    assert first["checked"] == 0 and first["total"] == 6 and not first["done"]
    ledger.append("new", {}, T0)
    results = list(audit)
    assert [r["checked"] for r in results] == [2, 4, 6, 6]
    assert results[-1]["ok"] and results[-1]["to_seq"] == 6
    assert results[-1]["head"] == first["snapshot_head"] != ledger.head[1]


@pytest.mark.parametrize(("sql", "reason"), [
    ("UPDATE events SET body = '{}' WHERE seq = 4", "hash mismatch"),
    ("UPDATE events SET prev = 'bad' WHERE seq = 4", "broken link"),
    ("UPDATE events SET sig = 'bad' WHERE seq = 4", "bad signature"),
    ("DELETE FROM events WHERE seq = 4", "missing event"),
    ("DELETE FROM events WHERE seq = 6", "missing event"),
])
def test_full_verification_detects_storage_corruption(sql, reason):
    ledger = ledger_with_events(6)
    tamper(ledger, sql)
    result = list(ledger.verify_progress(batch_size=2))[-1]
    assert result["done"] and not result["ok"] and result["reason"] == reason


def test_recent_check_does_not_claim_to_certify_old_history():
    ledger = ledger_with_events(10)
    tamper(ledger, "UPDATE events SET body = '{}' WHERE seq = 1")
    recent = list(ledger.verify_progress(recent=3))[-1]
    assert recent["ok"] and recent["scope"] == "recent" and recent["events"] == 3
    assert (recent["from_seq"], recent["to_seq"]) == (8, 10)
    assert not ledger.verify()["ok"]


def test_recent_anchor_must_have_an_authentic_signature():
    ledger = ledger_with_events(10)
    tamper(ledger, "UPDATE events SET sig = 'bad' WHERE seq = 7")
    result = list(ledger.verify_progress(recent=3))[-1]
    assert not result["ok"] and result["reason"] == "bad anchor"


def test_empty_chain_and_invalid_sizes():
    ledger = ledger_with_events(0)
    result = ledger.verify()
    assert result["ok"] and result["events"] == 0
    with pytest.raises(ValueError):
        list(ledger.verify_progress(recent=0))
    with pytest.raises(ValueError):
        list(ledger.verify_progress(batch_size=0))
