CC ?= cc
CFLAGS ?= -std=c11 -Os -Wall -Wextra -Werror -Wformat=2 -fstack-protector-strong
CPPFLAGS ?= -D_DEFAULT_SOURCE -D_DARWIN_C_SOURCE
LDFLAGS ?=
OUT ?= ../build/native/xiaoai_bridge

all: $(OUT)

$(OUT): src/bridge.c src/json.c src/json.h src/json_ops.c src/json_ops.h src/airplay.c src/airplay.h src/airplay_volume.c src/airplay_volume.h
	mkdir -p $(dir $(OUT))
	$(CC) $(CPPFLAGS) $(CFLAGS) src/bridge.c src/json.c src/json_ops.c src/airplay.c src/airplay_volume.c $(LDFLAGS) -o $(OUT)

check:
	mkdir -p ../build
	$(CC) $(CPPFLAGS) $(CFLAGS) -fsanitize=address,undefined -g src/json.c ../tests/json_test.c -o ../build/json_test
	../build/json_test
	$(CC) $(CPPFLAGS) $(CFLAGS) -DXIAOAI_TEST_AIRPLAY -fsanitize=address,undefined -g src/json.c src/json_ops.c src/airplay.c src/airplay_volume.c ../tests/json_ops_cli.c -o ../build/json_ops_sanitized
	printf '%s\n' '{"revision":0}' | ../build/json_ops_sanitized --json-op desired-update master 50

clean:
	rm -f $(OUT)
.PHONY: all check clean
