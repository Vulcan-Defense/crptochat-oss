"""Updates from the latest GitHub release (through the API), with the site as fallback."""

import http.server
import json
import os
import ssl
import threading

import pytest
from test_update import NEW_BINARY, NOW, OLD, OLD_BUILT, Site, installed, make_release, publish  # noqa: F401

from crptochat import update
from crptochat.server import ensure_cert

REPO = "acme/crptochat-client"


class FakeGitHub:
    """api.github.com + github.com + its object storage, on one local https server."""

    def __init__(self, tmp_path):
        self.mode = "ok"
        self.files = tmp_path / "gh-files"
        self.files.mkdir()
        self.tag = "v1.1.0"
        self.headers, self.paths = [], []
        gh = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                gh.headers.append(dict(self.headers))
                gh.paths.append(self.path)
                gh.route(self)

            def do_HEAD(self):
                self.do_GET()

            def log_message(self, *args):
                pass

        (tmp_path / "gh-cert").mkdir()
        cert, key = ensure_cert(str(tmp_path / "gh-cert"))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"https://127.0.0.1:{self.httpd.server_address[1]}"

    def release_json(self):
        assets = []
        for f in sorted(os.listdir(self.files)):
            link = f"{self.url}/{REPO}/releases/download/{self.tag}/{f}"
            if self.mode == "evil_link" and f.startswith("crptochat-"):
                link = f"https://evil.example/{f}"
            assets.append({"name": f, "browser_download_url": link, "size": os.path.getsize(self.files / f)})
        return {"tag_name": self.tag, "draft": False, "prerelease": False, "assets": assets}

    def route(self, req):
        path = req.path
        if path == f"/repos/{REPO}/releases/latest":
            if self.mode == "ratelimit":
                body = b'{"message": "API rate limit exceeded"}'
                req.send_response(403)
                req.send_header("X-RateLimit-Remaining", "0")
            elif self.mode == "down":
                body = b"oops"
                req.send_response(502)
            else:
                body = json.dumps(self.release_json()).encode()
                req.send_response(200)
            req.send_header("Content-Length", str(len(body)))
            req.end_headers()
            req.wfile.write(body)
            return
        prefix = f"/{REPO}/releases/download/{self.tag}/"
        if path.startswith(prefix):
            name = path[len(prefix):]
            target = f"{self.url}/objects/{name}"
            if self.mode == "http_redirect":
                target = target.replace("https://", "http://")
            req.send_response(302)                       # like GitHub: to the object storage
            req.send_header("Location", target)
            req.send_header("Content-Length", "0")
            req.end_headers()
            return
        if path.startswith("/objects/"):
            data = (self.files / path[len("/objects/"):]).read_bytes()
            req.send_response(200)
            req.send_header("Content-Length", str(len(data)))
            req.end_headers()
            req.wfile.write(data)
            return
        req.send_response(404)
        req.send_header("Content-Length", "0")
        req.end_headers()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def github(tmp_path, monkeypatch):
    gh = FakeGitHub(tmp_path)
    client_ctx = ssl.create_default_context()
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE        # test certificate; the release signature is what counts
    monkeypatch.setattr(update, "_SSL_CONTEXT", client_ctx)
    monkeypatch.setattr(update, "GITHUB_API", gh.url)
    monkeypatch.setattr(update, "GITHUB_WEB", gh.url)
    monkeypatch.setattr(update, "DEFAULT_SOURCE", "github:" + REPO)
    monkeypatch.delenv("CRPTOCHAT_UPDATE_URL", raising=False)
    monkeypatch.delenv("CRPTOCHAT_UPDATE_FALLBACK", raising=False)
    monkeypatch.setattr(update, "DEFAULT_URL", "file:///nonexistent-crptochat-site")   # no real site
    monkeypatch.setenv("GITHUB_TOKEN", "nunca-deve-sair")
    monkeypatch.setenv("GH_TOKEN", "nem-este")
    yield gh
    gh.close()


@pytest.fixture
def signed(monkeypatch):
    private, public = update.generate_key()
    monkeypatch.setattr(update, "SIGNING_KEY", public)
    return private


def test_update_from_the_latest_github_release(github, installed, signed):
    make_release(github.files, key=signed)
    (github.files / "SHA256SUMS").write_text("só para quem baixa à mão\n")
    source = update.source_for({})
    assert source == "github:" + REPO
    info = update.latest(source)
    assert info["version"] == "1.1.0" and info["signed"] and info["source"] == source and info["tag"] == "v1.1.0"
    assert "fallback" not in info
    done = update.apply(source)
    assert installed.read_text() == NEW_BINARY and done["version"] == "1.1.0"
    # API first, then the assets of that release, through the storage redirect
    assert github.paths[0] == f"/repos/{REPO}/releases/latest"
    assert f"/{REPO}/releases/download/v1.1.0/{update.asset_name()}" in github.paths
    assert f"/objects/{update.asset_name()}" in github.paths
    # anonymous: no token, login or cookie, even with GH_TOKEN / GITHUB_TOKEN set
    assert github.headers and not [h for h in github.headers if "Authorization" in h or "Cookie" in h]
    api = [h for h, p in zip(github.headers, github.paths) if p.startswith("/repos/")]
    assert api[0].get("Accept") == "application/vnd.github+json"
    assert {h.get("User-Agent") for h in github.headers} == {"crptochat-updater"}


