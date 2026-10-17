"""Passphrase key hierarchy, locked mode, and disaster-recovery bundles."""
import os
import secrets

import pytest
from fastapi.testclient import TestClient

from app import config, crypto, db, dr, keys, settings

PASS, BPASS = "database passphrase 1", "backup password 22"


def server(tmp, name):
    """Point the app at a self-contained 'server' under tmp (key file, keyring, runtime key, database)."""
    d = tmp / name
    d.mkdir()
    kf = d / "db.key"
    kf.write_text(secrets.token_hex(32))
    return {"DB_PATH": str(d / "billtrack.db"), "DB_KEY_FILE": str(kf), "KEYRING": str(d / "keyring.json"),
            "RUNTIME_KEY": str(d / "run" / "dek"), "DB_KEY": ""}


def use(monkeypatch, srv):
    for k, v in srv.items():
        monkeypatch.setattr(config, k, v)
    settings.reset_cache()


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A pre-2.9 install: database encrypted directly with the key file, with a sealed secret and some data."""
    srv = server(tmp_path, "old")
    use(monkeypatch, srv)
    db.init()
    with db.tx() as con:
        con.execute("INSERT INTO accounts(name) VALUES ('KeepMe')")
    settings.update({"smtp_pass": "smtp-secret-123"})
    bdir = db.backup_dir()
    db.snapshot(bdir, 7, "manual")
    yield srv
    settings.reset_cache()


def test_migrate_then_two_keys_required(legacy, monkeypatch):
    old_key = open(legacy["DB_KEY_FILE"]).read().strip()
    r = dr.migrate_to_passphrase(PASS)
    assert r["backups_converted"] == 1 and keys.enabled()
    dek = db.load_key()
    assert dek != old_key
    with pytest.raises(db.KeyError_):
        db.connect(key=old_key)                                   # key file alone no longer opens the data
    settings.reset_cache()
    assert settings.get("smtp_pass") == "smtp-secret-123"         # sealed secrets followed the new key
    with db.tx() as con:
        assert con.execute("SELECT name FROM accounts").fetchone()[0] == "KeepMe"
    snap = [f for f in os.listdir(db.backup_dir()) if db.SNAP_RE.match(f)][0]
    db.connect(os.path.join(db.backup_dir(), snap), dek).close()  # old server backups converted too

    keys.clear_runtime()                                          # reboot
    assert db.locked()
    with pytest.raises(keys.WrongKey):
        dr.unlock("not the passphrase")
    other = legacy["DB_KEY_FILE"] + ".other"
    open(other, "w").write(secrets.token_hex(32))
    with pytest.raises(keys.WrongKey):
        keys.unlock_dek(PASS, keyfile=other)                      # right passphrase, wrong key file: no
    assert dr.unlock(PASS) == keys.fingerprint(bytes.fromhex(dek))
    assert not db.locked()


def test_locked_mode_api(legacy, monkeypatch):
    dr.migrate_to_passphrase(PASS)
    keys.clear_runtime()
    from app.main import app
    c = TestClient(app)
    assert c.get("/healthz").json() == {"ok": True, "locked": True}
    r = c.post("/api/login", json={"username": "x", "password": "y"}, headers={"X-BT": "1"})
    assert r.status_code == 503 and r.json()["locked"] is True
    dr.unlock(PASS)
    assert c.get("/healthz").json()["locked"] is False


def test_dr_bundle_roundtrip_to_blank_server(legacy, tmp_path, monkeypatch):
    dr.migrate_to_passphrase(PASS)
    cfg = tmp_path / "cfg"; cfg.mkdir()
    (cfg / "billtrack.env").write_text("BT_ICS_TOKEN=abc\n")
    blob = dr.export_bundle(BPASS, {"config/billtrack.env": str(cfg / "billtrack.env"), "tls/ca.key": str(cfg / "missing")})
    assert b"KeepMe" not in blob and b"BT_ICS_TOKEN" not in blob and b"smtp-secret" not in blob
    assert blob != dr.export_bundle(BPASS, {})[:len(blob)]       # fresh salt/nonces every time

    # wrong backup password / wrong database passphrase / tampering all fail
    with pytest.raises(dr.BundleError):
        dr.open_bundle(blob, "wrong backup password", PASS)
    with pytest.raises(keys.WrongKey):
        dr.open_bundle(blob, BPASS, "wrong database passphrase")
    bad = bytearray(blob); bad[len(bad) // 2] ^= 1
    with pytest.raises(dr.BundleError):
        dr.open_bundle(bytes(bad), BPASS, PASS)
    with pytest.raises(dr.BundleError):
        dr.open_bundle(blob[:-20], BPASS, PASS)

    # brand-new server: different key file, nothing else
    new = server(tmp_path, "new")
    use(monkeypatch, new)
    monkeypatch.setenv("BT_ENV_FILE", str(tmp_path / "new" / "billtrack.env"))
    monkeypatch.setenv("BT_INSTALL_CONF", str(tmp_path / "new" / "install.conf"))
    monkeypatch.setenv("BT_TLS_DIR", str(tmp_path / "new" / "tls"))
    r = dr.import_bundle(blob, BPASS, PASS)
    assert "tls/ca.key" in r["not_included"] and open(tmp_path / "new" / "billtrack.env").read() == "BT_ICS_TOKEN=abc\n"
    db.init()
    settings.reset_cache()
    with db.tx() as con:
        assert con.execute("SELECT name FROM accounts").fetchone()[0] == "KeepMe"
    assert settings.get("smtp_pass") == "smtp-secret-123"
    keys.clear_runtime()
    assert dr.unlock(PASS)                                        # new server: passphrase + NEW key file
    with pytest.raises(keys.WrongKey):
        keys.unlock_dek(PASS, keyfile=legacy["DB_KEY_FILE"])      # the old server's key file can't open it


def test_change_passphrase_keeps_data_old_bundles_need_old(legacy, monkeypatch):
    dr.migrate_to_passphrase(PASS)
    old_blob = dr.export_bundle(BPASS, {})
    keys.change_passphrase(PASS, "a brand new passphrase")
    keys.clear_runtime()
    with pytest.raises(keys.WrongKey):
        dr.unlock(PASS)
    dr.unlock("a brand new passphrase")
    with db.tx() as con:
        assert con.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1
    dr.open_bundle(old_blob, BPASS, PASS)                         # older backups: old passphrase
    with pytest.raises(ValueError):
        dr.export_bundle("short", {})


def test_admin_dr_download(legacy, monkeypatch):
    dr.migrate_to_passphrase(PASS)
    from app import security
    from app.main import app
    with db.tx() as con:
        con.execute("INSERT INTO users(username,pw_hash,role) VALUES ('adm',?,'admin')", (security.hash_pw("correct horse battery"),))
    c = TestClient(app)
    c.__enter__()
    H = {"X-BT": "1"}
    c.post("/api/login", json={"username": "adm", "password": "correct horse battery"}, headers=H)
    r = c.post("/api/admin/dr-backup", json={"password": BPASS}, headers=H)
    assert r.status_code == 200 and r.content.startswith(dr.MAGIC)
    assert dr.open_bundle(r.content, BPASS, PASS)[0]["files"]
    sys_ = c.get("/api/admin/system").json()
    assert sys_["key_mode"] == "passphrase" and sys_["key_fingerprint"]
    name = c.get("/api/admin/backups").json()[0]["name"]
    r = c.post(f"/api/admin/backups/{name}/download", json={"password": BPASS}, headers=H)
    assert r.status_code == 200 and dr.open_bundle(r.content, BPASS, PASS)
