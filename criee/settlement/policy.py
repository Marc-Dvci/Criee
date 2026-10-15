"""What the settlement worker must do next, as a pure function of market state.

PENDING matches get a hold. A hold is captured when the buyer's pickup code is scanned, or, for a
threshold lot, when the threshold is reached. A hold is voided on a no-show, when a threshold
fails, or when the seller withdraws the lot. A pending match whose lot can no longer complete is
voided without calling PayPal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..domain import ClearingMode
from ..engine import Market, MatchStatus


@dataclass(frozen=True)
class Action:
    kind: str          # authorize | capture | void
    match_id: str
    paypal: bool = True


def settlement_actions(market: Market, now: datetime) -> list[Action]:
    out: list[Action] = []
    for mid, m in sorted(market.matches.items()):
        lot = market.lots[m.fill.lot_id]
        mode = lot.terms.mode
        dead = lot.closed in ("threshold_failed", "withdrawn")
        if m.status is MatchStatus.PENDING:
            out.append(Action("void", mid, paypal=False) if dead else Action("authorize", mid))
        elif m.status is MatchStatus.HELD:
            if dead:
                out.append(Action("void", mid))
            elif mode is ClearingMode.THRESHOLD:
                if lot.reached:
                    out.append(Action("capture", mid))
            elif m.scanned:
                out.append(Action("capture", mid))
        elif m.status is MatchStatus.NO_SHOW:
            out.append(Action("void", mid))
    return out
