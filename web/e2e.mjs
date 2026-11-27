// End-to-end test of web/dist in a real browser (CI): a crptochat server with its
// WebSocket entrance, the built page served locally, Chromium driven by Playwright.
//
//   node web/e2e.mjs            (after build.py; needs crptochat installed and Playwright's Chromium)
//
// Flow: a new key -> the backup panel (download) -> the chat online; then, in a clean
// browser, the downloaded backup imported (a wrong passphrase refused) -> online again
// with the same key, accepted by the server.
import { chromium } from "playwright";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const DIST = path.join(HERE, "dist");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "crptochat-e2e-"));
const PY = process.env.PYTHON || "python3";
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
                ".json": "application/json", ".wasm": "application/wasm" };

let failures = 0;
const check = (label, ok, extra = "") => {
  console.log(`${ok ? "ok " : "FALHOU"}  ${label}${extra ? ": " + extra : ""}`);
  if (!ok) failures++;
};

// the page, from dist/, on a free port
const site = http.createServer((req, res) => {
  const rel = decodeURIComponent(new URL(req.url, "http://x").pathname).replace(/^\/+/, "") || "index.html";
  const file = path.join(DIST, rel);
  if (!file.startsWith(DIST + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    res.writeHead(404); res.end(); return;
  }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});
await new Promise((ok) => site.listen(0, "127.0.0.1", ok));
const origin = `http://localhost:${site.address().port}`;
// the same page from another origin: one the server was not told to accept
const otherSite = http.createServer((req, res) => site.emit("request", req, res));
await new Promise((ok) => otherSite.listen(0, "127.0.0.1", ok));
const otherOrigin = `http://127.0.0.1:${otherSite.address().port}`;

// the server, with the WebSocket entrance for that origin
const wsPort = 20000 + Math.floor(Math.random() * 20000);
const server = spawn(PY, ["-m", "crptochat.server", "--data", path.join(TMP, "srv"), "--port", "0", "--no-tls",
                          "--no-stats", "--ws-port", String(wsPort), "--ws-origin", origin],
                     { stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, PYTHONUNBUFFERED: "1" } });   // its banner tells when it is up
let serverOut = "";
server.stdout.on("data", (d) => (serverOut += d)); server.stderr.on("data", (d) => (serverOut += d));
for (let i = 0; i < 300 && !/cliente web/.test(serverOut) && server.exitCode === null; i++) {
  await new Promise((ok) => setTimeout(ok, 100));
}
if (!/cliente web/.test(serverOut)) {          // no WebSocket entrance: nothing below can work
  console.log("FALHOU  servidor com a entrada WebSocket não subiu (pip install -e '.[web]'?)\n" + serverOut.slice(-2000));
  server.kill(); site.close();
  process.exit(1);
}

const browser = await chromium.launch();
// The page no longer exposes the terminal object: read what xterm drew instead.
const termText = (p) => p.evaluate(() => {
  let s = "";
  for (const row of document.querySelectorAll(".xterm-rows > div")) {
    s += row.textContent.replace(/\u00a0/g, " ").trimEnd() + "\n";
  }
  return s;
});
const until = async (p, re, secs) => {
  for (let i = 0; i < secs * 4; i++) { const t = await termText(p); if (re.test(t)) return t; await p.waitForTimeout(250); }
  return null;
};
const open = async () => {
  const ctx = await browser.newContext({ acceptDownloads: true });
  const p = await ctx.newPage();
  p.on("pageerror", (e) => check("sem erro na página", false, e.message));
  await p.goto(origin + "/");
  await p.waitForSelector("#go:not([disabled])", { timeout: 120000 });
  await p.fill("#url", `ws://localhost:${wsPort}`);
  return { ctx, p };
};

