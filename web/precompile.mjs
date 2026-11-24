// Precompiles the Python the browser runs, with Pyodide's own Python (same
// version and bytecode as in the page), so the page does not compile it again
// on every visit. Used by build.py; needs Node 18+ and `npm ci` in web/.
//
//   node web/precompile.mjs VENDOR_DIR APP_ZIP OUT_DIR
//
// Writes to OUT_DIR:
//   python_stdlib.zip   the part of the standard library the page imports before
//                       the login, as bytecode (.pyc in place of .py)
//   stdlib-rest.zip.gz     the rest of it, fetched while the person types
//   later.zip.gz           packages not needed to open the chat (Pygments, only for
//                       syntax colours), for zipimport once the chat is open
//   site.zip.gz            the pure-Python packages (Textual, Rich...) and the app,
//                       installed, as bytecode (.pyc in place of .py, plus their
//                       data files); and __pycache__ for the compiled packages
//                       (cryptography...) that loadPackage installs with sources
//
// Sourceless: smaller and faster to unpack; tracebacks show no source lines.
//
// Bytecode is "unchecked hash" (PEP 552): no timestamps, never revalidated
// against the source, the same bytes for the same source.
import { loadPyodide } from "./vendor/pyodide/pyodide.mjs";
import fs from "node:fs";
import path from "node:path";

const [vendor, appZip, outDir] = process.argv.slice(2).map((p) => path.resolve(p));
const manifest = JSON.parse(fs.readFileSync(path.join(vendor, "manifest.json"), "utf8"));
const toPath = (p) => p.replace(/\\/g, "/");
const py = await loadPyodide({ indexURL: toPath(path.join(vendor, "pyodide")) + "/", fullStdLib: false, stdout: () => {}, stderr: (l) => console.error(l) });
const lock = JSON.parse(fs.readFileSync(path.join(vendor, "pyodide", "pyodide-lock.json"), "utf8"));
await py.loadPackage(manifest.pyodide_packages.map((n) => toPath(path.join(vendor, "pyodide", lock.packages[n].file_name))), { messageCallback: () => {} });
await py.loadPackage(manifest.wheels.map((f) => toPath(path.join(vendor, "wheels", f))), { messageCallback: () => {} });
py.unpackArchive(new Uint8Array(fs.readFileSync(appZip)), "zip", { extractDir: "/app" });

