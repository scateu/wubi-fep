/*
 * wubi-ime - a Chinese input-method front-end processor (FEP), in the spirit of
 * uim-fep / UCDOS. Run it inside a tmux pane (or any terminal); it wraps your
 * shell in a pty and sits transparently in the byte stream.
 *
 *   [En]  English  - bytes pass straight through (default).
 *   [五]  Wubi 86   - a-z buffer, candidate bar, commit hanzi to the child.
 *   [拼]  Pinyin    - same, with paging (many candidates per syllable).
 *
 * A toggle key cycles En -> 五 -> 拼 -> En. Captured toggles: Ctrl-Space / Ctrl-@
 * (both byte 0x00) and Ctrl-\ (0x1C). The candidate bar is drawn on the terminal's
 * bottom line (like uim-fep); the child's cursor is saved/restored around it.
 *
 * Build: see Makefile.  Tables: wubi.tab / pinyin.tab from gen_table.py.
 */
/* Expose POSIX + BSD symbols (PATH_MAX, setenv, sigaction, forkpty, ...) under
   -std=c11 on glibc; harmless on macOS/BSD. Must precede all includes. */
#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE
#endif

#include "table.h"
#include "tables_embed.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <sys/wait.h>

#if defined(__APPLE__) || defined(__FreeBSD__) || defined(__NetBSD__) || \
    defined(__OpenBSD__)
#include <util.h>          /* forkpty */
#elif defined(__linux__)
#include <pty.h>           /* forkpty */
#else
#include <util.h>
#endif

/* ----- key bytes ------------------------------------------------------- */
#define KEY_CTRL_SPACE  0x00   /* also Ctrl-@ */
#define KEY_CTRL_BSLASH 0x1C   /* Ctrl-\ */
#define KEY_ESC         0x1B
#define KEY_CR          0x0D
#define KEY_LF          0x0A
#define KEY_BS          0x08
#define KEY_DEL         0x7F

/* ----- modes ----------------------------------------------------------- */
enum { MODE_EN = 0, MODE_WUBI, MODE_PINYIN, MODE_COUNT };
static const char *const MODE_TAG[MODE_COUNT] = { "[En]", "[五]", "[拼]" };

#define PAGE 9                 /* candidates shown per page */
#define MAX_CANDS 256          /* upper bound fetched from the table per lookup */
#define CODE_MAX IME_CODE_LEN  /* max buffered code letters */

/* ----- global state (single-threaded; signal handler only sets flags) -- */
static struct termios g_orig_termios;
static int            g_raw_active = 0;
static volatile sig_atomic_t g_winch = 0;
static volatile sig_atomic_t g_child_dead = 0;
static int            g_master = -1;
static struct winsize g_ws = { 24, 80, 0, 0 };

/* IME state */
static int        g_mode = MODE_EN;
static int        g_last_cn = MODE_WUBI; /* last non-En mode; Ctrl-\ target */
static char       g_code[CODE_MAX + 1];
static size_t     g_code_len = 0;
static int        g_page = 0;
static ime_table  g_tab_wubi, g_tab_pinyin;
static int        g_have_wubi = 0, g_have_pinyin = 0;
static int        g_bar_shown = 0;   /* is the bottom bar currently drawn? */
static int        g_wubi_autocommit = 0; /* -a: auto-commit a unique 4-key code */

/* Which schemes to load (--scheme). Bitmask; controls resident memory since an
   unbound embedded table's pages are never faulted in. Default: both. */
#define WANT_WUBI   (1 << 0)
#define WANT_PINYIN (1 << 1)
static int        g_want = WANT_WUBI | WANT_PINYIN;

/* --vim: follow vim's insert mode. We scan the child's output for the literal
   "-- INSERT --"; when it appears the editor is in insert mode and the IME
   should be on (a CN mode), when it goes away it should be off ([En]). See
   vim_scan(). g_vim_insert tracks the last-seen state to switch only on edges. */
static int        g_vim = 0;
static int        g_vim_insert = 0;

/* Escape-sequence pass-through state. When an ESC is forwarded to the child in
   a Chinese mode, the rest of the sequence (Meta chords like M-b = ESC b, and
   CSI/SS3 sequences like arrow keys = ESC [ A) must ALSO be forwarded verbatim
   instead of being interpreted as IME input. */
enum { ESC_NONE = 0, ESC_GOT_ESC, ESC_CSI };
static int        g_esc = ESC_NONE;

