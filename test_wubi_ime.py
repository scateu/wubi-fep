#!/usr/bin/env python3
"""
Test suite for wubi-ime.

Two layers:
  1. Table tests  - decode wubi.tab / pinyin.tab in Python and check that the
                    binary .tab format, prefix index, and ranking are sane.
  2. FEP tests    - drive the built ./wubi-ime through a pty, feeding keystrokes
                    and observing the candidate bar and what it commits to the
                    child, plus the bottom-line scroll region and --scheme.

Run via `make test`, or directly:  python3 test_wubi_ime.py
Exit status is nonzero if any check fails.

The FEP tests use two kinds of child:
  * /bin/cat            - echoes committed hanzi back (cooked pty); good for
                          observing commits and the bar (which we write to the
                          master ourselves).
  * `stty raw -echo; cat` inside /bin/sh - a RAW echo child, so forwarded
                          control bytes (readline chords) come back verbatim.
"""
import os
import pty
import re
import select
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "wubi-ime")

# --------------------------------------------------------------------------- #
# tiny test framework
# --------------------------------------------------------------------------- #
_fail = 0
_pass = 0


def check(name, cond):
    global _fail, _pass
    if cond:
        _pass += 1
        print(f"  PASS {name}")
    else:
        _fail += 1
        print(f"  FAIL {name}")


def section(title):
    print(f"\n== {title} ==")


# --------------------------------------------------------------------------- #
# .tab decoder (mirror of table.c / gen_table.py)
# --------------------------------------------------------------------------- #
CODE_LEN = 16
HDR = 16
IDX = 677 * 4
REC = CODE_LEN + 6  # code[CODE_LEN] + off(u32) + wordLen(u8) + rank(u8)


def load_tab(path):
    b = open(path, "rb").read()
    assert b[:4] == b"IMET", f"{path}: bad magic {b[:4]!r}"
    ver = b[4]
    scheme = b[5]
    code_len = struct.unpack_from("<H", b, 6)[0]
    count, pool_bytes = struct.unpack_from("<II", b, 8)
    return dict(b=b, ver=ver, scheme=scheme, code_len=code_len,
                count=count, pool_bytes=pool_bytes)


def tab_lookup(t, query):
    """Return [(code, word, rank, exact), ...] for records whose code has
    `query` as a prefix, exact matches first (by rank) then extensions."""
    b = t["b"]
    count = t["count"]
    code_len = t["code_len"]
    rec_base = HDR + IDX
    pool_base = rec_base + count * (code_len + 6)
    c0 = ord(query[0]) - 97
    c1 = (ord(query[1]) - 97) if len(query) >= 2 else 0
    index = struct.unpack_from("<677I", b, HDR)
    if len(query) == 1:
        lo = index[c0 * 26]
        hi = index[(c0 + 1) * 26] if c0 + 1 <= 25 else count
    else:
        k = c0 * 26 + c1
        lo, hi = index[k], (index[k + 1] if k + 1 < 677 else count)
    exact, ext = [], []
    for i in range(lo, hi):
        off_rec = rec_base + i * (code_len + 6)
        code = b[off_rec:off_rec + code_len].rstrip(b"\x00").decode()
        off, wl, rk = struct.unpack_from("<IBB", b, off_rec + code_len)
        if not code.startswith(query):
            continue
        w = b[pool_base + off:pool_base + off + wl].decode("utf-8")
        (exact if code == query else ext).append((code, w, rk))
    return exact + ext


# --------------------------------------------------------------------------- #
# pty harness
# --------------------------------------------------------------------------- #
class Fep:
    def __init__(self, args=None, shell="/bin/cat", env=None):
        e = dict(os.environ)
        e["SHELL"] = shell
        e.pop("WUBI_IME_DIR", None)
        if env:
            e.update(env)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(BIN, [BIN] + (args or []), e)
            os._exit(127)
        time.sleep(0.35)
        self.startup = self.drain_s(0.3)  # captured startup output (incl. bar)

    def drain(self, t=0.3):
        """Raw bytes read from the master within `t` seconds."""
        buf = b""
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    d = os.read(self.fd, 65536)
                except OSError:
                    break
                if not d:
                    break
                buf += d
        return buf

    def drain_s(self, t=0.3):
        """Same as drain() but decoded to str (for tag/candidate checks)."""
        return self.drain(t).decode("utf-8", "replace")

    def send(self, data, settle=0.12):
        os.write(self.fd, data)
        time.sleep(settle)

    def typ(self, s):
        for ch in s:
            self.send(ch.encode())

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass


