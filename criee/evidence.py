from dataclasses import dataclass
from io import BytesIO
from typing import Any, Callable
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


@dataclass(frozen=True)
class LedgerLine:
    seq: int
    at: str
    type: str
    hash: str
    prev: str


@dataclass(frozen=True)
class FactLine:
    attr: str
    value: str
    source: str


@dataclass(frozen=True)
class EvidencePack:
    dispute_id: str
    dispute_reason: str
    disputed_amount: str
    match_id: str
    units: int
    unit_price: str
    total: str
    rule: str
    round: int
    matched_at: str
    wish_id: str
    readback: str
    terms_hash: str
    signed_at: str
    signature_kind: str
    credential_id: str
    lot_id: str
    item_name: str
    shop_name: str
    facts: tuple[FactLine, ...]
    pickup_code: str
    scanned_at: str | None
    tracking_number: str | None
    order_id: str
    authorization_id: str
    capture_id: str
    ledger: tuple[LedgerLine, ...]
    ledger_head: str
    ledger_verified: bool
    public_key_pem: str
    crop_images: tuple[tuple[str, bytes], ...] = ()


ACCENT = colors.HexColor("#1F5BFF")
PANEL = colors.HexColor("#F3F4F6")
RULE = colors.HexColor("#D1D5DB")
INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6B7280")
MARGIN = 18 * mm
WIDTH = A4[0] - 2 * MARGIN
CROP_MAX_H = 35 * mm
MAX_CROPS = 4
LEDGER_EDGE = 10
READBACK_MAX = 1000
NOTES_MAX = 2000

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=12.5, textColor=INK)
LABEL = ParagraphStyle("label", parent=BODY, textColor=MUTED)
NOTE = ParagraphStyle("note", parent=BODY, fontSize=8.5, leading=11, textColor=MUTED)
MONO = ParagraphStyle("mono", parent=BODY, fontName="Courier", fontSize=8, leading=10.5, wordWrap="CJK")
TITLE = ParagraphStyle("title", parent=BODY, fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=ACCENT)
HEADING = ParagraphStyle("heading", parent=BODY, fontName="Helvetica-Bold", fontSize=11.5, leading=14,
                         textColor=ACCENT, spaceBefore=12, spaceAfter=5, keepWithNext=1)
READBACK_STYLE = ParagraphStyle("readback", parent=BODY, backColor=PANEL, borderPadding=7,
                                spaceBefore=2, spaceAfter=6)


def _plain(text: str) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(limit - 3, 0)] + "..."


def _latin(text: str) -> str:
    return text.encode("cp1252", "replace").decode("cp1252")


def _p(text: str, style: ParagraphStyle = BODY) -> Paragraph:
    return Paragraph(_latin(escape(_plain(text))), style)


def _mono(text: str) -> Paragraph:
    return _p(text, MONO)


def _table(rows: list[list[Any]], widths: list[float], header: bool = False,
           extra: tuple[tuple[Any, ...], ...] = ()) -> Table:
    table = Table(rows, colWidths=widths, hAlign="LEFT", repeatRows=int(header))
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        *extra,
    ]))
    return table


def _kv(rows: list[tuple[str, Any]]) -> Table:
    return _table([[_p(k, LABEL), v] for k, v in rows], [38 * mm, WIDTH - 38 * mm])


def _readback(text: str) -> Paragraph:
    shown = _plain(text)
    if len(shown) > READBACK_MAX:
        shown = f"{shown[:READBACK_MAX]} [{len(shown) - READBACK_MAX} characters not shown]"
    return _p(shown, READBACK_STYLE)


def _signature(pack: EvidencePack) -> Paragraph:
    kind = _latin(escape(_plain(pack.signature_kind)))
    credential = _latin(escape(_plain(pack.credential_id)))
    return Paragraph(f'{kind}<br/><font face="Courier" size="8">{credential}</font>', BODY)


