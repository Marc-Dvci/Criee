"""Play as a judge: one wish for a vegetable box completes the farm's group order."""
import base64
import sys
import time

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
key = ec.generate_private_key(ec.SECP256R1())
n = key.public_key().public_numbers()
c = httpx.Client(base_url=BASE, timeout=90)
c.post("/api/play").raise_for_status()
cid = c.post("/api/devicekey/register", json={"jwk": {"kty": "EC", "crv": "P-256", "x": b64u(n.x.to_bytes(32, "big")),
                                                       "y": b64u(n.y.to_bytes(32, "big"))}}).json()["credential_id"]
d = c.post("/api/wishes/compile", json={"text": "A 10 kg vegetable box from a local farm, up to 28 euros",
                                         "lat": 48.5795, "lon": 7.7555, "place": "Krutenau"}).json()
print("readback:", d["readback"])
r, s = decode_dss_signature(key.sign(bytes.fromhex(d["digest"]), ec.ECDSA(hashes.SHA256())))
w = c.post(f"/api/drafts/{d['id']}/sign", json={"kind": "device", "credential_id": cid,
                                                 "signature": b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}).json()
print("signed", w["id"], "commitments", w.get("commitments"))
for _ in range(40):
    time.sleep(2)
    lots = [l for l in c.get("/api/board").json()["lots"] if l["mode"] == "threshold"]
    t = lots[0]["threshold"] if lots else {}
    if t.get("burst"):
        print("burst:", t["burst"])
        break
time.sleep(10)
m = c.get("/api/matches").json()["matches"]
print("judge match:", [(x["name"], x["status"], x["paypal"]["authorization_id"], x["paypal"]["capture_id"]) for x in m])
