/* table.h - mmap + lookup for a wubi-ime `.tab` file (see gen_table.py). */
#ifndef WUBI_IME_TABLE_H
#define WUBI_IME_TABLE_H

#include <stdint.h>
#include <stddef.h>

#define IME_CODE_LEN 16   /* fits multi-syllable pinyin phrases (concatenated) */
#define IME_INDEX_ENTRIES (26 * 26 + 1)   /* 677 */

#define IME_SCHEME_WUBI   0
#define IME_SCHEME_PINYIN 1

/* One on-disk record: fixed (IME_CODE_LEN + 6) bytes, little-endian, sorted by
   (code, rank). */
typedef struct {
    char     code[IME_CODE_LEN]; /* ASCII a-z, NUL-padded */
    uint32_t pool_off;           /* byte offset into the string pool */
    uint8_t  word_len;           /* UTF-8 byte length of the word (1..255) */
    uint8_t  rank;               /* 0 = best (most common) for this code */
} __attribute__((packed)) ime_record;

typedef struct {
    const uint8_t   *base;       /* mmap'd file base */
    size_t           size;       /* mmap'd size */
    int              scheme;     /* IME_SCHEME_* */
    uint32_t         count;      /* number of records */
    uint32_t         pool_bytes;
    const uint32_t  *index;      /* IME_INDEX_ENTRIES prefix lower-bounds */
    const ime_record*records;    /* count records */
    const char      *pool;       /* pool_bytes UTF-8 bytes */
    int              owns_map;   /* 1 if `base` is an mmap to munmap on close */
} ime_table;

/* A single candidate returned by a lookup. `word`/`word_len` point into the
   mmap'd pool (not NUL-terminated). `exact` is 1 when the record's code equals
   the query exactly, 0 when it is only a prefix extension of the query. */
typedef struct {
    const char *word;
    uint8_t     word_len;
    uint8_t     rank;
    int         exact;
} ime_cand;

/* Open/close. Returns 0 on success, -1 on error (errno set / message printed). */
int  ime_table_open(ime_table *t, const char *path, int expect_scheme);

/* Bind `t` to an already-in-memory table image (e.g. a table compiled into the
   binary). The buffer must outlive `t` and is NOT freed by ime_table_close.
   Returns 0 on success, -1 on a malformed image. `name` is used in messages. */
int  ime_table_open_mem(ime_table *t, const void *buf, size_t size,
                        int expect_scheme, const char *name);

void ime_table_close(ime_table *t);

/* Fill `out` (capacity `max`) with candidates whose code starts with `query`
   (lowercase a-z, length 1..IME_CODE_LEN). Results are ordered exact-matches
   first (by rank), then prefix extensions (by code then rank). Returns the
   number written. */
int  ime_lookup(const ime_table *t, const char *query, size_t qlen,
                ime_cand *out, int max);

#endif /* WUBI_IME_TABLE_H */
