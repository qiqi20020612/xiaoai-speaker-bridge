/* On-demand recovery for a confirmed Shairport 4.3.5 shared ALSA mixer.
 * The gain model follows player.c/common.c in the upstream 4.3.5 tag. It is
 * expressed in centi-dB, never in sender percentages or an assumed ALSA curve.
 * Unknown configuration, includes and mixed HW/SW ranges fail closed.
 */
#include "airplay_volume.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

struct config {
    const char *p;
    char word[256];
    unsigned seen;
    double maximum, range;
};

static int next(struct config *c) {
    const char *p = c->p;
    for (;;) {
        while (isspace((unsigned char)*p))
            p++;
        if (*p == '#' || !strncmp(p, "//", 2)) {
            while (*p && *p != '\n')
                p++;
        } else if (!strncmp(p, "/*", 2)) {
            const char *end = strstr(p + 2, "*/");
            if (!end)
                return -1;
            p = end + 2;
        } else
            break;
    }
    size_t n = 0;
    int type = (unsigned char)*p;
    if (!*p)
        return 0;
    if (*p == '"') {
        p++;
        while (*p && *p != '"') {
            if (*p == '\\')
                return -1;
            if (n + 1 >= sizeof c->word)
                return -1;
            c->word[n++] = *p++;
        }
        if (*p++ != '"')
            return -1;
        type = 's';
    } else if (isalnum((unsigned char)*p) || strchr("_+.-", *p)) {
        while (*p && (isalnum((unsigned char)*p) || strchr("_+.-", *p))) {
            if (n + 1 >= sizeof c->word)
                return -1;
            c->word[n++] = *p++;
        }
        type = 'w';
    } else if (strchr("{}=:;(),[]", *p))
        p++;
    else
        return -1;
    c->word[n] = 0;
    c->p = p;
    return type;
}

static int number(const char *s, double *n) {
    char *end;
    errno = 0;
    *n = strtod(s, &end);
    return !*s || *end || errno || !isfinite(*n) ? -1 : 0;
}

static int setting(struct config *c, int group, const char *key, int type) {
    unsigned bit = 0;
    if (group == 1 && !strcmp(key, "volume_max_db")) {
        bit = 1;
        if (type != 'w' || number(c->word, &c->maximum))
            return -1;
    } else if (group == 1 && !strcmp(key, "volume_range_db")) {
        bit = 2;
        if (type != 'w' || number(c->word, &c->range))
            return -1;
    } else if (group == 1 && !strcmp(key, "ignore_volume_control")) {
        bit = 4;
        if (type != 's' || strcmp(c->word, "no"))
            return -1;
    } else if (group == 2 && !strcmp(key, "mixer_control_name")) {
        bit = 8;
        if (type != 's' || strcmp(c->word, "mysoftvol"))
            return -1;
    } else if (group == 2 && (!strcmp(key, "mixer_device") || !strcmp(key, "output_device"))) {
        bit = !strcmp(key, "mixer_device") ? 16 : 32;
        if (type != 's' || strcmp(c->word, "default"))
            return -1;
    } else if (group == 2 && !strcmp(key, "mixer_control_index")) {
        bit = 64;
        if (type != 'w' || strcmp(c->word, "0"))
            return -1;
    } else if (group == 2 && !strcmp(key, "use_hardware_mute_if_available")) {
        bit = 128;
        if (type != 's' || strcmp(c->word, "no"))
            return -1;
    } else if (group == 2 && !strcmp(key, "mute_using_playback_switch")) {
        bit = 256;
        if (type != 's' || strcmp(c->word, "no"))
            return -1;
    } else if (group == 1 && !strcmp(key, "output_backend")) {
        bit = 512;
        if (type != 's' || strcmp(c->word, "alsa"))
            return -1;
    }
    if (c->seen & bit)
        return -1;
    c->seen |= bit;
    return 0;
}

