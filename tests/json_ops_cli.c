/* Test harness for the same production helpers, built with ASan and UBSan. */
#include "../speaker/src/json_ops.h"
#ifdef XIAOAI_TEST_AIRPLAY
#include "../speaker/src/airplay.h"
#include <stdio.h>
#include <unistd.h>
#endif
#include <string.h>

int main(int argc, char **argv) {
#ifdef XIAOAI_TEST_AIRPLAY
    if (argc == 2 && !strcmp(argv[1], "--airplay-metadata"))
        return bridge_airplay_metadata();
    if (argc == 4 && !strcmp(argv[1], "--test-artwork")) {
        size_t size;
        const char *mime;
        int fd = bridge_airplay_artwork_open(argv[2], argv[3], &size, &mime);
        if (fd < 0)
            return 2;
        printf("%zu %s\n", size, mime);
        close(fd);
        return 0;
    }
#endif
    if (argc >= 3 && !strcmp(argv[1], "--json-op"))
        return bridge_json_cli(argc - 2, argv + 2);
    if (argc == 4 && !strcmp(argv[1], "--trace-pcm"))
        return bridge_trace_pcm(argv[2], argv[3]);
    return 2;
}
