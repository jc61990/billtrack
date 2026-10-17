"""Root-side HTTPS certificate agent.

The web app runs as an unprivileged user and can't touch nginx or /etc/billtrack/tls. An admin action only drops a
JSON request into SPOOL/requests/ (writable by the app). systemd (billtrack-tls.path -> billtrack-tls.service) runs
`python -m app.tlsagent process` as root, which validates the request, installs the certificate into TLS/active/,
checks `nginx -t`, reloads nginx, and rolls back if anything fails. Results go to SPOOL/status.json (readable by
the app). Modes: self (setup.sh's local CA), letsencrypt (certbot, auto-renewing), custom (imported PFX/PEM).

Commands:
  process            handle queued requests (run by systemd)
  deploy-hook        certbot renewal hook: install the renewed cert and reload nginx
  status             print status.json"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

TLS = os.getenv("BT_TLS_DIR", "/etc/billtrack/tls")
SPOOL = os.getenv("BT_TLS_SPOOL", "/var/lib/billtrack/tls")
WEBROOT = os.getenv("BT_ACME_WEBROOT", "/var/www/billtrack-acme")
CERTBOT = os.getenv("BT_CERTBOT", "certbot")
NGINX = os.getenv("BT_NGINX", "nginx")
LE_NAME = "billtrack"
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,63}$")
CF_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,100}$")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _svc_ids():
    try:
        import pwd
        p = pwd.getpwnam("billtrack")
        return p.pw_uid, p.pw_gid
    except (KeyError, ImportError):
        return None


def _write(path, data, mode, readable_by_app=False):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as f:
        f.write(data)
    if readable_by_app and os.geteuid() == 0 and _svc_ids():
        os.chown(tmp, 0, _svc_ids()[1])
    os.replace(tmp, path)


def cert_info(pem: str) -> dict:
    certs = x509.load_pem_x509_certificates(pem.encode())
    c = certs[0]
    try:
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(n.value) for n in san]
    except x509.ExtensionNotFound:
        names = []
    return {"subject": c.subject.rfc4514_string(), "issuer": c.issuer.rfc4514_string(), "names": names,
            "not_before": c.not_valid_before_utc.isoformat(), "not_after": c.not_valid_after_utc.isoformat(),
            "chain_length": len(certs)}


def mode() -> str:
    try:
        return open(os.path.join(TLS, "mode")).read().strip() or "self"
    except OSError:
        return "self"


def status(**kw):
    os.makedirs(SPOOL, exist_ok=True)
    try:
        cur = json.load(open(os.path.join(SPOOL, "status.json")))
    except (OSError, ValueError):
        cur = {}
    cur.update(kw, updated=now(), mode=mode())
    try:
        cur["cert"] = cert_info(open(os.path.join(TLS, "active", "fullchain.pem")).read())
    except (OSError, ValueError):
        pass
    _write(os.path.join(SPOOL, "status.json"), json.dumps(cur, indent=1), 0o640, readable_by_app=True)


def _nginx(*args):
    return subprocess.run([NGINX, *args], capture_output=True, text=True, timeout=60)


def install(fullchain_pem: str, key_pem: str, new_mode: str):
    """Swap the active cert atomically; keep the previous one; roll back if nginx rejects it."""
    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    leaf = x509.load_pem_x509_certificates(fullchain_pem.encode())[0]
    if not isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
        raise ValueError("Only RSA or ECDSA keys are supported")
    pub = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if pub(key.public_key()) != pub(leaf.public_key()):
        raise ValueError("The private key doesn't belong to this certificate")
    if leaf.not_valid_after_utc <= datetime.now(timezone.utc):
        raise ValueError("That certificate has already expired")
    act, prev = os.path.join(TLS, "active"), os.path.join(TLS, "previous")
    os.makedirs(TLS, mode=0o700, exist_ok=True)
    stage = os.path.join(TLS, f"stage-{os.getpid()}")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, mode=0o700)
    _write(os.path.join(stage, "fullchain.pem"), fullchain_pem, 0o644)
    _write(os.path.join(stage, "privkey.pem"), key_pem, 0o600)
    shutil.rmtree(prev, ignore_errors=True)
    if os.path.isdir(act):
        os.replace(act, prev)
    os.replace(stage, act)
    t = _nginx("-t")
    if t.returncode != 0:
        shutil.rmtree(act, ignore_errors=True)
        if os.path.isdir(prev):
            os.replace(prev, act)
        raise RuntimeError("nginx rejected the certificate, so the previous one was kept: " + t.stderr.strip()[-300:])
    _write(os.path.join(TLS, "mode"), new_mode + "\n", 0o644)
    r = _nginx("-s", "reload")
    if r.returncode != 0:
        subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, timeout=60)


def install_self_signed():
    chain = open(os.path.join(TLS, "fullchain.crt")).read()
    key = open(os.path.join(TLS, "server.key")).read()
    install(chain, key, "self")


def letsencrypt(req: dict):
    domains = req.get("domains") or []
    email = req.get("email", "")
    if not domains or len(domains) > 10 or not all(DOMAIN_RE.match(d) for d in domains):
        raise ValueError("Enter real domain names (e.g. bills.example.com)")
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email for Let's Encrypt expiry notices")
    cmd = [CERTBOT, "certonly", "--non-interactive", "--agree-tos", "-m", email, "--cert-name", LE_NAME,
           "--key-type", "ecdsa", "--keep-until-expiring", "--expand",
           "--deploy-hook", os.getenv("BT_TLS_HOOK", "/usr/local/sbin/billtrack-tls-hook")]
    if req.get("staging"):
        cmd.append("--test-cert")
    creds = None
    if req.get("challenge") == "cloudflare":
        tok = req.get("cf_token", "")
        if not CF_TOKEN_RE.match(tok):
            raise ValueError("That doesn't look like a Cloudflare API token")
        creds = "/etc/billtrack/cloudflare.ini"
        _write(creds, f"dns_cloudflare_api_token = {tok}\n", 0o600)      # certbot reuses it for renewals
        cmd += ["--dns-cloudflare", "--dns-cloudflare-credentials", creds, "--dns-cloudflare-propagation-seconds", "30"]
    else:
        os.makedirs(os.path.join(WEBROOT, ".well-known", "acme-challenge"), exist_ok=True)
        cmd += ["--webroot", "-w", WEBROOT]
    for d in domains:
        cmd += ["-d", d]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-8:]
        raise RuntimeError("Let's Encrypt didn't issue the certificate:\n" + "\n".join(tail))
    live = os.path.join("/etc/letsencrypt/live", LE_NAME)
    install(open(os.path.join(live, "fullchain.pem")).read(), open(os.path.join(live, "privkey.pem")).read(), "letsencrypt")
    return {"domains": domains, "challenge": req.get("challenge", "http"), "staging": bool(req.get("staging"))}


def deploy_hook():
    """certbot calls this after each renewal (RENEWED_LINEAGE=/etc/letsencrypt/live/<name>)."""
    live = os.getenv("RENEWED_LINEAGE") or os.path.join("/etc/letsencrypt/live", LE_NAME)
    if mode() != "letsencrypt":      # first issuance (installed by letsencrypt()), or the admin switched to another cert
        return
    install(open(os.path.join(live, "fullchain.pem")).read(), open(os.path.join(live, "privkey.pem")).read(), "letsencrypt")
    status(state="ok", message="Let's Encrypt certificate renewed and installed", action="renew")


def process():
    rq = os.path.join(SPOOL, "requests")
    if not os.path.isdir(rq):
        return
    for name in sorted(os.listdir(rq)):
        path = os.path.join(rq, name)
        if not name.endswith(".json") or os.path.islink(path):
            continue
        try:
            st = os.stat(path)
            ids = _svc_ids()
            if ids and st.st_uid not in (0, ids[0]):          # only the app (or root) may queue jobs
                raise PermissionError("request not written by the app")
            if st.st_size > 64 * 1024:
                raise ValueError("request too large")
            req = json.load(open(path))
        except Exception as e:
            status(state="error", message=f"Bad request: {e}", action="?")
            os.remove(path)
            continue
        os.remove(path)                                          # contains key material: gone before work starts
        action = req.get("action")
        status(state="working", message=f"Working on: {action}", action=action, request_id=req.get("id"))
        try:
            if action == "letsencrypt":
                info = letsencrypt(req)
                msg = f"Let's Encrypt certificate installed for {', '.join(info['domains'])}. It renews automatically."
            elif action == "custom":
                install(req["fullchain_pem"], req["key_pem"], "custom")
                msg = "Your certificate is installed."
            elif action == "selfsigned":
                install_self_signed()
                msg = "Back to BillTrack's own (self-signed) certificate."
            else:
                raise ValueError("unknown action")
            status(state="ok", message=msg, action=action, request_id=req.get("id"), log="")
        except Exception as e:
            status(state="error", message=str(e)[:1500], action=action, request_id=req.get("id"))
        finally:
            req.clear()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "process"
    if cmd == "process":
        process()
    elif cmd == "deploy-hook":
        deploy_hook()
    elif cmd == "status":
        status()
        print(open(os.path.join(SPOOL, "status.json")).read())
    else:
        sys.exit(__doc__)
