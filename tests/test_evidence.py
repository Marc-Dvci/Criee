import hashlib
import re
from dataclasses import replace
from io import BytesIO

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4

from criee.evidence import EvidencePack, FactLine, LedgerLine, build_evidence_pdf, evidence_entries

READBACK = ("Buy 1 Pain de campagne from Boulangerie Émile & Fils <Petite France>, "
            "up to 3.80 EUR, pickup Tue 18:00-19:00.")
PAGE = re.compile(rb"/Type\s*/Page(?!s)")
PEM = Ed25519PrivateKey.generate().public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _ledger(n: int) -> tuple[LedgerLine, ...]:
    kinds = ["mandate.signed", "lot.posted", "match.formed", "match.held", "pickup.scanned", "settlement.captured"]
    lines, prev = [], "0" * 64
    for seq in range(1, n + 1):
        h = _hash(f"{prev}|{seq}")
        lines.append(LedgerLine(seq, f"2026-11-03T{17 + seq // 60:02d}:{seq % 60:02d}:00Z",
                                kinds[(seq - 1) % len(kinds)], h, prev))
        prev = h
    return tuple(lines)


def _crop_png() -> bytes:
    img = Image.new("RGB", (120, 80), (243, 244, 246))
    ImageDraw.Draw(img).rectangle((8, 8, 111, 71), outline=(31, 91, 255), width=3)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def sample_pack(**changes: object) -> EvidencePack:
    ledger = _ledger(6)
    base = EvidencePack(
        dispute_id="PP-D-7K2Q9X1", dispute_reason="UNAUTHORISED_TRANSACTION", disputed_amount="3.80 EUR",
        match_id="m_7f3a91c2", units=1, unit_price="3.80 EUR", total="3.80 EUR", rule="clock", round=12,
        matched_at="2026-11-03T17:12:04Z",
        wish_id="w_2b9e6d10", readback=READBACK, terms_hash=_hash("terms:w_2b9e6d10"),
        signed_at="2026-10-30T09:15:00Z", signature_kind="ed25519", credential_id="cred_5a0f33b8e1",
        lot_id="lot_0412", item_name="Pain de campagne 800 g", shop_name="Boulangerie Émile & Fils",
        facts=(FactLine("item", "Pain de campagne 800 g", "label crop crop_ab12"),
               FactLine("price", "3.80 EUR", "seller confirmed")),
        pickup_code="4471", scanned_at="2026-11-03T17:41:19Z", tracking_number=None,
        order_id="5O190127TN364715T", authorization_id="8MN10410AR800305M", capture_id="2GG279541U471931P",
        ledger=ledger, ledger_head=ledger[-1].hash, ledger_verified=True, public_key_pem=PEM,
        crop_images=(("crop_ab12", _crop_png()),),
    )
    return replace(base, **changes)


def _pages(pdf: bytes) -> int:
    return len(PAGE.findall(pdf))


def _text(pdf: bytes) -> str:
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(BytesIO(pdf))
    return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


def test_pdf_is_a4_within_three_pages():
    pdf = build_evidence_pdf(sample_pack())
    assert pdf.startswith(b"%PDF")
    assert 1 <= _pages(pdf) <= 3
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(BytesIO(build_evidence_pdf(sample_pack())))
    assert len(reader.pages) <= 3
    assert float(reader.pages[0].mediabox.width) == pytest.approx(A4[0])
    assert float(reader.pages[0].mediabox.height) == pytest.approx(A4[1])


def test_pdf_text_carries_user_text_unescaped_and_sections_in_order():
    text = _text(build_evidence_pdf(sample_pack()))
    assert "Criée evidence pack" in text
    assert "PP-D-7K2Q9X1" in text
    assert "Émile & Fils" in text
    assert "<Petite France>" in text
    assert "&amp;" not in text and "&lt;" not in text
    order = ["The signed mandate", "The match", "Facts the match relied on", "Collection", "Ledger proof"]
    positions = [text.index(heading) for heading in order]
    assert positions == sorted(positions)
    assert "Chain verified: yes" in text
    assert "Each event commits to the hash of the previous one" in text


def test_entries_are_two_plain_notes_within_limit():
    entries = evidence_entries(sample_pack())
    assert [e["evidence_type"] for e in entries] == ["PROOF_OF_FULFILLMENT", "OTHER"]
    assert "evidence_info" not in entries[0]
    for entry in entries:
        assert len(entry["notes"]) <= 2000
    assert READBACK in entries[1]["notes"]
    assert "chain verified: yes" in entries[1]["notes"]


def test_tracking_number_adds_carrier_info():
    entries = evidence_entries(sample_pack(tracking_number="LA123456789FR"))
    assert entries[0]["evidence_info"] == {"tracking_info": [{
        "carrier_name": "OTHER", "carrier_name_other": "Collected in store", "tracking_number": "LA123456789FR"}]}


def test_unscanned_pickup_is_not_described_as_collected():
    pack = sample_pack(scanned_at=None)
    notes = evidence_entries(pack)[0]["notes"]
    assert "not scanned" in notes
    assert "scanned at" not in notes
    assert "not scanned" in _text(build_evidence_pdf(pack))


def test_long_readback_renders_capped_and_is_truncated_in_notes():
    readback = ("Émile & <b> " * 500)[:5000]
    pack = sample_pack(readback=readback)
    pdf = build_evidence_pdf(pack)
    assert pdf.startswith(b"%PDF")
    assert 1 <= _pages(pdf) <= 3
    assert re.search(r"\[\d{4} characters not shown\]", _text(pdf))
    notes = evidence_entries(pack)[1]["notes"]
    assert len(notes) <= 2000
    assert "Émile & <b>" in notes
    assert "..." in notes
    assert "chain verified: yes." in notes


def test_long_ledger_is_capped_and_stays_within_three_pages():
    ledger = _ledger(400)
    pdf = build_evidence_pdf(sample_pack(ledger=ledger, ledger_head=ledger[-1].hash))
    assert 1 <= _pages(pdf) <= 3


def test_worst_case_layout_stays_within_three_pages():
    facts = tuple(FactLine(f"attribute {i}", "value text " * 3, "label crop crop_ab12") for i in range(24))
    crops = tuple((f"crop_{i:02d}", _crop_png()) for i in range(6))
    ledger = _ledger(400)
    pack = sample_pack(facts=facts, crop_images=crops, ledger=ledger, ledger_head=ledger[-1].hash,
                       readback=("Émile & <b> " * 500)[:5000], tracking_number="LA123456789FR")
    assert 1 <= _pages(build_evidence_pdf(pack)) <= 3


def test_unreadable_crop_does_not_crash_the_pack():
    pdf = build_evidence_pdf(sample_pack(crop_images=(("crop_bad", b"not an image"),)))
    assert pdf.startswith(b"%PDF")
    assert "image unavailable" in _text(pdf)


def test_empty_facts_render_a_placeholder():
    pdf = build_evidence_pdf(sample_pack(facts=(), crop_images=()))
    assert "No facts recorded." in _text(pdf)
