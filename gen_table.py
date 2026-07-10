#!/usr/bin/env python3
"""
Generate compact IME tables (`.tab`) for the wubi-ime FEP from rime dictionaries.

This is deliberately separate from `gen_ime.py` (which targets a micro-journal
firmware's fixed-size flash slot in the "IME4" format, GBK-restricted). Here the
target is a UTF-8 terminal, so:
  * no GBK restriction,
  * no fixed slot / 0xFF padding,
  * a simpler record layout with an explicit rank byte.

Schemes
-------
  wubi     Wubi 86 from `wubi86.dict.yaml`. Codes 1-4 letters. Keeps BOTH single
           hanzi AND multi-hanzi phrases (工期, 葡萄牙, ...). Ranked by descending
           weight (more common first).
  pinyin   Full Hanyu Pinyin from `pinyin_simp.dict.yaml`. Single hanzi, single
           syllable (1-6 letters). Ranked by descending weight.

Binary `.tab` format (little-endian) - see table.h for the C mirror:
  magic     : 4 bytes  "IMET"
  version   : u8   = 1
  scheme    : u8   0=wubi 1=pinyin
  reserved  : u16  0
  count     : u32  number of records
  poolBytes : u32  size of trailing UTF-8 string pool
  index     : u32[677]  lower-bound-by-two-letter-prefix index (26*26 + 1)
  records   : count * 12 bytes, sorted ascending by (code, rank):
                code    : char[6]  ASCII a-z, NUL-padded
                poolOff : u32       byte offset into pool
                wordLen : u8        UTF-8 byte length (1..255)
                rank    : u8        per-code rank, 0 = best (most common)
  pool      : poolBytes  concatenated de-duplicated UTF-8 phrases

Usage
-----
  python3 gen_table.py --scheme wubi   --src wubi86.dict.yaml       --out wubi.tab
  python3 gen_table.py --scheme pinyin --src pinyin_simp.dict.yaml  --out pinyin.tab

  --top N          keep only the N most common hanzi (0 = keep all).
  --max-phrases N  wubi only: cap multi-hanzi phrases (0 = keep all). Single
                   hanzi are always kept.
"""
import argparse
import os
import struct
import sys

MAGIC = b"IMET"
VERSION = 1
INDEX_ENTRIES = 26 * 26 + 1   # 677
CODE_LEN = 6                  # fits "shuang"
RECORD_SIZE = CODE_LEN + 4 + 1 + 1   # code[6] + poolOff(u32) + wordLen(u8) + rank(u8)
HEADER_SIZE = 16             # magic[4]+ver[1]+scheme[1]+resv[2]+count[4]+pool[4]

SCHEME_WUBI = 0
SCHEME_PINYIN = 1
SCHEME_IDS = {"wubi": SCHEME_WUBI, "pinyin": SCHEME_PINYIN}


def is_single_hanzi(s):
    return len(s) == 1 and 0x4E00 <= ord(s) <= 0x9FFF


def is_hanzi_word(s):
    """True for a run of >=1 BMP CJK chars (a single hanzi OR a phrase)."""
    return len(s) >= 1 and all(0x4E00 <= ord(c) <= 0x9FFF for c in s)


# ---------------------------------------------------------------------------
# sources (parsing mirrors gen_ime.py, minus the GBK filter)
# ---------------------------------------------------------------------------
def load_wubi(path):
    """Yield (code, word, score) from a rime wubi86 `.dict.yaml`.

    TSV under a YAML header ended by `...`. `columns:` declares the field order
    (text, code, weight, stem). `weight` is higher = more common, so score =
    -weight (lower = more common). Lines starting with `#` are rime's hidden
    alternate-code entries; skipped like the upstream builder."""
    started = False
    in_cols = False
    col_list = []
    col_map = {}
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
            w_idx = col_map.get("weight", col_map.get("freq", 2))
            weight = 0
            if len(parts) > w_idx:
                raw = parts[w_idx].strip()
                if raw.lstrip("-").isdigit():
                    weight = int(raw)
            yield code, word, -weight


def load_pinyin(path):
    """Yield (code, char, score) from rime pinyin_simp.dict.yaml. Single hanzi,
    single syllable. Higher weight = more common, so score = -weight."""
    started = False
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
            if len(char) != 1 or " " in syl or not is_single_hanzi(char):
                continue
            if not (syl.isascii() and syl.isalpha() and 1 <= len(syl) <= CODE_LEN):
                continue
            yield syl, char, -weight