static int section(struct config *c, int group, int depth) {
    if (depth > 8)
        return -1;
    for (;;) {
        int type = next(c);
        if (!type)
            return depth == 0 ? 0 : -1;
        if (type == '}')
            return depth ? 0 : -1;
        if (type == ';')
            continue;
        if (type != 'w')
            return -1;
        char key[sizeof c->word];
        strcpy(key, c->word);
        type = next(c);
        if (type != '=' && type != ':')
            return -1;
        type = next(c);
        if (type == '{') {
            int child = depth == 0 ? !strcmp(key, "general") ? 1 : !strcmp(key, "alsa") ? 2 : 0 : 0;
            if (section(c, child, depth + 1))
                return -1;
        } else {
            if (type != 'w' && type != 's')
                return -1;
            if (setting(c, group, key, type) || next(c) != ';')
                return -1;
        }
    }
}

struct mixer {
    long minimum, maximum, bottom, increment, left, right;
};

static int integer(const char *s, long *value) {
    if (!*s || strspn(s, "0123456789") != strlen(s))
        return -1;
    char *end;
    errno = 0;
    *value = strtol(s, &end, 10);
    return errno || *end || *value < 0 || *value > 65535 ? -1 : 0;
}

/* amixer renders DB_SCALE's integer centi-dB as exactly two decimals. Do not
 * round an unknown/sub-centi scale or use floating point to invert the TLV. */
static int centi_db(const char *s, long *value) {
    int negative = *s == '-';
    if (negative)
        s++;
    if (!isdigit((unsigned char)*s))
        return -1;
    char *end;
    errno = 0;
    long whole = strtol(s, &end, 10);
    if (errno || whole > 200 || *end != '.' || strlen(end + 1) != 2 ||
        !isdigit((unsigned char)end[1]) || !isdigit((unsigned char)end[2]))
        return -1;
    *value = (whole * 100 + (end[1] - '0') * 10 + end[2] - '0') * (negative ? -1 : 1);
    return 0;
}

static int only_space(const char *s) {
    while (isspace((unsigned char)*s))
        s++;
    return !*s;
}

static int mixer_read(struct mixer *m) {
    char input[16385];
    size_t size = fread(input, 1, sizeof input, stdin);
    if (ferror(stdin) || size >= sizeof input || memchr(input, 0, size))
        return -1;
    input[size] = 0;
    unsigned seen = 0;
    int lines = 0;
    char *next_line, *line = strtok_r(input, "\n", &next_line);
    for (; line; line = strtok_r(NULL, "\n", &next_line)) {
        if (++lines > 32 || strlen(line) >= 511)
            return -1;
        if (only_space(line))
            continue;
        char a[32], b[32], c[32], d[32], access[16];
        int end = 0;
        unsigned bit;
        long count, raw_step, mute, id;
        if (sscanf(line, " numid=%31[^,],iface=MIXER,name='mysoftvol'%n", a, &end) == 1 && end) {
            const char *tail = line + end;
            if (!strncmp(tail, ",index=0", 8))
                tail += 8;
            if (!only_space(tail) || integer(a, &id) || !id)
                return -1;
            bit = 1;
        } else if (
            sscanf(
                line,
                " ; type=INTEGER,access=%15[^,],values=%31[^,],min=%31[^,],max=%31[^,],step=%31s%n",
                access, a, b, c, d, &end) == 5 &&
            end) {
            if (!only_space(line + end) ||
                (strcmp(access, "rw---RW-") && strcmp(access, "rw---R--")) || integer(a, &count) ||
                count != 2 || integer(b, &m->minimum) || integer(c, &m->maximum) ||
                m->maximum <= m->minimum || integer(d, &raw_step) || raw_step > 1)
                return -1;
            bit = 2;
        } else if (sscanf(line, " : values=%31[^,],%31s%n", a, b, &end) == 2 && end) {
            if (!only_space(line + end) || integer(a, &m->left) || integer(b, &m->right))
                return -1;
            bit = 4;
        } else if (sscanf(line, " | dBscale-min=%31[^d]dB,step=%31[^d]dB,mute=%31s%n", a, b, c,
                          &end) == 3 &&
                   end) {
            if (!only_space(line + end) || centi_db(a, &m->bottom) || m->bottom < -20000 ||
                m->bottom > 0 || centi_db(b, &m->increment) || m->increment < 1 ||
                m->increment > 1000 || integer(c, &mute) || mute)
                return -1;
            bit = 8;
        } else
            return -1;
        if (seen & bit)
            return -1;
        seen |= bit;
    }
    return seen != 15 || m->left < m->minimum || m->left > m->maximum || m->right < m->minimum ||
                   m->right > m->maximum
               ? -1
               : 0;
}

