# wubi-ime - Chinese IME front-end processor for tmux / any terminal.

CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -std=c11
PYTHON  ?= python3

# forkpty lives in libutil on Linux; on macOS/BSD it is in libSystem/libc.
UNAME := $(shell uname -s)
ifeq ($(UNAME),Linux)
LDLIBS += -lutil
endif

# The IME tables are compiled into the binary (tables_embed.c) so wubi-ime is a
# single self-contained file that can be copied anywhere with no external .tab.
OBJS = wubi-ime.o table.o tables_embed.o
BIN  = wubi-ime

TABLES = wubi.tab pinyin.tab

.PHONY: all tables clean distclean

all: $(BIN)

$(BIN): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $(OBJS) $(LDLIBS)

wubi-ime.o: wubi-ime.c table.h tables_embed.h
	$(CC) $(CFLAGS) -c -o $@ wubi-ime.c

table.o: table.c table.h
	$(CC) $(CFLAGS) -c -o $@ table.c

tables_embed.o: tables_embed.c table.h tables_embed.h
	$(CC) $(CFLAGS) -c -o $@ tables_embed.c

# Auto-generated: the tables baked into the binary. Rebuilt whenever a .tab or
# the embedder changes. The .tab files themselves are (re)generated on demand.
tables_embed.c: gen_embed.py $(TABLES)
	$(PYTHON) gen_embed.py --out $@ wubi_tab:wubi.tab:0 pinyin_tab:pinyin.tab:1

# Regenerate the binary IME tables from the rime dictionaries.
tables: $(TABLES)

wubi.tab: gen_table.py wubi86.dict.yaml
	$(PYTHON) gen_table.py --scheme wubi --src wubi86.dict.yaml --out $@

pinyin.tab: gen_table.py pinyin_simp.dict.yaml
	$(PYTHON) gen_table.py --scheme pinyin --src pinyin_simp.dict.yaml --out $@

clean:
	rm -f $(OBJS) $(BIN) tables_embed.c

# `make distclean` also drops the generated tables.
distclean: clean
	rm -f $(TABLES)
