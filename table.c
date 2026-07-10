/* table.c - see table.h. Loads a `.tab` produced by gen_table.py. */
#include "table.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

static const char MAGIC[4] = {'I', 'M', 'E', 'T'};

/* Header layout mirror (see gen_table.py):
     magic[4] version[1] scheme[1] reserved[2] count[4] poolBytes[4]  = 16
     index[677 * u32]
     records[count * 12]
     pool[poolBytes]                                                          */
#define HDR_SIZE   16
#define IDX_SIZE   (IME_INDEX_ENTRIES * 4)

int ime_table_open(ime_table *t, const char *path, int expect_scheme)
{
    memset(t, 0, sizeof(*t));

    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        fprintf(stderr, "wubi-ime: open %s: %s\n", path, strerror(errno));
        return -1;
    }
    struct stat st;
    if (fstat(fd, &st) < 0) {
        fprintf(stderr, "wubi-ime: fstat %s: %s\n", path, strerror(errno));
        close(fd);
        return -1;
    }
    if ((size_t)st.st_size < HDR_SIZE + IDX_SIZE) {
        fprintf(stderr, "wubi-ime: %s too small to be a table\n", path);
        close(fd);
        return -1;
    }

    void *m = mmap(NULL, st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (m == MAP_FAILED) {
        fprintf(stderr, "wubi-ime: mmap %s: %s\n", path, strerror(errno));
        return -1;
    }

    const uint8_t *b = (const uint8_t *)m;
    if (memcmp(b, MAGIC, 4) != 0) {
        fprintf(stderr, "wubi-ime: %s: bad magic\n", path);
        munmap(m, st.st_size);
        return -1;
    }
    uint8_t  scheme = b[5];
    uint32_t count, pool_bytes;
    memcpy(&count, b + 8, 4);
    memcpy(&pool_bytes, b + 12, 4);

    size_t need = (size_t)HDR_SIZE + IDX_SIZE
                + (size_t)count * sizeof(ime_record) + pool_bytes;
    if (need > (size_t)st.st_size) {
        fprintf(stderr, "wubi-ime: %s: truncated (need %zu, have %lld)\n",
                path, need, (long long)st.st_size);
        munmap(m, st.st_size);
        return -1;
    }
    if (expect_scheme >= 0 && scheme != expect_scheme) {
        fprintf(stderr, "wubi-ime: %s: scheme %u, expected %d\n",
                path, scheme, expect_scheme);
        munmap(m, st.st_size);
        return -1;
    }

    t->base       = b;
    t->size       = st.st_size;
    t->scheme     = scheme;
    t->count      = count;
    t->pool_bytes = pool_bytes;
    t->index      = (const uint32_t *)(b + HDR_SIZE);
    t->records    = (const ime_record *)(b + HDR_SIZE + IDX_SIZE);
    t->pool       = (const char *)(b + HDR_SIZE + IDX_SIZE
                                     + (size_t)count * sizeof(ime_record));
    return 0;
}

void ime_table_close(ime_table *t)
{
    if (t->base)
        munmap((void *)t->base, t->size);
    memset(t, 0, sizeof(*t));
}

/* Compare a record's fixed-width code against a query. Returns <0/0/>0 like
   strcmp over the first min(code_len, qlen) chars, treating NUL-padding as
   end-of-code. */
static int code_cmp(const char *code, const char *q, size_t qlen)
{
    for (size_t i = 0; i < IME_CODE_LEN && i < qlen; i++) {
        unsigned char rc = (unsigned char)code[i];
        unsigned char qc = (unsigned char)q[i];
        if (rc == 0)               /* code shorter than query */
            return -1;
        if (rc != qc)
            return (rc < qc) ? -1 : 1;
    }
    return 0; /* code matches query for the query's full length (prefix hit) */
}

/* Binary-search the [lo,hi) record window for the first index whose code is a
   prefix-match-or-greater than the query. */
static uint32_t lower_bound(const ime_table *t, uint32_t lo, uint32_t hi,
                            const char *q, size_t qlen)
{
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        if (code_cmp(t->records[mid].code, q, qlen) < 0)
            lo = mid + 1;
        else
            hi = mid;
    }
    return lo;
}

int ime_lookup(const ime_table *t, const char *query, size_t qlen,
               ime_cand *out, int max)
{
    if (qlen == 0 || qlen > IME_CODE_LEN || max <= 0)
        return 0;

    int c0 = query[0] - 'a';
    int c1 = (qlen >= 2) ? (query[1] - 'a') : 0;
    if (c0 < 0 || c0 > 25 || c1 < 0 || c1 > 25)
        return 0;

    /* Prefix index gives a coarse window. For a 1-letter query, the whole
       first-letter block spans index[c0*26] .. index[(c0+1)*26]. For >=2
       letters, the two-letter cell index[c0*26+c1] .. next cell. */
    uint32_t win_lo, win_hi;
    if (qlen == 1) {
        win_lo = t->index[c0 * 26];
        win_hi = (c0 + 1 <= 25) ? t->index[(c0 + 1) * 26] : t->count;
    } else {
        int k = c0 * 26 + c1;
        win_lo = t->index[k];
        win_hi = (k + 1 < IME_INDEX_ENTRIES) ? t->index[k + 1] : t->count;
    }
    if (win_lo > t->count) win_lo = t->count;
    if (win_hi > t->count) win_hi = t->count;

    /* Narrow to records that actually prefix-match the query. */
    uint32_t lo = lower_bound(t, win_lo, win_hi, query, qlen);

    /* First pass: exact-code matches (code length == qlen). Then prefix
       extensions. Both passes preserve the on-disk (code, rank) order. */
    int n = 0;
    for (uint32_t i = lo; i < win_hi && n < max; i++) {
        const ime_record *r = &t->records[i];
        if (code_cmp(r->code, query, qlen) != 0)
            break; /* window is sorted; no more prefix hits */
        int exact = (r->code[qlen] == '\0' || qlen == IME_CODE_LEN) ? 1 : 0;
        if (!exact)
            continue;
        out[n].word     = t->pool + r->pool_off;
        out[n].word_len = r->word_len;
        out[n].rank     = r->rank;
        out[n].exact    = 1;
        n++;
    }
    for (uint32_t i = lo; i < win_hi && n < max; i++) {
        const ime_record *r = &t->records[i];
        if (code_cmp(r->code, query, qlen) != 0)
            break;
        int exact = (r->code[qlen] == '\0' || qlen == IME_CODE_LEN) ? 1 : 0;
        if (exact)
            continue;
        out[n].word     = t->pool + r->pool_off;
        out[n].word_len = r->word_len;
        out[n].rank     = r->rank;
        out[n].exact    = 0;
        n++;
    }
    return n;
}
