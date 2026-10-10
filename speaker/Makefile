CC ?= cc
CFLAGS ?= -std=c11 -Os -Wall -Wextra -Werror -Wformat=2 -fstack-protector-strong
CPPFLAGS ?= -D_DEFAULT_SOURCE -D_DARWIN_C_SOURCE
LDFLAGS ?=
OUT ?= ../build/native/xiaoai_bridge

all: $(OUT)

$(OUT): src/bridge.c src/json.c src/json.h
	mkdir -p $(dir $(OUT))
	$(CC) $(CPPFLAGS) $(CFLAGS) src/bridge.c src/json.c $(LDFLAGS) -o $(OUT)

check:
	mkdir -p ../build
	$(CC) $(CPPFLAGS) $(CFLAGS) -fsanitize=address,undefined -g src/json.c ../tests/json_test.c -o ../build/json_test
	../build/json_test

clean:
	rm -f $(OUT)
.PHONY: all check clean
