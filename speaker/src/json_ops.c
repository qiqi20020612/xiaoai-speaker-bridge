/* Small fixed schemas for the audio critical path. No jq interpreter, config
 * load, locks, network, hardware access or persistent writes in these helpers.
 * The shell caller keeps FD 7 and atomically commits validated output. */
#define _POSIX_C_SOURCE 200809L
#include "json_ops.h"
#include "json.h"
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define LIMIT 65536
struct document {
    struct json_doc d;
    char *data;
};

static int load(struct document *doc, const char *path, int optional) {
    int fd = path ? open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC) : STDIN_FILENO;
    if (path && fd < 0 && optional && (!*path || errno == ENOENT)) {
        doc->data = strdup("{}");
        return doc->data ? json_parse(&doc->d, doc->data, 2) : -1;
    }
    struct stat st;
    if (fd < 0 || (path && (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode)))) {
        if (path && fd >= 0)
            close(fd);
        return -1;
    }
    doc->data = malloc(LIMIT + 1);
    if (!doc->data) {
        if (path)
            close(fd);
        return -1;
    }
    size_t used = 0;
    for (;;) {
        ssize_t n = read(fd, doc->data + used, LIMIT + 1 - used);
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 || used + (size_t)n > LIMIT) {
            if (path)
                close(fd);
            return -1;
        }
        if (!n)
            break;
        used += (size_t)n;
    }
    if (path)
        close(fd);
    doc->data[used] = 0;
    return json_parse(&doc->d, doc->data, used);
}
static int literal(struct document *doc, const char *s) {
    if (strlen(s) > LIMIT || !(doc->data = strdup(s)))
        return -1;
    return json_parse(&doc->d, doc->data, strlen(s));
}
static int object(const struct document *doc, int i) {
    return i >= 0 && i < doc->d.used && doc->d.t[i].type == J_OBJECT;
}
static int field(const struct document *doc, int i, const char *key) {
    return json_get(&doc->d, i, key);
}
static int number(const struct document *doc, int i, double *n, double fallback) {
    if (i < 0 || doc->d.t[i].type == J_NULL) {
        *n = fallback;
        return 0;
    }
    return json_number(&doc->d, i, n);
}
static int numeric(const char *s, double *n, int nullable) {
    struct json_doc d;
    if (nullable && strcmp(s, "null") == 0) {
        *n = -1;
        return 0;
    }
    return json_parse(&d, s, strlen(s)) == 0 && json_number(&d, 0, n) == 0 && (!nullable || *n >= 0)
               ? 0
               : -1;
}
static int counter(double n) {
    return n >= 0 && n <= 9007199254740991.0 && (double)(uint64_t)n == n;
}
static int starttime(const char *s) {
    return strlen(s) < 64 && strspn(s, "0123456789") == strlen(s);
}
static int volume(double n) {
    return n >= 0 && n <= 100;
}
static int optional_volume(const struct document *doc, int i, double *n) {
    if (i < 0 || doc->d.t[i].type == J_NULL) {
        *n = -1;
        return 0;
    }
    return json_number(&doc->d, i, n) == 0 && volume(*n) ? 0 : -1;
}
static int string(const struct document *doc, int i, char *s, size_t n) {
    if (i < 0 || doc->d.t[i].type == J_NULL) {
        s[0] = 0;
        return 0;
    }
    return json_string(&doc->d, i, s, n);
}
static int token(const struct document *doc, int i) {
    if (i < 0 || i >= doc->d.used)
        return -1;
    return fwrite(doc->data + doc->d.t[i].start, 1, (size_t)(doc->d.t[i].end - doc->d.t[i].start),
                  stdout) == (size_t)(doc->d.t[i].end - doc->d.t[i].start)
               ? 0
               : -1;
}
static int quoted(const char *s) {
    size_t length = strlen(s);
    if (length > LIMIT)
        return -1;
    size_t size = length * 6 + 3;
    char *out = malloc(size);
    if (!out)
        return -1;
    struct json_doc check;
    int n = json_quote(s, out, size);
    int result = n < 0 || json_parse(&check, out, (size_t)n) < 0 ? -1 : 0;
    if (!result && fputs(out, stdout) == EOF)
        result = -1;
    free(out);
    return result;
}
/* The caller prints replacements first. Preserve all unrelated fields, including
 * journal metadata, without using string substitutions or evaluating input. */
