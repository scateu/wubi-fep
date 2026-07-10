#!/usr/bin/env python3
"""
Generate an on-device IME table (Wubi / Pinyin / Shuangpin) for the micro-journal
Chinese IME, in the unified "IME3" format, padded into a fixed-size flash slot.

Only the Python standard library is used - no PlatformIO / build toolchain needed
to (re)generate a table. Pair with IME/inject_ime.py to swap the table into a
prebuilt firmware.bin without rebuilding. All IME assets live in the IME/ folder.

Schemes
-------
  wubi       Wubi 86. Source: rime `IME/wubi86.dict.yaml` (Jidian 6 table, TSV
             `<text>\\t<code>\\t<weight>` under a YAML header). Codes 1-4 letters,
             ranked by descending weight (higher = more common). Restricted to
             the GBK charset. Keeps BOTH single hanzi AND multi-hanzi PHRASES
             (工期, 葡萄牙, ...).
  pinyin     Full Hanyu Pinyin. Source: rime `IME/pinyin_simp.dict.yaml`
             (`<char>\\t<syllable>\\t<weight>`). Syllables 1-6 letters, ranked
             by descending weight so common chars come first. Single-char.
  shuangpin  Xiaohe (小鹤) double-pinyin. Derived from the same pinyin source:
             each full syllable is mapped to its 2-letter Xiaohe code, keeping
             the char + weight ranking. Codes are always 2 letters. Single-char.

IME4 binary format (little-endian), consumed by src/service/IME/IME.cpp
------------------------------------------------------------------------
Records are FIXED-WIDTH so the on-device binary search + prefix index are
untouched; the (variable-length) hanzi/phrase text lives in a separate string
pool appended after the records, referenced by (offset,len). Single-hanzi
schemes just have len==3.

  magic     : 4 bytes   "IME4"
  scheme    : 1 byte    0=wubi 1=pinyin 2=shuangpin  (drives the [五]/[拼]/[双]
                        indicator and the max input length on-device)
  codeLen   : 1 byte    fixed code width in bytes (6 for all schemes here)
  reserved  : 2 bytes   0
  count     : uint32    number of records
  poolBytes : uint32    size of the trailing string pool in bytes
  index     : 677 * uint32   first-two-letter prefix lower-bound index
  records   : count * (codeLen + 4) bytes, sorted ascending by code:
                code   : codeLen bytes  ASCII a-z, NUL-padded
                poolOff: 3 bytes (uint24 LE)  byte offset into the pool
                wordLen: 1 byte               phrase length in bytes (1..255)
  pool      : poolBytes bytes   concatenated UTF-8 phrases (de-duplicated),
                                addressed by (poolOff, wordLen)

The pool starts immediately after the records (recordBase + count*recordSize),
so its absolute offset is derived on-device; poolBytes bounds it.

Fixed slot
----------
The whole IME3 blob is padded to a fixed SLOT_SIZE (default 512 KiB) so a table
can be hot-swapped into a prebuilt firmware.bin (the reserved region never moves
or changes size). The padding after the real blob is 0xFF (flash-erase value).
The firmware ignores the padding (it reads `count` from the header).

Usage (run from the repo root)
------------------------------
  python3 IME/gen_ime.py --scheme wubi      --src IME/wubi86.dict.yaml      --out IME/ime_table.bin
  python3 IME/gen_ime.py --scheme pinyin    --src IME/pinyin_simp.dict.yaml --out IME/ime_table.bin
  python3 IME/gen_ime.py --scheme shuangpin --src IME/pinyin_simp.dict.yaml --out IME/ime_table.bin

  --top N   keep only the N most common hanzi (0 = keep all).
  --slot N  reserved slot size in bytes (default 524288 = 512 KiB).
"""
import argparse
import os
import struct
import sys

MAGIC = b"IME4"
INDEX_ENTRIES = 26 * 26 + 1  # 677: one lower-bound per two-letter prefix + sentinel
CODE_LEN = 6                  # fixed code width (fits pinyin zhuang/chuang/shuang)
# magic[4] + scheme[1] + codeLen[1] + reserved[2] + count[4] + poolBytes[4]
HEADER_SIZE = 16
RECORD_EXTRA = 4             # poolOff (uint24) + wordLen (uint8)
SLOT_SIZE_DEFAULT = 512 * 1024
MAX_PHRASES_DEFAULT = 30000  # top-N phrases kept for wubi (0 = keep all)
PAD_BYTE = 0xFF