try {
  // 1. a new key, the backup before the chat, then online
  let { ctx, p } = await open();
  await p.fill("#nick", "e2e_novo");
  check("nick novo pede confirmação da senha", await p.isVisible("#pass2"));
  await p.fill("#pass", "senha-do-e2e-1"); await p.fill("#pass2", "senha-do-e2e-1");
  await p.check("#sensitive");                 // the first session exercises the sensitive path
  await p.click("#go");
  await p.waitForFunction(() => localStorage.getItem("crpto.sensitive") === "1");
  check("modo sensível salvo na preferência", (await p.evaluate(() => localStorage.getItem("crpto.sensitive"))) === "1");
  await p.waitForSelector("#export:not([hidden])", { timeout: 30000 });
  const fp = (await p.textContent("#exp-fp")).replace("fingerprint ", "");
  check("backup antes do chat, com a fingerprint", /^\d{5}( \d{5}){5}$/.test(fp), fp);
  const [dl] = await Promise.all([p.waitForEvent("download"), p.click("#exp-download")]);
  const backup = path.join(TMP, "backup.json");
  await dl.saveAs(backup);
  check("arquivo de backup é a chave cifrada", JSON.parse(fs.readFileSync(backup, "utf8")).nick === "e2e_novo");
  await p.click("#exp-done");
  const first = await until(p, /conectado a .* como e2e_novo/, 60);
  check("online com a chave nova", !!first && /registrado/.test(first));
  check("terminal não é global", await p.evaluate(() => !("crptoTerm" in window)));
  // leaving the page in sensitive mode also drops the terminal's own buffer
  await p.locator(".xterm-helper-textarea").focus();
  await p.keyboard.type("segredo-da-tela"); await p.keyboard.press("Enter");
  await until(p, /segredo-da-tela/, 30);
  await p.evaluate(() => dispatchEvent(new Event("pagehide")));
  await p.waitForFunction(() => !Array.from(document.querySelectorAll(".xterm-rows > div"))
                                     .some((r) => r.textContent.includes("segredo-da-tela")),
                          null, { timeout: 15000 });
  check("modo sensível limpa também o buffer do xterm ao sair da página",
        !(await termText(p)).includes("segredo-da-tela"));
  await ctx.close();

  // 2. a clean browser: the backup back in, then online with the same key
  ({ ctx, p } = await open());
  await p.click("#open-import");
  await p.setInputFiles("#imp-file", backup);
  await p.fill("#imp-pass", "senha-errada-1"); await p.click("#imp-go");
  await p.waitForFunction(() => document.querySelector("#imp-status").classList.contains("err"), null, { timeout: 30000 });
  check("senha errada recusada na importação", /não abre/.test(await p.textContent("#imp-status")));
  await p.fill("#imp-pass", "senha-do-e2e-1"); await p.click("#imp-go");
  await p.waitForFunction(() => /importada/.test(document.querySelector("#imp-status").textContent), null, { timeout: 30000 });
  check("backup importado com a mesma fingerprint", (await p.textContent("#imp-status")).includes(fp));
  await p.click("#import [data-close]");
  await p.fill("#pass", "senha-do-e2e-1"); await p.click("#go");
  const again = await until(p, /conectado a .* como e2e_novo/, 60);
  check("online de novo, o servidor aceitou a mesma chave", !!again && !/registrado/.test(again));

  // 2b. a message on several lines: Shift+Enter and the "↵ linha" key break the
  // line in the box, Enter sends it all, and the log shows it on three lines: the
  // 2nd and 3rd only indented after the panel border (no time, no nick), so it is
  // one message, not three
  await p.locator(".xterm-helper-textarea").focus();
  await p.keyboard.type("/join #lobby"); await p.keyboard.press("Enter");
  await until(p, /você entrou em #lobby/, 30);
  await p.keyboard.type("linha um"); await p.keyboard.press("Shift+Enter");
  await p.keyboard.type("linha dois");
  await p.locator("[data-k=nl]").dispatchEvent("click");
  await p.locator(".xterm-helper-textarea").focus();
  await p.keyboard.type("linha tres"); await p.keyboard.press("Enter");
  const multi = await until(p, /▏linha um[^\n]*\n[^\n]*┃ +▏linha dois[^\n]*\n[^\n]*┃ +▏linha tres/, 30);
  check("Shift+Enter e o botão ↵ linha: uma mensagem em três linhas", !!multi,
        multi ? "" : (await termText(p)).split("\n").filter((l) => /linha/.test(l)).join(" | "));

  // 2b'. /convite inside the chat, in the browser: the link to this page with this server
  await p.locator(".xterm-helper-textarea").focus();
  await p.keyboard.type("/convite"); await p.keyboard.press("Enter");
  const invite = await until(p, /#servidor=ws:\/\/localhost:\d+/, 20);
  check("/convite no navegador: link com o servidor", !!invite && invite.includes(`${origin}/#servidor=ws://localhost:${wsPort}`),
        invite ? "" : (await termText(p)).split("\n").filter((l) => /convite|servidor=/.test(l)).join(" | "));
  await ctx.close();

  // 2e. an invite link fills the server field and says so, without connecting;
  // the fragment leaves the address bar; a bad invite fills nothing
  ctx = await browser.newContext({ permissions: ["clipboard-read", "clipboard-write"] });
  p = await ctx.newPage();
  p.on("pageerror", (e) => check("sem erro na página", false, e.message));
  const invitedServer = `ws://localhost:${wsPort}`;
  await p.goto(`${origin}/#servidor=${encodeURIComponent(invitedServer)}`);
  await p.waitForSelector("#go:not([disabled])", { timeout: 120000 });
  check("convite preenche o servidor", (await p.inputValue("#url")) === invitedServer);
  check("convite avisa de onde veio e não conecta sozinho",
        await p.isVisible("#invite-note") && (await p.textContent("#invite-server")) === invitedServer &&
        await p.isVisible("#login") && !/conectando/.test(await p.textContent("#status")));
  check("o #servidor= sai da barra de endereço", (await p.evaluate(() => location.hash)) === "");
  await p.click("#invite-copy");
  await p.waitForSelector("#invite-out:not([hidden])");
  const copiedText = await p.textContent("#invite-out");
  const expected = `${origin}/#servidor=${invitedServer}`;
  const clip = await p.evaluate(() => navigator.clipboard.readText()).catch(() => "");
  check("botão copiar convite", copiedText.includes(expected) && (clip === expected || /copie o convite/.test(copiedText)),
        copiedText);
  p = await ctx.newPage();                       // a new load: a hash change alone does not reload the page
  await p.goto(`${origin}/#servidor=${encodeURIComponent("ws://evil.example")}`);
  await p.waitForSelector("#go:not([disabled])", { timeout: 120000 });
  check("convite inválido não preenche nada",
        (await p.inputValue("#url")) !== "ws://evil.example" && !(await p.isVisible("#invite-note")));
  await ctx.close();

  // 2c. no server of this site: a page the server does not accept (403 on the
  // handshake), and a port nobody listens on, are told on the form, before the chat
  // starts; the browser hides which one it was, so the text lists what to check
  for (const [label, pageOrigin, url] of [
    ["servidor que não aceita esta origem (403)", otherOrigin, `ws://localhost:${wsPort}`],
    ["porta sem servidor", origin, "ws://localhost:9"],
  ]) {
    ctx = await browser.newContext();
    p = await ctx.newPage();
    p.on("pageerror", (e) => check("sem erro na página", false, e.message));
    await p.goto(pageOrigin + "/");
    await p.waitForSelector("#go:not([disabled])", { timeout: 120000 });
    await p.fill("#url", url); await p.fill("#nick", "e2e_origem"); await p.check("#anon"); await p.click("#go");
    await p.waitForFunction(() => document.querySelector("#status").classList.contains("err"), null, { timeout: 30000 });
    const said = await p.textContent("#status");
    check(`${label}: explicação no formulário, com o --ws-origin desta página`,
          said.includes("não foi possível conectar") && said.includes(`--ws-origin ${pageOrigin}`) &&
          await p.isVisible("#login") && await p.isEnabled("#go"), said);
    await ctx.close();
  }

  // 2d. a server address saved back when this site had one (its own host) is dropped
  ctx = await browser.newContext();
  await ctx.addInitScript((u) => { try { localStorage.setItem("crpto.url", u); } catch {} }, `ws://localhost:${site.address().port}/ws`);
  p = await ctx.newPage();
  await p.goto(origin + "/");
  await p.waitForSelector("#go:not([disabled])", { timeout: 120000 });
  const field = await p.inputValue("#url");
  check("endereço salvo do próprio site é ignorado", !field.includes(`:${site.address().port}`), field);
  check("a ajuda mostra a origem que o servidor precisa aceitar",
        (await p.textContent("#url-help")).includes(`--ws-origin ${origin}`));
  await ctx.close();

  // 3. a browser without DecompressionStream (Safari < 16.4): a plain message, no crash
  ctx = await browser.newContext();
  await ctx.addInitScript(() => { delete window.DecompressionStream; });
  p = await ctx.newPage();
  await p.goto(origin + "/");
  await p.waitForFunction(() => document.querySelector("#status").classList.contains("err"), null, { timeout: 30000 });
  const msg = await p.textContent("#status");
  check("sem DecompressionStream: aviso claro", /DecompressionStream/.test(msg) && /16\.4/.test(msg), msg);
  check("sem DecompressionStream: o botão continua desligado", await p.isDisabled("#go"));
  await ctx.close();
} catch (err) {
  check("fluxo completo", false, String(err));
} finally {
  await browser.close();
  server.kill();
  site.close();
  otherSite.close();
  fs.rmSync(TMP, { recursive: true, force: true });
}
if (failures) { console.log(serverOut.slice(-2000)); process.exit(1); }