# ---------------------------------------------------------------------------
# prefix index
# ---------------------------------------------------------------------------
def build_prefix_index(recs):
    """index[k] = first record index whose code sorts >= the two-letter prefix
    k = (c0-'a')*26 + (c1-'a'). A 1-letter code uses (c0-'a')*26 (col 0).
    Monotonic non-decreasing; index[676] == count (sentinel)."""
    count = len(recs)
    index = [count] * INDEX_ENTRIES
    for i, (code, _word, _rank) in enumerate(recs):
        c0 = ord(code[0]) - 97
        c1 = (ord(code[1]) - 97) if len(code) >= 2 else 0
        k = c0 * 26 + c1
        if index[k] == count:
            index[k] = i
    nxt = count
    for k in range(INDEX_ENTRIES - 1, -1, -1):
        if index[k] == count:
            index[k] = nxt
        else:
            nxt = index[k]
    return index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheme", required=True, choices=list(SCHEME_IDS))
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=0,
                    help="keep only the N most common hanzi (0 = keep all)")
    ap.add_argument("--max-phrases", type=int, default=0,
                    help="wubi only: cap multi-hanzi phrases (0 = keep all)")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        sys.exit(f"source not found: {args.src}")

    scheme = SCHEME_IDS[args.scheme]
    gen = load_wubi(args.src) if scheme == SCHEME_WUBI else load_pinyin(args.src)

    # de-duplicate (code, word), keep best (lowest) score
    best = {}
    for code, word, score in gen:
        if len(code) > CODE_LEN:
            continue
        key = (code, word)
        if key not in best or score < best[key]:
            best[key] = score
    records = [(code, word, score) for (code, word), score in best.items()]

    # optional frequency filter across the whole vocabulary
    if args.top > 0:
        word_best = {}
        for code, word, score in records:
            if word not in word_best or score < word_best[word]:
                word_best[word] = score
        kept = set(sorted(word_best, key=lambda w: word_best[w])[:args.top])
        records = [r for r in records if r[1] in kept]

    # wubi phrase cap (no-op for pinyin, which is single-char)
    if args.max_phrases > 0:
        singles = [r for r in records if len(r[1]) == 1]
        phrases = [r for r in records if len(r[1]) > 1]
        phrases.sort(key=lambda r: r[2])
        phrases = phrases[:args.max_phrases]
        records = singles + phrases

    # sort by (code asc, score asc), then assign a per-code rank byte
    records.sort(key=lambda r: (r[0], r[2]))
    recs = []           # (code, word, rank)
    last_code = None
    rank = 0
    for code, word, _score in records:
        if code != last_code:
            rank = 0
            last_code = code
        recs.append((code, word, min(rank, 255)))
        rank += 1

    index = build_prefix_index(recs)

    # string pool: de-duplicate word bytes
    pool = bytearray()
    pool_off = {}
    for _code, word, _rank in recs:
        wb = word.encode("utf-8")
        if wb not in pool_off:
            pool_off[wb] = len(pool)
            pool += wb

    rec_bytes = bytearray()
    written = 0
    for code, word, rk in recs:
        cb = code.encode("ascii")
        wb = word.encode("utf-8")
        if len(cb) > CODE_LEN or not (1 <= len(wb) <= 255):
            continue
        off = pool_off[wb]
        rec_bytes += cb + b"\x00" * (CODE_LEN - len(cb))
        rec_bytes += struct.pack("<IBB", off, len(wb), rk)
        written += 1

    out = bytearray()
    out += MAGIC
    out += struct.pack("<BBH", VERSION, scheme, 0)
    out += struct.pack("<II", written, len(pool))
    out += struct.pack("<%dI" % INDEX_ENTRIES, *index)
    out += rec_bytes
    out += pool

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(out)

    n_phrases = sum(1 for _c, w, _r in recs if len(w) > 1)
    print(f"scheme       : {args.scheme} ({scheme})")
    print(f"records      : {written}  (record size {RECORD_SIZE} B)")
    print(f"unique words : {len(set(r[1] for r in recs))}  ({n_phrases} phrases)")
    print(f"pool bytes   : {len(pool)}  ({len(pool_off)} unique strings)")
    print(f"total bytes  : {len(out)}  ({len(out)/1024:.1f} KiB)")
    print(f"output       : {args.out}")


if __name__ == "__main__":
    sys.exit(main())