/* ----- tiny output helpers (write to the real terminal, fd 1) ---------- */
static void out_raw(const char *s, size_t n)
{
    while (n > 0) {
        ssize_t w = write(STDOUT_FILENO, s, n);
        if (w < 0) {
            if (errno == EINTR) continue;
            return;
        }
        s += w; n -= (size_t)w;
    }
}
/* Send bytes to the child as if typed. */
static void to_child(const char *s, size_t n)
{
    while (n > 0) {
        ssize_t w = write(g_master, s, n);
        if (w < 0) {
            if (errno == EINTR) continue;
            return;
        }
        s += w; n -= (size_t)w;
    }
}

/* ----- bottom-line reservation (DECSTBM scrolling region) -------------- */
/* The FEP owns the terminal's last row: the child is confined to rows
   1..rows-1 via a top/bottom scroll margin (DECSTBM) AND is told the screen is
   one row shorter (winsize.ws_row-1), so the child never scrolls into or draws
   on the last row. The status bar lives on that reserved row - no flicker. */
static int g_region_set = 0;

static int term_rows(void)
{
    return g_ws.ws_row > 1 ? g_ws.ws_row : 24;
}

/* Rows visible to the child = terminal rows minus the reserved bar row. */
static int child_rows(void)
{
    int r = term_rows() - 1;
    return r > 0 ? r : 1;
}

/* Confine the child to rows 1..child_rows() and park the cursor inside it.
   Used at startup / on resize where homing the cursor is fine. */
static void set_scroll_region(void)
{
    char buf[64];
    /* DECSTBM: ESC[<top>;<bottom>r  then home the cursor into the region. */
    int p = snprintf(buf, sizeof(buf), "\033[1;%dr\033[%d;1H",
                     child_rows(), child_rows());
    out_raw(buf, (size_t)p);
    g_region_set = 1;
}

/* Re-assert the DECSTBM margin WITHOUT moving the child's cursor. The child's
   output may contain a clear/reset (ESC[2J, ESC[r, RIS) that drops our margin;
   we re-apply it after each output chunk. Save/restore the cursor around the
   DECSTBM so the child's cursor position is untouched. */
static void assert_scroll_region(void)
{
    char buf[64];
    int p = snprintf(buf, sizeof(buf), "\0337\033[1;%dr\0338", child_rows());
    out_raw(buf, (size_t)p);
    g_region_set = 1;
}

/* Release the reservation: full-screen margin + clear the bar row. */
static void reset_scroll_region(void)
{
    if (!g_region_set)
        return;
    char buf[64];
    /* Reset margin to full screen, move to the (real) last row, clear it. */
    int p = snprintf(buf, sizeof(buf), "\033[r\033[%d;1H\033[2K", term_rows());
    out_raw(buf, (size_t)p);
    g_region_set = 0;
}

/* ----- terminal setup -------------------------------------------------- */
static void restore_termios(void)
{
    if (g_raw_active) {
        tcsetattr(STDIN_FILENO, TCSAFLUSH, &g_orig_termios);
        g_raw_active = 0;
    }
}

static int enter_raw(void)
{
    if (tcgetattr(STDIN_FILENO, &g_orig_termios) < 0)
        return -1;
    struct termios raw = g_orig_termios;
    /* Full raw: the FEP is the sole arbiter of input; the child's own pty gets
       its own line discipline from forkpty defaults. */
    raw.c_iflag &= ~(BRKINT | ICRNL | INPCK | ISTRIP | IXON);
    raw.c_oflag &= ~(OPOST);
    raw.c_lflag &= ~(ECHO | ICANON | IEXTEN | ISIG);
    raw.c_cflag |= CS8;
    raw.c_cc[VMIN] = 1;
    raw.c_cc[VTIME] = 0;
    if (tcsetattr(STDIN_FILENO, TCSAFLUSH, &raw) < 0)
        return -1;
    g_raw_active = 1;
    return 0;
}

/* ----- signals --------------------------------------------------------- */
static void on_winch(int sig) { (void)sig; g_winch = 1; }
static void on_child(int sig) { (void)sig; g_child_dead = 1; }

/* ----- candidate bar rendering ----------------------------------------- */
/* UCDOS-inspired status-line palette (ANSI SGR):
   BAR   - the whole reserved row: black text on light-gray background.
   TAG   - the mode chip: bright white text on a blue background (like the DOS
           highlighted field), so [五]/[拼]/[En] stands out.
   DIM   - a muted note (e.g. "(no match)"): dark-gray text on the gray bar. */
#define SGR_BAR_ATTR "\033[30;47m" /* black fg, light-gray bg */
#define SGR_TAG "\033[1;37;44m"    /* bold bright-white fg, blue bg */
#define SGR_DIM "\033[90;47m"      /* bright-black (gray) fg on gray bg */
#define SGR_OFF "\033[0m"

