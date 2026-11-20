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

let keys = null;                       // crptoweb.keys (Python), once loaded
const tick = () => new Promise((ok) => setTimeout(ok, 30));   // lets "working…" paint before scrypt

function pyMessage(err) {
  const text = String((err && err.message) || err);
  const m = text.match(/KeyProblem: ([^\n]*)\s*$/) || text.match(/Error: ([^\n]*)\s*$/);
  return m ? m[1].trim() : text.split("\n").filter(Boolean).pop();
}

function call(fn, ...args) {
  try {
    const out = keys[fn](...args);
    return out && out.toJs ? out.toJs({ dict_converter: Object.fromEntries }) : out;
  } catch (err) {
    throw new Error(pyMessage(err));
  }
}

const ready = boot().then((py) => {
  py.runPython("import crptoweb.boot; crptoweb.boot.prepare()");
  keys = py.pyimport("crptoweb.keys");
  status(`pronto em ${(marks.ready / 1000).toFixed(1)} s`);
  $("go").disabled = false;
  $("open-import").disabled = false;
  refreshMode();
  return py;
}).catch((err) => { status("falha ao carregar: " + err, true); throw err; });

// ------------------------------------------------ new key or existing one

const nickOk = (nick) => /^[A-Za-z0-9_-]{3,20}$/.test(nick);
let creating = false;

function refreshMode() {
  const anon = $("anon").checked, nick = $("nick").value.trim();
  $("nick").disabled = $("pass").disabled = anon;
  const has = !anon && keys && nickOk(nick) && call("has_key", nick);
  creating = !anon && !!keys && nickOk(nick) && !has;
  // what password managers read: a new key asks for a new password (they offer to generate one)
  $("pass").autocomplete = creating ? "new-password" : "current-password";
  $("pass2-row").hidden = !creating;
  $("pass2").required = creating;
  $("open-export").disabled = !has;
  $("go").textContent = anon ? "entrar anônimo" : creating ? "criar chave e entrar" : "entrar";
  const info = $("keyinfo");
  info.classList.remove("err");
  info.textContent = anon || !nickOk(nick) ? "" : has
    ? `a chave de ${nick} está neste navegador`
    : `${nick} não tem chave neste navegador: uma chave nova será criada aqui. Já tem uma? Use "importar chave".`;
}
$("nick").addEventListener("input", refreshMode);
$("anon").addEventListener("change", refreshMode);

// Asks the browser's password manager to keep nick + key passphrase (Chromium), only when
// the person ticked "pedir ao navegador para guardar esta senha" (off by default): the
// passphrase and the backup of the key in the same manager leave it as the only protection.
function offerToSave(nick, pass) {
  if (!$("save-pw").checked) return;
  if (!("PasswordCredential" in window) || !navigator.credentials) return;
  try {
    navigator.credentials.store(new PasswordCredential({ id: nick, password: pass, name: `${nick} · crptochat` }))
      .catch(() => {});
  } catch { /* not allowed here */ }
}

// ------------------------------------------------------- export / backup

function showPanel(id) {
  for (const p of ["import", "export"]) $(p).hidden = p !== id;
  $("form").hidden = !!id;
  if (id) $(id).scrollIntoView({ block: "start" });
}
for (const b of document.querySelectorAll("[data-close]")) b.addEventListener("click", () => { showPanel(null); refreshMode(); });

let exported = { nick: "", text: "" };
let afterBackup = null;                // set when the backup panel was opened right after creating a key

function openExport(nick, fingerprint, isNew) {
  exported = { nick, text: call("export_key", nick) };
  $("exp-title").textContent = isNew ? `chave de ${nick} criada: faça o backup agora` : `backup da chave de ${nick}`;
  $("exp-why").textContent = isNew
    ? "Ela existe só neste navegador. Se os dados do site forem apagados (no iPhone o Safari pode fazer isso sozinho) e não houver backup, este nick nunca mais poderá ser usado."
    : "";
  $("exp-pass").textContent = `pass insert -m crptochat/${nick.toLowerCase()}`;
  $("exp-fp").textContent = fingerprint ? `fingerprint ${fingerprint}` : "";
  $("exp-done").textContent = isNew ? "continuar para o chat" : "fechar";
  $("exp-status").textContent = "";
  showPanel("export");
}

$("open-export").addEventListener("click", () => {
  try { openExport($("nick").value.trim(), "", false); } catch (err) { status(err.message, true); }
});

function download(nick, text) {
  const url = URL.createObjectURL(new Blob([text + "\n"], { type: "application/json" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: `crptochat-${nick.toLowerCase()}.json` });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

$("exp-download").addEventListener("click", () => {
  download(exported.nick, exported.text);
  $("exp-status").className = "hint ok"; $("exp-status").textContent = "arquivo baixado: guarde-o no cofre e apague a cópia solta";
});

$("exp-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(exported.text);
    $("exp-status").className = "hint ok"; $("exp-status").textContent = "copiada: cole no cofre (nota segura) e depois copie outra coisa por cima";
  } catch {
    $("exp-status").className = "hint err"; $("exp-status").textContent = "o navegador não deixou copiar; use \"baixar arquivo\"";
  }
});