def last_tag(s):
    tags = re.findall(r"\[(?:En|五|拼)\]", s)
    return tags[-1] if tags else "?"


# --------------------------------------------------------------------------- #
# 1. table tests
# --------------------------------------------------------------------------- #
def test_tables():
    section("table format & lookup")
    for name, path, scheme in [("wubi", "wubi.tab", 0),
                               ("pinyin", "pinyin.tab", 1)]:
        p = os.path.join(HERE, path)
        if not os.path.exists(p):
            check(f"{path} exists (run `make tables`)", False)
            continue
        t = load_tab(p)
        check(f"{path}: magic+scheme", t["scheme"] == scheme)
        check(f"{path}: version 2", t["ver"] == 2)
        check(f"{path}: codeLen 16", t["code_len"] == 16)
        check(f"{path}: has records", t["count"] > 0)

    w = load_tab(os.path.join(HERE, "wubi.tab"))
    py = load_tab(os.path.join(HERE, "pinyin.tab"))

    # wubi: wq -> 你 (exact, rank 0)
    r = tab_lookup(w, "wq")
    check("wubi wq -> 你 first & exact", r and r[0][1] == "你" and r[0][0] == "wq")
    # wubi: q -> 我 (single-letter code window)
    r = tab_lookup(w, "q")
    check("wubi q -> 我 first", r and r[0][1] == "我")

    # pinyin single: ni -> 你 first
    r = tab_lookup(py, "ni")
    check("pinyin ni -> 你 first", r and r[0][1] == "你")
    # pinyin exact ranking is ascending
    exact = [x for x in r if x[0] == "ni"]
    check("pinyin ni exact ranks ascending",
          [x[2] for x in exact] == sorted(x[2] for x in exact))
    # pinyin phrase: nihao -> 你好 present as an exact match
    r = tab_lookup(py, "nihao")
    check("pinyin phrase nihao -> 你好 exact",
          any(w2 == "你好" and code == "nihao" for code, w2, _ in r))
    # pinyin long phrase code (>6) exists -> proves 16-wide codes work
    r = tab_lookup(py, "zhongguo")
    check("pinyin phrase zhongguo -> 中国", any(w2 == "中国" for _, w2, _ in r))


# --------------------------------------------------------------------------- #
# 2. FEP tests
# --------------------------------------------------------------------------- #
def test_toggles():
    section("mode toggles")
    f = Fep()
    check("startup shows [En]", "[En]" in f.startup)
    f.send(b"\x00"); check("Ctrl-@ #1 -> [拼]", last_tag(f.drain_s()) == "[拼]")
    f.send(b"\x00"); check("Ctrl-@ #2 -> [五]", last_tag(f.drain_s()) == "[五]")
    f.send(b"\x00"); check("Ctrl-@ #3 -> [En]", last_tag(f.drain_s()) == "[En]")
    # cycle to 五 then Ctrl-\ toggles En<->五
    f.send(b"\x00"); f.send(b"\x00"); check("cycle to [五]", last_tag(f.drain_s()) == "[五]")
    f.send(b"\x1c"); check("Ctrl-\\ -> [En]", last_tag(f.drain_s()) == "[En]")
    f.send(b"\x1c"); check("Ctrl-\\ -> [五] (last CN)", last_tag(f.drain_s()) == "[五]")
    f.close()


def test_wubi():
    section("wubi input")
    f = Fep()
    f.send(b"\x00"); f.send(b"\x00")   # -> 五
    f.typ("wq")
    check("wq shows 你 in bar", "你" in f.drain_s())
    f.send(b" ")
    check("space commits 你", "你" in f.drain_s())
    f.close()


def test_wubi_autocommit():
    section("wubi 4-letter auto-commit (-a)")

    def four_key_then_space(args):
        f = Fep(args=args, shell="/bin/sh")
        f.drain(0.3)
        f.send(b"stty raw -echo; cat\n", settle=0.4); f.drain(0.4)
        f.send(b"\x00"); f.send(b"\x00")  # -> 五
        f.typ("aaa"); f.drain()
        f.send(b"d"); f.drain()           # 4th key
        f.send(b" ")                      # space
        after = f.drain_s()
        f.close()
        # 工期 present after the space => code was still buffered (NOT auto).
        return "工期" in after

    check("default: 4-key NOT auto (space commits it)", four_key_then_space([]))
    check("-a: 4-key auto-commits (space is a no-op)",
          not four_key_then_space(["-a"]))