/* Draw the bar on the last terminal row, preserving the child's cursor. The
   whole row is painted gray (UCDOS style): set the bar colours, then ESC[K
   erases to end-of-line filling with the current background. */
static void draw_bar(const ime_cand *cands, int ncand)
{
    char buf[4096];
    int p = 0;
    int rows = term_rows();

    /* Save cursor; go to the reserved last row col 1; set the bar palette and
       clear the whole line with it (ESC[K fills with the current bg colour). */
    p += snprintf(buf + p, sizeof(buf) - p,
                  "\0337\033[%d;1H" SGR_BAR_ATTR "\033[K", rows);

    /* Mode chip in the highlighted (blue) field, then back to the bar palette. */
    p += snprintf(buf + p, sizeof(buf) - p,
                  SGR_TAG " %s " SGR_OFF SGR_BAR_ATTR " ", MODE_TAG[g_mode]);
    if (g_code_len)
        p += snprintf(buf + p, sizeof(buf) - p, "%.*s", (int)g_code_len, g_code);

    if (ncand > 0) {
        int start = g_page * PAGE;
        int end = start + PAGE;
        if (end > ncand) end = ncand;
        p += snprintf(buf + p, sizeof(buf) - p, "  ");
        for (int i = start; i < end && p < (int)sizeof(buf) - 64; i++) {
            p += snprintf(buf + p, sizeof(buf) - p, "%d", i - start + 1);
            /* word is not NUL-terminated: bounded copy */
            int wl = cands[i].word_len;
            if (p + wl < (int)sizeof(buf) - 32) {
                memcpy(buf + p, cands[i].word, wl);
                p += wl;
            }
            buf[p++] = ' ';
        }
        int pages = (ncand + PAGE - 1) / PAGE;
        if (pages > 1)
            p += snprintf(buf + p, sizeof(buf) - p, " (%d/%d)", g_page + 1, pages);
    } else if (g_code_len) {
        p += snprintf(buf + p, sizeof(buf) - p, "  " SGR_DIM "(no match)"
                      SGR_BAR_ATTR);
    }

    /* Reset attributes and restore the child's cursor. */
    p += snprintf(buf + p, sizeof(buf) - p, SGR_OFF "\0338");
    out_raw(buf, (size_t)p);
    g_bar_shown = 1;
}

/* ----- IME editing ----------------------------------------------------- */
static ime_table *cur_table(void)
{
    if (g_mode == MODE_WUBI)   return g_have_wubi   ? &g_tab_wubi   : NULL;
    if (g_mode == MODE_PINYIN) return g_have_pinyin ? &g_tab_pinyin : NULL;
    return NULL;
}

static int lookup_cur(ime_cand *cands)
{
    ime_table *t = cur_table();
    if (!t || g_code_len == 0)
        return 0;
    return ime_lookup(t, g_code, g_code_len, cands, MAX_CANDS);
}

static void reset_input(void)
{
    g_code_len = 0;
    g_code[0] = '\0';
    g_page = 0;
}

static void refresh(void)
{
    ime_cand cands[MAX_CANDS];
    int n = lookup_cur(cands);   /* 0 in En mode (no current table) */
    /* The bar is always shown - even in [En] - so the active mode is always
       visible. With nothing buffered we draw just the tag. */
    if (g_code_len == 0) {
        draw_bar(cands, 0);
        return;
    }
    draw_bar(cands, n);
}

/* Commit candidate index `idx` (0-based, absolute) to the child. */
static void commit(const ime_cand *cands, int idx)
{
    to_child(cands[idx].word, cands[idx].word_len);
    reset_input();
}

/* Is a Chinese mode usable (its table loaded)? */
static int mode_available(int m)
{
    if (m == MODE_WUBI)   return g_have_wubi;
    if (m == MODE_PINYIN) return g_have_pinyin;
    return 1; /* En always available */
}

/* Switch to `m`, clearing any pending input, and redraw the always-on bar.
   Remembers the last non-En mode so Ctrl-\ can jump back to it. */
static void switch_to(int m)
{
    reset_input();
    g_mode = m;
    if (m != MODE_EN)
        g_last_cn = m;
    refresh();   /* bar is drawn in every mode, En included */
}

/* Ctrl-@ : cycle En -> 拼 -> 五 -> En, skipping any scheme whose table is
   missing. */
static void cycle_mode(void)
{
    static const int order[] = { MODE_EN, MODE_PINYIN, MODE_WUBI };
    const int N = (int)(sizeof(order) / sizeof(order[0]));
    int cur = 0;
    for (int i = 0; i < N; i++)
        if (order[i] == g_mode) { cur = i; break; }
    for (int step = 1; step <= N; step++) {
        int cand = order[(cur + step) % N];
        if (mode_available(cand)) { switch_to(cand); return; }
    }
}