SCHEME_WUBI = 0
SCHEME_PINYIN = 1
SCHEME_SHUANGPIN = 2
SCHEME_IDS = {"wubi": SCHEME_WUBI, "pinyin": SCHEME_PINYIN, "shuangpin": SCHEME_SHUANGPIN}


# ---------------------------------------------------------------------------
# Xiaohe (小鹤) double-pinyin mapping
# ---------------------------------------------------------------------------
# Initials: zh/ch/sh -> v/i/u; all single-letter initials map to themselves.
XIAOHE_INITIAL = {
    "zh": "v", "ch": "i", "sh": "u",
    "b": "b", "p": "p", "m": "m", "f": "f", "d": "d", "t": "t", "n": "n",
    "l": "l", "g": "g", "k": "k", "h": "h", "j": "j", "q": "q", "x": "x",
    "r": "r", "z": "z", "c": "c", "s": "s", "y": "y", "w": "w",
}
# Finals -> key (canonical Xiaohe layout). Single-vowel finals map to their own
# letter (a/o/e/i/u); "v" is for ü.
XIAOHE_FINAL = {
    "a": "a", "o": "o", "e": "e", "i": "i", "u": "u", "v": "v",
    "ai": "d", "an": "j", "ang": "h", "ao": "c",
    "ei": "w", "en": "f", "eng": "g", "er": "r",
    "ia": "x", "ian": "m", "iang": "l", "iao": "n", "ie": "p",
    "in": "b", "ing": "k", "iong": "s", "iu": "q",
    "ong": "s", "ou": "z",
    "ua": "x", "uai": "k", "uan": "r", "uang": "l", "ue": "t",
    "ui": "v", "un": "y", "uo": "o",
    "ve": "t", "ng": "g",
}
# Zero-initial syllables (start with a vowel): first letter is the initial key,
# then the final key. Full standard set for the 2-letter code.
XIAOHE_ZERO = {
    "a": "aa", "o": "oo", "e": "ee",
    "ai": "ai", "an": "an", "ang": "ah", "ao": "ao",
    "ei": "ei", "en": "en", "eng": "eg", "er": "er",
    "ou": "ou",
}


def split_pinyin(syl):
    """Split a full pinyin syllable into (initial, final). initial may be ''."""
    for ini in ("zh", "ch", "sh"):
        if syl.startswith(ini):
            return ini, syl[len(ini):]
    if syl and syl[0] in "bpmfdtnlgkhjqxrzcsyw":
        return syl[0], syl[1:]
    return "", syl  # zero-initial (starts with a vowel)


def to_xiaohe(syl):
    """Map a full pinyin syllable to its 2-letter Xiaohe code, or None if the
    syllable is outside the standard scheme (rare interjections m/n/ng/hm...)."""
    if syl in XIAOHE_ZERO:
        return XIAOHE_ZERO[syl]

    ini, fin = split_pinyin(syl)
    if ini == "":
        return None  # a vowel-initial syllable not in the zero table

    ikey = XIAOHE_INITIAL.get(ini)
    if ikey is None:
        return None

    if len(fin) == 1 and fin in "aoeiuv":
        return ikey + fin
    fkey = XIAOHE_FINAL.get(fin)
    if fkey is None:
        return None
    return ikey + fkey


# ---------------------------------------------------------------------------
# prefix index
# ---------------------------------------------------------------------------
def build_prefix_index(records, count):
    """index[k] = first record index whose code sorts >= the two-letter prefix
    k encodes (k=(c0-'a')*26+(c1-'a')). Monotonic; index[676]==count."""
    index = [count] * INDEX_ENTRIES
    for i, (code, _char) in enumerate(records):
        if len(code) < 2:
            k = (ord(code[0]) - 97) * 26
        else:
            k = (ord(code[0]) - 97) * 26 + (ord(code[1]) - 97)
        if index[k] == count:
            index[k] = i
    nxt = count
    for k in range(INDEX_ENTRIES - 1, -1, -1):
        if index[k] == count:
            index[k] = nxt
        else:
            nxt = index[k]
    return index


