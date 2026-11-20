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

// the server, with the WebSocket entrance for that origin
const wsPort = 20000 + Math.floor(Math.random() * 20000);
const server = spawn(PY, ["-m", "crptochat.server", "--data", path.join(TMP, "srv"), "--port", "0", "--no-tls",
                          "--no-stats", "--ws-port", String(wsPort), "--ws-origin", origin], { stdio: ["ignore", "pipe", "pipe"] });
let serverOut = "";
server.stdout.on("data", (d) => (serverOut += d)); server.stderr.on("data", (d) => (serverOut += d));
for (let i = 0; i < 100 && !/cliente web/.test(serverOut); i++) await new Promise((ok) => setTimeout(ok, 100));

const browser = await chromium.launch();
const termText = (p) => p.evaluate(() => {
  const b = window.crptoTerm && window.crptoTerm.buffer.active;
  if (!b) return "";
  let s = ""; for (let i = 0; i < b.length; i++) s += b.getLine(i).translateToString(true) + "\n";
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
  await p.click("#go");
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
  fs.rmSync(TMP, { recursive: true, force: true });
}
if (failures) { console.log(serverOut.slice(-2000)); process.exit(1); }