/* Ctrl-\ : quick toggle between En and the last-selected non-En mode
   (defaults to 五 until a mode is chosen via Ctrl-@). Falls back to whatever
   scheme is actually loaded. */
static void toggle_last(void)
{
    if (g_mode != MODE_EN) {
        switch_to(MODE_EN);
        return;
    }
    int target = g_last_cn;
    if (!mode_available(target))
        target = g_have_wubi ? MODE_WUBI : (g_have_pinyin ? MODE_PINYIN : MODE_EN);
    switch_to(target);
}

/* --vim edges. Entering insert restores the last CN mode (like Ctrl-\ from En);
   leaving insert drops to En. Both are no-ops if already in the target state,
   so repeated "-- INSERT --" writes don't fight the user. */
static void vim_enter_insert(void)
{
    if (g_mode == MODE_EN)
        toggle_last();          /* En -> last CN mode */
}
static void vim_leave_insert(void)
{
    if (g_mode != MODE_EN)
        switch_to(MODE_EN);
}

/* Scan a chunk of child output for vim's "-- INSERT --" mode message, but only
   accept it when it is drawn on the child's BOTTOM row (child_rows(), just above
   our IME bar) - that is where vim puts the mode message. This way the literal
   text "-- INSERT --" appearing elsewhere (typed into a file, a commit message,
   grep output, ...) does NOT falsely trigger insert mode.

   We track the cursor row by interpreting the CSI cursor-position sequences vim
   emits (ESC[<r>;<c>H, ESC[<r>H, ESC[H, and the 'f' variant) plus plain LF/CR
   movement. `cur_row` persists across chunks (vim may position on one read and
   write the message on the next). Rising edge on a bottom-row match turns the
   IME on; leaving insert is still driven by the ESC keypress (process_input). */
static int g_cur_row = 1;   /* best-effort tracked cursor row (1-based) */

static void vim_scan(const unsigned char *buf, size_t n)
{
    static const char NEEDLE[] = "-- INSERT --";
    static const size_t NL = sizeof(NEEDLE) - 1;
    if (!g_vim)
        return;

    int bottom = child_rows();
    for (size_t i = 0; i < n; i++) {
        unsigned char c = buf[i];

        /* Track cursor row through CSI cursor-position sequences. */
        if (c == 0x1B && i + 1 < n && buf[i + 1] == '[') {
            size_t j = i + 2;
            int row = 0, have_row = 0;
            while (j < n && buf[j] >= '0' && buf[j] <= '9') {
                row = row * 10 + (buf[j] - '0'); have_row = 1; j++;
            }
            /* CUP is ESC[H / ESC[<r>H / ESC[<r>;<c>H (and the 'f' variant): the
               row is the FIRST parameter, so a following ';<col>' doesn't change
               it - skip past it to the final H/f. */
            size_t k = j;
            if (k < n && buf[k] == ';') {
                k++;
                while (k < n && buf[k] >= '0' && buf[k] <= '9') k++;
            }
            if (k < n && (buf[k] == 'H' || buf[k] == 'f')) {
                g_cur_row = have_row ? row : 1;   /* default row 1 for bare H */
                i = k;
                continue;
            }
            /* Other CSI (colour, etc.): skip to its final byte so its digits and
               semicolons aren't misread as cursor moves. */
            while (j < n && !(buf[j] >= 0x40 && buf[j] <= 0x7E))
                j++;
            i = (j < n) ? j : n - 1;
            continue;
        }
        if (c == '\n') { g_cur_row++; continue; }

        /* Needle match at the current position, on the bottom row only. */
        if (c == '-' && i + NL <= n && memcmp(buf + i, NEEDLE, NL) == 0) {
            if (g_cur_row == bottom && !g_vim_insert) {
                g_vim_insert = 1;
                vim_enter_insert();
            }
            i += NL - 1;
        }
    }
}

/*
 * Handle one input byte in a Chinese mode. Returns 1 if the byte was consumed
 * by the IME, 0 if it should be passed through to the child unchanged.
 */