int bridge_airplay_volume_target(const char *path, const char *raw, const char *profile,
                                 const char *baseline, const char *start, int verify) {
    double volume, saved, ticks;
    if (number(raw, &volume) || (volume != -144 && (volume < -30 || volume > 0)) ||
        number(baseline, &saved) || saved < 0 || saved > 100 || number(start, &ticks) ||
        ticks <= 0 || ticks > 9007199254740991.0 || strspn(start, "0123456789") != strlen(start) ||
        (strcmp(profile, "standard") && strcmp(profile, "flat")))
        return 2;
    int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
    struct stat st;
    if (fd < 0)
        return 2;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size > 16384) {
        close(fd);
        return 2;
    }
    FILE *file = fdopen(fd, "rb");
    if (!file) {
        close(fd);
        return 2;
    }
    struct timespec uptime, wall;
#ifdef CLOCK_BOOTTIME
    int clock = CLOCK_BOOTTIME;
#else
    int clock = CLOCK_MONOTONIC;
#endif
    long hz = sysconf(_SC_CLK_TCK);
    char source[16385];
    size_t size = fread(source, 1, sizeof source, file);
    int invalid = fstat(fileno(file), &st) || !S_ISREG(st.st_mode) || ferror(file) ||
                  size >= sizeof source || memchr(source, 0, size) || hz <= 0 ||
                  clock_gettime(CLOCK_REALTIME, &wall) || clock_gettime(clock, &uptime);
    fclose(file);
    if (invalid || ticks / hz > uptime.tv_sec + 1)
        return 2;
#ifdef __APPLE__
    double modified = st.st_mtimespec.tv_sec + st.st_mtimespec.tv_nsec / 1e9;
#else
    double modified = st.st_mtim.tv_sec + st.st_mtim.tv_nsec / 1e9;
#endif
    double process_born =
        wall.tv_sec + wall.tv_nsec / 1e9 - uptime.tv_sec - uptime.tv_nsec / 1e9 + ticks / hz;
    if (modified > process_born)
        return 2;
    source[size] = 0;
    struct config c = {.p = source};
    if (section(&c, 0, 0) || (c.seen & 11) != 11 || c.maximum < -200 || c.maximum > 0 ||
        c.range < 30 || c.range > 150 || (long)c.range != c.range)
        return 2;

    struct mixer mixer = {0};
    if (mixer_read(&mixer))
        return 2;
    long bottom = mixer.bottom, increment = mixer.increment;
    long top = bottom + (mixer.maximum - mixer.minimum) * increment;
    long high = (long)c.maximum * 100, range = (long)(c.range * 100);
    if (increment < 1 || high > top || high < bottom || high - range < bottom)
        return 2;
    if (volume == -144) {
        /* Shairport's software mute remains active; undo only our master cap. */
        if (verify)
            return 2;
        printf("%.17g%%\n", saved);
        return 0;
    }
    double gain;
    if (!strcmp(profile, "flat"))
        gain = range * (30 + volume) / 30 + (high - range);
    else {
        /* Intersection of the three straight segments in the standard taper.
         * Keep upstream's integer half-range and centi-dB truncation. */
        gain = (range / 2) * volume / 30;
        double middle = (range + (double)(range / 2)) * (volume + 5) / 50;
        double low = range * (volume + 17) / 13;
        if (middle < gain)
            gain = middle;
        if (low < gain)
            gain = low;
        gain += high;
    }
    long target = (long)gain;
    if (target < bottom || target > top)
        return 2;
    /* Match snd_tlv_convert_from_dB(..., dir=0), Shairport's first choice:
     * nearest integer with halfway cases upward. Nonnegative integer math
     * avoids errors at negative dB boundaries, including odd centi-dB steps. */
    long offset = target - bottom;
    long value = mixer.minimum + offset / increment + (2 * (offset % increment) >= increment);
    if (value < mixer.minimum || value > mixer.maximum ||
        (verify && (mixer.left != value || mixer.right != value)))
        return 2;
    printf("%ld,%ld\n", value, value);
    return 0;
}