def test_pinyin():
    section("pinyin input (single + phrase)")
    f = Fep()
    f.send(b"\x00")   # -> 拼
    f.typ("ni")
    check("ni shows 你", "你" in f.drain_s())
    # digit selection: 3rd candidate commits something
    f.send(b"\x1b"); f.drain()
    f.typ("ni")
    f.drain()
    f.send(b"3")
    check("digit 3 commits a candidate", len(f.drain_s()) > 0)
    # clear and try a phrase
    f.send(b"\x1b"); f.drain()
    f.typ("nihao")
    check("nihao shows 你好", "你好" in f.drain_s())
    f.send(b" ")
    check("space commits 你好", "你好" in f.drain_s())
    # a long (>6 char) phrase code
    f.typ("zhongguo")
    check("zhongguo shows 中国", "中国" in f.drain_s())
    f.send(b" ")
    check("space commits 中国", "中国" in f.drain_s())
    f.close()


def test_readline_passthrough():
    section("readline / editor key pass-through in CN mode")
    f = Fep(shell="/bin/sh")
    f.drain(0.3)
    f.send(b"stty raw -echo; cat\n", settle=0.4); f.drain(0.4)
    f.send(b"\x00")   # -> 拼 (a CN mode)

    def probe(name, raw):
        os.write(f.fd, raw); time.sleep(0.12)
        check(name, raw in f.drain())

    probe("C-n (0x0e) passes through", b"\x0e")
    probe("C-a (0x01) passes through", b"\x01")
    probe("C-e (0x05) passes through", b"\x05")
    probe("C-k (0x0b) passes through", b"\x0b")
    probe("M-b (ESC b) passes through", b"\x1bb")
    probe("M-f (ESC f) passes through", b"\x1bf")
    probe("Up arrow (ESC [ A) passes through", b"\x1b[A")
    probe("Home (ESC O H) passes through", b"\x1bOH")
    f.close()


def test_scheme_switch():
    section("--scheme selection")

    def tags_seen(args):
        f = Fep(args=args)
        seen = set()
        for _ in range(3):
            f.send(b"\x00")
            for t in re.findall(r"\[(?:En|五|拼)\]", f.drain_s()):
                seen.add(t)
        f.close()
        return seen

    both = tags_seen(["-s", "both"])
    check("both: cycles 五 and 拼", "[五]" in both and "[拼]" in both)
    wubi = tags_seen(["-s", "wubi"])
    check("wubi-only: has [五], never [拼]", "[五]" in wubi and "[拼]" not in wubi)
    py = tags_seen(["-s", "pinyin"])
    check("pinyin-only: has [拼], never [五]", "[拼]" in py and "[五]" not in py)

    # bad value exits nonzero
    import subprocess
    rc = subprocess.run([BIN, "-s", "bogus"], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL).returncode
    check("bad --scheme exits nonzero", rc != 0)


def test_bottom_line():
    section("bottom-line reservation (scroll region + child size)")
    import fcntl
    import signal
    import termios
    ROWS, COLS = 24, 80
    f = Fep(shell="/bin/sh")
    fcntl.ioctl(f.fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    os.kill(f.pid, signal.SIGWINCH)
    time.sleep(0.3)
    out = f.drain(0.5)
    check("DECSTBM ESC[1;23r emitted", f"\033[1;{ROWS-1}r".encode() in out)
    check("bar drawn at real last row ESC[24;1H", f"\033[{ROWS};1H".encode() in out)
    f.send(b"stty size\n", settle=0.3)
    out2 = f.drain(0.5)
    check("child sees one row less (23 80)", b"23 80" in out2)
    f.close()


def test_self_contained():
    section("self-contained: embedded tables, no external .tab")
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="wubi_sc_")
    try:
        binc = os.path.join(d, "wubi-ime")
        shutil.copy(BIN, binc)
        e = dict(os.environ)
        e["SHELL"] = "/bin/cat"
        e.pop("WUBI_IME_DIR", None)
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(d)
            os.execve(binc, [binc], e)
            os._exit(127)
        time.sleep(0.4)

        def drain(t=0.3):
            buf = b""
            end = time.time() + t
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    try:
                        dd = os.read(fd, 65536)
                    except OSError:
                        break
                    if not dd:
                        break
                    buf += dd
            return buf.decode("utf-8", "replace")

        drain(0.3)
        os.write(fd, b"\x00"); time.sleep(0.12); drain()  # -> 拼
        for ch in "nihao":
            os.write(fd, ch.encode()); time.sleep(0.1)
        check("embedded pinyin phrase nihao -> 你好", "你好" in drain())
        os.close(fd)
        os.waitpid(pid, 0)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_gray_bar():
    section("status bar colour (UCDOS-style gray)")
    f = Fep()
    s = f.startup.encode("utf-8", "replace")
    check("bar painted light-gray bg (SGR 47)", b"47m" in s)
    check("mode chip on blue bg (SGR 44)", b"44m" in s)
    check("line filled with bg via ESC[K", b"\x1b[K" in s)
    f.close()