static int handle_cn_byte(unsigned char c)
{
    ime_cand cands[MAX_CANDS];

    /* a-z: extend the code buffer. */
    if (c >= 'a' && c <= 'z') {
        if (g_code_len < CODE_MAX) {
            g_code[g_code_len++] = (char)c;
            g_code[g_code_len] = '\0';
            g_page = 0;
        }
        int n = lookup_cur(cands);
        /* Wubi convenience (opt-in via -a): a full 4-letter code with a single
           exact match auto-commits, like classic wubi. Off by default. */
        if (g_wubi_autocommit &&
            g_mode == MODE_WUBI && g_code_len == 4 && n >= 1 && cands[0].exact) {
            int exacts = 0;
            for (int i = 0; i < n && cands[i].exact; i++) exacts++;
            if (exacts == 1) {
                commit(cands, 0);
                refresh();
                return 1;
            }
        }
        draw_bar(cands, n);
        return 1;
    }

    /* Digit selects a candidate on the current page. */
    if (c >= '1' && c <= '9' && g_code_len > 0) {
        int n = lookup_cur(cands);
        int abs = g_page * PAGE + (c - '1');
        if (abs < n) {
            commit(cands, abs);
            refresh();
        }
        return 1;
    }

    /* Space: commit the first candidate on the page (if any). */
    if (c == ' ') {
        int n = lookup_cur(cands);
        if (g_code_len > 0 && n > 0) {
            commit(cands, g_page * PAGE);
            refresh();
            return 1;
        }
        /* No pending input: let space through to the child. */
        return 0;
    }

    /* Backspace: delete last code letter. */
    if (c == KEY_BS || c == KEY_DEL) {
        if (g_code_len > 0) {
            g_code[--g_code_len] = '\0';
            g_page = 0;
            refresh();
            return 1;
        }
        return 0; /* nothing buffered -> pass through */
    }

    /* Paging: '-'/',' prev, '='/'.' next (only meaningful with candidates). */
    if ((c == '=' || c == '.') && g_code_len > 0) {
        int n = lookup_cur(cands);
        int pages = (n + PAGE - 1) / PAGE;
        if (g_page + 1 < pages) g_page++;
        draw_bar(cands, n);
        return 1;
    }
    if ((c == '-' || c == ',') && g_code_len > 0) {
        int n = lookup_cur(cands);
        if (g_page > 0) g_page--;
        draw_bar(cands, n);
        return 1;
    }

    /* Enter with a pending buffer commits the first candidate; otherwise the
       Enter passes through. */
    if (c == KEY_CR || c == KEY_LF) {
        int n = lookup_cur(cands);
        if (g_code_len > 0) {
            if (n > 0) commit(cands, g_page * PAGE);
            else       reset_input();
            refresh();
            return 1;
        }
        return 0;
    }

    /* ESC: if a composition is pending, ESC cancels it and is SWALLOWED (the
       user meant "discard these candidates", not "send ESC to the program").
       With no pending code, ESC passes through so editors/readline see it (and
       process_input forwards any following escape-sequence bytes verbatim). */
    if (c == KEY_ESC) {
        if (g_code_len > 0) {
            reset_input();
            refresh();
            return 1;   /* consumed: do not forward */
        }
        return 0;
    }

    /* Any other control byte (C-n, C-p, C-a, C-e, C-k, Ctrl-C, ...): these are
       readline / shell / signal keys. Drop any pending code and pass the byte
       through so they keep working while a Chinese mode is active. */
    if (c < 0x20) {
        if (g_code_len > 0) {
            reset_input();
            refresh();
        }
        return 0;
    }

    /* Other printable ASCII with no pending code: pass through. With a pending
       code, treat as a separator - commit first candidate then pass the byte. */
    if (g_code_len > 0) {
        int n = lookup_cur(cands);
        if (n > 0) commit(cands, g_page * PAGE);
        else       reset_input();
        refresh();
        /* fall through: the punctuation itself still goes to the child */
    }
    return 0;
}

/* Process a chunk of stdin bytes, forwarding to the child what the IME does not
   consume. Handles the mode-toggle keys in every mode. */
