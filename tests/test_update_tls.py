"""The updater must check the site's certificate even where the frozen OpenSSL
finds no system CA (macOS): it ships certifi's bundle."""

import datetime
import http.server
import ipaddress
import ssl
import subprocess
import sys
import threading
import urllib.error

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from crptochat import update


@pytest.fixture(autouse=True)
def fresh_context(monkeypatch):
    monkeypatch.setattr(update, "_TLS_CONTEXT", None)
    monkeypatch.setattr(update, "_SSL_CONTEXT", None)
    yield
    update._TLS_CONTEXT = None


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_pki(tmp_path, san):
    """A test CA and a server certificate it signed for ``san``."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("CA de teste")).issuer_name(_name("CA de teste"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(_name("site")).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_pem, cert_pem, key_pem = tmp_path / "ca.pem", tmp_path / "site.pem", tmp_path / "site.key"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return str(ca_pem), str(cert_pem), str(key_pem)


class Site:
    def __init__(self, cert, key):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"https://127.0.0.1:{self.httpd.server_address[1]}/downloads/RELEASE"

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def no_system_cas(monkeypatch):
    """Like the PyInstaller binary on macOS: OpenSSL finds no CA in the system."""
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, purpose=None: None)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


def test_the_context_always_checks_certificate_and_host_name():
    ctx = update.tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    info = update.tls_info()
    assert info["bundle"] and info["ca"] >= 100            # certifi's public CAs are there
    assert update.tls_context() is ctx                      # built once


def test_without_system_cas_the_bundle_still_validates(tmp_path, monkeypatch, no_system_cas):
    ca, cert, key = make_pki(tmp_path, x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    site = Site(cert, key)
    try:
        # the bug: no system CA and no bundle, so even a valid certificate fails
        monkeypatch.setattr(update, "_ca_bundle", lambda: None)
        with pytest.raises(urllib.error.URLError) as err:
            update._http(site.url, 100, 10)
        assert "CERTIFICATE_VERIFY_FAILED" in str(err.value)

        # the fix: the shipped bundle (here, one that holds the test CA) is enough
        monkeypatch.setattr(update, "_TLS_CONTEXT", None)
        monkeypatch.setattr(update, "_ca_bundle", lambda: ca)
        assert update._http(site.url, 100, 10) == b"ok"
    finally:
        site.close()


def test_a_certificate_for_another_name_is_refused(tmp_path, monkeypatch, no_system_cas):
    ca, cert, key = make_pki(tmp_path, x509.DNSName("outro.exemplo"))
    site = Site(cert, key)
    monkeypatch.setattr(update, "_ca_bundle", lambda: ca)
    try:
        with pytest.raises(urllib.error.URLError) as err:
            update._http(site.url, 100, 10)
        assert "CERTIFICATE_VERIFY_FAILED" in str(err.value) or "hostname" in str(err.value).lower()
    finally:
        site.close()


def test_an_untrusted_ca_is_refused(tmp_path, monkeypatch, no_system_cas):
    (tmp_path / "real").mkdir()
    (tmp_path / "other").mkdir()
    _, cert, key = make_pki(tmp_path / "real", x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    other_ca, _, _ = make_pki(tmp_path / "other", x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    site = Site(cert, key)
    monkeypatch.setattr(update, "_ca_bundle", lambda: other_ca)
    try:
        with pytest.raises(urllib.error.URLError) as err:
            update._http(site.url, 100, 10)
        assert "CERTIFICATE_VERIFY_FAILED" in str(err.value)
    finally:
        site.close()


def test_tls_info_flag():
    out = subprocess.run([sys.executable, "-m", "crptochat.cli", "--tls-info"], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0
    words = out.stdout.split()
    assert words[0] == "crptochat-tls"
    fields = dict(w.split("=", 1) for w in words[1:])
    assert fields["bundle"] == "sim" and int(fields["ca"]) >= 100