// what the page imports before the login (main.js: boot); those stdlib modules go
// into the zip Python starts with, the rest into one fetched while the person types
py.runPython(`
import os, sys
sys.path.insert(0, "/app")
os.environ["CRPTOCHAT_HOME"] = "/tmp/precompile-home"
import crptoweb.boot, crptochat.tui, crptoweb.keys
crptoweb.boot.prepare()
`);
py.globals.set("COMPILED_PACKAGES", py.toPy(manifest.pyodide_packages));
const result = py.runPython(`
import gzip, importlib.util, io, os, sys, sysconfig, zipfile
from importlib._bootstrap_external import _code_to_hash_pyc

FIXED = (1980, 1, 1, 0, 0, 0)

def pyc(source_bytes, filename):
    code = compile(source_bytes, filename, "exec", dont_inherit=True, optimize=0)
    return _code_to_hash_pyc(code, importlib.util.source_hash(source_bytes), checked=False)

def add(zf, name, data):
    info = zipfile.ZipInfo(name, FIXED)
    info.compress_type = zf.compression
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)

# 1. the standard library zip, with bytecode next to every module
stdlib = next(p for p in sys.path if p.endswith(".zip"))
used = set()
for mod in list(sys.modules.values()):
    origin = getattr(getattr(mod, "__spec__", None), "origin", None) or ""
    if origin.startswith(stdlib + "/"):
        used.add(origin[len(stdlib) + 1:])
out, rest = io.BytesIO(), io.BytesIO()
n_std = n_core = 0
with zipfile.ZipFile(stdlib) as src, \
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as core_zip, \
        zipfile.ZipFile(rest, "w", zipfile.ZIP_STORED) as rest_zip:
    for name in sorted(src.namelist()):
        data = src.read(name)
        dst = core_zip if (name in used or not name.endswith(".py")) else rest_zip
        if name.endswith(".py"):
            try:
                add(dst, name[:-3] + ".pyc", pyc(data, os.path.join(stdlib, name)))
                n_std += 1
                n_core += dst is core_zip
                continue                 # sourceless: zipimport loads the .pyc
            except SyntaxError:      # test data and the like: stays source only
                pass
        add(dst, name, data)
# the zips the page fetches itself are stored (no per-file compression) and
# gzipped whole: smaller across files, unpacked natively by the browser
# (DecompressionStream), and Python reads them without inflating in wasm
def gz(data):
    return gzip.compress(data, compresslevel=9, mtime=0)

stdlib_zip, stdlib_rest = out.getvalue(), gz(rest.getvalue())

# 2. site-packages and the app: pure packages whole, compiled ones only their bytecode
site = sysconfig.get_paths()["purelib"]
compiled_tops = set()
for dist in COMPILED_PACKAGES:
    # top-level names installed by loadPackage for the compiled packages
    import importlib.metadata as md
    try:
        files = md.files(dist) or []
    except md.PackageNotFoundError:
        files = []
    for f in files:
        compiled_tops.add(str(f).split("/")[0])
LATER = {"pygments"}      # not needed to open the chat: its own zip, fetched afterwards
out, later = io.BytesIO(), io.BytesIO()
n_site = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as main_zip, \
        zipfile.ZipFile(later, "w", zipfile.ZIP_STORED) as later_zip:
    for root, prefix in ((site, site.lstrip("/")), ("/app", "app")):
        for base, dirs, files in sorted(os.walk(root)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            rel = os.path.relpath(base, root)
            top = rel.split(os.sep)[0]
            only_pyc = root == site and top in compiled_tops
            if root == site and top in LATER:
                dst, arc_root = later_zip, ""          # a zip for zipimport: paths from the package down
            else:
                dst, arc_root = main_zip, None
            for name in sorted(files):
                full = os.path.join(base, name)
                arc = os.path.normpath(os.path.join(prefix if arc_root is None else arc_root, rel, name))
                with open(full, "rb") as fh:
                    data = fh.read()
                if name.endswith(".py"):
                    try:
                        code = pyc(data, full)
                    except SyntaxError:
                        code = None
                    if code is not None:
                        n_site += 1
                        if only_pyc:     # the .py is installed by loadPackage: bytecode in __pycache__
                            tag = sys.implementation.cache_tag
                            add(dst, os.path.join(os.path.dirname(arc), "__pycache__", f"{name[:-3]}.{tag}.pyc"), code)
                        else:            # sourceless module: the .pyc where the .py would be
                            add(dst, arc[:-3] + ".pyc", code)
                        continue
                if not only_pyc:
                    add(dst, arc, data)
site_zip, later_zip = gz(out.getvalue()), gz(later.getvalue())
(stdlib_zip, site_zip, later_zip, stdlib_rest, n_std, n_core, n_site, sorted(compiled_tops))
`);
const [stdlibZip, siteZip, laterZip, stdlibRest, nStd, nCore, nSite, tops] = result.toJs();
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, "python_stdlib.zip"), stdlibZip);
fs.writeFileSync(path.join(outDir, "site.zip.gz"), siteZip);
fs.writeFileSync(path.join(outDir, "later.zip.gz"), laterZip);
fs.writeFileSync(path.join(outDir, "stdlib-rest.zip.gz"), stdlibRest);
console.log(`pré-compilado: ${nStd} módulos da stdlib (${nCore} no boot), ${nSite} de pacotes e app (compilados só .pyc: ${tops.join(", ")})`);