static void process_input(const unsigned char *buf, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        unsigned char c = buf[i];

        /* Mid escape sequence: forward everything verbatim to the child until
           the sequence ends, so Meta chords (M-b, M-f, M-d, ...) and CSI/SS3
           keys (arrows, Home/End, F-keys) reach readline/editors intact. */
        if (g_esc != ESC_NONE) {
            to_child((const char *)&c, 1);
            if (g_esc == ESC_GOT_ESC) {
                /* Byte right after ESC: '[' or 'O' begins a multi-byte CSI/SS3
                   sequence; anything else is a single-byte Meta chord. */
                g_esc = (c == '[' || c == 'O') ? ESC_CSI : ESC_NONE;
            } else { /* ESC_CSI: consume params until a final byte 0x40-0x7E. */
                if (c >= 0x40 && c <= 0x7E)
                    g_esc = ESC_NONE;
            }
            continue;
        }

        if (c == KEY_CTRL_SPACE) {      /* Ctrl-@ / Ctrl-Space: cycle modes */
            cycle_mode();
            continue;
        }
        if (c == KEY_CTRL_BSLASH) {     /* Ctrl-\: toggle En <-> last CN mode */
            toggle_last();
            continue;
        }

        /* Arm escape pass-through only when the ESC is followed by more bytes
           in THIS read - i.e. a real multi-byte key chord (Meta/arrow), which
           terminals deliver atomically. A lone trailing ESC is a bare Escape
           press: forward it but don't swallow the next, unrelated keystroke. */
        int esc_seq = (c == KEY_ESC && i + 1 < n);

        if (g_mode == MODE_EN) {
            to_child((const char *)&c, 1);
            if (esc_seq) g_esc = ESC_GOT_ESC;
            /* A bare ESC forwarded to the child while vim is in insert mode
               means the user is leaving insert -> drop the IME to [En]. */
            if (c == KEY_ESC && g_vim_insert) {
                g_vim_insert = 0;
                vim_leave_insert();
            }
            continue;
        }

        if (handle_cn_byte(c) == 0) {
            to_child((const char *)&c, 1);
            if (esc_seq) g_esc = ESC_GOT_ESC;
            if (c == KEY_ESC && g_vim_insert) {
                g_vim_insert = 0;
                vim_leave_insert();
            }
        }
    }
}

/* ----- child-output framing -------------------------------------------- */
/* Return the number of bytes of `buf` (length n) that are safe to write out
   right now, i.e. NOT ending inside a partial terminal escape sequence. The
   remainder (a trailing incomplete ESC.../OSC...) should be carried to the next
   read so our own bar/margin escapes never get spliced into the child's. We
   recognise the common forms:
     ESC alone; ESC [ ... (CSI, ends at a byte 0x40-0x7E);
     ESC ] ... (OSC, ends at BEL or ESC \); ESC <single-char> (2-byte).
   Anything we don't recognise is treated as complete (emit it). */
static size_t safe_output_len(const unsigned char *buf, size_t n)
{
    /* Scan back from the end for a lone ESC that begins an unfinished seq. */
    size_t i = n;
    while (i > 0) {
        i--;
        if (buf[i] != 0x1B)
            continue;
        /* buf[i] is an ESC; is the sequence starting here complete within n? */
        size_t j = i + 1;
        if (j >= n)
            return i;              /* bare trailing ESC: carry it */
        unsigned char c = buf[j];
        if (c == '[' ) {           /* CSI: params until final 0x40-0x7E */
            j++;
            while (j < n && !(buf[j] >= 0x40 && buf[j] <= 0x7E))
                j++;
            return (j < n) ? n : i; /* found final -> whole buf ok; else carry */
        }
        if (c == ']') {            /* OSC: until BEL or ST (ESC \) */
            j++;
            while (j < n) {
                if (buf[j] == 0x07)
                    return n;
                if (buf[j] == 0x1B && j + 1 < n && buf[j + 1] == '\\')
                    return n;
                j++;
            }
            return i;               /* unterminated OSC: carry */
        }
        /* Two-byte ESC x sequence: complete iff the byte after ESC exists. */
        return n;                   /* j<n guaranteed above -> complete */
    }
    return n;                       /* no trailing ESC found: all complete */
}

/* Non-blocking: is there data ready to read on fd right now? */
static int fd_ready(int fd)
{
    fd_set s;
    FD_ZERO(&s);
    FD_SET(fd, &s);
    struct timeval tv = { 0, 0 };
    return select(fd + 1, &s, NULL, NULL, &tv) > 0 && FD_ISSET(fd, &s);
}

/* ----- window size ----------------------------------------------------- */
static void sync_winsize(void)
{
    struct winsize ws;
    if (ioctl(STDIN_FILENO, TIOCGWINSZ, &ws) == 0) {
        g_ws = ws;                        /* real terminal size */
        if (g_master >= 0) {
            /* Tell the child the screen is one row shorter, reserving the last
               row for the bar. */
            struct winsize cws = ws;
            if (cws.ws_row > 1)
                cws.ws_row -= 1;
            ioctl(g_master, TIOCSWINSZ, &cws);
        }
    }
}

/* ----- table loading --------------------------------------------------- */
/* Try to open an external table override at <dir>/<name>. Silent when the file
   simply does not exist (the embedded copy is the normal case); only a present
   but malformed file prints a diagnostic (from ime_table_open). */
static int try_open(ime_table *t, int scheme, const char *dir, const char *name)
{
    char path[PATH_MAX];
    if (dir && *dir)
        snprintf(path, sizeof(path), "%s/%s", dir, name);
    else
        snprintf(path, sizeof(path), "%s", name);
    if (access(path, R_OK) != 0)
        return 0;   /* not present here: fall through to the next dir / embedded */
    return ime_table_open(t, path, scheme) == 0 ? 1 : 0;
}