def _facts(facts: tuple[FactLine, ...]) -> list[Any]:
    if not facts:
        return [_p("No facts recorded.", NOTE)]
    head = [[_p(h, LABEL) for h in ("Attribute", "Value", "Source")]]
    rows = [[_p(f.attr), _p(f.value), _p(f.source)] for f in facts]
    return [_table(head + rows, [40 * mm, 70 * mm, WIDTH - 110 * mm], header=True)]


def _crop(crop_id: str, data: bytes, width: float) -> list[Any]:
    try:
        w, h = ImageReader(BytesIO(data)).getSize()
    except Exception:
        return [_p("image unavailable", NOTE), _mono(crop_id)]
    scale = min(CROP_MAX_H / h, (width - 12) / w)
    return [Image(BytesIO(data), width=w * scale, height=h * scale, mask="auto"), Spacer(1, 2 * mm), _mono(crop_id)]


def _crops(crops: tuple[tuple[str, bytes], ...]) -> list[Any]:
    if not crops:
        return []
    shown = crops[:MAX_CROPS]
    width = WIDTH / len(shown)
    row = [_crop(crop_id, data, width) for crop_id, data in shown]
    return [Spacer(1, 6), _p("Image crops", LABEL), Spacer(1, 2),
            Table([row], colWidths=[width] * len(shown), hAlign="LEFT")]


def _ledger(lines: tuple[LedgerLine, ...]) -> Table:
    rows: list[list[Any]] = [[_p(h, LABEL) for h in ("Seq", "Time", "Type", "Hash (first 16)", "Prev (first 16)")]]
    spans: list[tuple[Any, ...]] = []
    entries: list[LedgerLine | None] = list(lines)
    hidden = len(entries) - 2 * LEDGER_EDGE
    if hidden > 0:
        entries = entries[:LEDGER_EDGE] + [None] + entries[-LEDGER_EDGE:]
    for line in entries:
        if line is None:
            spans.append(("SPAN", (0, len(rows)), (-1, len(rows))))
            rows.append([_p(f"{hidden} events between these rows are not listed. The head hash commits to every event.",
                            NOTE)] + [""] * 4)
        else:
            rows.append([_p(str(line.seq)), _p(line.at), _p(line.type), _mono(line.hash[:16]), _mono(line.prev[:16])])
    return _table(rows, [14 * mm, 46 * mm, 48 * mm, 33 * mm, 33 * mm], header=True, extra=tuple(spans))


