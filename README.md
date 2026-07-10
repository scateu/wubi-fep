DEMO: <https://asciinema.org/a/ybRjkiDkExr9qIUA>


<img width="1914" height="1130" alt="image" src="https://github.com/user-attachments/assets/774bd9ba-fff7-4d1a-9385-437402f4e514" />

# wubi-ime

A Chinese input-method **front-end processor (FEP)** for the terminal, in the
spirit of [`uim-fep`](https://github.com/uim/uim) and DOS-era UCDOS. It wraps
your shell in a pty and sits transparently in the byte stream, so it works
inside a **tmux** pane (or any terminal) without needing a system IME.

A status bar on the terminal's bottom line is **always shown** and displays the
active mode. The bar owns that row exclusively: the wrapped program is confined
to the rows above it (via a DECSTBM scroll region and a one-row-shorter reported
size), so a regular terminal simply has **one line less** and the bar never
flickers or fights the program's output. **Ctrl-@** (a.k.a. Ctrl-Space) cycles
the mode:

```
[En]  English   bytes pass straight through (default)
[拼]  Pinyin     type a syllable, pick from paged candidates
[五]  Wubi 86    type a 1-4 letter code, pick a hanzi/phrase
```

**Ctrl-\\** is a quick toggle between `En` and the last Chinese mode you selected
(so once you've cycled to 五, Ctrl-\\ flips En↔五 without walking through 拼).

When a Chinese mode is active the chosen hanzi are sent to the underlying program
as if you had typed them; the bar shows the code and candidates:

```
[五] wq  1你           <- exact wubi code "wq" -> 你
[拼] ni  1你 2拟 3尼 4呢 5泥 6妳 7妮 8腻 9妞  (1/8)
```

## Build

```sh
make            # build ./wubi-ime (generates + embeds the tables automatically)
```

`make` regenerates the `.tab` tables from the committed `*.dict.yaml` sources,
**embeds them into the binary** (`tables_embed.c`), and links everything, so the
resulting `wubi-ime` is a **single self-contained file** — copy it anywhere and
run it, no external table files needed. (`make tables` regenerates just the
`.tab` files if you want them on disk.)

The `.tab` tables, `tables_embed.c`, and build artifacts are all git-ignored
(derived from the committed sources).

Requirements: a C11 compiler and Python 3 (stdlib only). `forkpty` is used from
`<util.h>` (macOS/BSD) or `<pty.h>` + `-lutil` (Linux); the Makefile picks the
right link flag from `uname`.

## Run

Standalone:

```sh
./wubi-ime            # wraps $SHELL (default /bin/sh)
./wubi-ime -a         # + wubi auto-commit on a unique 4-letter code
```

Options:

| Flag | Effect |
|------|--------|
| `-a`, `--auto-commit` | Wubi: auto-commit a full **4-letter** code that has a single exact match, so common words need no `Space`. **Off by default** (a 4-letter code otherwise waits for you to pick with `Space`/a digit). |
| `-h`, `--help` | Show usage. |

Inside tmux — launch it as the pane command:

```sh
tmux new '/path/to/wubi-ime'
```

or bind a key to open a pane running it:

```tmux
# ~/.tmux.conf
bind C-i split-window '/path/to/wubi-ime'
```

The tables are **compiled into the binary**, so nothing else is needed at
runtime. An external `.tab` still takes precedence *when present*, letting you
override a table without rebuilding: wubi-ime looks for `wubi.tab` / `pinyin.tab`
in `$WUBI_IME_DIR`, then the executable's directory, then the current directory,
and falls back to the embedded copy if none is found.

```sh
export WUBI_IME_DIR=/path/to/custom/tables   # optional override
```

## Keys

| Key | Action |
|-----|--------|
| **Ctrl-@** / **Ctrl-Space** | cycle `En → 拼 → 五 → En` (remembers the last Chinese mode) |
| **Ctrl-\\** | quick toggle `En ↔ last Chinese mode` (defaults to 五 until one is chosen) |
| `a`–`z` | append to the current code |
| `1`–`9` | commit the Nth candidate on the page |
| `Space` | commit the first candidate |
| `Enter` | commit the first candidate (or pass through if no code) |
| `Backspace` | delete the last code letter |
| `=` / `.` | next candidate page |
| `-` / `,` | previous candidate page |
| `Esc` | cancel the pending code; the Esc (and any following escape sequence) passes through |
| any other punctuation | commits the first candidate, then the punctuation is sent |
| Ctrl / Meta / arrow / F-keys | always passed through to the child (see below) |

Wubi convenience (opt-in with `-a`): a full **4-letter** code with a single
exact match auto-commits, so common words need no `Space`. Without `-a` (the
default), a 4-letter code behaves like any other — pick with `Space` or a digit.

### Editing keys pass through

While a Chinese mode is active, control keys and escape sequences are **not**
intercepted — they go straight to the child so shell/readline editing keeps
working exactly as normal:

- Ctrl chords: `C-a C-e C-b C-f C-n C-p C-k C-u C-w C-d`, `Ctrl-C`, `Ctrl-Z`, …
- Meta/Alt chords: `M-b M-f M-d M-Backspace`, … (sent as `ESC` + key)
- Cursor/function keys: arrows, Home/End, PageUp/Down, F-keys (CSI / SS3)

If you press one of these mid-composition, the pending code is discarded first
and then the key is forwarded. Only the plain `a`–`z`, digit, `Space`, `Enter`,
`Backspace`, and paging keys listed above are consumed by the IME.

(A lone `Esc` press cancels the pending code; an `Esc` that is the start of a
multi-byte key chord is recognised as such and forwarded whole.)

## Files

| File | Purpose |
|------|---------|
| `wubi-ime.c` | the FEP: pty wrapper, bottom-line reservation, mode toggle, candidate bar, commit logic |
| `table.c` / `table.h` | table lookup: `mmap` a `.tab` **or** bind an in-memory image; prefix-window binary search |
| `tables_embed.h` | interface to the tables compiled into the binary |
| `gen_table.py` | rime `.dict.yaml` → compact `.tab` binary (see file header for format) |
| `gen_embed.py` | `.tab` files → `tables_embed.c` (byte arrays baked into the binary) |
| `wubi86.dict.yaml` | rime Wubi 86 source dictionary (input to `gen_table.py`) |
| `pinyin_simp.dict.yaml` | rime simplified-Pinyin source dictionary |

### Regenerating / trimming tables

```sh
# keep every entry (default):
python3 gen_table.py --scheme wubi   --src wubi86.dict.yaml      --out wubi.tab
python3 gen_table.py --scheme pinyin --src pinyin_simp.dict.yaml --out pinyin.tab

# smaller tables:
python3 gen_table.py --scheme wubi --src wubi86.dict.yaml --out wubi.tab \
    --max-phrases 30000        # cap multi-hanzi phrases (single hanzi kept)
python3 gen_table.py --scheme pinyin --src pinyin_simp.dict.yaml --out pinyin.tab \
    --top 8000                 # keep only the 8000 most common hanzi
```

## Known limitations (v1)

- The candidate bar uses the terminal's **bottom line** (like `uim-fep`), which
  is reserved via a scroll region so the wrapped program can't draw on it. This
  relies on the terminal honouring DECSTBM (`ESC[t;br`) and reporting size via
  `TIOCGWINSZ` — true of xterm, tmux, iTerm2, and the like. Inline (at-cursor)
  candidate rendering is not implemented.
- Shuangpin (Xiaohe) is supported by the generator lineage but not wired into the
  FEP's mode toggle.
- Key bindings are compile-time constants (no config file yet).
- Candidate selection is by frequency rank from the source dictionaries; there is
  no learning / user-frequency adaptation.
