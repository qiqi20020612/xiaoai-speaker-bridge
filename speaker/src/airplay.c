#define _POSIX_C_SOURCE 200809L
#include "airplay.h"
#include "json.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* dbus-send pretty-prints typed values. Convert its bounded, read-only reply to
 * JSON before selecting an allowlist. Never source/eval it or accept partial
 * structures. dbus-send prints string contents literally: embedded quotes and
 * backslashes are data; multiline/control strings safely reject this sample. */
struct parser {
    const char *at;
    char json[32768];
    size_t used;
};
static void spaces(struct parser *p) {
    while (isspace((unsigned char)*p->at))
        p->at++;
}
static int word(struct parser *p, const char *s) {
    spaces(p);
    size_t n = strlen(s);
    if (strncmp(p->at, s, n) || (isalnum((unsigned char)p->at[n]) || p->at[n] == '_'))
        return 0;
    p->at += n;
    return 1;
}
static int emit(struct parser *p, const char *s) {
    size_t n = strlen(s);
    if (p->used + n >= sizeof p->json)
        return -1;
    memcpy(p->json + p->used, s, n + 1);
    p->used += n;
    return 0;
}
static int quoted(struct parser *p) {
    spaces(p);
    if (*p->at++ != '"')
        return -1;
    const char *start = p->at, *end = strchr(start, '\n');
    if (!end)
        end = start + strlen(start);
    while (end > start && isspace((unsigned char)end[-1]))
        end--;
    if (end <= start || end[-1] != '"' || end - start > 4096)
        return -1;
    for (const char *s = start; s < end - 1; s++)
        if ((unsigned char)*s < 32)
            return -1;
    char raw[4096], json[8192];
    size_t n = (size_t)(end - start - 1);
    memcpy(raw, start, n);
    raw[n] = 0;
    if (json_quote(raw, json, sizeof json) < 0 || emit(p, json) < 0)
        return -1;
    p->at = end;
    return 0;
}
static int value(struct parser *p, unsigned depth) {
    if (depth > 16)
        return -1;
    if (word(p, "variant"))
        return value(p, depth + 1);
    if (word(p, "array")) {
        spaces(p);
        if (*p->at++ != '[')
            return -1;
        spaces(p);
        int dict = !strncmp(p->at, "dict", 4), count = 0;
        if (emit(p, dict ? "{" : "[") < 0)
            return -1;
        while (*p->at != ']') {
            if (++count > 256 || (count > 1 && emit(p, ",") < 0))
                return -1;
            if (dict) {
                if (!word(p, "dict") || !word(p, "entry"))
                    return -1;
                spaces(p);
                if (*p->at++ != '(' || !word(p, "string") || quoted(p) < 0 || emit(p, ":") < 0 ||
                    value(p, depth + 1) < 0)
                    return -1;
                spaces(p);
                if (*p->at++ != ')')
                    return -1;
            } else if (value(p, depth + 1) < 0)
                return -1;
            spaces(p);
        }
        p->at++;
        return emit(p, dict ? "}" : "]");
    }
    if (word(p, "string"))
        return quoted(p);
    if (word(p, "object")) {
        if (!word(p, "path"))
            return -1;
        return quoted(p);
    }
    if (word(p, "boolean")) {
        if (word(p, "true"))
            return emit(p, "true");
        if (word(p, "false"))
            return emit(p, "false");
        return -1;
    }
    if (!(word(p, "int64") || word(p, "uint64") || word(p, "int32") || word(p, "uint32") ||
          word(p, "int16") || word(p, "uint16") || word(p, "byte") || word(p, "double")))
        return -1;
    spaces(p);
    const char *start = p->at;
    while (*p->at && !isspace((unsigned char)*p->at) && *p->at != ']' && *p->at != ')')
        p->at++;
    size_t n = (size_t)(p->at - start);
    if (!n || n >= 64)
        return -1;
    char number[64];
    memcpy(number, start, n);
    number[n] = 0;
    return emit(p, number);
}
static int copy_string(const struct json_doc *d, int object, const char *key, const char *output) {
    char raw[513], quoted_value[1028];
    int i = json_get(d, object, key);
    if (i < 0 || json_string(d, i, raw, sizeof raw) < 0 || !*raw)
        return 0;
    if (json_quote(raw, quoted_value, sizeof quoted_value) < 0)
        return -1;
    printf(",\"%s\":%s", output, quoted_value);
    return 0;
}
int bridge_airplay_artwork_id(const char *name) {
    if (strlen(name) != 42 || strncmp(name, "cover-", 6) ||
        (strcmp(name + 38, ".jpg") && strcmp(name + 38, ".png")))
        return 0;
    for (int i = 6; i < 38; i++)
        if (!isdigit((unsigned char)name[i]) && !(name[i] >= 'a' && name[i] <= 'f'))
            return 0;
    return 1;
}
static int progress(const char *s, uint32_t *start, uint32_t *now, uint32_t *end) {
    uint32_t *parts[] = {start, now, end};
    for (int i = 0; i < 3; i++) {
        if (!isdigit((unsigned char)*s))
            return -1;
        char *tail;
        errno = 0;
        unsigned long long n = strtoull(s, &tail, 10);
        if (errno || n > UINT32_MAX || (i < 2 ? *tail != '/' : *tail != 0))
            return -1;
        *parts[i] = (uint32_t)n;
        s = tail + (i < 2);
    }
    return 0;
}
int bridge_airplay_metadata(void) {
    char *input = malloc(65537);
    struct parser *p = calloc(1, sizeof *p);
    struct json_doc *d = malloc(sizeof *d);
    int result = 2;
    if (!input || !p || !d)
        goto done;
    size_t n = fread(input, 1, 65536, stdin);
    if (ferror(stdin) || n == 65536 || memchr(input, 0, n))
        goto done;
    input[n] = 0;
    p->at = input;
    if (!strncmp(input, "method return ", 14)) {
        p->at = strchr(input, '\n');
        if (!p->at)
            goto done;
    }
    if (value(p, 0) < 0)
        goto done;
    spaces(p);
    if (*p->at || json_parse(d, p->json, p->used) < 0 || d->t[0].type != J_OBJECT)
        goto done;
    int metadata = json_get(d, 0, "Metadata");
    if (metadata < 0 || d->t[metadata].type != J_OBJECT) {
        puts("{}");
        result = 0;
        goto done;
    }
    char uri[1025], raw_progress[128] = "", track[257];
    double duration = -1, kind = 0;
    json_number(d, json_get(d, metadata, "sps:songdatakind"), &kind);
    double length;
    if (kind != 1 && json_number(d, json_get(d, metadata, "mpris:length"), &length) == 0 &&
        length > 0 && length <= 604800000000.0)
        duration = length / 1000000.0;
    fputs("{\"song\":{", stdout);
    /* A leading bounded field keeps subsequent optional fields simple. */
    fputs("\"file\":", stdout);
    if (json_string(d, json_get(d, metadata, "mpris:trackid"), track, sizeof track) >= 0) {
        char q[516];
        json_quote(track, q, sizeof q);
        fputs(q, stdout);
    } else
        fputs("null", stdout);
    copy_string(d, metadata, "xesam:title", "title");
    copy_string(d, metadata, "xesam:album", "album");
    int artists = json_get(d, metadata, "xesam:artist");
    if (artists >= 0 && d->t[artists].type == J_ARRAY && d->t[artists].count <= 8) {
        char artist[257], q[516];
        int valid = 1;
        for (int i = artists + 1; i < d->t[artists].next; i = d->t[i].next)
            if (json_string(d, i, artist, sizeof artist) < 0)
                valid = 0;
        if (valid) {
            fputs(",\"artist\":[", stdout);
            int count = 0;
            for (int i = artists + 1; i < d->t[artists].next; i = d->t[i].next) {
                json_string(d, i, artist, sizeof artist);
                json_quote(artist, q, sizeof q);
                printf("%s%s", count++ ? "," : "", q);
            }
            fputs("]", stdout);
        }
    }
    fputs("},\"status\":{", stdout);
    uint32_t start, now, end;
    int valid_progress =
        kind != 1 &&
        json_string(d, json_get(d, 0, "ProgressString"), raw_progress, sizeof raw_progress) >= 0 &&
        progress(raw_progress, &start, &now, &end) == 0;
    double elapsed = -1;
    if (valid_progress) {
        uint32_t span = end - start, at = now - start;
        /* 4.3.5 rtsp.c defines these RTP stamps at 44100 frames/s. Reject
         * ambiguous half-cycle spans, malformed/old progress for a new song. */
        double seconds = span / 44100.0;
        if (span > 0 && span < INT32_MAX && at <= span &&
            (duration < 0 || (seconds > duration - 2 && seconds < duration + 2))) {
            elapsed = at / 44100.0;
            if (duration < 0)
                duration = seconds;
        } else
            valid_progress = 0;
    }
    if (duration >= 0)
        printf("\"duration\":%.6f", duration);
    if (elapsed >= 0)
        printf("%s\"elapsed\":%.6f", duration >= 0 ? "," : "", elapsed);
    fputs("}", stdout);
    if (valid_progress)
        printf(",\"progress\":\"%s\"", raw_progress);
    if (json_string(d, json_get(d, metadata, "mpris:artUrl"), uri, sizeof uri) >= 0) {
        const char *path = !strncmp(uri, "file://", 7) ? uri + 7 : uri;
        size_t prefix = strlen(AIRPLAY_ARTWORK_DIRECTORY);
        if (!strncmp(path, AIRPLAY_ARTWORK_DIRECTORY, prefix) && path[prefix] == '/' &&
            bridge_airplay_artwork_id(path + prefix + 1))
            printf(",\"artwork_id\":\"%s\"", path + prefix + 1);
    }
    puts("}");
    result = 0;
done:
    free(input);
    free(p);
    free(d);
    return result;
}
int bridge_airplay_artwork_open(const char *directory, const char *id, size_t *size,
                                const char **mime) {
    if (!bridge_airplay_artwork_id(id) || directory[0] != '/' || strlen(directory) >= 1024)
        return -1;
    int fd = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    char path[1024];
    strcpy(path, directory + 1);
    char *save, *part = strtok_r(path, "/", &save);
    while (part) {
        if (!strcmp(part, ".") || !strcmp(part, "..")) {
            close(fd);
            return -1;
        }
        int next = openat(fd, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        close(fd);
        if (next < 0)
            return -1;
        fd = next;
        part = strtok_r(NULL, "/", &save);
    }
    int image = openat(fd, id, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    close(fd);
    if (image < 0)
        return -1;
    struct stat st;
    unsigned char magic[8];
    if (fstat(image, &st) < 0 || !S_ISREG(st.st_mode) || st.st_nlink != 1 || st.st_size < 8 ||
        st.st_size > AIRPLAY_ARTWORK_LIMIT || pread(image, magic, sizeof magic, 0) != sizeof magic)
        goto bad;
    if (!strcmp(id + 38, ".png") && !memcmp(magic, "\x89PNG\r\n\x1a\n", 8))
        *mime = "image/png";
    else if (!strcmp(id + 38, ".jpg") && !memcmp(magic, "\xff\xd8\xff", 3))
        *mime = "image/jpeg";
    else
        goto bad;
    *size = (size_t)st.st_size;
    return image;
bad:
    close(image);
    return -1;
}