def _footer(dispute_id: str) -> Callable[[Any, Any], None]:
    def draw(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFillColorRGB(1, 1, 1)
        canvas.rect(0, 0, A4[0], A4[1], stroke=0, fill=1)
        canvas.setFillColor(MUTED)
        y = 9 * mm
        lead = "Criée · "
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(MARGIN, y, lead)
        x = MARGIN + stringWidth(lead, "Helvetica", 7.5)
        canvas.setFont("Courier", 7.5)
        canvas.drawString(x, y, dispute_id)
        x += stringWidth(dispute_id, "Courier", 7.5)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(x, y, f" · page {canvas.getPageNumber()}")
        canvas.restoreState()
    return draw


def _story(pack: EvidencePack) -> list[Any]:
    tracking = _mono(pack.tracking_number) if pack.tracking_number is not None else _p("none")
    return [
        _p("Criée evidence pack", TITLE),
        Spacer(1, 4),
        _kv([("Dispute id", _mono(pack.dispute_id)), ("Reason", _p(pack.dispute_reason)),
             ("Disputed amount", _p(pack.disputed_amount))]),
        Spacer(1, 3),
        _p("Generated from Criée's signed event log.", NOTE),
        _p("The signed mandate", HEADING),
        _p("Readback", LABEL),
        Spacer(1, 2),
        _readback(pack.readback),
        Spacer(1, 6),
        _kv([("Wish id", _mono(pack.wish_id)), ("Signed at", _p(pack.signed_at)),
             ("Signature", _signature(pack)), ("Mandate hash", _mono(pack.terms_hash))]),
        _p("The match", HEADING),
        _kv([("Match id", _mono(pack.match_id)), ("Lot id", _mono(pack.lot_id)), ("Item", _p(pack.item_name)),
             ("Shop", _p(pack.shop_name)), ("Quantity", _p(f"{pack.units} x {pack.unit_price} = {pack.total}")),
             ("Clearing rule", _p(pack.rule)), ("Round", _p(str(pack.round))),
             ("Matched at", _p(pack.matched_at))]),
        _p("Facts the match relied on", HEADING),
        *_facts(pack.facts),
        *_crops(pack.crop_images),
        _p("Collection", HEADING),
        _kv([("Pickup code", _mono(pack.pickup_code)),
             ("Scanned at", _p(pack.scanned_at if pack.scanned_at is not None else "not scanned")),
             ("Tracking number", tracking), ("PayPal order", _mono(pack.order_id)),
             ("Authorization", _mono(pack.authorization_id)), ("Capture", _mono(pack.capture_id))]),
        _p("Ledger proof", HEADING),
        _ledger(pack.ledger),
        Spacer(1, 4),
        _p("Each event commits to the hash of the previous one and is signed with Criée's Ed25519 key."),
        Spacer(1, 4),
        _kv([("Ledger head", _mono(pack.ledger_head))]),
        Spacer(1, 4),
        _p(f"Chain verified: {'yes' if pack.ledger_verified else 'no'}"),
        Spacer(1, 6),
        _p("Public key (PEM)", LABEL),
        Paragraph(_latin(escape(pack.public_key_pem.strip())).replace("\n", "<br/>"), MONO),
    ]


def build_evidence_pdf(pack: EvidencePack) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN,
                            bottomMargin=MARGIN, title=f"Criée evidence pack {pack.dispute_id}", author="Criée")
    footer = _footer(pack.dispute_id)
    doc.build(_story(pack), onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _fulfilment_notes(pack: EvidencePack) -> str:
    scan = (f"scanned at {pack.scanned_at}" if pack.scanned_at is not None
            else "not scanned; no pickup scan is recorded in Criée's signed event log")
    text = (f"Pickup of {pack.item_name} (lot {pack.lot_id}, match {pack.match_id}) at {pack.shop_name}. "
            f"Pickup code {pack.pickup_code}: {scan}.")
    return _cut(_plain(text), NOTES_MAX)


def _mandate_notes(pack: EvidencePack) -> str:
    head = _plain(f'Standing mandate {pack.wish_id} was signed at {pack.signed_at} '
                  f'({pack.signature_kind}, credential {pack.credential_id}). Readback: "')
    tail = _plain(f'" Mandate hash: {pack.terms_hash}. Purchase {pack.match_id}: {pack.units} unit(s) at '
                  f'{pack.unit_price}, total {pack.total}, clearing rule {pack.rule}, round {pack.round}, '
                  f"matched at {pack.matched_at}. The purchase was inside the mandate's price, quantity and weekly "
                  f"limits. Ledger head {pack.ledger_head}; chain verified: "
                  f"{'yes' if pack.ledger_verified else 'no'}.")
    readback = _cut(_plain(pack.readback), max(NOTES_MAX - len(head) - len(tail), 3))
    return _cut(head + readback + tail, NOTES_MAX)


def evidence_entries(pack: EvidencePack) -> list[dict[str, Any]]:
    fulfilment: dict[str, Any] = {"evidence_type": "PROOF_OF_FULFILLMENT"}
    if pack.tracking_number is not None:
        fulfilment["evidence_info"] = {"tracking_info": [{
            "carrier_name": "OTHER", "carrier_name_other": "Collected in store",
            "tracking_number": pack.tracking_number}]}
    fulfilment["notes"] = _fulfilment_notes(pack)
    return [fulfilment, {"evidence_type": "OTHER", "notes": _mandate_notes(pack)}]
