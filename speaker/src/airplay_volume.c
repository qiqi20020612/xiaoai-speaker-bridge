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

int bridge_airplay_volume_target(const char *path, const char *raw, const char *profile,
                                 const char *baseline, const char *start) {
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

    char line[512];
    int have_name = 0, have_range = 0, have_db = 0, lines = 0;
    long minimum = 0, maximum = 0;
    double minimum_db = 0, step = 0;
    while (fgets(line, sizeof line, stdin)) {
        if (++lines > 32 || (strlen(line) == sizeof line - 1 && !strchr(line, '\n')))
            return 2;
        if (strstr(line, "iface=MIXER,name='mysoftvol'"))
            have_name++;
        int channels, mute;
        long raw_step;
        if (sscanf(line, " ; type=INTEGER,access=%*[^,],values=%d,min=%ld,max=%ld,step=%ld",
                   &channels, &minimum, &maximum, &raw_step) == 4) {
            if (channels != 2 || minimum < 0 || maximum <= minimum || maximum > 65535)
                return 2;
            have_range++;
        }
        if (sscanf(line, " | dBscale-min=%lfdB,step=%lfdB,mute=%d", &minimum_db, &step, &mute) ==
            3) {
            if (mute || !isfinite(minimum_db) || !isfinite(step) || minimum_db < -200 ||
                minimum_db > 0 || step <= 0 || step > 10)
                return 2;
            have_db++;
        }
    }
    if (ferror(stdin) || have_name != 1 || have_range != 1 || have_db != 1)
        return 2;
    long bottom = (long)(minimum_db * 100 - 0.5);
    long increment = (long)(step * 100 + 0.5);
    long top = bottom + (maximum - minimum) * increment;
    long high = (long)c.maximum * 100, range = (long)(c.range * 100);
    if (increment < 1 || high > top || high < bottom || high - range < bottom)
        return 2;
    if (volume == -144) {
        /* Shairport's software mute remains active; undo only our master cap. */
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
    printf("%.2fdB\n", target / 100.0);
    return 0;
}
