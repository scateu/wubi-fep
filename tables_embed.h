/* tables_embed.h - interface to the IME tables compiled into the binary.
   The array itself is defined in the auto-generated tables_embed.c. */
#ifndef WUBI_IME_TABLES_EMBED_H
#define WUBI_IME_TABLES_EMBED_H

#include <stddef.h>

typedef struct {
    const char          *name;    /* e.g. "wubi.tab" (for messages) */
    int                  scheme;  /* IME_SCHEME_* */
    const unsigned char *data;    /* table image */
    size_t               size;    /* bytes */
} embedded_table;

extern const embedded_table embedded_tables[];
extern const int embedded_tables_count;

#endif /* WUBI_IME_TABLES_EMBED_H */