def is_single_hanzi(s):
    return len(s) == 1 and 0x4E00 <= ord(s) <= 0x9FFF


def is_hanzi_word(s):
    """True for a run of >=1 BMP CJK chars (a single hanzi OR a phrase)."""
    return len(s) >= 1 and all(0x4E00 <= ord(c) <= 0x9FFF for c in s)


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
def load_wubi(path):
    """Yield (code, word, score) from a rime wubi86 `.dict.yaml`, keeping BOTH
    single hanzi AND multi-hanzi phrases, restricted to the GBK charset.

    The rime dict is TSV under a YAML header ended by `...`. Columns are declared
    in the header (`columns: [text, code, weight, stem]`) — note `text` comes
    BEFORE `code`, unlike the old ywvim layout. `weight` is a Google-derived
    frequency where HIGHER = more common, so score = -weight (lower = more
    common), matching the pinyin source. Lines beginning with `#` are rime's
    hidden alternate-code entries (e.g. `#子 b`): the char is still reachable by
    its full code elsewhere, so we skip them like the upstream ccime2 builder.

    GBK filter: the on-device display only ships GBK-range hanzi, so words with
    any non-GBK codepoint are dropped (mirrors ccime2 `build_dict.py --gbk`)."""
    started = False           # True once past the `...` end-of-header marker
    in_cols = False
    col_list = []
    col_map = {}
    dropped_gbk = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n\r")
            if line.startswith("---"):
                continue
            if line.startswith("..."):
                started = True
                if not col_map and col_list:
                    col_map = {c: i for i, c in enumerate(col_list)}
                continue
            if not started:
                # still inside the YAML header: capture the `columns:` list
                if line.startswith("columns:"):
                    in_cols = True
                    continue
                if in_cols:
                    stripped = line.strip()
                    if stripped.startswith("- "):
                        col_list.append(stripped[2:].strip())
                        continue
                    in_cols = False
                continue
            # data section
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            if not col_map:
                col_map = {"text": 0, "code": 1}
            word = parts[col_map["text"]]
            code = parts[col_map["code"]].lower()
            if not (code.isascii() and code.isalpha() and 1 <= len(code) <= 4):
                continue
            if not is_hanzi_word(word):
                continue
            try:
                word.encode("gbk")
            except UnicodeEncodeError:
                dropped_gbk += 1
                continue
            w_idx = col_map.get("weight", col_map.get("freq", 2))
            weight = 0
            if len(parts) > w_idx:
                raw = parts[w_idx].strip()
                if raw.lstrip("-").isdigit():
                    weight = int(raw)
            yield code, word, -weight
    if dropped_gbk:
        print(f"wubi: dropped {dropped_gbk} non-GBK entries", file=sys.stderr)


