"""Encrypted browser channel: a Python client doing exactly what static/js/channel.js does."""
import base64
import json
import os
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from app import config
from app.main import app
from tests.test_app import PW, mkuser

H = {"X-BT": "1"}


class Client:
    def __init__(self):
        self.http = TestClient(app)
        priv = ec.generate_private_key(ec.SECP256R1())
        pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        r = self.http.post("/api/hs", json={"pub": base64.b64encode(pub).decode()}, headers=H).json()
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.b64decode(r["pub"]))
        key = HKDF(hashes.SHA256(), 32, base64.b64decode(r["salt"]), b"billtrack-channel-v1").derive(priv.exchange(ec.ECDH(), peer))
        self.aes, self.cid, self.seq = AESGCM(key), r["cid"], 0
        self.last_raw = None

    def envelope(self, method, path, body=None, seq=None):
        self.seq = seq or self.seq + 1
        pt = json.dumps({"m": method, "p": path, "b": body, "s": self.seq, "t": int(time.time() * 1000)}).encode()
        n = os.urandom(12)
        return base64.b64encode(n + self.aes.encrypt(n, pt, f"{self.cid}|req".encode())).decode()

    def call(self, method, path, body=None, raw_env=None):
        env = raw_env or self.envelope(method, path, body)
        r = self.http.post("/api/x", content=env, headers={**H, "X-BT-C": self.cid, "Content-Type": "text/plain"})
        if r.status_code != 200:
            return r.status_code, None
        self.last_raw = r.content
        b = base64.b64decode(r.content)
        res = json.loads(self.aes.decrypt(b[:12], b[12:], f"{self.cid}|res|{self.seq}".encode()))
        body = base64.b64decode(res["b"])
        return res["s"], (json.loads(body) if "json" in res["h"].get("content-type", "") and body else body)


@pytest.fixture(autouse=True)
def require_channel(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_CHANNEL", True)


def test_plaintext_api_blocked_for_browsers():
    c = TestClient(app)
    assert c.get("/api/me").status_code == 403
    assert c.post("/api/login", json={"username": "jc", "password": PW}, headers=H).status_code == 403


def test_channel_login_and_data_are_opaque():
    mkuser("jc", "admin", "jc@example.com")
    c = Client()
    s, out = c.call("POST", "/api/login", {"username": "jc", "password": PW})
    assert s == 200 and out["ok"]
    s, me = c.call("GET", "/api/me")
    assert s == 200 and me["username"] == "jc"
    c.call("POST", "/api/accounts", {"name": "ZZOPAQUE"})
    s, accts = c.call("GET", "/api/accounts")
    assert any(a["name"] == "ZZOPAQUE" for a in accts)
    assert b"ZZOPAQUE" not in c.last_raw and b"name" not in c.last_raw
    # identical request twice -> different bytes on the wire both ways
    c.call("GET", "/api/accounts"); a = c.last_raw
    c.call("GET", "/api/accounts"); b = c.last_raw
    assert a != b and len(a) != len(b) or a != b


def test_replay_tamper_and_unknown_channel_rejected():
    mkuser("jc", "admin")
    c = Client()
    c.call("POST", "/api/login", {"username": "jc", "password": PW})
    env = c.envelope("GET", "/api/me")
    assert c.call(None, None, raw_env=env)[0] == 200
    assert c.call(None, None, raw_env=env)[0] == 419                     # replay
    bad = bytearray(base64.b64decode(c.envelope("GET", "/api/me"))); bad[-1] ^= 1
    assert c.call(None, None, raw_env=base64.b64encode(bytes(bad)).decode())[0] == 419  # tampered
    r = c.http.post("/api/x", content=c.envelope("GET", "/api/me"), headers={**H, "X-BT-C": "nope"})
    assert r.status_code == 419
    assert c.call("GET", "/api/x")[0] == 400                               # no tunnelling the tunnel


def test_channel_bound_to_session():
    mkuser("jc", "admin")
    c = Client()
    c.call("POST", "/api/login", {"username": "jc", "password": PW})
    c.http.cookies.set("bt_session", "someone-elses-cookie")
    assert c.call("GET", "/api/me")[0] == 419


def test_bearer_scripts_still_work():
    mkuser("jc", "admin")
    c = Client()
    c.call("POST", "/api/login", {"username": "jc", "password": PW})
    s, t = c.call("POST", "/api/me/tokens", {"name": "cron", "scope": "read"})
    r = TestClient(app).get("/api/week", headers={"Authorization": f"Bearer {t['token']}"})
    assert r.status_code == 200


def test_index_source_differs_every_load():
    c = TestClient(app)
    a, b = c.get("/"), c.get("/")
    assert a.text != b.text and "nonce-" in a.headers["content-security-policy"]
    assert a.headers["content-security-policy"] != b.headers["content-security-policy"]
