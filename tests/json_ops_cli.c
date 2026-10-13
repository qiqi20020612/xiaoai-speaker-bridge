/* Test harness for the same production helpers, built with ASan and UBSan. */
#include "../speaker/src/json_ops.h"
#include <string.h>

int main(int argc, char **argv) {
    if (argc >= 3 && !strcmp(argv[1], "--json-op"))
        return bridge_json_cli(argc - 2, argv + 2);
    if (argc == 4 && !strcmp(argv[1], "--trace-pcm"))
        return bridge_trace_pcm(argv[2], argv[3]);
    return 2;
}
