#!/usr/bin/env python3
"""Randomized end-to-end invariant fuzzer for the Arabic Windows file-task assistant
(TaskRunner + independent Verifier + undo journal + .trash).

Invariants
  I1 NO DATA LOSS    every initially present file's content (multiset of sha256) still exists somewhere
                     inside the workspace (.trash included) after any run (ok / failed / killed / crashed / faulted)
                     and after undo.
  I2 UNDO IS EXACT   undo_task / undo_all (same process, after restart, after workspace rename)
                     restores the initial tree (names, dirs, contents, symlinks; .trash/.undo_journal* excluded),
                     except for files the scenario itself created externally.
  I3 NO ESCAPE       sibling sentinel dir (reachable through a symlink farm inside the workspace) and the
                     parent dir are bit-identical before/after.
  I4 REPORT HONESTY  success=True => each step's effect really holds on disk (own oracle, run right after
                     every step through the progress callback); success=False => failures non-empty and
                     points_charged==0; undo_available is False only if nothing changed.
  I5 LEDGER          balance never negative, charged only (and exactly) on success, no stuck STARTED rows.

Usage:  python3 fuzz_inv.py [--n 3000] [--seed0 1000] [--time-limit 240] [--show SEED] [--only SEED] [-v]
Deterministic: scenario i uses random.Random(seed0 + i). `--show SEED` dumps the full spec, reports,
before/after trees and violations of one scenario.
"""
import argparse
import collections
import contextlib
import errno
import hashlib
import json
import os
import posixpath
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True  # never touch the repo tree
REPO = os.environ.get("FUZZ_REPO") or str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)

from assistant.models.rules import RuleProvider          # noqa: E402
from assistant.planner import PlanStep                   # noqa: E402
from assistant.runner import TaskRunner                  # noqa: E402
from assistant.ledger import SQLiteLedger                # noqa: E402
from assistant.safety import KillSwitch                  # noqa: E402
from assistant.executor import ApprovalTimedOut          # noqa: E402
from assistant.tools.files import FileManager            # noqa: E402
from assistant.verifier import Verifier                  # noqa: E402

REAL_RENAME, REAL_REPLACE, REAL_COPY2 = os.rename, os.replace, shutil.copy2
REAL_SAVE, REAL_VERIFY = FileManager._save_journal, Verifier.verify_step
TMPROOT = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else tempfile.gettempdir()

# --------------------------------------------------------------------------------------
# name pools
# --------------------------------------------------------------------------------------
SAFE_FILES = ["a.txt", "A.txt", "b.pdf", "B.PDF", "c.JPG", "c.jpg", "d.mp3", "تقرير.docx", "صورة 1.png", "notes.tmp", "x.tmp",
              "archive.zip", "noext", "dup1.txt", "dup2.txt", "report (1).pdf", "report.pdf", "a (1).txt", "a (2).txt",
              "rel:x.txt", "git~1.txt", "my file.xlsx", "file.with.many.dots.tar.gz", ".hidden", ".hidden.txt",
              "a..b", "..x", "ملف  بمسافتين.txt", "ملف\u0645\u0631\u062d\u0628\u0627.txt", "\u00e9.txt", "e\u0301.txt", "\u00df.txt",
              "ss.txt", "SS.TXT", "\u0130.txt", "😀.txt", "a b c.csv",
              "UPPER.TXT", "Mixed.Case.Pdf", "(1).txt", " (1)", "x (1) (1).txt", "٠١٢.txt", "Thumbs.db", "desktop.ini",
              "long_" + "ن" * 100 + ".txt", "L" * 250 + ".t", "2026-10", "report.pdf (1)", "x(1).txt", "$weird#name!.log",
              "photo.jpeg", "movie.mp4", "sheet.xls", "slides.pptx", "setup.exe", ".trashy", ".cos_rename_tmp_a.txt", "partial_a.txt"]
UNSAFE_FILES = ["bad\udcffbyte.txt", "name.", "noext.", ".undo_journal_x.txt", "CON", "con.txt", "NUL", "COM1.txt", "a:b", "trail.", "trail ", "GIT~1", "git~2", ".git", "x. ", "aux"]
DIR_POOL = ["docs", "Pics", "مجلد", "Backup", "with space", "dir.d", "صور", "مستندات", "أخرى", "نسخة_احتياطية", "مكررات", "2026-10",
            "DOCS", "Docs", "docs (1)", "a.txt.d", "node_modules", "deep/a/b/c/d/e/f", "x/y", "ملفات/فرعي", ".hiddendir", "src", "tests"]
SENT_NAMES = ["s1.txt", "s2.txt", "a.txt", "docs/inner.txt", "sub/deep/s3.bin"]
HOSTILE = ["../sentinel/s1.txt", "../sentinel", "../sentinel/new.txt", "{S}/s1.txt", "{S}", "{S}/newfile", "..", "../..", ".", "./", "", "  ",
           ".trash", ".trash/x", ".trash/a.txt", ".undo_journal.json", ".undo_journal.json.tmp", ".undo_journal.corrupt.json",
           "lnk_file", "lnk_rel", "lnk_dir", "lnk_dir/s1.txt", "lnk_dir_rel/s2.txt", "dangling", "loop_a", "lnk_ws", "lnk_up", "lnk_self",
           "CON", "aux.txt", "a:b", "x.", "x ", "\\\\server\\share\\x", "C:\\Windows\\x", ".git", ".git/config", "GIT~1", "git~1",
           "docs/../../sentinel/s1.txt", "a\x00b", "{W}/../sentinel/s2.txt", "{W}/a.txt", "/", "~", "docs/./.././a.txt", "a.txt/", "a.txt/b", "a.txt/..",
           "x" * 300, "ملف/" * 40 + "z"]
FARM = [("lnk_file", "{S}/s1.txt"), ("lnk_rel", "../sentinel/s2.txt"), ("lnk_dir", "{S}"), ("lnk_dir_rel", "../sentinel"),
        ("dangling", "nonexistent_target"), ("loop_a", "loop_b"), ("loop_b", "loop_a"), ("lnk_ws", "a.txt"), ("lnk_up", ".."),
        ("lnk_self", "."), ("docs/lnk_in", "{S}/s1.txt"), ("docs/lnk_dir_in", "{S}"), ("Pics/dangling2", "../nope")]
EXTS_NOISE = ["", ".txt", ".pdf"]


def safe_name(n):
    stem = n.split(".")[0].strip().upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)[1-9]", stem):
        return False
    if n != n.rstrip(" .") or ":" in n or "\x00" in n:
        return False
    if n.lower() in {".git", ".ssh", ".gnupg", ".aws"} or re.fullmatch(r"\.?(git|ssh|gnupg|aws)~\d+", n, re.I):
        return False
    return True


def safe_rel(rel):
    return all(safe_name(p) for p in rel.split("/") if p) and not rel.startswith(".trash") and not rel.startswith(".undo_journal")


def pjoin(parent, name):
    return f"{parent}/{name}" if parent else name


def pparent(rel):
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def pbase(rel):
    return rel.rsplit("/", 1)[-1]


def first_free(keys, base):
    if base not in keys:
        return base
    par, name = pparent(base), pbase(base)
    pp = PurePosixPath(name)
    stem, suf = pp.stem, pp.suffix
    n = 1
    while True:
        cand = pjoin(par, f"{stem} ({n}){suf}")
        if cand not in keys:
            return cand
        n += 1


# --------------------------------------------------------------------------------------
# filesystem helpers
# --------------------------------------------------------------------------------------
def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            c = f.read(65536)
            if not c:
                break
            h.update(c)
    return h.hexdigest()


def snap(root, include_trash=False):
    """rel -> 'd' | ('f', sha) | ('l', target); excludes .undo_journal* (top level) and .trash unless include_trash."""
    out = {}
    root = str(root)
    for cur, dirs, files in os.walk(root, followlinks=False):
        relc = os.path.relpath(cur, root)
        relc = "" if relc == "." else relc.replace(os.sep, "/")
        if relc == "":
            dirs[:] = [d for d in dirs if not d.startswith(".undo_journal") and (include_trash or d != ".trash")]
            files[:] = [f for f in files if not f.startswith(".undo_journal")]  # journal, .tmp and .corrupt copies
        for name in sorted(dirs + files):
            full = os.path.join(cur, name)
            rel = pjoin(relc, name)
            if os.path.islink(full):
                out[rel] = ("l", os.readlink(full))
            elif os.path.isdir(full):
                out[rel] = "d"
            else:
                try:
                    out[rel] = ("f", sha_file(full))
                except OSError as e:
                    out[rel] = ("f", f"ERR:{e.errno}")
        # symlinked dirs appear in dirs but are not descended by os.walk(followlinks=False)
    return out


