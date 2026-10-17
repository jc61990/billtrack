"""HTTPS certificate management: app-side validation/queueing, root-side agent install/rollback/Let's Encrypt."""
import base64
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from app import tlsagent
from tests.test_app import H, admin, login, mkuser, user  # noqa: F401


def make_cert(name="bills.example.com", days=90, key=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=2))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), False).sign(key, hashes.SHA256()))
    return key, cert


def pem(key, cert):
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    tls, spool, web = tmp_path / "tls", tmp_path / "spool", tmp_path / "acme"
    tls.mkdir(); spool.mkdir()
    for k, v in (("TLS", str(tls)), ("SPOOL", str(spool)), ("WEBROOT", str(web))):
        monkeypatch.setattr(tlsagent, k, v)
    monkeypatch.setenv("BT_TLS_SPOOL", str(spool))
    nginx = tmp_path / "nginx"
    nginx.write_text("#!/bin/sh\n[ -f \"$NGINX_FAIL\" ] && { echo 'emerg: bad cert' >&2; exit 1; }\nexit 0\n")
    nginx.chmod(0o755)
    monkeypatch.setattr(tlsagent, "NGINX", str(nginx))
    k, c = make_cert("selfsigned.local")
    cp, kp = pem(k, c)
    (tls / "fullchain.crt").write_text(cp); (tls / "server.key").write_text(kp)
    tlsagent.install_self_signed()
    return {"tls": tls, "spool": spool, "tmp": tmp_path}


def test_pfx_import_end_to_end(admin, dirs):
    k, c = make_cert("bills.example.com")
    pfx = pkcs12.serialize_key_and_certificates(b"bt", k, c, None, serialization.BestAvailableEncryption(b"pfx pass 1"))
    b64 = base64.b64encode(pfx).decode()
    assert admin.post("/api/admin/tls/pfx", json={"pfx_b64": b64, "password": "wrong"}, headers=H).status_code == 422
    assert admin.post("/api/admin/tls/pfx", json={"pfx_b64": "bm90IGEgcGZ4", "password": "x"}, headers=H).status_code == 422
    r = admin.post("/api/admin/tls/pfx", json={"pfx_b64": b64, "password": "pfx pass 1"}, headers=H)
    assert r.status_code == 200 and r.json()["names"] == ["bills.example.com"]
    assert admin.post("/api/admin/tls/selfsigned", headers=H).status_code == 409        # one job at a time
    req = os.listdir(dirs["spool"] / "requests")[0]
    assert stat.S_IMODE(os.stat(dirs["spool"] / "requests" / req).st_mode) == 0o600
    tlsagent.process()                                                                   # what systemd runs as root
    assert os.listdir(dirs["spool"] / "requests") == []                                 # key material removed
    st = admin.get("/api/admin/tls").json()
    assert st["state"] == "ok" and st["mode"] == "custom" and st["cert"]["names"] == ["bills.example.com"]
    assert stat.S_IMODE(os.stat(dirs["tls"] / "active" / "privkey.pem").st_mode) == 0o600
    mkuser("u5")
    assert login("u5").get("/api/admin/tls").status_code == 403


def test_agent_rolls_back_when_nginx_refuses(dirs, monkeypatch):
    before = (dirs["tls"] / "active" / "fullchain.pem").read_text()
    k, c = make_cert("new.example.com")
    monkeypatch.setenv("NGINX_FAIL", str(dirs["tmp"] / "fail")); (dirs["tmp"] / "fail").write_text("1")
    with pytest.raises(RuntimeError):
        tlsagent.install(*pem(k, c), "custom")
    assert (dirs["tls"] / "active" / "fullchain.pem").read_text() == before and tlsagent.mode() == "self"
    k2, _ = make_cert()
    with pytest.raises(ValueError):
        tlsagent.install(pem(k, c)[0], pem(k2, c)[1], "custom")                         # mismatched key


def test_letsencrypt_via_certbot(admin, dirs, monkeypatch):
    live = dirs["tmp"] / "live" / "billtrack"
    k, c = make_cert("bills.example.com")
    cp, kp = pem(k, c)
    certbot = dirs["tmp"] / "certbot"
    certbot.write_text(f"""#!/bin/sh
echo "$@" > {dirs['tmp']}/certbot.args
mkdir -p {live}; cat > {live}/fullchain.pem <<'E'
{cp}E
cat > {live}/privkey.pem <<'E'
{kp}E
""")
    certbot.chmod(0o755)
    monkeypatch.setattr(tlsagent, "CERTBOT", str(certbot))
    real_join = os.path.join
    monkeypatch.setattr(tlsagent.os.path, "join", lambda *a: real_join(str(dirs["tmp"] / "live"), *a[1:])
                        if a and a[0] == "/etc/letsencrypt/live" else real_join(*a))
    assert admin.post("/api/admin/tls/letsencrypt", json={"domains": ["bad domain"], "email": "a@b.co"}, headers=H).status_code == 422
    r = admin.post("/api/admin/tls/letsencrypt", json={"domains": ["Bills.Example.com"], "email": "jc@example.com",
                                                       "staging": True}, headers=H)
    assert r.status_code == 200
    tlsagent.process()
    st = admin.get("/api/admin/tls").json()
    assert st["state"] == "ok", st["message"]
    assert st["mode"] == "letsencrypt" and st["cert"]["names"] == ["bills.example.com"]
    args = (dirs["tmp"] / "certbot.args").read_text()
    for part in ("certonly", "--non-interactive", "--webroot", "-d bills.example.com", "--test-cert", "--deploy-hook", "--cert-name billtrack"):
        assert part in args, part
    # renewal hook installs the renewed cert
    k3, c3 = make_cert("bills.example.com", days=89)
    cp3, kp3 = pem(k3, c3)
    (live / "fullchain.pem").write_text(cp3); (live / "privkey.pem").write_text(kp3)
    monkeypatch.setenv("RENEWED_LINEAGE", str(live))
    tlsagent.deploy_hook()
    assert (dirs["tls"] / "active" / "fullchain.pem").read_text() == cp3