def test_rate_limit_falls_back_to_the_site(github, installed, signed, tmp_path, monkeypatch):
    root = tmp_path / "www"
    (root / "downloads").mkdir(parents=True)
    site = Site(root, tmp_path)
    site.folder = root / "downloads"
    try:
        publish(site, signed)
        monkeypatch.setattr(update, "DEFAULT_URL", site.url)
        github.mode = "ratelimit"
        info = update.latest(update.source_for({}))
        assert info["source"] == site.url and "limite de consultas" in info["fallback"]
        done = update.apply(update.source_for({}))
        assert done["source"] == site.url and installed.read_text() == NEW_BINARY

        # GitHub down (5xx) also falls back; with the fallback off it just fails
        github.mode = "down"
        assert update.latest(update.source_for({}))["source"] == site.url
        monkeypatch.setenv("CRPTOCHAT_UPDATE_FALLBACK", "off")
        with pytest.raises(update.SourceUnavailable, match="HTTP 502"):
            update.latest(update.source_for({}))
        github.mode = "ratelimit"
        with pytest.raises(update.SourceUnavailable, match="limite de consultas"):
            update.latest(update.source_for({}))
    finally:
        site.close()


def test_a_release_missing_files_falls_back_but_a_bad_signature_stops(github, installed, signed, tmp_path,
                                                                     monkeypatch):
    root = tmp_path / "www"
    (root / "downloads").mkdir(parents=True)
    site = Site(root, tmp_path)
    site.folder = root / "downloads"
    try:
        publish(site, signed)
        monkeypatch.setattr(update, "DEFAULT_URL", site.url)
        # a release still being uploaded (no RELEASE yet): the site has the same one
        (github.files / "crptochat-linux-amd64").write_text("parcial")
        assert update.latest(update.source_for({}))["source"] == site.url

        # a release signed by someone else is an attack, not an outage: no fallback
        for f in github.files.iterdir():
            f.unlink()
        make_release(github.files, key=signed, bad_sig=True)
        site.paths.clear()
        with pytest.raises(update.UpdateError, match="ASSINATURA INVÁLIDA") as err:
            update.apply(update.source_for({}))
        assert not isinstance(err.value, update.SourceUnavailable)
        assert site.paths == [] and installed.read_text() == OLD

        # same for a binary that does not match the signed manifest
        for f in github.files.iterdir():
            f.unlink()
        make_release(github.files, key=signed, tamper=True)
        with pytest.raises(update.UpdateError, match="não confere"):
            update.apply(update.source_for({}))
        assert site.paths == [] and installed.read_text() == OLD
    finally:
        site.close()


def test_no_downgrade_from_github(github, installed, signed):
    for case in (dict(version="0.9.0", built=NOW), dict(version="1.1.0", built=OLD_BUILT - 1)):
        for f in github.files.iterdir():
            f.unlink()
        make_release(github.files, key=signed, **case)
        with pytest.raises(update.UpdateError, match="downgrade"):
            update.apply(update.source_for({}))
    assert installed.read_text() == OLD


def test_unsigned_github_release_is_refused(github, installed, signed):
    make_release(github.files, key=signed)
    (github.files / "RELEASE.sig").unlink()
    with pytest.raises(update.UpdateError, match="não está assinada"):
        update.apply(update.source_for({}))
    assert installed.read_text() == OLD
    # nothing but the API and the manifest was read: the binary is never fetched first
    assert not [p for p in github.paths if p.endswith(update.asset_name())]


def test_assets_must_belong_to_that_release_and_stay_on_https(github, installed, signed):
    make_release(github.files, key=signed)
    github.mode = "evil_link"
    with pytest.raises(update.UpdateError, match="fora da release") as err:
        update.apply(update.source_for({}))
    assert not isinstance(err.value, update.SourceUnavailable)
    github.mode = "http_redirect"
    with pytest.raises(update.UpdateError, match="https"):
        update.apply(update.source_for({}))
    assert installed.read_text() == OLD


def test_every_file_comes_from_the_same_release(github, installed, signed):
    """The release is resolved once; files are fetched from that tag even if
    'latest' changes in the middle."""
    make_release(github.files, key=signed)
    release = update.github_release(REPO)
    github.tag = "v9.9.9"                          # a newer release appears meanwhile
    with pytest.raises(update.SourceUnavailable):  # the old tag's links are gone on this fake server
        update.fetch("github:" + REPO, "RELEASE", update.MAX_MANIFEST, update.SMALL_DEADLINE, release)
    assert all("/v1.1.0/" in p for p in github.paths if "/releases/download/" in p)


def test_describe():
    assert update.describe("github:a/b") == "GitHub (a/b, última release)"
    assert update.describe("https://x.example/dl/") == "https://x.example/dl/"