def file_multiset(root):
    """sha256 -> count of regular files anywhere under root (.trash included, journal files excluded)."""
    c = collections.Counter()
    for cur, dirs, files in os.walk(str(root), followlinks=False):
        if os.path.abspath(cur) == os.path.abspath(str(root)):
            files[:] = [f for f in files if f not in (".undo_journal.json", ".undo_journal.json.tmp")]
        for f in files:
            p = os.path.join(cur, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            c[sha_file(p)] += 1
    return c


def hash_outside(base, ws_names):
    """digest of everything under `base` except the workspace dir(s) named in ws_names."""
    h = hashlib.sha256()
    entries = []
    for name in sorted(os.listdir(base)):
        if name in ws_names:
            continue
        for cur, dirs, files in os.walk(os.path.join(base, name), followlinks=False):
            dirs.sort()
            for n in sorted(dirs + files):
                p = os.path.join(cur, n)
                rel = os.path.relpath(p, base)
                st = os.lstat(p)
                if os.path.islink(p):
                    entries.append(f"L|{rel}|{os.readlink(p)}")
                elif os.path.isdir(p):
                    entries.append(f"D|{rel}|{st.st_mode}")
                else:
                    entries.append(f"F|{rel}|{st.st_mode}|{st.st_mtime_ns}|{sha_file(p)}")
        entries.append(f"TOP|{name}")
    entries.append("LIST|" + "|".join(sorted(n for n in os.listdir(base) if n not in ws_names)))
    for e in entries:
        h.update(e.encode("utf-8", "surrogateescape") + b"\n")
    return h.hexdigest(), entries


def diff_trees(a, b, limit=12):
    ks = sorted(set(a) | set(b))
    out = []
    for k in ks:
        if a.get(k) != b.get(k):
            out.append(f"  {k!r}: {fmt(a.get(k))} -> {fmt(b.get(k))}")
            if len(out) >= limit:
                out.append("  ...")
                break
    return "\n".join(out)


def fmt(v):
    if v is None:
        return "<absent>"
    if v == "d":
        return "DIR"
    if v[0] == "f":
        return "file#" + v[1][:8]
    return "link->" + v[1]


# --------------------------------------------------------------------------------------
# scenario generation (pure data; deterministic)
# --------------------------------------------------------------------------------------
def gen_content(rng, name):
    r = rng.random()
    if r < 0.12:
        return b""
    if r < 0.40:
        return rng.choice([b"same", b"same", b"same\n", b"SAME"])
    if r < 0.55:
        return name.encode("utf-8", "replace")
    if r < 0.62:
        return (b"BIG" * 30000)[: rng.choice([65535, 65536, 65537, 90000])]
    return rng.randbytes(rng.randint(1, 80))


PROFILE = {"name": "mixed", "mode": "all"}
NORMAL_FILES = ["a.txt", "b.pdf", "c.jpg", "C.JPG", "d.mp3", "report.pdf", "report (1).pdf", "my file.xlsx", "dup1.txt", "dup2.txt", "notes.tmp", "x.tmp",
                "photo.png", "صورة 1.png", "تقرير.docx", "archive.zip", "noext", "movie.mp4", "setup.exe", "slides.pptx", "data.csv", "readme.md", "a..b",
                "v1..2.txt", "file.tar.gz", ".hidden", "Thumbs.db", "2026-10", "a (1).txt", "e\u0301.txt", "\u00e9.txt"]


def gen_tree(rng):
    if PROFILE["name"] == "nl":
        entries, taken, dirs = [], {}, []
        for d in rng.sample(["docs", "Pics", "مجلد", "Backup", "صور", "مستندات", "أخرى", "x/y", "deep/a/b", "2026-10", "src", "مكررات", "نسخة_احتياطية"], rng.randint(0, 4)):
            parts = d.split("/")
            for i in range(1, len(parts) + 1):
                r = "/".join(parts[:i])
                if r not in taken:
                    taken[r] = "d"
                    entries.append({"t": "d", "p": r})
                    dirs.append(r)
        for _ in range(rng.randint(0, 18)):
            name = rng.choice(NORMAL_FILES)
            parent = rng.choice([""] * 3 + dirs) if dirs else ""
            rel = pjoin(parent, name)
            if rel in taken:
                continue
            taken[rel] = "f"
            entries.append({"t": "f", "p": rel, "c": gen_content(rng, name).hex(), "mt": rng.choice([0, 1, 2, 3])})
        return entries, []
    entries = []
    taken = {}
    dirs = []

    def add_dir(rel):
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            r = "/".join(parts[:i])
            if r in taken:
                if taken[r] != "d":
                    return False
                continue
            taken[r] = "d"
            entries.append({"t": "d", "p": r})
            dirs.append(r)
        return True

    for _ in range(rng.choice([0, 1, 2, 2, 3, 4, 5])):
        add_dir(rng.choice(DIR_POOL))
    nfiles = rng.choice([0, 1, 2, 4, 6, 8, 10, 14, 20])
    for _ in range(nfiles):
        name = rng.choice(UNSAFE_FILES) if rng.random() < 0.05 else rng.choice(SAFE_FILES)
        parent = rng.choice([""] * 3 + dirs) if dirs else ""
        rel = pjoin(parent, name)
        if rel in taken or any(rel.startswith(t + "/") and k != "d" for t, k in taken.items()):
            continue
        taken[rel] = "f"
        entries.append({"t": "f", "p": rel, "c": gen_content(rng, name).hex(), "mt": rng.choice([0, 1, 2, 3])})
        if rng.random() < 0.04 and rel.count("/") == 0:  # hard link twin
            twin = rng.choice(["hl_" + name, "HL.txt"])
            if twin not in taken:
                taken[twin] = "f"
                entries.append({"t": "h", "p": twin, "of": rel})
    # explicit duplicate content
    if rng.random() < 0.4:
        names = [e for e in entries if e["t"] == "f"]
        if names:
            src = rng.choice(names)
            for nm in ("dupcopy.txt", "dup/dupcopy2.txt"):
                if nm not in taken and pparent(nm) in ("", ) + tuple(dirs):
                    taken[nm] = "f"
                    entries.append({"t": "f", "p": nm, "c": src["c"], "mt": src["mt"]})
    # symlink farm
    if rng.random() < 0.55:
        for name, tgt in FARM:
            if rng.random() < 0.6 and name not in taken and (pparent(name) == "" or pparent(name) in taken):
                taken[name] = "l"
                entries.append({"t": "l", "p": name, "to": tgt})
    pre_trash = []
    if rng.random() < 0.05:
        entries.append({"t": "j", "p": ".undo_journal.json", "c": rng.choice([b"{corrupt", b"", b"[1,2,3]", b"null", b"\xff\xfe"]).hex()})
    if rng.random() < 0.25:
        for nm in rng.sample(["a.txt", "b.pdf", "dup1.txt", "report.pdf", "x (1).txt", "docs"], rng.randint(1, 3)):
            pre_trash.append({"p": nm, "c": gen_content(rng, nm).hex(), "dir": nm == "docs"})
    return entries, pre_trash


class Model:
    def __init__(self, entries):
        self.k = {}
        for e in entries:
            if e["t"] == "l":
                continue
            if e["t"] == "j":
                continue
            self.k[e["p"]] = "d" if e["t"] == "d" else "f"
            par = pparent(e["p"])
            while par:
                self.k.setdefault(par, "d")
                par = pparent(par)

    def sub(self, rel):
        return [r for r in self.k if r == rel or r.startswith(rel + "/")]

    def add_dirs(self, rel):
        while rel:
            self.k.setdefault(rel, "d")
            rel = pparent(rel)

    def apply(self, act, target, params):
        t = posixpath.normpath(target) if target else ""
        if t not in self.k:
            return
        if act in ("delete", "trash"):
            for r in self.sub(t):
                del self.k[r]
        elif act in ("move", "copy", "rename"):
            if act == "rename":
                nn = params.get("new_name", "")
                if "/" in nn or not nn:
                    return
                base = pjoin(pparent(t), nn)
            else:
                d = posixpath.normpath(params.get("dest", "") or "x")
                if d.startswith("..") or d.startswith("/") or d == ".":
                    return
                base = pjoin(d, pbase(t)) if self.k.get(d) == "d" and d != t else d
            R = first_free(self.k, base)
            if R == t or R.startswith(t + "/"):
                return
            subs = self.sub(t)
            self.add_dirs(pparent(R))
            vals = {r: self.k[r] for r in subs}
            for r in subs:
                self.k[R + r[len(t):]] = vals[r]
            if act != "copy":
                for r in subs:
                    del self.k[r]
        elif act == "create_dir":
            self.add_dirs(t)


def pick_src(rng, m, hostile=True):
    r = rng.random()
    if hostile and r < 0.035:
        return rng.choice(HOSTILE)
    if r < 0.12 or not m.k:
        return sane_choice(rng, SAFE_FILES + ["nope.txt", "ghost/x.txt"] + DIR_POOL)
    keys = list(m.k)
    ok = [k for k in keys if safe_rel(k)]
    if ok and rng.random() < 0.93:
        keys = ok
    if rng.random() < 0.55:
        fl = [k for k in keys if m.k[k] == "f"]
        if fl:
            return rng.choice(sorted(fl))
    return rng.choice(sorted(keys))


def sane_choice(rng, seq):
    for _ in range(6):
        x = rng.choice(seq)
        if safe_rel(x) or rng.random() < 0.04:
            return x
    return x


def pick_dest(rng, m, src):
    r = rng.random()
    dirs = sorted(k for k, v in m.k.items() if v == "d" and safe_rel(k))
    files = sorted(k for k, v in m.k.items() if v == "f" and safe_rel(k))
    if r < 0.04:
        return rng.choice(HOSTILE)
    if r < 0.24 and dirs:
        return rng.choice(dirs)
    if r < 0.38 and dirs:
        return pjoin(rng.choice(dirs), pbase(src) if src else "n.txt")
    if r < 0.50 and files:
        return rng.choice(files)  # collision
    if r < 0.56:
        return src  # self
    if r < 0.62:
        return pjoin(src, rng.choice(["inner", "z.txt", "a/b"]))  # into self
    if r < 0.66:
        return pjoin(src, "")  # trailing slash
    if r < 0.72:
        return sane_choice(rng, DIR_POOL) + "/" + sane_choice(rng, SAFE_FILES)
    if r < 0.80:
        return rng.choice(["new1", "new2/sub/x.txt", "الجديد/ملف.txt", "نسخة (1)", "N.txt", "A.TXT"])
    return sane_choice(rng, SAFE_FILES + DIR_POOL)


def gen_steps(rng, m):
    """return list of (action, target, params)."""
    steps = []
    nsteps = rng.choice([1, 1, 2, 2, 3, 3, 4, 5, 6, 8, 12, 20])
    macro = rng.random() < 0.35
    if macro:
        steps += gen_macro(rng, m)
    while len(steps) < nsteps:
        act = rng.choices(["create_dir", "move", "copy", "rename", "delete", "inspect", "trash"], [12, 26, 18, 14, 16, 8, 2])[0]
        t = pick_src(rng, m)
        if act == "create_dir":
            if rng.random() < 0.7:
                t = sane_choice(rng, DIR_POOL) if rng.random() < 0.6 else pjoin(rng.choice(sorted(k for k, v in m.k.items() if v == "d" and safe_rel(k)) or ["nd"]), rng.choice(["sub", "نو", "x y"]))
            p = {}
        elif act in ("move", "copy"):
            p = {"dest": pick_dest(rng, m, t)}
        elif act == "rename":
            nn = rng.choice([sane_choice(rng, SAFE_FILES + DIR_POOL)] * 6 + ([] if FS_SAFE["on"] else [pbase(t).upper(), pbase(t).lower()]) + [pbase(t) + "2", "../x", "a/b", "..", ".", "CON", "x.", "(1)"])
            if rng.random() < 0.15:
                nn = pbase(t)
            p = {"new_name": nn}
        else:
            p = {}
        steps.append((act, t, p))
        m.apply(act, t, p)
    if rng.random() < 0.04:
        steps.append((rng.choice(["permanent_delete", "format_disk", "bogus_action", "DELETE_FOREVER"]), pick_src(rng, m), {}))
    if rng.random() < 0.03:
        steps.insert(rng.randint(0, len(steps)), ("MOVE", pick_src(rng, m), {"dest": pick_dest(rng, m, "a.txt")}))
    return steps


def gen_macro(rng, m):
    files = sorted(k for k, v in m.k.items() if v == "f" and safe_rel(k))
    dirs = sorted(k for k, v in m.k.items() if v == "d" and safe_rel(k))
    f = rng.choice(files) if files else "a.txt"
    f2 = rng.choice(files) if files else "b.pdf"
    d = rng.choice(dirs) if dirs else "docs"
    kinds = ["swap", "delrecreate", "nestmove", "copyrenamedel", "cycle", "collide", "deepchain", "selfmoves",
             "caseonly", "organize", "dirsandback", "delparent", "moveintofile", "samename"]
    if FS_SAFE["on"]:
        kinds.remove("caseonly")   # على نظام ملفات غير حساس للحالة a.txt وA.TXT اسم واحد: النموذج المرجعي يفترض العكس
    kind = rng.choice(kinds)
    S = []
    if kind == "swap":
        S = [("move", f, {"dest": "tmp_swap.bin"}), ("move", f2, {"dest": f}), ("move", "tmp_swap.bin", {"dest": f2})]
    elif kind == "delrecreate":
        S = [("delete", d, {}), ("create_dir", d, {}), ("move", f, {"dest": pjoin(d, pbase(f))})]
    elif kind == "nestmove":
        S = [("move", d, {"dest": "E/" + pbase(d)}), ("move", "E", {"dest": "F"}), ("rename", "F/" + pbase(d), {"new_name": "back"})]
    elif kind == "copyrenamedel":
        S = [("copy", d if rng.random() < 0.5 else f, {"dest": "cp"}), ("rename", "cp", {"new_name": "cp2"}), ("delete", d if rng.random() < 0.5 else f, {})]
    elif kind == "cycle":
        S = [("rename", f, {"new_name": pbase(f2)}), ("rename", f2, {"new_name": pbase(f)}), ("rename", f, {"new_name": "zz"})]
    elif kind == "collide":
        S = [("copy", f, {"dest": f}), ("copy", f, {"dest": f}), ("copy", f, {"dest": f}), ("move", f2, {"dest": f}), ("rename", f, {"new_name": pbase(f2)})]
    elif kind == "deepchain":
        S = [("create_dir", "p/q/r/s", {}), ("move", f, {"dest": "p/q/r/s/" + pbase(f)}), ("copy", "p", {"dest": "p2"}), ("delete", "p", {})]
    elif kind == "selfmoves":
        S = [("move", d, {"dest": d}), ("move", d, {"dest": d + "/sub/" + pbase(d)}), ("copy", d, {"dest": d + "/sub"}), ("rename", d, {"new_name": pbase(d)}),
             ("move", f, {"dest": f + "/x"}), ("move", d, {"dest": d + "/."}), ("copy", d, {"dest": d})]
    elif kind == "caseonly":
        S = [("rename", f, {"new_name": pbase(f).upper()}), ("rename", d, {"new_name": pbase(d).upper()}), ("rename", f, {"new_name": pbase(f).lower()}),
             ("rename", d, {"new_name": pbase(d).title()})]
    elif kind == "organize":
        S = [("create_dir", c, {}) for c in ("صور", "مستندات", "أخرى")]
        for fl in files[:12]:
            S.append(("move", fl, {"dest": rng.choice(["صور", "مستندات", "أخرى"]) + "/" + pbase(fl)}))
    elif kind == "dirsandback":
        S = [("create_dir", "t1/t2/t3", {}), ("move", f, {"dest": "t1/t2/t3/" + pbase(f)}), ("move", "t1/t2/t3/" + pbase(f), {"dest": f}), ("delete", "t1", {})]
    elif kind == "delparent":
        S = [("delete", d, {}), ("move", d + "/" + pbase(f), {"dest": "out.txt"}), ("create_dir", d + "/n", {})]
    elif kind == "moveintofile":
        S = [("create_dir", f + "/sub", {}), ("move", f2, {"dest": f + "/x"}), ("copy", f2, {"dest": f + "/x"})]
    elif kind == "samename":
        S = [("create_dir", f, {}), ("create_dir", d, {}), ("inspect", ".", {}), ("inspect", d, {}), ("inspect", "nonexistent", {})]
    for a, t, p in S:
        m.apply(a, t, p)
    return S


NL_TEMPLATES = [
    "رتب الملفات حسب النوع", "رتّب الملفات حسب الشهر", "رتب حسب السنة", "رتب الملفات حسب التاريخ", "انقل ملفات pdf إلى مستندات",
    "انقل الصور إلى Pics", "ضع ملفات tmp في قمامة", "احذف ملفات tmp", "شيل {n}", "احذف {n}", "احذف «{n}»", "احذف {n} و {n2}",
    "انقل {n} إلى {d}", "انقل «{n}» إلى «{d}»", "انسخ {n} إلى {d}", "انسخ {n}", "انقل المكررات", "احذف المكررات", "انقل {n} إلى {d2}",
    "أعد تسمية ملفات {d} بإضافة البادئة 2026_", "أعد تسمية الملفات بإضافة التاريخ", "أعد تسمية الملفات بإضافة البادئة old_", "غير اسم {n} إلى «{n2}»",
    "غير اسم {n} إلى new_name.txt", "rename {n} to {n2}", "delete {n}", "move {n} to {d}", "copy {n} to {d}", "create folder {d}", "أنشئ مجلد {d}",
    "أنشئ مجلد «{d2}»", "أنشئ مشروع demo", "أنشئ مشروع «{d2}»", "افحص {n}", "افحص المجلد", "organize files by type", "remove duplicates", "احذف الصور",
    "انقل كل الصور إلى {d}", "انسخ ملفات pdf إلى {d}", "انقل ملفات txt إلى النصوص", "احذف ملفات txt", "انسخ {d} إلى {d2}", "انقل {d} إلى {d2}",
    "شيل {d}", "احذف المجلد {d}", "ضع {n} في {d}", "تخلص من {n}", "ضع ملفات pdf في «مستندات مهمة»", "رتب", "احذف", "انقل", "../../etc/passwd",
    "احذف ../sentinel/s1.txt", "انقل {n} إلى ../sentinel", "انسخ {n} إلى C:\\Windows", "انسخ {n} إلى CON", "أنشئ مجلد a:b", "احذف {S}/s1.txt",
    "انسخ {n} إلى {S}", "انقل {n} إلى {n2}", "انسخ {n} إلى {n}", "غير اسم {n} إلى {n}", "غير اسم {d} إلى {D}", "غير اسم {n} إلى {N}", "احذف .trash",
    "انقل .undo_journal.json إلى {d}", "افحص .trash", "احذف lnk_dir", "انسخ lnk_file إلى {d}", "انقل lnk_dir إلى {d}",
]


def gen_nl(rng, m):
    files = sorted(k for k, v in m.k.items() if v == "f")
    dirs = sorted(k for k, v in m.k.items() if v == "d")
    simple = lambda x: re.fullmatch(r"[\w.\-]+", x) is not None  # noqa: E731
    topf = [f for f in files if "/" not in f and (simple(f) or rng.random() < 0.2)] or files or ["a.txt"]
    topd = [d for d in dirs if "/" not in d and (simple(d) or rng.random() < 0.2)] or dirs or ["docs"]
    t = rng.choice(NL_TEMPLATES)
    n = rng.choice(topf)
    n2 = rng.choice(topf + SAFE_FILES)
    d = rng.choice(topd)
    return t.format(n=n, n2=n2, d=d, d2=rng.choice(DIR_POOL), D=d.upper(), N=n.upper(), S="{S}")


NO_AFTER = {"v": False}
FS_SAFE = {"on": False}   # ويندوز/نظام ملفات غير حساس للحالة: مجموعات أسماء متوافقة (انظر apply_fs_safe)


def gen_fault(rng, undo=False):
    r = rng.random()
    if r < 0.62:
        return None
    kinds = ["rename", "rename", "replace", "copy2", "save", "verify", "approver", "progress"]
    tgt = rng.choice(kinds if not undo else ["rename", "replace"])
    modes = {"rename": ["raise", "raise", "kill_after", "kill_before", "ki", "raise_after"], "replace": ["raise", "raise", "kill_after", "ki", "raise_after"],
             "copy2": ["raise", "raise_partial", "kill_before", "kill_after", "ki"], "save": ["raise", "kill_after", "raise_after"],
             "verify": ["kill_before"], "approver": ["kill_before"], "progress": ["kill_after"]}
    mode = rng.choice(modes[tgt])
    if (undo or NO_AFTER["v"]) and mode in ("ki", "raise_after"):
        mode = "raise"   # أخطاء «بعد وقوع الأثر» (قطع لحظي بين النقل وحفظ السجل) محاكاة لا تحدث في os.rename الحقيقي
    return {"target": tgt, "k": rng.choice([1, 1, 1, 2, 2, 3, 3, 4, 5, 8]), "mode": mode, "persist": rng.random() < 0.1 and mode == "raise"}


def gen_spec(rng):
    entries, pre_trash = gen_tree(rng)
    m = Model(entries)
    spec = {"ws_name": rng.choice(["ws", "ws", "ws", "مجلد العمل", "work space", "ws.d"]), "entries": entries, "pre_trash": pre_trash,
            "balance": 1000, "limit": 1000, "tasks": [], "undo": {}}
    r = rng.random()
    if r < 0.05:
        spec["balance"], spec["limit"] = rng.choice([(0, 5), (1, 1), (2, 100), (100, 1), (3, 3)])
    ntasks = rng.choice([1, 1, 1, 2, 2, 3])
    for ti in range(ntasks):
        kind = "nl" if rng.random() < 0.4 else "plan"
        if rng.random() < 0.08:
            kind = "api"
        if PROFILE["name"] == "nl":
            kind = "nl"
        task = {"kind": kind, "approver": rng.choice(["yes", "yes", "yes", "yes", "random", "none", "timeout", "kill"] if PROFILE["name"] != "nl" else ["yes"] * 9 + ["random"]),
                "fault": gen_fault(rng) if PROFILE["name"] != "nl" else None, "reset_kill": rng.random() < 0.7, "ledger_fault": rng.choice([None] * 40 + ["success", "start"]),
                "dup_id": ti > 0 and rng.random() < 0.03}
        task["timeout_steps"] = rng.choice([None] * 24 + [0, 1, 2, 3]) if kind != "api" else None
        task["swap"] = {"k": rng.choice([1, 1, 2, 3]), "to": rng.choice(["dir", "file", "rel"])} if kind == "plan" and rng.random() < 0.05 else None
        if kind == "api":
            ops = []
            for _ in range(rng.randint(1, 8)):
                op = rng.choice(["move", "copy", "rename", "delete", "create_dir", "move", "copy", "delete", "undo_last"])
                a = pick_src(rng, m, hostile=False) if rng.random() > 0.3 else rng.choice(HOSTILE)
                b = pick_dest(rng, m, a) if rng.random() > 0.3 else rng.choice(HOSTILE)
                if op == "rename":
                    b = rng.choice([pbase(a) + "x", pbase(a).upper(), "../escape", "a/b", "..", "(1)", sane_choice(rng, SAFE_FILES)])
                ops.append({"op": op, "a": a, "b": b})
                m.apply(op, a, {"dest": b, "new_name": b})
            task["ops"] = ops
        elif kind == "nl":
            task["text"] = gen_nl(rng, m)
        elif False:
            pass
        else:
            task["steps"] = [{"id": rng.choice([i + 1, i + 1, i + 1, i * 7 + 3]) if i else 1, "a": a, "t": t, "p": p} for i, (a, t, p) in enumerate(gen_steps(rng, m))]
            if rng.random() < 0.03:
                n0 = len(task["steps"])
                extra = rng.choice([59, 60, 61, 70])
                task["steps"] += [{"id": n0 + 1 + i, "a": rng.choice(["create_dir", "inspect"]), "t": f"bulk/{i}" if i % 2 else ".", "p": {}} for i in range(extra)]
            seen = set()
            for s in task["steps"]:
                while s["id"] in seen:
                    s["id"] += 100
                seen.add(s["id"])
        spec["tasks"].append(task)
    spec["undo"] = {"method": "all" if any(t["kind"] == "api" for t in spec["tasks"]) else rng.choice(["task", "task", "task", "all"]), "restart": rng.random() < 0.35, "rename_ws": rng.random() < 0.25,
                    "ext": rng.choice([None] * 6 + ["root", "in_created", "collide"]), "fault": gen_fault(rng, undo=True) if rng.random() < 0.2 else None,
                    "retry_restart": rng.random() < 0.5}
    return spec


# --------------------------------------------------------------------------------------
# fault injection
# --------------------------------------------------------------------------------------
class FaultCtx:
    def __init__(self, f, ks):
        self.f, self.ks = f, ks
        self.cnt = collections.Counter()
        self.fired = False

    def _hit(self, name):
        f = self.f
        if not f or f["target"] != name:
            return False
        self.cnt[name] += 1
        if self.cnt[name] == f["k"] or (f.get("persist") and self.cnt[name] >= f["k"]):
            self.fired = True
            return True
        return False

    def _fire(self, real, partial=None):
        mode = self.f["mode"]
        if mode == "raise":
            raise OSError(errno.EIO, "injected EIO")
        if mode == "raise_partial":
            if partial:
                partial()
            raise OSError(errno.ENOSPC, "injected ENOSPC")
        if mode == "raise_after":
            real()
            raise OSError(errno.EIO, "injected EIO after effect")
        if mode == "kill_before":
            self.ks.trigger("fuzz kill")
            return real()
        if mode == "kill_after":
            r = real()
            self.ks.trigger("fuzz kill")
            return r
        if mode == "ki":
            raise KeyboardInterrupt("injected")
        return real()

    def __enter__(self):
        me = self

        def rename(src, dst, *a, **k):
            real = lambda: REAL_RENAME(src, dst, *a, **k)  # noqa: E731
            return me._fire(real) if me._hit("rename") else real()

        def replace(src, dst, *a, **k):
            real = lambda: REAL_REPLACE(src, dst, *a, **k)  # noqa: E731
            return me._fire(real) if me._hit("replace") else real()

        def copy2(src, dst, *a, **k):
            real = lambda: REAL_COPY2(src, dst, *a, **k)  # noqa: E731

            def partial():
                data = open(src, "rb").read()
                with open(dst, "wb") as fh:
                    fh.write(data[: len(data) // 2])
            return me._fire(real, partial) if me._hit("copy2") else real()

        def save(self_fm):
            real = lambda: REAL_SAVE(self_fm)  # noqa: E731
            return me._fire(real) if me._hit("save") else real()

        def verify(self_v, *a, **k):
            real = lambda: REAL_VERIFY(self_v, *a, **k)  # noqa: E731
            return me._fire(real) if me._hit("verify") else real()

        os.rename, os.replace, shutil.copy2 = rename, replace, copy2
        FileManager._save_journal, Verifier.verify_step = save, verify
        return self

    def __exit__(self, *exc):
        os.rename, os.replace, shutil.copy2 = REAL_RENAME, REAL_REPLACE, REAL_COPY2
        FileManager._save_journal, Verifier.verify_step = REAL_SAVE, REAL_VERIFY
        return False


# --------------------------------------------------------------------------------------
# effect oracle (independent of Verifier)
# --------------------------------------------------------------------------------------
def norm_rel(ws_real, p):
    full = os.path.normpath(os.path.join(ws_real, p))
    rel = os.path.relpath(full, ws_real)
    if rel == ".":
        return ""
    if rel == ".." or rel.startswith("../"):
        return None
    return rel.replace(os.sep, "/")


def subtree(snapd, rel):
    return [k for k in snapd if k == rel or k.startswith(rel + "/")]


def oracle(ws_real, step, B, A, skipped=False):
    """returns list of problems (empty = effect is exactly what the step promises)."""
    act = step.action_type
    probs = []
    changed = [k for k in B if k in A and B[k] != A[k]]
    removed = {k for k in B if k not in A}
    added = {k for k in A if k not in B}
    if skipped:
        if changed or removed or added:
            probs.append(f"skipped step changed disk: +{sorted(added)[:3]} -{sorted(removed)[:3]}")
        return probs
    if changed:
        probs.append(f"existing entries changed/overwritten: {[(k, fmt(B[k]), fmt(A[k])) for k in changed[:4]]}")
    if act == "inspect":
        if removed or added:
            probs.append(f"inspect modified disk +{sorted(added)[:3]} -{sorted(removed)[:3]}")
        return probs
    tgt = norm_rel(ws_real, step.target if step.target.strip() not in (".", "./") else ".")
    if tgt is None:
        return [f"target escapes workspace: {step.target!r}"]
    if act == "create_dir":
        if removed:
            probs.append(f"create_dir removed entries {sorted(removed)[:3]}")
        if A.get(tgt) != "d":
            probs.append(f"create_dir target {tgt!r} is not a dir afterwards ({fmt(A.get(tgt))})")
        extra = [k for k in added if not (tgt == k or tgt.startswith(k + "/")) or A[k] != "d"]
        if extra:
            probs.append(f"create_dir created unrelated entries {sorted(extra)[:4]}")
        return probs
    if tgt not in B:
        probs.append(f"{act} of a nonexistent source {tgt!r} reported ok")
        return probs
    S = tgt
    sub = subtree(B, S)
    if act in ("delete", "trash"):
        if set(sub) != removed:
            probs.append(f"delete removed {sorted(removed)[:4]} but source subtree is {sorted(sub)[:4]}")
        base = ".trash/" + pbase(S)
        keysB = set(B)
        R = first_free(keysB, base)
        want = {R + k[len(S):]: B[k] for k in sub}
        if added != set(want):
            probs.append(f"delete: trash additions {sorted(added)[:4]} != expected {sorted(want)[:4]}")
        for k, v in want.items():
            if A.get(k) != v:
                probs.append(f"delete: trash content mismatch at {k!r}: {fmt(v)} vs {fmt(A.get(k))}")
                break
        return probs
    if act in ("move", "copy", "rename"):
        if act == "rename":
            nn = step.params.get("new_name", "")
            base = pjoin(pparent(S), nn)
        else:
            D = norm_rel(ws_real, step.params.get("dest", ""))
            if D is None:
                return [f"dest escapes workspace: {step.params.get('dest')!r}"]
            base = pjoin(D, pbase(S)) if B.get(D) == "d" and D != S else D
        R = first_free(set(B), base)
        mapped = {R + k[len(S):]: B[k] for k in sub}
        if act == "copy":
            if removed:
                probs.append(f"copy removed {sorted(removed)[:4]}")
        else:
            if set(sub) != removed:
                probs.append(f"{act} removed {sorted(removed)[:4]} but source subtree is {sorted(sub)[:4]}")
        anc = set()
        p = pparent(R)
        while p:
            if p not in B:
                anc.add(p)
            p = pparent(p)
        want_added = set(mapped) | anc
        if added != want_added:
            probs.append(f"{act}: naming/extent mismatch: added {sorted(added)[:4]} expected {sorted(want_added)[:4]}")
        for k, v in mapped.items():
            if A.get(k) != v:
                probs.append(f"{act}: content mismatch at {k!r}: {fmt(v)} vs {fmt(A.get(k))}")
                break
        for k in anc:
            if A.get(k) != "d":
                probs.append(f"{act}: ancestor {k!r} not a dir")
        return probs
    return [f"oracle: unknown action {act}"]


# --------------------------------------------------------------------------------------
# scenario execution
# --------------------------------------------------------------------------------------
class Violation:
    def __init__(self, seed, inv, msg, detail=""):
        self.seed, self.inv, self.msg, self.detail = seed, inv, msg, detail

    def sig(self):
        s = re.sub(r"\d+", "N", self.msg)
        s = re.sub(r"'[^']*'|\"[^\"]*\"", "Q", s)[:90]
        return f"{self.inv}:{s}"


class Ctx:
    pass


def make_approver(mode, rng, ks):
    def ap(step):
        if mode == "yes":
            return True
        if mode == "random":
            return rng.random() < 0.5
        if mode == "timeout":
            raise ApprovalTimedOut()
        if mode == "kill":
            ks.trigger("fuzz kill in approver")
            return False
        return False
    return None if mode == "none" else ap


def subst(s, S, W):
    return s.replace("{S}", S).replace("{W}", W) if isinstance(s, str) else s


def build_ws(spec, base, S):
    ws = base / spec["ws_name"]
    ws.mkdir()
    mts = [1_700_000_000, 1_730_000_000, 1_750_000_000, 1_760_000_000]
    for e in sorted(spec["entries"], key=lambda e: (e["p"].count("/"), e["t"] != "d")):
        if e["t"] == "j" and False:
            continue
        p = ws / e["p"]
        try:
            if e["t"] == "d":
                p.mkdir(parents=True, exist_ok=True)
            elif e["t"] == "f":
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(bytes.fromhex(e["c"]))
                os.utime(p, (mts[e["mt"]], mts[e["mt"]]))
            elif e["t"] == "j":
                p.write_bytes(bytes.fromhex(e["c"]))
        except OSError:
            e["_skip"] = True
    for e in spec["entries"]:
        p = ws / e["p"]
        try:
            if e["t"] == "h" and (ws / e["of"]).exists():
                os.link(ws / e["of"], p)
            elif e["t"] == "l":
                os.symlink(subst(e["to"], str(S), str(ws)), p)
        except OSError:
            pass
    if spec["pre_trash"]:
        tr = ws / ".trash"
        tr.mkdir()
        for e in spec["pre_trash"]:
            if e["dir"]:
                (tr / e["p"]).mkdir()
                (tr / e["p"] / "old.txt").write_bytes(bytes.fromhex(e["c"]))
            else:
                (tr / e["p"]).write_bytes(bytes.fromhex(e["c"]))
    return ws


def run_scenario(seed, keep=None):
    if PROFILE["mode"] == "all":
        PROFILE["name"] = "nl" if seed % 3 == 2 else "mixed"
    else:
        PROFILE["name"] = PROFILE["mode"]
    rng = random.Random(seed)
    spec = gen_spec(rng)
    arng = random.Random(seed ^ 0x5EED)
    V = []
    log = []
    stats = collections.Counter()

    def viol(inv, msg, detail=""):
        V.append(Violation(seed, inv, msg, detail))

    base = Path(tempfile.mkdtemp(prefix="fz_", dir=TMPROOT)).resolve()
    ldir = Path(tempfile.mkdtemp(prefix="fzl_", dir=TMPROOT)).resolve()
    try:
        S = base / "sentinel"
        for nm in SENT_NAMES:
            (S / nm).parent.mkdir(parents=True, exist_ok=True)
            (S / nm).write_bytes(("SENTINEL:" + nm).encode())
        ws = build_ws(spec, base, S)
        ws_names = {spec["ws_name"], "ws_renamed"}
        init_snap = snap(ws)
        init_total = snap(ws, True)
        init_files = file_multiset(ws)
        out0, _ = hash_outside(base, ws_names)
        ledger = SQLiteLedger(ldir / "l.db", initial_balance=spec["balance"], spending_limit=spec["limit"])
        ks = KillSwitch()
        ctx = Ctx()
        ctx.ws = ws
        ctx.runner = TaskRunner(ws, RuleProvider(), ledger, kill_switch=ks, approver=lambda s: True)
        ran_ids = []
        swapped = [False]
        fault_seen = [False]
        crashed_ki = False
        postfault = False
        init_corrupt = [e for e in spec["entries"] if e["t"] == "j"]
        if keep is not None:
            keep.update(spec=spec, init_snap=init_snap, base=str(base), reports=[])

        def check_journal_sync(tag, runner):
            fm_ = runner.file_manager
            try:
                disk = json.loads((fm_.journal_path).read_text(encoding="utf-8"))
            except FileNotFoundError:
                disk = []
            except Exception as e:  # noqa: BLE001
                viol("I2j", f"journal unreadable on disk ({tag}): {type(e).__name__}")
                return
            mem = [fm_._entry_to_disk(e) for e in fm_.journal]
            disk = [{k: v for k, v in e.items() if k != "mac"} for e in disk if isinstance(e, dict)] if isinstance(disk, list) else disk   # التوقيع لا يدخل المقارنة
            if disk != mem:
                viol("I2j", f"on-disk journal != in-memory journal ({tag}): disk has {len(disk)} entries, memory {len(mem)}",
                     f"disk tail: {disk[-2:]}\nmem tail: {mem[-2:]}")

        def check_global(tag, expect_files=True):
            if expect_files:
                now = file_multiset(ws_cur())
                lost = {h: (c, now.get(h, 0)) for h, c in init_files.items() if now.get(h, 0) < c}
                if lost:
                    h = sorted(lost)[0]
                    missing_all = [h for h in lost if now.get(h, 0) == 0]
                    viol("I1", f"data loss ({tag}): {len(lost)} content(s) lost, {len(missing_all)} entirely gone",
                         "lost hashes (initial,now): " + ", ".join(f"{k[:8]}:{v}" for k, v in sorted(lost.items())[:6]))
            outn, ent = hash_outside(base, ws_names)
            if outn != out0:
                viol("I3", f"outside workspace changed ({tag})", "")

        cur_ws = {"p": ws}

        def ws_cur():
            return cur_ws["p"]

        # ---------------- run tasks ----------------
        for ti, task in enumerate(spec["tasks"]):
            tid = f"t{seed}_{ti}"
            if task["dup_id"] and ran_ids:
                tid = ran_ids[0]
            runner = ctx.runner
            if task["reset_kill"]:
                ks.reset()
            if task["kind"] == "api":
                fm = runner.file_manager
                fm.current_task = None
                fc_api = FaultCtx(task["fault"] if task["fault"] and task["fault"]["target"] in ("rename", "replace", "copy2", "save") else None, ks)
                S_, W_ = str(S), str(ws_cur())
                with fc_api:
                    for op in task["ops"]:
                        a_, b_ = subst(op["a"], S_, W_), subst(op["b"], S_, W_)
                        try:
                            if op["op"] == "move":
                                fm.move(a_, b_)
                            elif op["op"] == "copy":
                                fm.copy(a_, b_)
                            elif op["op"] == "rename":
                                fm.rename(a_, b_)
                            elif op["op"] == "delete":
                                fm.delete(a_)
                            elif op["op"] == "create_dir":
                                fm.create_dir(a_)
                            elif op["op"] == "undo_last":
                                fm.undo_last()
                            stats["api_ok_" + op["op"]] += 1
                        except KeyboardInterrupt:
                            crashed_ki = True
                            break
                        except Exception as e:  # noqa: BLE001
                            stats["api_exc_" + type(e).__name__] += 1
                if fc_api.fired:
                    fault_seen[0] = True
                    if task["fault"]["mode"] == "raise_after":
                        postfault = True
                ks.reset()
                check_global(f"after api task#{ti}")
                if crashed_ki:
                    break
                continue
            apr = make_approver(task["approver"], arng, ks)
            runner.approver = apr
            S_, W_ = str(S), str(ws_cur())
            plan = None
            captured = {}
            if task["kind"] == "plan":
                plan = [PlanStep(step_id=s["id"], action_type=s["a"], target=subst(s["t"], S_, W_),
                                 params={k: subst(v, S_, W_) for k, v in s["p"].items()}) for s in task["steps"]]
                captured["plan"] = plan
                text = "fuzz plan"
            else:
                text = subst(task["text"], S_, W_)
                orig_make = runner.make_plan

                def spy(obj, _o=orig_make, _c=captured):
                    p = _o(obj)
                    stats["nl_planned"] += 1
                    _c["plan"] = list(p)
                    return p
                runner.make_plan = spy
            events = []
            state = {"B": None, "oks": 0, "kill_at": None}
            fault = task["fault"]

            def progress(ev, _s=state, _c=captured, _e=events, _t=task, _ti=ti):
                if ev["event"] == "step_start":
                    if _t.get("swap"):
                        _s["sc"] = _s.get("sc", 0) + 1
                        if _s["sc"] == _t["swap"]["k"]:
                            st_ = next((x for x in _c.get("plan", []) if x.step_id == ev["step_id"]), None)
                            if st_ is not None:
                                comps = []
                                for pth in (st_.target, st_.params.get("dest")):
                                    if isinstance(pth, str):
                                        first = pth.replace("\\", "/").split("/")[0]
                                        if first and first not in (".", "..") and safe_name(first) and first not in comps:
                                            comps.append(first)
                                for first in comps:
                                    P = str(ws_cur() / first)
                                    if os.path.lexists(P) and not os.path.islink(P):
                                        try:
                                            REAL_RENAME(P, P + ".swapped")
                                            tgt = {"dir": str(S), "file": str(S / "s1.txt"), "rel": "../sentinel"}[_t["swap"]["to"]]
                                            os.symlink(tgt, P)
                                            swapped[0] = True
                                            stats["swap_done"] += 1
                                        except OSError:
                                            pass
                                        break
                    _s["B"] = snap(ws_cur(), True)
                elif ev["event"] == "step_done":
                    A = snap(ws_cur(), True)
                    step = next((x for x in _c.get("plan", []) if x.step_id == ev["step_id"]), None)
                    if step is not None and _s["B"] is not None and not swapped[0]:
                        skipped = str(ev.get("detail", "")).startswith("تخطّي")   # بقرارك، أو العنصر تغيّر بعد التخطيط
                        pr = oracle(str(ws_cur()), step, _s["B"], A, skipped)
                        stats["oracle_" + ("skip" if skipped else step.action_type) + ("_ok" if ev["ok"] else "_fail")] += 1
                        if ev["ok"] and pr:
                            viol("I4", f"verifier accepted a wrong effect: {step.action_type}: {pr[0][:110]}",
                                 f"task#{_ti} step {step.step_id} {step.action_type} target={step.target!r} params={step.params}\n  " + "\n  ".join(pr))
                        if (not ev["ok"]) and not pr:
                            stats["verifier_false_negative"] += 1
                            _e.append(("FN", step.step_id))
                    if ev["ok"]:
                        _s["oks"] += 1
                    if fault and fault["target"] == "progress":
                        _s["kcount"] = _s.get("kcount", 0) + 1
                        if _s["kcount"] == fault["k"]:
                            ks.trigger("fuzz kill (progress)")
            bal0, spent0 = ledger.get_balance(), ledger.get_total_spent()
            # ledger faults
            orig_succ, orig_start = ledger.record_task_success, ledger.record_task_start
            if task["ledger_fault"] == "success":
                def boom(*a, **k):
                    raise sqlite3.OperationalError("database is locked")
                ledger.record_task_success = boom
            elif task["ledger_fault"] == "start":
                def boom2(*a, **k):
                    raise sqlite3.OperationalError("database is locked")
                ledger.record_task_start = boom2
            pre_total = snap(ws_cur(), True)
            rep = None
            exc = None
            fc = FaultCtx(fault if fault and fault["target"] not in ("progress", "approver") else None, ks)
            if fault and fault["target"] == "approver" and apr is not None:
                inner = apr
                cnt = {"n": 0}

                def apr2(step, _i=inner, _c=cnt, _f=fault):
                    _c["n"] += 1
                    if _c["n"] == _f["k"]:
                        ks.trigger("fuzz kill (approver)")
                    return _i(step)
                runner.approver = apr2
            if task.get("timeout_steps") is not None:
                tick = [0]

                def clk(_t=tick):
                    _t[0] += 1
                    return _t[0]
                runner._clock, runner.max_seconds = clk, task["timeout_steps"] + 0.5
            try:
                with fc:
                    rep = runner.run(tid, text, plan=plan, progress=progress)
            except BaseException as e:  # noqa: BLE001
                exc = e
            finally:
                runner._clock, runner.max_seconds = time.monotonic, 1800.0
                ledger.record_task_success, ledger.record_task_start = orig_succ, orig_start
                if task["kind"] == "nl":
                    runner.make_plan = orig_make
            post_total = snap(ws_cur(), True)
            nopart = lambda d: {k: v for k, v in d.items() if not k.startswith(".trash/partial_")}  # noqa: E731
            changed = nopart(pre_total) != nopart(post_total)
            if fc.fired:
                fault_seen[0] = True
                stats["fault_fired_" + fault["target"] + "_" + fault["mode"]] += 1
                if fault["mode"] == "raise_after":
                    postfault = True
            if exc is not None:
                if isinstance(exc, KeyboardInterrupt):
                    crashed_ki = True
                    stats["run_KI"] += 1
                    if tid not in ran_ids:
                        ran_ids.append(tid)   # مهمة قُطعت: التطبيق يعرف معرّفها (last_journaled_task) فيتراجع المستخدم عنها كأي مهمة
                else:
                    viol("I4", f"TaskRunner.run raised {type(exc).__name__}: {str(exc)[:100]}", "".join(traceback.format_exception(exc))[-800:])
            if rep is not None:
                tid_new = tid not in ran_ids
                if tid_new:
                    ran_ids.append(tid)
                stats["runs"] += 1
                stats["run_success" if rep.success else "run_failed"] += 1
                if not rep.success:
                    stats["fail_" + re.sub(r"[\d'\"].*", "", rep.failures[0] if rep.failures else "?")[:34]] += 1
                if changed:
                    stats["runs_mutating"] += 1
                if keep is not None:
                    keep["reports"].append({"task": ti, "tid": tid, "success": rep.success, "failures": rep.failures, "charged": rep.points_charged,
                                            "undo": rep.undo_available, "executed": rep.executed_steps, "skipped": rep.skipped,
                                            "request": text if task["kind"] == "nl" else None,
                                            "plan": [(s.step_id, s.action_type, s.target, s.params) for s in captured.get("plan", [])]})
                # I4 report honesty
                if rep.success:
                    if rep.failures:
                        viol("I4", "success=True but failures non-empty")
                    if rep.points_charged < 1:
                        viol("I4", f"success=True with points_charged={rep.points_charged}")
                    plan_l = captured.get("plan", [])
                    if state["oks"] != len(plan_l):
                        viol("I4", f"success=True but only {state['oks']}/{len(plan_l)} steps reported done")
                    if not plan_l:
                        viol("I4", "success=True with empty plan")
                else:
                    if not rep.failures:
                        viol("I4", "success=False but failures list empty")
                    if rep.points_charged != 0:
                        viol("I4", f"success=False but points_charged={rep.points_charged}")
                if (task["kind"] == "nl" and captured.get("plan") and not rep.success and not fc.fired and task["approver"] == "yes"
                        and not task["ledger_fault"] and task.get("timeout_steps") is None and not ks.is_triggered and not (fault and fault["target"] in ("progress", "approver", "verify"))
                        and not rep.failures[0].startswith(("تم الإيقاف", "خطة غير صالحة", "تعذّر", "لم تبدأ", "مسار مرفوض", "الرصيد", "عدد خطوات", "تجاوز"))):
                    viol("R1", f"RuleProvider plan for NL request failed at execution: {rep.failures[0][:90]}",
                         f"request={text!r}\nplan={[(x.step_id, x.action_type, x.target, x.params) for x in captured['plan']]}\nfailure={rep.failures[0][:300]}")
                if changed and not rep.undo_available and not swapped[0] and not (fc.fired and fault["mode"] == "raise_after"):
                    viol("I4", "disk changed but report.undo_available is False",
                         f"task#{ti} success={rep.success} failures={rep.failures[:2]}\n" + diff_trees(pre_total, post_total))
                # I5 ledger
                bal1, spent1 = ledger.get_balance(), ledger.get_total_spent()
                if bal1 < 0 or spent1 < 0:
                    viol("I5", f"negative ledger balance={bal1} spent={spent1}")
                if rep.success:
                    if not (bal0 - bal1 == rep.points_charged == spent1 - spent0):
                        viol("I5", f"success charge mismatch: balance delta {bal0 - bal1}, reported {rep.points_charged}, spent delta {spent1 - spent0}")
                else:
                    if bal1 != bal0 or spent1 != spent0:
                        viol("I5", f"charged on failure: balance {bal0}->{bal1}, spent {spent0}->{spent1}", "; ".join(rep.failures[:2]))
                if spent1 > ledger.spending_limit:
                    viol("I5", f"total_spent {spent1} exceeds spending_limit {ledger.spending_limit}")
                with ledger._get_connection() as conn:
                    stuck = conn.execute("SELECT task_id FROM transactions WHERE status='STARTED'").fetchall()
                if stuck and exc is None:
                    viol("I5", f"{len(stuck)} ledger row(s) stuck in STARTED after run returned", str([r['task_id'] for r in stuck][:3]))
            check_global(f"after task#{ti}")
            if not fault_seen[0] and exc is None and not swapped[0]:
                check_journal_sync(f"after task#{ti}", runner)
            if crashed_ki:
                break

        # ---------------- undo phase ----------------
        u = spec["undo"]
        ks.reset()
        # external changes
        ext_paths = []
        ext_kind = None if swapped[0] else u["ext"]
        cur_ws["p"] = ws
        if u["rename_ws"]:
            new_ws = base / "ws_renamed"
            try:
                REAL_RENAME(ws, new_ws)
                cur_ws["p"] = new_ws
            except OSError:
                pass
        W = cur_ws["p"]
        pre_undo = snap(W)
        if ext_kind:
            try:
                if ext_kind == "in_created":
                    created = []
                    try:
                        jl = json.loads((W / ".undo_journal.json").read_text(encoding="utf-8"))
                        for je in jl:
                            if je.get("action") == "create_dir" and str(je.get("path", "")).startswith("rel:"):
                                rp = je["path"][4:]
                                if pre_undo.get(rp) == "d" and rp not in init_snap:
                                    created.append(rp)
                    except Exception:
                        pass
                    created = sorted(created)
                    if created:
                        pth = pjoin(created[0], "ext_in.bin")
                        (W / pth).write_bytes(b"EXT-IN-CREATED")
                        ext_paths.append(pth)
                    else:
                        ext_kind = "root"
                if ext_kind == "root":
                    (W / "ext_new.bin").write_bytes(b"EXT-ROOT")
                    ext_paths.append("ext_new.bin")
                elif ext_kind == "collide":
                    gone = sorted(k for k, v in init_snap.items() if isinstance(v, tuple) and v[0] == "f" and k not in pre_undo and len(pbase(k)) < 200)
                    if gone:
                        (W / gone[0]).parent.mkdir(parents=True, exist_ok=True)
                        (W / gone[0]).write_bytes(b"EXT-COLLIDE")
                        ext_paths.append(gone[0])
                    else:
                        ext_kind = None
            except OSError:
                ext_kind = None
        pre_ext = snap(W)

        def new_runner():
            return TaskRunner(W, RuleProvider(), ledger, kill_switch=ks, approver=lambda s: True)

        def do_undo(runner, method):
            n = 0
            try:
                if method == "task":
                    for tid in reversed(ran_ids):
                        n += runner.undo_task(tid)
                else:
                    n += runner.file_manager.undo_all()
            except BaseException as e:  # noqa: BLE001
                viol("I2", f"undo raised {type(e).__name__}: {str(e)[:100]}", "".join(traceback.format_exception(e))[-600:])
            return n

        runner_u = ctx.runner if not (u["restart"] or u["rename_ws"]) else new_runner()
        if u["rename_ws"]:
            stats["undo_after_rename"] += 1
        if u["restart"]:
            stats["undo_after_restart"] += 1
        if u["fault"]:
            fcu = FaultCtx(u["fault"], ks)
            with fcu:
                do_undo(runner_u, u["method"])
            if fcu.fired:
                stats["undo_fault_fired"] += 1
            ks.reset()
            check_global("after faulted undo", expect_files=True)
            runner_u = new_runner() if u["retry_restart"] else runner_u
            for _ in range(2):
                do_undo(runner_u, u["method"])
        else:
            do_undo(runner_u, u["method"])
        stats["undo_" + u["method"]] += 1
        if not u["fault"] and not crashed_ki and not postfault and not swapped[0] and not fault_seen[0]:
            check_journal_sync("after undo", runner_u)
        # one more pass that must be a no-op or finish what failed (idempotence)
        final = snap(W)
        expect = dict(init_snap)
        if ext_kind in ("root", "in_created"):
            if ext_kind == "root":
                expect["ext_new.bin"] = ("f", hashlib.sha256(b"EXT-ROOT").hexdigest())
            else:
                pass  # file moved with its dir to trash (or dir kept if non-created)
        compare_final = ext_kind != "collide"
        if ext_kind == "in_created":
            extp = ext_paths[0]
            if extp in final:  # dir wasn't removed: it must be as the plan left it => then expectation can't be 'init'
                expect[extp] = ("f", hashlib.sha256(b"EXT-IN-CREATED").hexdigest())
                for anc in {extp.rsplit("/", i)[0] for i in range(1, extp.count("/") + 1)}:
                    expect[anc] = "d"
        if crashed_ki or postfault:
            # KeyboardInterrupt / post-effect-fault: an op may be unjournaled; I2 is best-effort => separate invariant tag
            inv2 = "I2x"
            fm_ = [t["fault"]["mode"] + "@" + t["fault"]["target"] for t in spec["tasks"] if t["fault"]]
            inv2_tag = "KI" if crashed_ki else "raise_after"
        else:
            inv2 = "I2"
            inv2_tag = "DUPID" if any(t["dup_id"] for t in spec["tasks"]) else ""
        if swapped[0]:
            stats["i2_skipped_swap"] += 1
        elif compare_final:
            if final != expect:
                viol(inv2, f"{inv2_tag} undo did not restore initial tree (method={u['method']} restart={u['restart']} rename={u['rename_ws']} ext={ext_kind})",
                     diff_trees(expect, final))
        else:
            # collide: names may differ, but contents must all be present outside trash
            nt = collections.Counter(v[1] for v in final.values() if isinstance(v, tuple) and v[0] == "f")
            for h, c in collections.Counter(v[1] for v in init_snap.values() if isinstance(v, tuple) and v[0] == "f").items():
                if nt.get(h, 0) < c:
                    viol(inv2, "undo(ext_collide): an initial content is not back in the workspace tree", diff_trees(init_snap, final))
                    break
            if file_multiset(W).get(hashlib.sha256(b"EXT-COLLIDE").hexdigest(), 0) < 1:
                viol("I1", "external file destroyed by undo", ext_paths[0])
            elif final.get(ext_paths[0]) != ("f", hashlib.sha256(b"EXT-COLLIDE").hexdigest()):
                stats["ext_collide_trashed_by_undo"] += 1
        if ext_kind == "in_created":
            now = file_multiset(W)
            if now.get(hashlib.sha256(b"EXT-IN-CREATED").hexdigest(), 0) < 1:
                viol("I1", "external file inside a created dir vanished on undo")
        if ext_kind == "root" and final.get("ext_new.bin") is None:
            viol("I1", "external root file vanished on undo")
        check_global("after undo")
        if keep is not None:
            keep.update(final_snap=final, pre_undo=pre_undo, undo=u, ext=ext_kind, expect=expect, init_total=init_total,
                        post_total=snap(W, True), stats=dict(stats))
    except BaseException as e:  # noqa: BLE001
        V.append(Violation(seed, "FUZZER_ERROR", f"{type(e).__name__}: {str(e)[:100]}", traceback.format_exc()[-1500:]))
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(ldir, ignore_errors=True)
    return V, stats, spec


def fs_is_case_sensitive():
    """يجرّب مجلد الاختبار الفعلي: هل a وA اسمان مختلفان؟ (ويندوز وmacOS الافتراضي: لا)."""
    d = tempfile.mkdtemp(prefix="fuzz_case_", dir=TMPROOT)
    try:
        Path(d, "probe_a").write_bytes(b"1")
        return not Path(d, "PROBE_A").exists()
    except OSError:
        return os.name != "nt"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _win_ok(n):
    """اسم صالح كمكوّن مسار على ويندوز: بلا محارف محجوزة/نقطة أو مسافة أخيرة/اسم جهاز/بايت يتيم، وطول معقول (MAX_PATH)."""
    if len(n) > 80 or any(ch in n for ch in '<>:"|?*\\') or any(0xD800 <= ord(ch) <= 0xDFFF for ch in n):
        return False
    return safe_name(n)


def apply_fs_safe():
    """يصفّي مجموعات الأسماء في مكانها: لا أسماء تتصادم بفرق الحالة ولا أسماء غير صالحة على ويندوز، ولا إعادة تسمية بالحالة فقط.
    أسماء الجذور الأخرى (HOSTILE وسلاسل التخطيط العدائي) تبقى: رفضها اختبار مشروع على أي نظام."""
    FS_SAFE["on"] = True
    seen = set()
    for lst in (DIR_POOL, SAFE_FILES, NORMAL_FILES):
        out = []
        for n in lst:
            key = n.lower()
            if not all(_win_ok(part) for part in n.split("/")) or key in seen:
                continue
            seen.add(key)
            out.append(n)
        lst[:] = out
    # أسماء "غير آمنة" تبقى (المحجوزة تُرفض عند البناء فيُتخطى إنشاؤها)، لكن بلا تصادم حالة مع غيرها
    UNSAFE_FILES[:] = [n for n in UNSAFE_FILES if n.lower() not in seen and "\udcff" not in n]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6000)
    ap.add_argument("--seed0", type=int, default=1000)
    ap.add_argument("--time-limit", type=float, default=240.0)
    ap.add_argument("--show", type=int, default=None)
    ap.add_argument("--only", type=int, default=None)
    ap.add_argument("--examples", type=int, default=2)
    ap.add_argument("--json", default=None)
    ap.add_argument("--avoid-known", action="store_true", help="drop names that trigger already-reported bugs ('..' in name, leading space, lone surrogate)")
    ap.add_argument("--profile", default="all", choices=["all", "mixed", "nl"])
    ap.add_argument("--no-after-faults", action="store_true", help="لا أخطاء KeyboardInterrupt/raise_after (محاكاة غير واقعية)")
    ap.add_argument("--fs-safe", action="store_true", help="مجموعات أسماء لنظام ملفات غير حساس للحالة (تلقائي على ويندوز أو إذا كشف الفحص ذلك)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    if a.fs_safe or os.name == "nt" or not fs_is_case_sensitive():
        apply_fs_safe()
    NO_AFTER["v"] = a.no_after_faults
    PROFILE["mode"] = a.profile
    if a.avoid_known:
        bad = lambda n: ".." in n or n != n.strip() or "\udcff" in n  # noqa: E731
        for lst in (SAFE_FILES, UNSAFE_FILES, NORMAL_FILES, DIR_POOL):
            lst[:] = [n for n in lst if not bad(n)]

    if a.show is not None:
        keep = {}
        V, stats, spec = run_scenario(a.show, keep)
        print("=== SPEC ===")
        print(json.dumps(spec, ensure_ascii=False, indent=1)[:6000])
        print("=== INITIAL TREE ===")
        for k, v in sorted(keep.get("init_snap", {}).items()):
            print("  ", repr(k), fmt(v))
        for r in keep.get("reports", []):
            print("=== REPORT ===")
            print(json.dumps(r, ensure_ascii=False, indent=1, default=str)[:3000])
        print("=== TREE AFTER RUNS (before undo, .trash incl.) ===")
        for k, v in sorted(keep.get("post_total", {}).items()):
            print("  ", repr(k), fmt(v))
        print("=== FINAL TREE (after undo, no trash) ===")
        for k, v in sorted(keep.get("final_snap", {}).items()):
            print("  ", repr(k), fmt(v))
        print("=== undo:", keep.get("undo"), "ext:", keep.get("ext"))
        print("=== VIOLATIONS ===")
        for v in V:
            print(f"[{v.inv}] {v.msg}\n{v.detail}")
        return 0

    seeds = [a.only] if a.only is not None else [a.seed0 + i for i in range(a.n)]
    t0 = time.time()
    allv = []
    tot = collections.Counter()
    done = 0
    for sd in seeds:
        if time.time() - t0 > a.time_limit:
            print(f"!! time limit hit after {done} scenarios", file=sys.stderr)
            break
        V, st, spec = run_scenario(sd)
        done += 1
        tot.update(st)
        tot["scenarios"] += 1
        tot["scn_tasks_plan"] += sum(1 for t in spec["tasks"] if t["kind"] == "plan")
        tot["scn_tasks_nl"] += sum(1 for t in spec["tasks"] if t["kind"] == "nl")
        tot["scn_with_fault"] += any(t["fault"] for t in spec["tasks"])
        tot["scn_with_symlinks"] += any(e["t"] == "l" for e in spec["entries"])
        allv.extend(V)
        if a.v and V:
            print(f"seed {sd}: {[v.inv for v in V]}")
    el = time.time() - t0
    groups = collections.OrderedDict()
    for v in allv:
        groups.setdefault(v.sig(), []).append(v)
    print(f"\nscenarios run: {done} in {el:.1f}s   (repo: {REPO})")
    fails = sorted(((v, k) for k, v in tot.items() if k.startswith("fail_")), reverse=True)
    for k, v in sorted(tot.items()):
        if not k.startswith("fail_"):
            print(f"  {k}: {v}")
    print("  top failure reasons (expected: random plans are often invalid):")
    for v, k in fails[: (len(fails) if a.v else 8)]:
        print(f"    {v:5d} {k[5:]}")
    print(f"\ndistinct violation signatures: {len(groups)}   total violations: {len(allv)}")
    for sig, vs in sorted(groups.items(), key=lambda kv: (kv[1][0].inv, -len(kv[1]))):
        print(f"\n[{vs[0].inv}] x{len(vs)}  {vs[0].msg}")
        print("   seeds:", [v.seed for v in vs[:12]])
        for v in vs[: a.examples]:
            print(f"   --- seed {v.seed}")
            for ln in (v.detail or "").splitlines()[:14]:
                print("     " + ln)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as jf:
            json.dump([{"seed": v.seed, "inv": v.inv, "msg": v.msg, "detail": v.detail} for v in allv], jf, ensure_ascii=False, indent=1)
    return 1 if allv else 0


if __name__ == "__main__":
    sys.exit(main())