def test_bar_survives_clear():
    section("status bar survives a child clear/reset")
    # A child clear (ESC[2J) or margin reset (ESC[r) used to wash out the bar at
    # startup; the bar must be re-asserted + redrawn after any child output.
    import fcntl
    import struct
    import termios
    f = Fep(shell="/bin/sh")
    fcntl.ioctl(f.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    import signal
    os.kill(f.pid, signal.SIGWINCH)
    f.drain(0.4)
    f.send(b"clear\n", settle=0.4)
    s = f.drain(0.5)
    lc = max(s.rfind(b"\x1b[2J"), s.rfind(b"\x1b[3J"))
    tail = s[lc:] if lc >= 0 else s
    check("scroll region re-asserted after clear", b"\x1b[1;23r" in tail)
    check("gray bar [En] redrawn after clear",
          b"47m" in tail and "[En]".encode() in tail)
    f.close()


def test_no_escape_split():
    section("bar re-assert never splits a child escape (vim redraw)")
    import fcntl
    import shutil
    import signal
    import struct
    import termios
    vim = shutil.which("vim") or shutil.which("vi")
    if not vim:
        check("vim available (skipped)", True)
        return
    f = Fep(shell="/bin/sh", env={"TERM": "xterm-256color"})
    fcntl.ioctl(f.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    os.kill(f.pid, signal.SIGWINCH)
    f.drain(0.4)
    f.send(b"vim\n", settle=1.2)
    s = f.drain(1.0)
    # Every injected re-assert (ESC7 ESC[1;23r ESC8) must be preceded by a
    # COMPLETE child sequence, never a dangling "ESC[<params>" with no final.
    bad = 0
    for m in re.finditer(rb"\x1b7\x1b\[1;23r\x1b8", s):
        before = s[max(0, m.start() - 12):m.start()]
        if re.search(rb"\x1b\[[0-9;]*$", before):
            bad += 1
    check("no re-assert spliced mid-escape during vim redraw", bad == 0)
    f.send(b"\x1b:q!\n", settle=0.4)
    f.drain(0.3)
    f.close()


def test_esc_cancels_candidates():
    section("ESC cancels candidates (swallowed, not forwarded)")
    # With a pending composition, ESC must cancel it and NOT reach the child.
    f = Fep()
    f.send(b"\x00"); f.send(b"\x00")   # -> 五
    f.typ("wq")                        # candidates showing
    f.drain()
    f.send(b"\x1b")                    # ESC: cancel
    f.drain()
    # After cancel the buffer is empty: a fresh code composes from scratch.
    f.typ("wq")
    check("ESC cleared the pending code (fresh wq -> 你)", "你" in f.drain_s())
    f.close()

    # With an EMPTY buffer, ESC still passes through (editor keys keep working).
    f = Fep(shell="/bin/sh")
    f.drain(0.3)
    f.send(b"stty raw -echo; cat\n", settle=0.4); f.drain(0.4)
    f.send(b"\x00")                    # -> 拼, empty buffer
    os.write(f.fd, b"\x1b"); time.sleep(0.12)
    check("empty-buffer ESC passes through", b"\x1b" in f.drain())
    f.close()


def test_vim_mode():
    section("--vim: follow vim insert mode")
    import fcntl
    import signal
    import struct
    import termios
    # A fake 'vim' on a 24-row screen. On 'i' it prints '-- INSERT --' at the
    # BOTTOM child row (row 23 = 24-1, where vim's mode message lives); on ESC it
    # clears that row; on 'B' it prints the literal token in the BODY (row 5) to
    # simulate the text appearing as file content (must NOT trigger).
    fake = os.path.join(HERE, ".fakevim_test.py")
    with open(fake, "w") as fh:
        fh.write(
            "import os,tty\n"
            "try: tty.setraw(0)\n"
            "except Exception: pass\n"
            "os.write(1,b'\\x1b[1;1Hnormal')\n"
            "while True:\n"
            "    try: d=os.read(0,1)\n"
            "    except OSError: break\n"
            "    if not d: break\n"
            "    if d==b'i': os.write(1,b'\\x1b[23;1H-- INSERT --')\n"
            "    elif d==b'B': os.write(1,b'\\x1b[5;1H-- INSERT -- in body')\n"
            "    elif d==b'\\x1b': os.write(1,b'\\x1b[23;1H\\x1b[K')\n"
            "    else: os.write(1,d)\n")
    try:
        f = Fep(args=["--vim"], shell="/bin/sh")
        fcntl.ioctl(f.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.kill(f.pid, signal.SIGWINCH)
        f.drain(0.3)
        f.send(b"python3 " + fake.encode() + b"\n", settle=0.5)
        f.drain(0.4)
        f.send(b"i")                   # bottom-row -- INSERT --
        check("bottom-row '-- INSERT --' turns IME on",
              last_tag(f.drain_s()) in ("[五]", "[拼]"))
        f.send(b"\x1b")                # leave insert
        check("ESC (leaving insert) turns IME to [En]",
              last_tag(f.drain_s()) == "[En]")
        f.send(b"B")                   # token in the BODY, not the mode line
        tags = re.findall(r"\[(?:En|五|拼)\]", f.drain_s())
        check("body-text '-- INSERT --' does NOT switch (stays En)",
              all(t == "[En]" for t in tags))
        f.send(b"i")                   # bottom-row again
        check("bottom-row '-- INSERT --' again turns IME back on",
              last_tag(f.drain_s()) in ("[五]", "[拼]"))
        f.send(b"\x1b"); f.drain()
        f.close()

        # Without --vim, even a bottom-row needle must NOT switch modes.
        g = Fep()                      # cat child, default (no --vim)
        g.send(b"-- INSERT --")        # echoed by cat into the stream
        tags = re.findall(r"\[(?:En|五|拼)\]", g.drain_s())
        check("without --vim, '-- INSERT --' does not switch",
              all(t == "[En]" for t in tags))
        g.close()
    finally:
        try:
            os.remove(fake)
        except OSError:
            pass


def test_vim_preserve_mode():
    section("--vim: re-entering insert restores last-used Chinese mode")
    import fcntl
    import signal
    import struct
    import termios
    fake = os.path.join(HERE, ".fakevim_test.py")
    with open(fake, "w") as fh:
        fh.write(
            "import os,tty\n"
            "try: tty.setraw(0)\n"
            "except Exception: pass\n"
            "os.write(1,b'\\x1b[1;1Hnormal')\n"
            "while True:\n"
            "    try: d=os.read(0,1)\n"
            "    except OSError: break\n"
            "    if not d: break\n"
            "    if d==b'i': os.write(1,b'\\x1b[23;1H-- INSERT --')\n"
            "    elif d==b'\\x1b': os.write(1,b'\\x1b[23;1H\\x1b[K')\n"
            "    else: os.write(1,d)\n")
    try:
        f = Fep(args=["--vim"], shell="/bin/sh")
        fcntl.ioctl(f.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.kill(f.pid, signal.SIGWINCH)
        f.drain(0.3)
        f.send(b"python3 " + fake.encode() + b"\n", settle=0.5)
        f.drain(0.4)
        f.send(b"i"); f.drain()            # insert -> default 五
        f.send(b"\x00"); f.send(b"\x00")   # manual cycle 五->En->拼
        check("manual cycle set 拼", last_tag(f.drain_s()) == "[拼]")
        f.send(b"\x1b")                    # leave insert -> En
        check("ESC -> En", last_tag(f.drain_s()) == "[En]")
        f.send(b"i")                       # re-enter insert
        check("re-enter restores 拼 (last used, not default 五)",
              last_tag(f.drain_s()) == "[拼]")
        f.send(b"\x1b"); f.drain()
        f.close()
    finally:
        try:
            os.remove(fake)
        except OSError:
            pass


def main():
    if not os.path.exists(BIN):
        print("wubi-ime not built; run `make` first", file=sys.stderr)
        return 2

    test_tables()
    test_toggles()
    test_wubi()
    test_wubi_autocommit()
    test_pinyin()
    test_readline_passthrough()
    test_scheme_switch()
    test_bottom_line()
    test_self_contained()
    test_gray_bar()
    test_bar_survives_clear()
    test_no_escape_split()
    test_esc_cancels_candidates()
    test_vim_mode()
    test_vim_preserve_mode()

    print(f"\n{_pass} passed, {_fail} failed")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