def load_pinyin(path, shuangpin=False):
    """Yield (code, char, score) from rime pinyin_simp.dict.yaml. Higher weight =
    more common, so score = -weight (lower score = more common)."""
    started = False
    dropped = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line == "...":
                started = True
                continue
            if not started or not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            char, syl = parts[0], parts[1]
            weight = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            # single hanzi + single syllable only
            if len(char) != 1 or " " in syl or not is_single_hanzi(char):
                continue
            if not (syl.isascii() and syl.isalpha()):
                continue
            code = syl
            if shuangpin:
                code = to_xiaohe(syl)
                if code is None:
                    dropped += 1
                    continue
            yield code, char, -weight
    if dropped:
        print(f"warning: {dropped} syllables had no Xiaohe mapping (skipped)",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheme", required=True, choices=list(SCHEME_IDS))
    ap.add_argument("--src", required=True, help="source table for the scheme")
    ap.add_argument("--out", default=os.path.join("IME", "ime_table.bin"))
    ap.add_argument("--top", type=int, default=0,
                    help="keep only the N most common words (0 = keep all)")
    ap.add_argument("--max-phrases", type=int, default=MAX_PHRASES_DEFAULT,
                    help="wubi only: keep at most N multi-hanzi phrases, ranked "
                         "shortest-code-first (0 = keep all). Single hanzi are "
                         "always kept. Trims the table to fit the flash slot.")
    ap.add_argument("--slot", type=int, default=SLOT_SIZE_DEFAULT,
                    help="reserved flash slot size in bytes (default 512 KiB; "
                         "wubi-with-phrases uses 896 KiB = 917504)")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        sys.exit(f"source table not found: {args.src}")

    scheme = SCHEME_IDS[args.scheme]

    if args.scheme == "wubi":
        gen = load_wubi(args.src)
    else:
        gen = load_pinyin(args.src, shuangpin=(args.scheme == "shuangpin"))

    # collect, de-duplicating (code, char) keeping the best (lowest) score
    best = {}
    for code, char, score in gen:
        if len(code) > CODE_LEN:
            continue
        key = (code, char)
        if key not in best or score < best[key]:
            best[key] = score

    records = [(code, char, score) for (code, char), score in best.items()]

    # optional frequency filter: rank each word by its best score, keep top N
    if args.top > 0:
        char_best = {}
        for code, char, score in records:
            if char not in char_best or score < char_best[char]:
                char_best[char] = score
        kept = set(sorted(char_best, key=lambda c: char_best[c])[:args.top])
        records = [r for r in records if r[1] in kept]

    # Phrase trim (wubi has phrases; pinyin/shuangpin are single-char so this is
    # a no-op there). Single hanzi are ALWAYS kept; multi-hanzi phrases are
    # ranked by score (shortest-code-first) and truncated to --max-phrases so
    # the table fits the flash slot.
    if args.max_phrases > 0:
        singles = [r for r in records if len(r[1]) == 1]
        phrases = [r for r in records if len(r[1]) > 1]
        phrases.sort(key=lambda r: r[2])
        phrases = phrases[:args.max_phrases]
        records = singles + phrases

    # sort by code asc; within a code, by score asc (most common candidate first)
    records.sort(key=lambda r: (r[0], r[2]))
    recs = [(code, char) for code, char, _ in records]

    index = build_prefix_index(recs, len(recs))
    record_size = CODE_LEN + RECORD_EXTRA

    # Build the string pool: de-duplicate phrase bytes, remember each word's
    # byte offset. Records reference (offset, len) into this pool.
    pool = bytearray()
    pool_off = {}
    for _code, char in recs:
        hb = char.encode("utf-8")
        if hb not in pool_off:
            pool_off[hb] = len(pool)
            pool += hb
    if len(pool) > 0xFFFFFF:
        sys.exit(f"string pool {len(pool)} bytes exceeds the uint24 offset range")

    rec_bytes = bytearray()
    written = 0
    for code, char in recs:
        cb = code.encode("ascii")
        hb = char.encode("utf-8")
        if len(cb) > CODE_LEN or not (1 <= len(hb) <= 255):
            continue
        off = pool_off[hb]
        rec_bytes += cb + b"\x00" * (CODE_LEN - len(cb))          # code[CODE_LEN]
        rec_bytes += bytes((off & 0xFF, (off >> 8) & 0xFF, (off >> 16) & 0xFF))  # off u24 LE
        rec_bytes += bytes((len(hb),))                            # wordLen u8
        written += 1

    out = bytearray()
    out += MAGIC
    out += struct.pack("<BBH", scheme, CODE_LEN, 0)
    out += struct.pack("<I", written)
    out += struct.pack("<I", len(pool))
    out += struct.pack("<%dI" % INDEX_ENTRIES, *index)
    out += rec_bytes
    out += pool

    real = len(out)
    if real > args.slot:
        sys.exit(f"table {real} bytes exceeds slot {args.slot}; "
                 f"lower --max-phrases or raise --slot to shrink/fit")

    # pad to the fixed slot with the flash-erase value
    out += bytes([PAD_BYTE]) * (args.slot - real)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(out)

    n_phrases = sum(1 for _c, w in recs if len(w) > 1)
    print(f"scheme       : {args.scheme} ({scheme})")
    print(f"unique words : {len(set(r[1] for r in recs))}  ({n_phrases} phrases > 1 char)")
    print(f"records      : {written}  (record size {record_size} B)")
    print(f"pool bytes   : {len(pool)}  ({len(pool_off)} unique strings)")
    print(f"real bytes   : {real}  ({real/1024:.1f} KiB)")
    print(f"slot bytes   : {args.slot}  ({args.slot/1024:.0f} KiB, {real*100//args.slot}% used)")
    print(f"output       : {args.out}")


if __name__ == "__main__":
    sys.exit(main())