static int remaining(const struct document *doc, int i, const char *const *skip) {
    if (!object(doc, i))
        return -1;
    for (int k = i + 1; k < doc->d.t[i].next;) {
        char key[256];
        if (json_string(&doc->d, k, key, sizeof key) < 0)
            return -1;
        int omit = 0;
        for (int j = 0; skip[j]; j++)
            if (strcmp(skip[j], key) == 0)
                omit = 1;
        if (!omit) {
            putchar(',');
            if (token(doc, k) < 0)
                return -1;
            putchar(':');
            if (token(doc, k + 1) < 0)
                return -1;
        }
        k = doc->d.t[k + 1].next;
    }
    return 0;
}
static int channel(const char *s) {
    return strcmp(s, "master") == 0 || strcmp(s, "bluetooth") == 0 || strcmp(s, "tts") == 0 ||
           strcmp(s, "headphone") == 0;
}
static int volumes(const struct document *doc, int i) {
    if (!object(doc, i))
        return -1;
    for (int k = i + 1; k < doc->d.t[i].next; k = doc->d.t[k + 1].next) {
        char key[256];
        double n;
        if (json_string(&doc->d, k, key, sizeof key) < 0 || !channel(key) ||
            json_number(&doc->d, k + 1, &n) < 0 || !volume(n))
            return -1;
    }
    return 0;
}
static int snapshot(struct document *base, char **a) {
    const char *names[] = {"airplay", "bluetooth", "mpd"};
    int active[] = {strcmp(a[0], "true") == 0, strcmp(a[1], "true") == 0,
                    strcmp(a[2], "play") == 0};
    int observed[] = {strcmp(a[3], "null") == 0 ? -1 : strcmp(a[3], "true") == 0,
                      strcmp(a[4], "null") == 0 ? -1 : strcmp(a[4], "true") == 0,
                      strcmp(a[2], "unknown") == 0 ? -1 : active[2]};
    for (int i = 0; i < 2; i++)
        if (strcmp(a[i], "true") && strcmp(a[i], "false"))
            return -1;
    for (int i = 3; i < 5; i++)
        if (strcmp(a[i], "true") && strcmp(a[i], "false") && strcmp(a[i], "null"))
            return -1;
    double pid, master, bt;
    if (!object(base, 0) || numeric(a[5], &pid, 0) < 0 || !counter(pid) ||
        numeric(a[6], &master, 1) < 0 || (master != -1 && !volume(master)) ||
        numeric(a[7], &bt, 1) < 0 || (bt != -1 && !volume(bt)))
        return -1;
    char old[32];
    if (string(base, field(base, 0, "source"), old, sizeof old) < 0)
        return -1;
    int count = active[0] + active[1] + active[2], selected = -1;
    for (int i = 0; i < 3; i++)
        if (active[i] && (!strcmp(old, names[i]) || count == 1))
            selected = i;
    int priority[3] = {0, 1, 2}, length = 3;
    int p = field(base, 0, "priority");
    if (p >= 0 && base->d.t[p].type != J_NULL) {
        if (base->d.t[p].type != J_ARRAY || base->d.t[p].count > 3)
            return -1;
        length = 0;
        for (int k = p + 1; k < base->d.t[p].next; k = base->d.t[k].next) {
            char name[32];
            if (string(base, k, name, sizeof name) < 0)
                return -1;
            int found = -1;
            for (int i = 0; i < 3; i++)
                if (!strcmp(name, names[i]))
                    found = i;
            if (found < 0)
                return -1;
            priority[length++] = found;
        }
    }
    int uncertain = count > 1, position = -1;
    for (int i = 0; i < length; i++)
        if (priority[i] == selected && position < 0)
            position = i;
    for (int i = 0; i < length; i++)
        if (observed[priority[i]] < 0 && (selected < 0 || (position >= 0 && i < position)))
            uncertain = 1;
    printf("{\"source\":");
    if (selected < 0)
        fputs("null", stdout);
    else if (quoted(names[selected]) < 0)
        return -1;
    printf(",\"uncertain\":%s,\"active_sources\":[", uncertain ? "true" : "false");
    int comma = 0;
    for (int i = 0; i < 3; i++)
        if (active[i]) {
            if (comma++)
                putchar(',');
            if (quoted(names[i]) < 0)
                return -1;
        }
    fputs("],\"observed_sources\":{", stdout);
    for (int i = 0; i < 3; i++)
        printf("%s\"%s\":%s", i ? "," : "", names[i],
               observed[i] < 0 ? "null"
               : observed[i]   ? "true"
                               : "false");
    putchar('}');
    if (pid > 0) {
        printf(",\"type\":\"voice_begin\",\"pid\":%.0f,\"volumes\":{\"master\":", pid);
        if (master < 0)
            fputs("null", stdout);
        else
            printf("%.17g", master);
        fputs(",\"bluetooth\":", stdout);
        if (bt < 0)
            fputs("null", stdout);
        else
            printf("%.17g", bt);
        putchar('}');
    }
    const char *const skip_voice[] = {"source", "uncertain", "active_sources", "observed_sources",
                                      "type",   "pid",       "volumes",        NULL};
    const char *const skip_plain[] = {"source", "uncertain", "active_sources", "observed_sources",
                                      NULL};
    if (remaining(base, 0, pid > 0 ? skip_voice : skip_plain) < 0)
        return -1;
    puts("}");
    return 0;
}
static int journal(struct document *desired, struct document *duck, struct document *values,
                   char **a, int have_duck) {
    double revision, owner, current;
    if (!object(desired, 0) || !object(values, 0) || volumes(values, 0) < 0 ||
        number(desired, field(desired, 0, "revision"), &revision, 0) < 0 || !counter(revision) ||
        numeric(a[3], &owner, 0) < 0 || !counter(owner) || owner <= 1 || !starttime(a[4]) ||
        numeric(a[7], &current, 1) < 0 || (current != -1 && !volume(current)) ||
        strlen(a[5]) >= 128 || (strcmp(a[6], "") && strcmp(a[6], "true") && strcmp(a[6], "false")))
        return -1;
    const char *raw = a[5], *active = a[6];
    char duck_raw[128], duck_active[16];
    double applied = -1, master;
    int master_index = field(values, 0, "master"), override_master = 0;
    int sender_index = field(duck, 0, "sender_recovery");
    if (have_duck && sender_index >= 0 && duck->d.t[sender_index].type != J_BOOL)
        return -1;
    int sender_recovery = have_duck && json_bool(&duck->d, sender_index, 0);
    if (have_duck) {
        int v = field(duck, 0, "volumes");
        double target_version;
        if (!object(duck, 0) || volumes(duck, v) < 0 || master_index < 0 ||
            json_number(&duck->d, field(duck, 0, "revision"), &revision) < 0 ||
            !counter(revision) || json_number(&duck->d, field(duck, v, "master"), &master) < 0 ||
            optional_volume(duck, field(duck, 0, "applied_master"), &applied) < 0 ||
            string(duck, field(duck, 0, "airplay_raw"), duck_raw, sizeof duck_raw) < 0 ||
            string(duck, field(duck, 0, "airplay_active"), duck_active, sizeof duck_active) < 0 ||
            number(desired, field(desired, field(desired, 0, "versions"), "master"),
                   &target_version, 0) < 0 ||
            !counter(target_version))
            return -1;
        if (target_version > revision) {
            if (json_number(&desired->d, field(desired, field(desired, 0, "volumes"), "master"),
                            &master) < 0 ||
                !volume(master))
                return -1;
        }
        if (!*active || current == applied) {
            raw = duck_raw;
            active = duck_active;
        } else if ((sender_recovery || strcmp(raw, duck_raw) || strcmp(active, duck_active)) &&
                   target_version <= revision) {
            if (!volume(current))
                return -1;
            master = current;
            applied = -1;
        }
        override_master = 1;
    }
    printf("{\"owner\":%.0f,\"start\":", owner);
    if (quoted(a[4]) < 0)
        return -1;
    printf(",\"revision\":%.0f,\"airplay_raw\":", revision);
    if (quoted(raw) < 0)
        return -1;
    fputs(",\"airplay_active\":", stdout);
    if (quoted(active) < 0)
        return -1;
    if (applied != -1)
        printf(",\"applied_master\":%.17g", applied);
    if (sender_recovery)
        fputs(",\"sender_recovery\":true", stdout);
    fputs(",\"volumes\":", stdout);
    if (override_master) {
        const char *const skip[] = {"master", NULL};
        printf("{\"master\":%.17g", master);
        if (remaining(values, 0, skip) < 0)
            return -1;
        putchar('}');
    } else if (token(values, 0) < 0)
        return -1;
    puts("}");
    return 0;
}
static int recovery(struct document *saved, struct document *desired, char **a) {
    double revision, applied, current = -1;
    char raw[128], active[16];
    int v = field(saved, 0, "volumes");
    int sender_index = field(saved, 0, "sender_recovery");
    if (sender_index >= 0 && saved->d.t[sender_index].type != J_BOOL)
        return -1;
    int sender_recovery = json_bool(&saved->d, sender_index, 0);
    if (!object(saved, 0) || !object(desired, 0) || volumes(saved, v) < 0 ||
        json_number(&saved->d, field(saved, 0, "revision"), &revision) < 0 || !counter(revision) ||
        optional_volume(saved, field(saved, 0, "applied_master"), &applied) < 0 ||
        (*a[4] && (numeric(a[4], &current, 1) < 0 || (current != -1 && !volume(current)))) ||
        string(saved, field(saved, 0, "airplay_raw"), raw, sizeof raw) < 0 ||
        string(saved, field(saved, 0, "airplay_active"), active, sizeof active) < 0)
        return -1;
    for (int k = v + 1; k < saved->d.t[v].next; k = saved->d.t[k + 1].next) {
        char key[256];
        double version, n;
        if (json_string(&saved->d, k, key, sizeof key) < 0 ||
            number(desired, field(desired, field(desired, 0, "versions"), key), &version, 0) < 0 ||
            !counter(version) || json_number(&saved->d, k + 1, &n) < 0)
            return -1;
        if (version > revision) {
            if (json_number(&desired->d, field(desired, field(desired, 0, "volumes"), key), &n) <
                    0 ||
                !volume(n))
                return -1;
        } else if (!strcmp(key, "master") && sender_recovery) {
            /* A prior sender repair may have written the gain before dying or
             * observing another slider update. It must be verified, not mistaken
             * for an unrelated user's master write and silently discarded. */
            if (!strcmp(a[3], "false") && current >= 0 && current != applied)
                continue;
            printf("master\tpending\n");
            continue;
        } else if (!strcmp(key, "master") && (!strcmp(active, "true") || !strcmp(a[3], "true")) &&
                   (strcmp(raw, a[2]) || strcmp(active, a[3]))) {
            /* Compare JSON numbers, not their decimal formatting. Never infer
             * a sender target or use a tolerance around the applied cap. */
            if (!*a[3] || (applied != -1 && applied == current))
                printf("%s\tpending\n", key);
            continue;
        }
        printf("%s\t%.17g\n", key, n);
    }
    return 0;
}
static int desired_update(struct document *doc, char **a) {
    double level, revision;
    if ((!channel(a[0]) && strcmp(a[0], "airplay")) || numeric(a[1], &level, 0) < 0 ||
        !volume(level) || !object(doc, 0) ||
        number(doc, field(doc, 0, "revision"), &revision, 0) < 0 || !counter(revision) ||
        revision >= 9007199254740991.0)
        return -1;
    int v = field(doc, 0, "volumes"), versions = field(doc, 0, "versions");
    if ((v >= 0 && !object(doc, v) && doc->d.t[v].type != J_NULL) ||
        (versions >= 0 && !object(doc, versions) && doc->d.t[versions].type != J_NULL))
        return -1;
    const char *const skip_key[] = {a[0], NULL};
    printf("{\"revision\":%.0f,\"volumes\":{\"%s\":%.17g", revision + 1, a[0], level);
    if (object(doc, v) && remaining(doc, v, skip_key) < 0)
        return -1;
    printf("},\"versions\":{\"%s\":%.0f", a[0], revision + 1);
    if (object(doc, versions) && remaining(doc, versions, skip_key) < 0)
        return -1;
    putchar('}');
    const char *const skip[] = {"revision", "volumes", "versions", NULL};
    if (remaining(doc, 0, skip) < 0)
        return -1;
    puts("}");
    return 0;
}
int bridge_json_cli(int argc, char **argv) {
    struct document a = {0}, b = {0}, c = {0};
    int result = -1;
    const char *op = argv[0];
    char **args = argv + 1;
    if (argc == 1 && !strcmp(op, "validate"))
        result = load(&a, NULL, 0);
    else if (argc == 3 && !strcmp(op, "desired-update") && load(&a, NULL, 0) == 0)
        result = desired_update(&a, args);
    else if (argc == 9 && !strcmp(op, "snapshot") && load(&a, NULL, 0) == 0)
        result = snapshot(&a, args);
    else if (argc == 9 && !strcmp(op, "journal") && load(&a, args[0], 1) == 0 &&
             load(&b, args[1], 1) == 0 && literal(&c, args[2]) == 0)
        result = journal(&a, &b, &c, args, *args[1] != 0);
    else if (argc == 6 && !strcmp(op, "recovery-plan") && load(&a, args[0], 0) == 0 &&
             load(&b, args[1], 1) == 0)
        result = recovery(&a, &b, args);
    else if (argc == 2 && (!strcmp(op, "owner") || !strcmp(op, "journal-fields")) &&
             load(&a, args[0], 1) == 0 && object(&a, 0)) {
        double n;
        const char *name = !strcmp(op, "owner") ? "owner" : "revision";
        if (number(&a, field(&a, 0, name), &n, 0) == 0 && counter(n)) {
            result = 0;
            if (!strcmp(op, "journal-fields")) {
                double master;
                if (json_number(&a.d, field(&a, field(&a, 0, "volumes"), "master"), &master) < 0 ||
                    !volume(master))
                    result = -1;
                else
                    printf("%.17g\n", master);
            }
            if (!result)
                printf("%.0f\n", n);
            if (!strcmp(op, "owner")) {
                char start[64];
                if (string(&a, field(&a, 0, "start"), start, sizeof start) < 0 || !starttime(start))
                    result = -1;
                else
                    puts(start);
            }
        }
    } else if (argc == 1 && !strcmp(op, "sender-recovery") && load(&a, NULL, 0) == 0 &&
               object(&a, 0)) {
        const char *const skip[] = {"sender_recovery", NULL};
        fputs("{\"sender_recovery\":true", stdout);
        result = remaining(&a, 0, skip);
        puts("}");
    } else if (argc == 2 && !strcmp(op, "applied") && load(&a, NULL, 0) == 0 && object(&a, 0)) {
        double applied;
        if (numeric(args[0], &applied, 0) == 0 && volume(applied)) {
            const char *const skip[] = {"applied_master", NULL};
            printf("{\"applied_master\":%.17g", applied);
            result = remaining(&a, 0, skip);
            puts("}");
        }
    } else if (argc == 2 && !strcmp(op, "drop-channel") && channel(args[0]) &&
               load(&a, NULL, 0) == 0 && object(&a, 0)) {
        int v = field(&a, 0, "volumes");
        if (volumes(&a, v) == 0) {
            fputs("{\"volumes\":{", stdout);
            int comma = 0;
            for (int k = v + 1; k < a.d.t[v].next; k = a.d.t[k + 1].next) {
                char key[256];
                if (json_string(&a.d, k, key, sizeof key) < 0)
                    break;
                if (!strcmp(key, args[0]))
                    continue;
                if (comma++)
                    putchar(',');
                token(&a, k);
                putchar(':');
                token(&a, k + 1);
            }
            putchar('}');
            const char *const skip[] = {"volumes", NULL};
            result = remaining(&a, 0, skip);
            puts("}");
        }
    } else if (argc == 3 && !strcmp(op, "reply") && literal(&a, args[1]) == 0 && object(&a, 0)) {
        int source = field(&a, 0, "source"), uncertain = field(&a, 0, "uncertain");
        if ((source >= 0 && a.d.t[source].type != J_NULL && a.d.t[source].type != J_STRING) ||
            (uncertain >= 0 && a.d.t[uncertain].type != J_NULL && a.d.t[uncertain].type != J_BOOL))
            goto done;
        fputs("{\"reply_text\":", stdout);
        result = quoted(args[0]);
        if (!result) {
            fputs(",\"source\":", stdout);
            int s = field(&a, 0, "source");
            if (s < 0)
                fputs("null", stdout);
            else
                result = token(&a, s);
            fputs(",\"uncertain\":", stdout);
            int u = field(&a, 0, "uncertain");
            if (u < 0)
                fputs("null", stdout);
            else
                result |= token(&a, u);
            puts("}");
        }
    }
done:
    free(a.data);
    free(b.data);
    free(c.data);
    if (result < 0 || ferror(stdout)) {
        fputs("invalid_json_operation\n", stderr);
        return 2;
    }
    return 0;
}
/* Optional diagnostics only: the normal Listener never runs this helper.
 * Retain at most a fmt/chunk header, never PCM, and never delay forwarding.
 * A data frame here is after the original recorder's filters, not ALSA open. */