$("exp-done").addEventListener("click", () => { if (afterBackup) { const go = afterBackup; afterBackup = null; go(); } });

// ------------------------------------------------------------- import

$("open-import").addEventListener("click", () => { $("imp-status").textContent = ""; showPanel("import"); });

async function importText() {
  let text = $("imp-text").value;
  const file = $("imp-file").files[0];
  if (file) {
    if (file.size > 4096) throw new Error("arquivo grande demais para ser uma chave");
    text = await file.text();
  }
  return text;
}

async function doImport(text, replace) {
  const py = await ready, st = $("imp-status");
  const got = call("import_key", text, $("imp-pass").value, replace);
  await syncfs(py, false);
  $("imp-confirm").hidden = true;
  st.className = "hint ok"; st.textContent = `chave de ${got.nick} importada · fingerprint ${got.fingerprint}`;
  $("nick").value = got.nick; $("imp-text").value = ""; $("imp-pass").value = ""; $("imp-file").value = "";
  $("imp-replace").checked = false;
  remember("crpto.nick", got.nick);
}

let pendingReplace = null;             // the blob waiting for "apagar a atual e importar"

$("imp-go").addEventListener("click", async () => {
  const st = $("imp-status");
  $("imp-confirm").hidden = true; pendingReplace = null;
  st.className = "hint"; st.textContent = "conferindo a senha…"; await tick();
  try {
    const text = await importText();
    try {
      await doImport(text, false);       // the same key (or no key here): nothing is lost
    } catch (err) {
      const nick = call("parse_blob", text).nick;
      if (!$("imp-replace").checked || !call("has_key", nick) || !/substituir/.test(err.message)) throw err;
      // replacing destroys the key kept here: one more, explicit step, with a way out
      pendingReplace = { text, nick };
      st.className = "hint"; st.textContent = "";
      $("imp-confirm-text").textContent =
        `A chave de ${nick} que está neste navegador será APAGADA de vez e trocada pela importada. ` +
        `Se ela não tiver backup, o que ela registrou (o nick ${nick} neste servidor) fica inacessível. ` +
        `Exporte a atual antes, se ainda não exportou.`;
      $("imp-confirm").hidden = false;
    }
  } catch (err) {
    st.className = "hint err"; st.textContent = err.message;
  }
});

$("imp-export-current").addEventListener("click", () => {
  if (!pendingReplace) return;
  try {
    download(pendingReplace.nick, call("export_key", pendingReplace.nick));
    $("imp-status").className = "hint ok";
    $("imp-status").textContent = `chave atual de ${pendingReplace.nick} exportada; guarde o arquivo antes de continuar`;
  } catch (err) { $("imp-status").className = "hint err"; $("imp-status").textContent = err.message; }
});

$("imp-confirm-go").addEventListener("click", async () => {
  if (!pendingReplace) return;
  const { text } = pendingReplace;
  pendingReplace = null;
  try { await doImport(text, true); } catch (err) { $("imp-status").className = "hint err"; $("imp-status").textContent = err.message; }
});

$("imp-confirm-cancel").addEventListener("click", () => {
  pendingReplace = null; $("imp-confirm").hidden = true;
  $("imp-status").className = "hint"; $("imp-status").textContent = "nada foi alterado";
});

// -------------------------------------------------------------- login

$("form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const py = await ready;
  const url = $("url").value.trim(), nick = $("nick").value.trim(), pass = $("pass").value, anon = $("anon").checked;
  const info = $("keyinfo"), isNew = creating;
  if (!anon) {
    if (isNew && pass !== $("pass2").value) { info.className = "hint err"; info.textContent = "as senhas não conferem"; return; }
    $("go").disabled = true; status(isNew ? "criando a chave…" : "abrindo a chave…"); await tick();
    let opened;
    try {
      opened = call(isNew ? "create_key" : "check_passphrase", nick, pass);
    } catch (err) {
      info.className = "hint err"; info.textContent = err.message; status(""); $("go").disabled = false; return;
    }
    offerToSave(nick, pass);
    await syncfs(py, false);
    refreshMode();                       // the key exists now: clears old warnings, enables export
    if (isNew) {                         // no chat before the backup is offered
      afterBackup = () => launch(py, url, nick, pass, anon);
      status("");
      openExport(nick, opened.fingerprint, true);
      return;
    }
  }
  launch(py, url, nick, pass, anon);
});

async function launch(py, url, nick, pass, anon) {
  remember("crpto.url", url); if (!anon) remember("crpto.nick", nick);
  $("go").disabled = true; status("conectando…");
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
}
