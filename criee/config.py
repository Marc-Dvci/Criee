"""Runtime configuration, read once from the environment (and `.env` when present)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


@dataclass(frozen=True)
class Config:
    data_dir: Path
    public_url: str
    paypal_client_id: Optional[str]
    paypal_secret: Optional[str]
    currency: str
    fee_bps: int
    demo_vault_id: Optional[str]
    seller_payout_email: Optional[str]
    sandbox_buyer_email: Optional[str]
    sandbox_buyer_password: Optional[str]
    webhook_id: Optional[str]
    ai_provider: str
    gemini_api_key: Optional[str]
    gemini_model: str
    ai_until: Optional[date]
    ai_daily_cap: int
    ai_per_visitor_hourly: int
    session_secret: str
    rp_id: Optional[str]
    clock_scale: float
    ai_daily_usd: float = 1.0
    ai_usd_in_per_m: float = 0.75
    ai_usd_out_per_m: float = 3.75

    @property
    def paypal_live(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_secret)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "criee.db"


def load(env_file: Optional[Path] = None) -> Config:
    _load_dotenv(env_file or ROOT / ".env")
    e = os.environ.get
    data_dir = Path(e("CRIEE_DATA_DIR", str(ROOT / "var")))
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file = data_dir / "session.secret"
    if not secret_file.exists():
        secret_file.write_text(os.urandom(32).hex())
    until = e("CRIEE_AI_UNTIL")
    return Config(
        data_dir=data_dir,
        public_url=e("CRIEE_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
        paypal_client_id=e("PAYPAL_CLIENT_ID") or None,
        paypal_secret=e("PAYPAL_CLIENT_SECRET") or None,
        currency=e("PAYPAL_CURRENCY", "EUR"),
        fee_bps=int(e("CRIEE_FEE_BPS", "400")),
        demo_vault_id=e("CRIEE_DEMO_VAULT_ID") or None,
        seller_payout_email=e("SANDBOX_SELLER_EMAIL") or None,
        sandbox_buyer_email=e("SANDBOX_BUYER_EMAIL") or None,
        sandbox_buyer_password=e("SANDBOX_BUYER_PASSWORD") or None,
        webhook_id=e("PAYPAL_WEBHOOK_ID") or None,
        ai_provider=e("CRIEE_AI_PROVIDER", "recorded"),
        gemini_api_key=e("GEMINI_API_KEY") or None,
        gemini_model=e("CRIEE_GEMINI_MODEL", "gemini-3.7-flash"),
        ai_until=date.fromisoformat(until) if until else None,
        ai_daily_cap=int(e("CRIEE_AI_DAILY_CAP", "400")),
        ai_per_visitor_hourly=int(e("CRIEE_AI_PER_VISITOR_HOURLY", "30")),
        session_secret=secret_file.read_text().strip(),
        rp_id=e("CRIEE_RP_ID") or None,
        clock_scale=float(e("CRIEE_CLOCK_SCALE", "1")),
        ai_daily_usd=float(e("CRIEE_AI_DAILY_USD", "1.0")),
        ai_usd_in_per_m=float(e("CRIEE_AI_USD_IN_PER_M", "0.75")),
        ai_usd_out_per_m=float(e("CRIEE_AI_USD_OUT_PER_M", "3.75")),
    )
