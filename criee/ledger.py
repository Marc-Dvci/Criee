"""Append-only, hash-chained, signed event log on SQLite.

Each event stores the hash of its predecessor; the server signs every event hash with Ed25519.
`verify()` recomputes the chain from the first event and checks every signature.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import canonical, iso, parse_iso, sha256_hex

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    body TEXT NOT NULL,
    prev TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    sig TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_type ON events(type);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


@dataclass(frozen=True)
class Event:
    seq: int
    at: datetime
    type: str
    body: dict[str, Any]
    prev: str
    hash: str
    sig: str

    def public(self) -> dict[str, Any]:
        return {"seq": self.seq, "at": iso(self.at), "type": self.type, "body": self.body,
                "prev": self.prev, "hash": self.hash, "sig": self.sig}


def event_hash(prev: str, at: datetime, type_: str, body: dict[str, Any]) -> str:
    return sha256_hex(prev.encode() + canonical({"at": iso(at), "type": type_, "body": body}))


class Signer:
    def __init__(self, key: Ed25519PrivateKey):
        self._key = key
        self.public_key: Ed25519PublicKey = key.public_key()

    @classmethod
    def load_or_create(cls, path: Path) -> "Signer":
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            assert isinstance(key, Ed25519PrivateKey)
            return cls(key)
        key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        return cls(key)

    def sign(self, hex_digest: str) -> str:
        return base64.b64encode(self._key.sign(bytes.fromhex(hex_digest))).decode()

    def public_pem(self) -> str:
        return self.public_key.public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def verify(self, hex_digest: str, sig: str) -> bool:
        try:
            self.public_key.verify(base64.b64decode(sig), bytes.fromhex(hex_digest))
            return True
        except Exception:
            return False


class Ledger:
    def __init__(self, path: str | Path, signer: Signer):
        self.path = str(path)
        self.signer = signer
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        row = self._db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        self._head_seq, self._head = (row[0], row[1]) if row else (0, GENESIS)

    @property
    def head(self) -> tuple[int, str]:
        with self._lock:
            return self._head_seq, self._head

    def append(self, type_: str, body: dict[str, Any], at: datetime) -> Event:
        body = json.loads(canonical(body))
        with self._lock:
            prev = self._head
            h = event_hash(prev, at, type_, body)
            sig = self.signer.sign(h)
            cur = self._db.execute(
                "INSERT INTO events(at, type, body, prev, hash, sig) VALUES (?,?,?,?,?,?)",
                (iso(at), type_, canonical(body).decode(), prev, h, sig))
            seq = int(cur.lastrowid)
            self._head_seq, self._head = seq, h
            return Event(seq, parse_iso(iso(at)), type_, body, prev, h, sig)

    def append_many(self, items: list[tuple[str, dict[str, Any], datetime]]) -> list[Event]:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                out = [self.append(t, b, a) for t, b, a in items]
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                row = self._db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                self._head_seq, self._head = (row[0], row[1]) if row else (0, GENESIS)
                raise
            return out

    def events(self, after: int = 0, types: Optional[list[str]] = None, limit: int = 1_000_000) -> Iterator[Event]:
        q = "SELECT seq, at, type, body, prev, hash, sig FROM events WHERE seq > ?"
        args: list[Any] = [after]
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        q += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        for seq, at, type_, body, prev, h, sig in rows:
            yield Event(seq, parse_iso(at), type_, json.loads(body), prev, h, sig)

    def get(self, seq: int) -> Optional[Event]:
        return next(self.events(after=seq - 1, limit=1), None)

    def verify_progress(self, recent: Optional[int] = None, batch_size: int = 256) -> Iterator[dict[str, Any]]:
        """Verify a fixed head in bounded batches, without blocking appends during crypto work.

        A recent check authenticates its predecessor as an anchor; it explicitly does not
        certify the earlier history. Full verification always starts at genesis, with no cache.
        """
        if batch_size < 1 or (recent is not None and recent < 1):
            raise ValueError("verification sizes must be positive")
        target, target_hash = self.head
        start = max(1, target - recent + 1) if recent is not None else 1
        total = max(0, target - start + 1)
        base = {"scope": "recent" if recent is not None else "full", "from_seq": start,
                "to_seq": target, "total": total, "snapshot_head": target_hash}
        n, cursor, prev = 0, start - 1, GENESIS
        yield {**base, "done": False, "checked": 0}
        if cursor:
            anchor = self.get(cursor)
            if anchor is None or anchor.seq != cursor or not self.signer.verify(anchor.hash, anchor.sig):
                yield {**base, "done": True, "ok": False, "checked": 0, "seq": cursor, "reason": "bad anchor"}
                return
            prev = anchor.hash
        while cursor < target:
            batch = list(self.events(after=cursor, limit=min(batch_size, target - cursor)))
            if not batch:
                yield {**base, "done": True, "ok": False, "checked": n, "seq": cursor + 1, "reason": "missing event"}
                return
            for ev in batch:
                reason = None
                if ev.seq != cursor + 1:
                    reason = "missing event"
                elif ev.prev != prev:
                    reason = "broken link"
                elif event_hash(prev, ev.at, ev.type, ev.body) != ev.hash:
                    reason = "hash mismatch"
                elif not self.signer.verify(ev.hash, ev.sig):
                    reason = "bad signature"
                if reason:
                    yield {**base, "done": True, "ok": False, "checked": n, "seq": ev.seq, "reason": reason}
                    return
                prev, cursor, n = ev.hash, ev.seq, n + 1
            yield {**base, "done": False, "checked": n}
        ok = prev == target_hash
        yield {**base, "done": True, "ok": ok, "checked": n, "events": n, "head": prev,
               **({"seq": target, "reason": "snapshot head mismatch"} if not ok else {})}

    def verify(self) -> dict[str, Any]:
        for result in self.verify_progress():
            if result["done"]:
                return result
        raise RuntimeError("verification did not finish")

    # Small private key-value store for server-side secrets (clock seeds before reveal, tokens).
    def kv_get(self, k: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return row[0] if row else None

    def kv_set(self, k: str, v: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))

    def close(self) -> None:
        self._db.close()