struct pcm_trace {
    unsigned char header[16];
    size_t have, need;
    uint64_t skip;
    unsigned frame;
    int stage;
};
static uint32_t pcm_u32(const unsigned char *p) {
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static int pcm_frame(struct pcm_trace *s, const unsigned char *data, size_t n) {
    while (n && s->stage >= 0) {
        if (s->stage == 3) {
            size_t take = n < s->need ? n : s->need;
            s->need -= take;
            if (!s->need) {
                s->stage = -1;
                return 1;
            }
            return 0;
        }
        if (s->stage == 1 && s->skip) {
            size_t take = s->skip < n ? (size_t)s->skip : n;
            s->skip -= take;
            data += take;
            n -= take;
            continue;
        }
        size_t take = s->need - s->have;
        if (take > n)
            take = n;
        memcpy(s->header + s->have, data, take);
        s->have += take;
        data += take;
        n -= take;
        if (s->have != s->need)
            continue;
        s->have = 0;
        if (s->stage == 0) {
            s->stage = memcmp(s->header, "RIFF", 4) || memcmp(s->header + 8, "WAVE", 4) ? -1 : 1;
            s->need = 8;
        } else if (s->stage == 2) {
            unsigned format = s->header[0] | (unsigned)s->header[1] << 8;
            unsigned channels = s->header[2] | (unsigned)s->header[3] << 8;
            unsigned align = s->header[12] | (unsigned)s->header[13] << 8;
            unsigned bits = s->header[14] | (unsigned)s->header[15] << 8;
            s->frame = format == 1 && channels && channels <= 64 &&
                               (bits == 8 || bits == 16 || bits == 24 || bits == 32) &&
                               align == channels * (bits / 8)
                           ? align
                           : 0;
            s->stage = 1;
            s->need = 8;
        } else {
            uint32_t length = pcm_u32(s->header + 4);
            s->skip = (uint64_t)length + (length & 1);
            if (!memcmp(s->header, "fmt ", 4) && length >= 16) {
                s->stage = 2;
                s->need = 16;
                s->skip -= 16;
            } else if (!memcmp(s->header, "data", 4)) {
                s->stage = s->frame && length >= s->frame ? 3 : -1;
                s->need = s->frame;
            }
        }
    }
    return 0;
}
static void trace_phase(const char *path, const char *cycle, const char *phase) {
    if (!*cycle || strspn(cycle, "0123456789.") != strlen(cycle) || strlen(cycle) >= 96)
        return;
    int fd = open(path, O_WRONLY | O_APPEND | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    struct stat st;
    if (fd >= 0 && fstat(fd, &st) == 0 && S_ISREG(st.st_mode) && st.st_size < 65280) {
        /* Same boot-time clock as shell phases, including after suspend. */
        FILE *clock = fopen("/proc/uptime", "r");
        double at;
        if (clock && fscanf(clock, "%lf", &at) == 1) {
            char line[256];
            int length = snprintf(line, sizeof line, "pid=%ld phase=%s uptime=%.3f cycle=%s\n",
                                  (long)getpid(), phase, at, cycle);
            ssize_t logged = write(fd, line, (size_t)length);
            (void)logged;
        }
        if (clock)
            fclose(clock);
    }
    if (fd >= 0)
        close(fd);
}
int bridge_trace_pcm(const char *path, const char *cycle) {
    /* Byte-exact pass-through; diagnostics never change the audio result.
     * First-output remains available separately from optional WAV PCM parsing. */
    int recorded = 0;
    const char *option = getenv("XIAOAI_PROFILE_PCM");
    int inspect = option && !strcmp(option, "1");
    struct pcm_trace pcm = {.need = 12};
    char buffer[4096];
    for (;;) {
        ssize_t n = read(STDIN_FILENO, buffer, sizeof buffer);
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0)
            return 1;
        if (!n)
            return 0;
        if (!recorded) {
            recorded = 1;
            trace_phase(path, cycle, "recording_first_output");
        }
        size_t sent = 0;
        while (sent < (size_t)n) {
            ssize_t out = write(STDOUT_FILENO, buffer + sent, (size_t)n - sent);
            if (out < 0 && errno == EINTR)
                continue;
            if (out <= 0)
                return 1;
            sent += (size_t)out;
        }
        /* Forward this block before optional PCM inspection/log I/O. The phase
         * observes a complete frame passed through the tap, not ADC sampling. */
        if (inspect && pcm_frame(&pcm, (const unsigned char *)buffer, (size_t)n)) {
            inspect = 0;
            trace_phase(path, cycle, "recording_first_pcm");
        }
    }
}