/* Bind the embedded (compiled-in) table for a scheme, if present. */
static int load_embedded(ime_table *t, int scheme)
{
    for (int i = 0; i < embedded_tables_count; i++) {
        const embedded_table *e = &embedded_tables[i];
        if (e->scheme != scheme)
            continue;
        if (ime_table_open_mem(t, e->data, e->size, scheme, e->name) == 0)
            return 1;
    }
    return 0;
}

/*
 * Load the schemes selected by g_want (--scheme). Tables are compiled into the
 * binary (see gen_embed.py), so wubi-ime is self-contained and can be copied
 * anywhere; loading only the wanted scheme(s) keeps the others' pages off the
 * resident set. An external .tab still takes precedence when found via
 * $WUBI_IME_DIR / the exe dir / cwd, so a table can be overridden without a
 * rebuild.
 */
static void load_tables(const char *argv0)
{
    const char *env = getenv("WUBI_IME_DIR");

    /* directory of the executable */
    char exedir[PATH_MAX] = "";
    if (argv0) {
        snprintf(exedir, sizeof(exedir), "%s", argv0);
        char *slash = strrchr(exedir, '/');
        if (slash) *slash = '\0';
        else exedir[0] = '\0';
    }

    const char *dirs[3];
    int nd = 0;
    if (env && *env) dirs[nd++] = env;
    if (exedir[0])   dirs[nd++] = exedir;
    dirs[nd++] = ".";

    /* External file override first (only when explicitly present), then the
       embedded copy - but only for the scheme(s) requested. */
    if (g_want & WANT_WUBI) {
        for (int i = 0; i < nd && !g_have_wubi; i++)
            g_have_wubi = try_open(&g_tab_wubi, IME_SCHEME_WUBI, dirs[i],
                                   "wubi.tab");
        if (!g_have_wubi)
            g_have_wubi = load_embedded(&g_tab_wubi, IME_SCHEME_WUBI);
        if (!g_have_wubi)
            fprintf(stderr, "wubi-ime: warning: no wubi table; [五] disabled\n");
    }
    if (g_want & WANT_PINYIN) {
        for (int i = 0; i < nd && !g_have_pinyin; i++)
            g_have_pinyin = try_open(&g_tab_pinyin, IME_SCHEME_PINYIN, dirs[i],
                                     "pinyin.tab");
        if (!g_have_pinyin)
            g_have_pinyin = load_embedded(&g_tab_pinyin, IME_SCHEME_PINYIN);
        if (!g_have_pinyin)
            fprintf(stderr, "wubi-ime: warning: no pinyin table; [拼] disabled\n");
    }
}

/* ----- main ------------------------------------------------------------ */
static void usage(const char *prog)
{
    fprintf(stderr,
        "usage: %s [-s SCHEME] [-a] [-V] [-h]\n"
        "  -s, --scheme SCHEME  which tables to load (default: both):\n"
        "                         both    wubi + pinyin\n"
        "                         wubi    wubi only\n"
        "                         pinyin  pinyin only\n"
        "                       Loading fewer schemes lowers resident memory.\n"
        "  -a, --auto-commit    wubi: auto-commit a full 4-letter code that has a\n"
        "                       single exact match (off by default)\n"
        "  -V, --vim            follow vim insert mode: turn the IME on when vim\n"
        "                       shows \"-- INSERT --\", off when you leave insert\n"
        "  -h, --help           show this help\n",
        prog);
}

