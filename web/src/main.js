// crptochat no navegador: carrega o Python (Pyodide) e o cliente de sempre, e
// desenha a interface Textual num xterm.js. Tudo vem desta mesma origem
// (vendor/ e app.zip); nada de CDN, nada de script de terceiros.
import { loadPyodide } from "./vendor/pyodide/pyodide.mjs";
import { Terminal } from "./vendor/xterm/xterm.mjs";
import { FitAddon } from "./vendor/xterm/addon-fit.mjs";

const $ = (id) => document.getElementById(id);
const status = (text, err = false) => { $("status").textContent = text; $("status").classList.toggle("err", err); };
const t0 = performance.now();
const marks = {};
const mark = (name) => { marks[name] = Math.round(performance.now() - t0); };
window.crptoMetrics = marks;

const remember = (k, v) => { try { v === undefined ? (v = localStorage.getItem(k)) : localStorage.setItem(k, v); } catch { v = null; } return v; };
$("url").value = remember("crpto.url") || (location.hostname === "localhost" ? "ws://localhost:7778" : "");
$("nick").value = remember("crpto.nick") || "";

async function syncfs(py, populate) {
  return new Promise((ok, fail) => py.FS.syncfs(populate, (err) => (err ? fail(err) : ok())));
}

async function boot() {
  const py = await loadPyodide({ indexURL: new URL("vendor/pyodide/", location.href).href, fullStdLib: false });
  mark("pyodide");
  status("carregando a criptografia…");
  await py.loadPackage(["cryptography"], { checkIntegrity: true, messageCallback: () => {} });
  const manifest = await (await fetch("vendor/manifest.json")).json();
  await py.loadPackage(manifest.wheels.map((f) => new URL("vendor/wheels/" + f, location.href).href),
                       { messageCallback: () => {} });
  mark("packages");
  status("carregando o crptochat…");
  const app = await (await fetch("app.zip")).arrayBuffer();
  py.unpackArchive(app, "zip", { extractDir: "/app" });
  py.FS.mkdirTree("/data");
  py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data");
  await syncfs(py, true);                                  // chaves e preferências salvas antes
  py.runPython("import sys; sys.path.insert(0, '/app')");
  await py.runPythonAsync("import crptoweb.boot, crptochat.tui");   // importa tudo antes do login
  mark("ready");
  return py;
}

function terminalBridge(term) {
  let onData = null, onResize = null;
  const keys = { esc: "\x1b", tab: "\t", up: "\x1b[A", down: "\x1b[B", right: "\x1b[C", left: "\x1b[D",
                 slash: "/", pgup: "\x1b[5~", pgdn: "\x1b[6~" };
  let ctrl = false;
  term.onData((d) => {
    if (ctrl && d.length === 1) { const c = d.toUpperCase().charCodeAt(0); if (c >= 64 && c < 96) d = String.fromCharCode(c - 64); }
    ctrl = false; $("keys").querySelector("[data-k=ctrl]").classList.remove("on");
    onData && onData(d);
  });
  term.onResize(({ cols, rows }) => onResize && onResize(cols, rows));
  $("keys").addEventListener("click", (e) => {
    const k = e.target.dataset && e.target.dataset.k; if (!k) return;
    e.preventDefault();
    if (k === "ctrl") { ctrl = !ctrl; e.target.classList.toggle("on", ctrl); }
    else onData && onData(keys[k]);
    term.focus();
  });
  return {
    write: (data) => term.write(data),
    size: () => [term.cols, term.rows],
    set_handlers: (data, resize) => { onData = data; onResize = resize; },
  };
}

const ready = boot().then((py) => {
  status(`pronto em ${(marks.ready / 1000).toFixed(1)} s`);
  $("go").disabled = false; $("go").textContent = "entrar";
  return py;
}).catch((err) => { status("falha ao carregar: " + err, true); throw err; });

$("anon").addEventListener("change", () => { $("nick").disabled = $("pass").disabled = $("anon").checked; });

$("form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const py = await ready;
  const url = $("url").value.trim(), nick = $("nick").value.trim(), pass = $("pass").value, anon = $("anon").checked;
  remember("crpto.url", url); if (!anon) remember("crpto.nick", nick);
  $("go").disabled = true; status("abrindo a chave…");
  $("login").hidden = true; $("term").hidden = false; $("keys").hidden = false;
  const term = new Terminal({ fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace', fontSize: 14,
                              cursorBlink: false, allowProposedApi: false, scrollback: 0,
                              theme: { background: "#0b0b12" } });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.open($("term"));
  fit.fit();
  new ResizeObserver(() => fit.fit()).observe($("term"));
  term.focus();
  const saver = setInterval(() => syncfs(py, false).catch(() => {}), 3000);
  addEventListener("pagehide", () => syncfs(py, false).catch(() => {}));
  const start = py.globals.get("__import__")("crptoweb.boot").boot.start;
  mark("login");
  window.crptoTerm = term;
  try {
    const why = await start(terminalBridge(term), url, nick, pass, anon);
    term.reset(); term.write("\r\n  " + why + "\r\n  recarregue a página para entrar de novo.\r\n");
  } catch (err) {
    term.write("\r\n\x1b[31m  " + String(err).split("\n").slice(-2).join(" ") + "\x1b[0m\r\n  recarregue a página para tentar de novo.\r\n");
  } finally {
    clearInterval(saver);
    await syncfs(py, false).catch(() => {});
  }
});
