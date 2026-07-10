# wubi-ime - Chinese IME front-end processor for tmux / any terminal.

CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -std=c11
PYTHON  ?= python3

# forkpty lives in libutil on Linux; on macOS/BSD it is in libSystem/libc.
UNAME := $(shell uname -s)
ifeq ($(UNAME),Linux)
LDLIBS += -lutil
endif

OBJS = wubi-ime.o table.o
BIN  = wubi-ime

TABLES = wubi.tab pinyin.tab

.PHONY: all tables clean

all: $(BIN)

$(BIN): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $(OBJS) $(LDLIBS)

wubi-ime.o: wubi-ime.c table.h
	$(CC) $(CFLAGS) -c -o $@ wubi-ime.c

table.o: table.c table.h
	$(CC) $(CFLAGS) -c -o $@ table.c

# Regenerate the binary IME tables from the rime dictionaries.
tables: $(TABLES)

wubi.tab: gen_table.py wubi86.dict.yaml
	$(PYTHON) gen_table.py --scheme wubi --src wubi86.dict.yaml --out $@

pinyin.tab: gen_table.py pinyin_simp.dict.yaml
	$(PYTHON) gen_table.py --scheme pinyin --src pinyin_simp.dict.yaml --out $@

clean:
	rm -f $(OBJS) $(BIN)

# `make distclean` also drops the generated tables.
distclean: clean
	rm -f $(TABLES)