int main(int argc, char **argv)
{
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "-a") || !strcmp(a, "--auto-commit")) {
            g_wubi_autocommit = 1;
        } else if (!strcmp(a, "-V") || !strcmp(a, "--vim")) {
            g_vim = 1;
        } else if (!strcmp(a, "-s") || !strcmp(a, "--scheme")) {
            const char *val = (i + 1 < argc) ? argv[++i] : "";
            if (!strcmp(val, "both"))        g_want = WANT_WUBI | WANT_PINYIN;
            else if (!strcmp(val, "wubi"))   g_want = WANT_WUBI;
            else if (!strcmp(val, "pinyin")) g_want = WANT_PINYIN;
            else {
                fprintf(stderr, "wubi-ime: --scheme wants both|wubi|pinyin, "
                        "got '%s'\n", val);
                usage(argv[0]);
                return 2;
            }
        } else if (!strcmp(a, "-h") || !strcmp(a, "--help")) {
            usage(argv[0]);
            return 0;
        } else {
            fprintf(stderr, "wubi-ime: unknown option '%s'\n", a);
            usage(argv[0]);
            return 2;
        }
    }

    load_tables(argv[0]);

    /* Inherit the current window size; the child gets one row less so the last
       row stays reserved for the bar. */
    struct winsize real_ws = { 24, 80, 0, 0 };
    if (ioctl(STDIN_FILENO, TIOCGWINSZ, &real_ws) == 0)
        g_ws = real_ws;
    struct winsize child_ws = g_ws;
    if (child_ws.ws_row > 1)
        child_ws.ws_row -= 1;

    pid_t pid = forkpty(&g_master, NULL, NULL, &child_ws);
    if (pid < 0) {
        perror("wubi-ime: forkpty");
        return 1;
    }
    if (pid == 0) {
        /* Child: exec the user's shell (login-ish). */
        const char *shell = getenv("SHELL");
        if (!shell || !*shell) shell = "/bin/sh";
        setenv("WUBI_IME", "1", 1);
        execl(shell, shell, (char *)NULL);
        perror("wubi-ime: exec shell");
        _exit(127);
    }

    /* Parent. */
    if (enter_raw() < 0) {
        perror("wubi-ime: raw mode");
        /* keep going without raw; degraded but usable */
    }
    atexit(restore_termios);

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_winch;
    sigaction(SIGWINCH, &sa, NULL);
    sa.sa_handler = on_child;
    sigaction(SIGCHLD, &sa, NULL);

    /* Reserve the last row for the bar (child confined to the rows above) and
       draw the always-on status bar immediately so [En] shows from the start. */
    set_scroll_region();
    refresh();

    /* Child-output carry: bytes of a partial escape sequence at the end of a
       read are held here and prepended to the next read, so we never write our
       own bar/margin escapes into the middle of one of the child's sequences
       (which corrupts full-screen apps like vim). */
    static unsigned char carry[64];
    size_t carry_len = 0;

    unsigned char ibuf[8192];
    for (;;) {
        if (g_winch) {
            g_winch = 0;
            sync_winsize();
            set_scroll_region();  /* re-assert margin for the new size */
            refresh();
        }
        if (g_child_dead) {
            /* Drain any final output below, then exit after select reports EOF. */
        }

        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(STDIN_FILENO, &rfds);
        FD_SET(g_master, &rfds);
        int maxfd = g_master > STDIN_FILENO ? g_master : STDIN_FILENO;

        int r = select(maxfd + 1, &rfds, NULL, NULL, NULL);
        if (r < 0) {
            if (errno == EINTR) continue;
            break;
        }

        /* Child output -> terminal. */
        if (FD_ISSET(g_master, &rfds)) {
            /* Prepend any carried partial-escape bytes from last time. */
            memcpy(ibuf, carry, carry_len);
            ssize_t nr = read(g_master, ibuf + carry_len, sizeof(ibuf) - carry_len);
            if (nr <= 0) {
                if (nr < 0 && errno == EINTR) continue;
                if (carry_len) {          /* flush leftover bytes on EOF */
                    out_raw((const char *)ibuf, carry_len);
                    carry_len = 0;
                }
                break; /* child closed the pty */
            }
            size_t total = carry_len + (size_t)nr;
            carry_len = 0;

            /* Split off any trailing incomplete escape sequence and carry it. */
            size_t emit = safe_output_len(ibuf, total);
            if (emit < total) {
                size_t rem = total - emit;
                if (rem > sizeof(carry)) {   /* pathological: don't stall */
                    emit = total;
                    rem = 0;
                }
                if (rem) {
                    memcpy(carry, ibuf + emit, rem);
                    carry_len = rem;
                }
            }

            out_raw((const char *)ibuf, emit);
            vim_scan(ibuf, emit);         /* --vim: follow "-- INSERT --" */

            /* Only re-assert the margin + redraw the bar when the child has gone
               quiet (nothing more pending). This lands at a burst boundary, not
               mid-frame, so it never interleaves with the child's redraw. */
            if (!fd_ready(g_master)) {
                assert_scroll_region();
                refresh();
            }
        }

        /* Terminal input -> IME/child. */
        if (FD_ISSET(STDIN_FILENO, &rfds)) {
            ssize_t nr = read(STDIN_FILENO, ibuf, sizeof(ibuf));
            if (nr <= 0) {
                if (nr < 0 && errno == EINTR) continue;
                break;
            }
            process_input(ibuf, (size_t)nr);
        }
    }

    reset_scroll_region();   /* release the reserved row + clear it */
    restore_termios();

    int status = 0;
    waitpid(pid, &status, 0);
    if (g_have_wubi)   ime_table_close(&g_tab_wubi);
    if (g_have_pinyin) ime_table_close(&g_tab_pinyin);

    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return 0;
}
