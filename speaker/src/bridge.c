#define _POSIX_C_SOURCE 200809L
#include "json.h"
#include "json_ops.h"
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define VERSION "0.1.8"
#define CLIENTS 8
#define BODY 12288
#define OUTPUT 32768
#define HISTORY 16
#define WORKERS 3
#define LOCAL_MESSAGE 4096
#define PROBE_TIMEOUT 15
#define STARTUP_PROBE_ATTEMPTS 3
#define CLEANUP_GRACE 20
#define PROBE_INTERVAL 15
#define LOG_LIMIT 65536

struct config {
    char bind[64], token[129], id[65], name[129], adapter[512], runtime[256], path[512];
    int port, timeout;
    struct {
        uint32_t network, mask;
    } allow[8];
    int allowed;
} cfg;
struct connection {
    int fd;
    size_t used, sent, total;
    char input[BODY + 4096], output[OUTPUT + 512];
    double deadline;
    int events, pending;
    uint64_t since, identity;
} conn[CLIENTS];
struct job {
    char id[65], kind[24], status[24], payload[BODY], result[8192];
    int active;
    double created;
} jobs[HISTORY];
struct worker {
    pid_t pid;
    int fd, kind, owner, owner_fd, job;
    uint64_t owner_identity;
    size_t used;
    char output[OUTPUT];
    double deadline, kill_at;
    int timed_out, overflow;
} workers[WORKERS];
static char capabilities[8192] = "{}", cached_state[OUTPUT] = "{}";
static uint64_t sequence = 1, connection_identity = 0;
static char instance_id[64];
static double rate_tokens = 40, rate_at = 0;
static int listening = -1, local_socket = -1, voice_active = 0;
static pid_t voice_pid = 0;
static char voice_source[32] = "", policy_source[32] = "", voice_observations[256] = "{}";
static char voice_sources[128] = "[]";
static char policy_priority[128] = "[\"airplay\",\"bluetooth\",\"mpd\"]";
static char voice_volumes[256] = "{}";
static int voice_uncertain = 1, policy_uncertain = 1;
static double next_probe = 0, last_airplay_check = 0;
static time_t airplay_mtime = 0;
static time_t cached_at = 0;
static pid_t restart_pid = 0;
static int probe_ok = 0;
static unsigned probe_attempt = 0;
static int probe_running = 0, startup_exhausted = 0, server_logging = 0;
static const char *probe_status = "pending";
static double server_started = 0;
static volatile sig_atomic_t stopping = 0;

static double monotime(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static void stop_signal(int s) {
    (void)s;
    stopping = 1;
}
static int nonblock(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
        return -1;
    return fcntl(fd, F_SETFD, FD_CLOEXEC);
}
static void runtime_path(char *out, size_t n, const char *name) {
    snprintf(out, n, "%s/%s", cfg.runtime, name);
}
/* Events are fixed literals; never log configuration, adapter output or HTTP data.
 * Open per event so both the daemon and start.sh follow rotation during runtime. */
static void diagnostic(const char *event, int code) {
    char line[256];
    int n = snprintf(line, sizeof line,
                     "time=%ld elapsed=%.3f bridge event=%s attempt=%u code=%d\n", (long)time(NULL),
                     server_started ? monotime() - server_started : 0, event, probe_attempt, code);
    int logged = 0;
    if (server_logging) {
        char path[512], previous[544];
        runtime_path(path, sizeof path, "xiaoai_bridge.log");
        snprintf(previous, sizeof previous, "%s.1", path);
        int fd = open(path, O_CREAT | O_WRONLY | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0600);
        struct stat st;
        if (fd >= 0 && fstat(fd, &st) == 0 && S_ISREG(st.st_mode)) {
            if (st.st_size + n > LOG_LIMIT) {
                close(fd);
                fd = -1;
                if (rename(path, previous) == 0)
                    fd = open(path, O_CREAT | O_WRONLY | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0600);
            }
            if (fd >= 0)
                logged = write(fd, line, (size_t)n) == n;
        }
        if (fd >= 0)
            close(fd);
    }
    struct stat sink;
    /* start.sh redirects stderr to the same log. Avoid duplicate writes (including
     * to the old inode after rotation); keep diagnostics on a terminal/pipe. */
    if (!logged || fstat(STDERR_FILENO, &sink) < 0 || !S_ISREG(sink.st_mode)) {
        size_t at = 0;
        while (at < (size_t)n) {
            ssize_t written = write(STDERR_FILENO, line + at, (size_t)n - at);
            if (written > 0)
                at += (size_t)written;
            else if (written == 0 || errno != EINTR)
                break;
        }
    }
}
static int initialization_failed(const char *event) {
    diagnostic(event, errno);
    return 2;
}
static void probe_failed(const char *reason, int code) {
    probe_ok = 0;
    probe_status = reason;
    diagnostic(reason, code);
    if (!cached_at && probe_attempt < STARTUP_PROBE_ATTEMPTS) {
        next_probe = monotime() + probe_attempt; /* first retries after 1s and 2s */
        diagnostic("probe_retry_scheduled", (int)probe_attempt);
    } else {
        next_probe = monotime() + PROBE_INTERVAL;
        if (!cached_at && !startup_exhausted) {
            startup_exhausted = 1;
            diagnostic("startup_probe_retries_exhausted", STARTUP_PROBE_ATTEMPTS);
        }
    }
}
static int read_file(const char *path, char *out, size_t cap) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW);
    if (fd < 0)
        return -1;
    ssize_t n = read(fd, out, cap - 1);
    char c;
    int more = n >= 0 && read(fd, &c, 1) > 0;
    close(fd);
    if (n < 0 || more)
        return -1;
    out[n] = 0;
    return (int)n;
}
static int write_file(const char *name, const char *s) {
    char path[512], tmp[544];
    runtime_path(path, sizeof path, name);
    snprintf(tmp, sizeof tmp, "%s.%ld", path, (long)getpid());
    int fd = open(tmp, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, 0600);
    if (fd < 0)
        return -1;
    size_t n = strlen(s);
    ssize_t written = write(fd, s, n);
    int ok = written == (ssize_t)n && fsync(fd) == 0;
    close(fd);
    if (!ok || rename(tmp, path) < 0) {
        unlink(tmp);
        return -1;
    }
    return 0;
}
static int text(const struct json_doc *d, int object, const char *key, char *out, size_t n) {
    return json_string(d, json_get(d, object, key), out, n);
}
static int valid_id(const char *s) {
    size_t n = strlen(s);
    if (!n || n > 64)
        return 0;
    for (; *s; s++)
        if (!isalnum((unsigned char)*s) && *s != '_' && *s != '-')
            return 0;
    return 1;
}
static int token_equal(const char *s, const char *token) {
    size_t a = strlen(s), b = strlen(token);
    unsigned diff = (unsigned)(a ^ b);
    for (size_t i = 0; i < 128; i++)
        diff |=
            (unsigned)((i < a ? (unsigned char)s[i] : 0) ^ (i < b ? (unsigned char)token[i] : 0));
    return diff == 0;
}
static int load_config(const char *path) {
    char raw[OUTPUT];
    struct json_doc d;
    struct stat st;
    if (stat(path, &st) < 0 || (st.st_mode & 0077) || !S_ISREG(st.st_mode))
        return -1;
    int n = read_file(path, raw, sizeof raw);
    if (n < 0 || json_parse(&d, raw, (size_t)n) < 0 || d.t[0].type != J_OBJECT)
        return -1;
    memset(&cfg, 0, sizeof cfg);
    cfg.port = 8766;
    cfg.timeout = 180;
    strcpy(cfg.bind, "0.0.0.0");
    strcpy(cfg.runtime, "/tmp");
    strcpy(cfg.adapter, "/data/xiaoai_bridge/lib/device.sh");
    strcpy(cfg.name, "XiaoAI Speaker");
    if (text(&d, 0, "token", cfg.token, sizeof cfg.token) < 32 ||
        text(&d, 0, "device_id", cfg.id, sizeof cfg.id) < 1 || !valid_id(cfg.id))
        return -1;
    for (const char *p = cfg.token; *p; p++)
        if (isspace((unsigned char)*p) || *p < '!' || *p > 126)
            return -1;
    if (strstr(cfg.token, "REPLACE") || strstr(cfg.token, "CHANGE"))
        return -1;
    if (json_get(&d, 0, "bind") >= 0 && text(&d, 0, "bind", cfg.bind, sizeof cfg.bind) < 0)
        return -1;
    if (json_get(&d, 0, "runtime_dir") >= 0 &&
        text(&d, 0, "runtime_dir", cfg.runtime, sizeof cfg.runtime) < 0)
        return -1;
    if (json_get(&d, 0, "adapter") >= 0 &&
        text(&d, 0, "adapter", cfg.adapter, sizeof cfg.adapter) < 0)
        return -1;
    if (json_get(&d, 0, "name") >= 0 && text(&d, 0, "name", cfg.name, sizeof cfg.name) < 0)
        return -1;
    for (const unsigned char *p = (const unsigned char *)cfg.name; *p; p++)
        if (*p < 32)
            return -1;
    if (cfg.adapter[0] != '/' || cfg.runtime[0] != '/' || strlen(cfg.runtime) > 200)
        return -1;
    struct in_addr bind_address;
    if (inet_pton(AF_INET, cfg.bind, &bind_address) != 1)
        return -1;
    double number;
    if (json_get(&d, 0, "port") >= 0) {
        if (json_number(&d, json_get(&d, 0, "port"), &number) < 0 || number < 1 || number > 65535 ||
            number != (int)number)
            return -1;
        cfg.port = (int)number;
    }
    if (json_get(&d, 0, "task_timeout") >= 0) {
        if (json_number(&d, json_get(&d, 0, "task_timeout"), &number) < 0 || number < 1 ||
            number > 180)
            return -1;
        cfg.timeout = (int)number;
    }
    int allow = json_get(&d, 0, "allowlist");
    if (allow >= 0) {
        if (d.t[allow].type != J_ARRAY || d.t[allow].count > 8)
            return -1;
        for (int i = allow + 1; i < d.t[allow].next; i = d.t[i].next) {
            char cidr[64];
            if (json_string(&d, i, cidr, sizeof cidr) < 0)
                return -1;
            char *slash = strchr(cidr, '/');
            int prefix = 32;
            if (slash) {
                *slash++ = 0;
                char *end;
                long p = strtol(slash, &end, 10);
                if (!*slash || *end || p < 0 || p > 32)
                    return -1;
                prefix = (int)p;
            }
            struct in_addr ip;
            if (inet_pton(AF_INET, cidr, &ip) != 1)
                return -1;
            uint32_t mask = prefix ? UINT32_MAX << (32 - prefix) : 0;
            cfg.allow[cfg.allowed].mask = mask;
            cfg.allow[cfg.allowed++].network = ntohl(ip.s_addr) & mask;
        }
    }
    snprintf(cfg.path, sizeof cfg.path, "%s", path);
    return 0;
}
static void close_conn(int i) {
    if (conn[i].fd >= 0)
        close(conn[i].fd);
    memset(&conn[i], 0, sizeof conn[i]);
    conn[i].fd = -1;
}
static void response(int i, int status, const char *body) {
    if (i < 0 || i >= CLIENTS || conn[i].fd < 0)
        return;
    size_t n = strlen(body);
    if (n > OUTPUT) {
        body = "{\"error\":\"response_too_large\"}";
        n = strlen(body);
        status = 500;
    }
    int head = snprintf(conn[i].output, sizeof conn[i].output,
                        "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: "
                        "%zu\r\nConnection: close\r\nCache-Control: "
                        "no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n",
                        status, status >= 400 ? "Error" : "OK", n);
    memcpy(conn[i].output + head, body, n);
    conn[i].total = (size_t)head + n;
    conn[i].sent = 0;
    conn[i].events = 0;
    conn[i].pending = 0;
    conn[i].deadline = monotime() + 5;
}
static void error(int i, int status, const char *code) {
    char out[128];
    snprintf(out, sizeof out, "{\"error\":\"%s\"}", code);
    response(i, status, out);
}
static int job_find(const char *id) {
    for (int j = 0; j < HISTORY; j++)
        if (strcmp(jobs[j].id, id) == 0)
            return j;
    return -1;
}
static int job_slot(void) {
    int oldest = -1;
    double at = 1e100;
    for (int j = 0; j < HISTORY; j++) {
        if (!jobs[j].id[0])
            return j;
        if (!jobs[j].active && jobs[j].created < at) {
            oldest = j;
            at = jobs[j].created;
        }
    }
    return oldest;
}
static void job_json(int j, char *out, size_t cap) {
    struct job *job = &jobs[j];
    char qid[140], qstatus[64];
    json_quote(job->id, qid, sizeof qid);
    json_quote(job->status, qstatus, sizeof qstatus);
    const char *result = job->result[0] ? job->result : "{}";
    size_t n = strlen(result);
    snprintf(out, cap, "{\"request_id\":%s,\"status\":%s%s%.*s}", qid, qstatus, n > 2 ? "," : "",
             n > 2 ? (int)n - 2 : 0, n > 2 ? result + 1 : "");
}
static int worker_spawn(int kind, int owner, int job, const char *operation, const char *payload) {
    int slot = -1;
    for (int k = 0; k < WORKERS; k++)
        if (workers[k].pid == 0) {
            slot = k;
            break;
        }
    if (slot < 0) {
        errno = EBUSY;
        return -1;
    }
    if (kind == 2)
        for (int k = 0; k < WORKERS; k++)
            if (workers[k].pid && workers[k].kind == 2)
                return -1;
    if (kind == 1)
        for (int k = 0; k < WORKERS; k++)
            if (workers[k].pid && workers[k].kind == 1)
                return -1;
    int input[2], output[2];
    if (pipe(input) < 0)
        return -1;
    if (pipe(output) < 0) {
        int saved = errno;
        close(input[0]);
        close(input[1]);
        errno = saved;
        return -1;
    }
    pid_t pid = fork();
    if (pid < 0) {
        int saved = errno;
        close(input[0]);
        close(input[1]);
        close(output[0]);
        close(output[1]);
        errno = saved;
        return -1;
    }
    if (pid == 0) {
        setpgid(0, 0);
        dup2(input[0], STDIN_FILENO);
        dup2(output[1], STDOUT_FILENO);
        int null = open("/dev/null", O_WRONLY);
        if (null >= 0) {
            dup2(null, STDERR_FILENO);
            close(null);
        }
        close(input[0]);
        close(input[1]);
        close(output[0]);
        close(output[1]);
        for (int i = 0; i < CLIENTS; i++)
            if (conn[i].fd >= 0)
                close(conn[i].fd);
        if (listening >= 0)
            close(listening);
        if (local_socket >= 0)
            close(local_socket);
        for (int k = 0; k < WORKERS; k++)
            if (workers[k].fd >= 0)
                close(workers[k].fd);
        char parent[32];
        snprintf(parent, sizeof parent, "%ld", (long)getppid());
        setenv("XIAOAI_BRIDGE_CONFIG", cfg.path, 1);
        setenv("XIAOAI_BRIDGE_PID", parent, 1);
        setenv("XIAOAI_BRIDGE_RUNTIME", cfg.runtime, 1);
        execl(cfg.adapter, cfg.adapter, operation, (char *)NULL);
        _exit(127);
    }
    setpgid(pid, pid);
    close(input[0]);
    close(output[1]);
    size_t length = strlen(payload), at = 0;
    nonblock(input[1]);
    double limit = monotime() + 1;
    while (at < length && monotime() < limit) {
        ssize_t n = write(input[1], payload + at, length - at);
        if (n > 0)
            at += (size_t)n;
        else if (errno == EAGAIN || errno == EINTR) {
            struct pollfd p = {input[1], POLLOUT, 0};
            poll(&p, 1, 20);
        } else
            break;
    }
    close(input[1]);
    nonblock(output[0]);
    workers[slot] = (struct worker){.pid = pid,
                                    .fd = output[0],
                                    .kind = kind,
                                    .owner = owner,
                                    .owner_fd = owner >= 0 ? conn[owner].fd : -1,
                                    .owner_identity = owner >= 0 ? conn[owner].identity : 0,
                                    .job = job,
                                    .deadline = monotime() + (kind == 2   ? cfg.timeout
                                                              : kind == 0 ? PROBE_TIMEOUT
                                                                          : 15)};
    if (at < length) {
        kill(-pid, SIGTERM);
        workers[slot].kill_at = monotime() + 2;
    }
    if (owner >= 0)
        conn[owner].pending = 1;
    return slot;
}
static int cap_bool(const char *key) {
    struct json_doc d;
    if (json_parse(&d, capabilities, strlen(capabilities)) < 0)
        return 0;
    return json_bool(&d, json_get(&d, 0, key), 0);
}
static void save_snapshot(void) {
    char raw[512], q[80];
    json_quote(policy_source, q, sizeof q);
    snprintf(raw, sizeof raw, "{\"source\":%s,\"uncertain\":%s,\"updated\":%ld,\"priority\":%s}",
             policy_source[0] ? q : "null", policy_uncertain ? "true" : "false", (long)time(NULL),
             policy_priority);
    write_file("xiaoai_bridge.source.json", raw);
}
static void state_json(char *out, size_t cap) {
    size_t n = strlen(cached_state);
    char voice[1100], source[80];
    json_quote(voice_source, source, sizeof source);
    int audio = 0;
    for (int k = 0; k < WORKERS; k++)
        if (workers[k].pid && workers[k].kind == 2)
            audio = 1;
    snprintf(voice, sizeof voice,
             "\"voice\":{\"active\":%s,\"source\":%s,\"active_sources\":%s,"
             "\"volumes\":%s,\"uncertain\":%s,\"observed_sources\":%s}",
             voice_active ? "true" : "false", voice_source[0] ? source : "null", voice_sources,
             voice_volumes, voice_uncertain ? "true" : "false", voice_observations);
    snprintf(out, cap,
             "{%.*s%s%s,\"audio_active\":%s,\"capabilities\":%s,\"updated\":%ld,"
             "\"state_stale\":%s,\"instance_id\":\"%s\",\"device_id\":"
             "\"%s\"}",
             n > 2 ? (int)n - 2 : 0, n > 2 ? cached_state + 1 : "", n > 2 ? "," : "", voice,
             audio ? "true" : "false", capabilities, (long)cached_at, probe_ok ? "false" : "true",
             instance_id, cfg.id);
}
static void restart_bridge(int i) {
    if (restart_pid) {
        response(i, 202, "{\"accepted\":true}");
        return;
    }
    if (voice_active) {
        error(i, 409, "busy");
        return;
    }
    for (int k = 0; k < WORKERS; k++)
        if (workers[k].pid && workers[k].kind != 0) {
            error(i, 409, "busy");
            return;
        }
    // Voice can own these locks before its local wake event reaches HTTP.
    for (int k = 0; k < 2; k++) {
        char gate[512];
        runtime_path(gate, sizeof gate,
                     k ? "xiaoai_bridge.session.lock" : "xiaoai_bridge.audio.lock");
        int fd = open(gate, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd < 0 || flock(fd, LOCK_EX | LOCK_NB) < 0) {
            if (fd >= 0)
                close(fd);
            error(i, 409, "busy");
            return;
        }
        close(fd);
    }
    const char *dir = getenv("XIAOAI_BRIDGE_DIR");
    if (!dir)
        dir = "/data/xiaoai_bridge";
    char script[1024], path[512];
    if (snprintf(script, sizeof script, "%s/start.sh", dir) >= (int)sizeof script ||
        access(script, X_OK) < 0) {
        error(i, 503, "restart_unavailable");
        return;
    }
    runtime_path(path, sizeof path, "xiaoai_bridge.restart.lock");
    int guard = open(path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (guard < 0) {
        error(i, 503, "restart_unavailable");
        return;
    }
    if (flock(guard, LOCK_EX | LOCK_NB) < 0) {
        int busy = errno == EWOULDBLOCK;
        close(guard);
        if (busy)
            response(i, 202, "{\"accepted\":true}");
        else
            error(i, 503, "restart_unavailable");
        return;
    }
    restart_pid = fork();
    if (restart_pid == 0) {
        // A short-lived start.sh caller, outside the daemon's worker groups.
        // stdin retains only the restart guard; the new daemon uses /dev/null.
        if (setsid() < 0 || dup2(guard, STDIN_FILENO) < 0)
            _exit(127);
        close(guard);
        int null = open("/dev/null", O_WRONLY);
        if (null < 0 || dup2(null, STDOUT_FILENO) < 0 || dup2(null, STDERR_FILENO) < 0)
            _exit(127);
        if (null > STDERR_FILENO)
            close(null);
        setenv("XIAOAI_BRIDGE_DIR", dir, 1);
        setenv("XIAOAI_BRIDGE_RUNTIME", cfg.runtime, 1);
        setenv("XIAOAI_BRIDGE_CONFIG", cfg.path, 1);
        setenv("XIAOAI_SKIP_LISTENER_START", "1", 1);
        setenv("XIAOAI_BRIDGE_RESTART_API", "1", 1);
        // Let the already-buffered acknowledgement reach HA before HTTP exits.
        struct timespec delay = {1, 0};
        while (nanosleep(&delay, &delay) < 0 && errno == EINTR) {
        }
        execl(script, script, "restart", (char *)NULL);
        _exit(127);
    }
    close(guard);
    if (restart_pid < 0) {
        restart_pid = 0;
        error(i, 503, "restart_unavailable");
        return;
    }
    response(i, 202, "{\"accepted\":true}");
}
static void dispatch(int i, char *method, char *path, char *body) {
    char out[OUTPUT];
    struct json_doc d;
    if (strcmp(method, "GET") == 0) {
        if (strcmp(path, "/api/health") == 0) {
            if (!cached_at) {
                snprintf(out, sizeof out,
                         "{\"error\":\"dependencies_not_ready\",\"probe_status\":\"%s\","
                         "\"probe_attempt\":%u,\"probe_running\":%s,\"startup_failed\":%s}",
                         probe_status, probe_attempt, probe_running ? "true" : "false",
                         startup_exhausted ? "true" : "false");
                response(i, 503, out);
                return;
            }
            snprintf(out, sizeof out, "{\"ready\":true,\"version\":\"%s\",\"protocol\":1}",
                     VERSION);
            response(i, 200, out);
            return;
        }
        if (strcmp(path, "/api/info") == 0) {
            if (!cached_at) {
                error(i, 503, "dependencies_not_ready");
                return;
            }
            char id[140], name[280];
            json_quote(cfg.id, id, sizeof id);
            json_quote(cfg.name, name, sizeof name);
            snprintf(out, sizeof out,
                     "{\"device_id\":%s,\"name\":%s,\"model\":\"LX06\",\"version\":"
                     "\"%s\",\"protocol\":1,\"capabilities\":%s}",
                     id, name, VERSION, capabilities);
            response(i, 200, out);
            return;
        }
        if (strcmp(path, "/api/state") == 0 || strcmp(path, "/api/playback") == 0) {
            state_json(out, sizeof out);
            response(i, 200, out);
            return;
        }
        if (strncmp(path, "/api/events?since=", 18) == 0) {
            char *end;
            uint64_t since = strtoull(path + 18, &end, 10);
            if (!*(path + 18) || *end) {
                error(i, 400, "invalid_sequence");
                return;
            }
            conn[i].events = 1;
            conn[i].since = since;
            conn[i].deadline = monotime() + 25;
            return;
        }
        if (strncmp(path, "/api/jobs/", 10) == 0) {
            const char *id = path + 10;
            if (!valid_id(id)) {
                error(i, 400, "invalid_request_id");
                return;
            }
            int j = job_find(id);
            if (j < 0) {
                error(i, 404, "job_not_found");
                return;
            }
            job_json(j, out, sizeof out);
            response(i, 200, out);
            return;
        }
        error(i, 404, "not_found");
        return;
    }
    if (strcmp(method, "POST") != 0) {
        error(i, 405, "method_not_allowed");
        return;
    }
    // Keep health/state readable during an installation, but admit no new work.
    char install_path[512];
    runtime_path(install_path, sizeof install_path, "xiaoai_bridge.install.lock");
    int install_fd = open(install_path, O_RDONLY | O_CLOEXEC);
    if (install_fd >= 0) {
        int install_busy = flock(install_fd, LOCK_SH | LOCK_NB) < 0;
        close(install_fd);
        if (install_busy) {
            error(i, 503, "installation_in_progress");
            return;
        }
    } else if (errno != ENOENT) {
        error(i, 503, "installation_state_unavailable");
        return;
    }
    if (json_parse(&d, body, strlen(body)) < 0 || d.t[0].type != J_OBJECT) {
        error(i, 400, "invalid_json");
        return;
    }
    if (strcmp(path, "/api/bridge/restart") == 0) {
        restart_bridge(i);
        return;
    }
    if (restart_pid) {
        error(i, 503, "restart_in_progress");
        return;
    }
    if (strcmp(path, "/api/policy") == 0) {
        int source = json_get(&d, 0, "source");
        char value[32] = "";
        if (source >= 0 && d.t[source].type != J_NULL &&
            json_string(&d, source, value, sizeof value) < 0) {
            error(i, 400, "invalid_source");
            return;
        }
        if (value[0] && strcmp(value, "mpd") && strcmp(value, "airplay") &&
            strcmp(value, "bluetooth")) {
            error(i, 400, "invalid_source");
            return;
        }
        int priority = json_get(&d, 0, "priority");
        if (priority >= 0) {
            int mask = 0;
            char candidate[sizeof policy_priority];
            if (d.t[priority].type != J_ARRAY || d.t[priority].count != 3 ||
                json_copy(&d, priority, candidate, sizeof candidate) < 0) {
                error(i, 400, "invalid_source_priority");
                return;
            }
            for (int node = priority + 1, n = 0; n < 3; n++, node = d.t[node].next) {
                char name[32];
                if (json_string(&d, node, name, sizeof name) >= 0) {
                    if (strcmp(name, "airplay") == 0)
                        mask |= 1;
                    else if (strcmp(name, "bluetooth") == 0)
                        mask |= 2;
                    else if (strcmp(name, "mpd") == 0)
                        mask |= 4;
                }
            }
            if (mask != 7) {
                error(i, 400, "invalid_source_priority");
                return;
            }
            strcpy(policy_priority, candidate);
        }
        strcpy(policy_source, value);
        policy_uncertain = json_bool(&d, json_get(&d, 0, "uncertain"), 1);
        save_snapshot();
        response(i, 200, "{\"accepted\":true}");
        return;
    }
    if (strncmp(path, "/api/jobs/", 10) == 0) {
        char *suffix = strstr(path + 10, "/cancel");
        if (!suffix || suffix[7]) {
            error(i, 404, "not_found");
            return;
        }
        *suffix = 0;
        if (!valid_id(path + 10)) {
            error(i, 400, "invalid_request_id");
            return;
        }
        int j = job_find(path + 10);
        if (j < 0) {
            error(i, 404, "job_not_found");
            return;
        }
        if (jobs[j].active && strcmp(jobs[j].kind, "send_text") == 0) {
            error(i, 409, "cannot_cancel_running_conversation");
            return;
        }
        if (jobs[j].active)
            for (int k = 0; k < WORKERS; k++)
                if (workers[k].pid && workers[k].kind == 2 && workers[k].job == j) {
                    kill(-workers[k].pid, SIGTERM);
                    workers[k].kill_at = monotime() + CLEANUP_GRACE;
                    strcpy(jobs[j].status, "cancelled");
                }
        job_json(j, out, sizeof out);
        response(i, 200, out);
        return;
    }
    char id[65];
    if (text(&d, 0, "request_id", id, sizeof id) < 0 || !valid_id(id)) {
        error(i, 400, "invalid_request_id");
        return;
    }
    const char *kind = NULL, *operation = NULL;
    if (strcmp(path, "/api/tts/announce") == 0) {
        kind = "announce";
        operation = "announce";
    }
    if (strcmp(path, "/api/conversation/text") == 0) {
        kind = "send_text";
        operation = "send_text";
    }
    if (strcmp(path, "/api/volume") == 0)
        operation = "volume";
    else if (strcmp(path, "/api/playback/control") == 0)
        operation = "playback";
    else if (strcmp(path, "/api/listener") == 0)
        operation = "listener";
    else if (strcmp(path, "/api/conversation/reset") == 0)
        operation = "reset_context";
    else if (strcmp(path, "/api/ir") == 0)
        operation = "ir";
    if (!operation) {
        error(i, 404, "not_found");
        return;
    }
    int existing = job_find(id);
    if (existing >= 0) {
        if (strcmp(jobs[existing].kind, operation) || strcmp(jobs[existing].payload, body)) {
            error(i, 409, "request_id_conflict");
            return;
        }
        job_json(existing, out, sizeof out);
        response(i, 200, out);
        return;
    }
    if (kind) {
        if (json_get(&d, 0, "agent_id") >= 0) {
            error(i, 400, "unsupported_agent_override");
            return;
        }
        char value[4097];
        int n = text(&d, 0, "text", value, sizeof value);
        if (n < 1 || n > 4096) {
            error(i, 400, "invalid_text");
            return;
        }
        if (!cap_bool(strcmp(kind, "announce") == 0 ? "announce" : "conversation")) {
            error(i, 409, "voice_adapter_not_ready");
            return;
        }
        if (voice_active) {
            error(i, 409, "busy");
            return;
        }
        for (int k = 0; k < WORKERS; k++)
            if (workers[k].pid && workers[k].kind == 2) {
                error(i, 409, "busy");
                return;
            }
        int j = job_slot();
        if (j < 0) {
            error(i, 429, "job_limit");
            return;
        }
        memset(&jobs[j], 0, sizeof jobs[j]);
        strcpy(jobs[j].id, id);
        strcpy(jobs[j].kind, kind);
        strcpy(jobs[j].payload, body);
        strcpy(jobs[j].status, "accepted");
        jobs[j].created = monotime();
        if (worker_spawn(2, -1, j, operation, body) < 0) {
            memset(&jobs[j], 0, sizeof jobs[j]);
            error(i, 409, "busy");
            return;
        }
        jobs[j].active = 1;
        job_json(j, out, sizeof out);
        response(i, 202, out);
        ++sequence;
        return;
    }
    int j = job_slot();
    if (j < 0) {
        error(i, 429, "job_limit");
        return;
    }
    memset(&jobs[j], 0, sizeof jobs[j]);
    strcpy(jobs[j].id, id);
    strcpy(jobs[j].kind, operation);
    strcpy(jobs[j].payload, body);
    strcpy(jobs[j].status, "running");
    jobs[j].active = 1;
    jobs[j].created = monotime();
    if (worker_spawn(1, i, j, operation, body) < 0) {
        memset(&jobs[j], 0, sizeof jobs[j]);
        error(i, 409, "busy");
        return;
    }
}
static void http_read(int i) {
    struct connection *c = &conn[i];
    ssize_t n = read(c->fd, c->input + c->used, sizeof c->input - c->used - 1);
    if (n == 0) {
        close_conn(i);
        return;
    }
    if (n < 0) {
        if (errno != EAGAIN && errno != EINTR)
            close_conn(i);
        return;
    }
    c->used += (size_t)n;
    c->input[c->used] = 0;
    if (memchr(c->input, 0, c->used)) {
        error(i, 400, "invalid_request");
        return;
    }
    char *headers = strstr(c->input, "\r\n\r\n");
    if (!headers) {
        if (c->used >= 4096)
            error(i, 431, "headers_too_large");
        return;
    }
    size_t header_length = (size_t)(headers - c->input) + 4;
    if (header_length >= 4096) {
        error(i, 431, "headers_too_large");
        return;
    }
    char head[4096];
    memcpy(head, c->input, header_length);
    head[header_length] = 0;
    char *line_end = strstr(head, "\r\n");
    if (!line_end) {
        error(i, 400, "invalid_request");
        return;
    }
    *line_end = 0;
    char method[8], path[256], version[16], extra[2];
    if (sscanf(head, "%7s %255s %15s %1s", method, path, version, extra) != 3 ||
        strcmp(version, "HTTP/1.1")) {
        error(i, 400, "invalid_request");
        return;
    }
    char auth[160] = "";
    long length = 0;
    int auths = 0, lengths = 0, hosts = 0;
    char *line = line_end + 2;
    while (*line && strcmp(line, "\r\n")) {
        char *end = strstr(line, "\r\n");
        if (!end)
            break;
        *end = 0;
        char *colon = strchr(line, ':');
        if (!colon || colon == line || line[0] == ' ' || line[0] == '\t') {
            error(i, 400, "invalid_headers");
            return;
        }
        *colon++ = 0;
        for (char *p = line; *p; p++)
            if (!isalnum((unsigned char)*p) && *p != '-') {
                error(i, 400, "invalid_headers");
                return;
            }
        while (*colon == ' ' || *colon == '\t')
            ++colon;
        if (strcasecmp(line, "Authorization") == 0) {
            if (++auths > 1 || strncmp(colon, "Bearer ", 7) || strlen(colon + 7) >= sizeof auth) {
                error(i, 401, "auth_failed");
                return;
            }
            strcpy(auth, colon + 7);
        }
        if (strcasecmp(line, "Content-Length") == 0) {
            char *end_num;
            errno = 0;
            length = strtol(colon, &end_num, 10);
            if (++lengths > 1 || !isdigit((unsigned char)*colon) || *end_num || errno ||
                length < 0 || length > BODY - 1) {
                error(i, 400, "invalid_content_length");
                return;
            }
        }
        if (strcasecmp(line, "Transfer-Encoding") == 0 || strcasecmp(line, "Expect") == 0) {
            error(i, 400, "unsupported_encoding");
            return;
        }
        if (strcasecmp(line, "Host") == 0)
            ++hosts;
        line = end + 2;
    }
    if (hosts != 1) {
        error(i, 400, "invalid_host_header");
        return;
    }
    if (auths != 1 || !token_equal(auth, cfg.token)) {
        error(i, 401, "auth_failed");
        return;
    }
    if (strcmp(method, "POST") == 0 && lengths != 1) {
        error(i, 411, "length_required");
        return;
    }
    if (c->used < header_length + (size_t)length)
        return;
    if (c->used != header_length + (size_t)length) {
        error(i, 400, "pipelining_not_supported");
        return;
    }
    if (strcmp(method, "GET") == 0 && length) {
        error(i, 400, "unexpected_body");
        return;
    }
    double now = monotime();
    rate_tokens += (now - rate_at) * 10;
    if (rate_tokens > 40)
        rate_tokens = 40;
    rate_at = now;
    if (rate_tokens < 1) {
        error(i, 429, "rate_limited");
        return;
    }
    rate_tokens -= 1;
    dispatch(i, method, path, c->input + header_length);
}
static void finish_worker(int k, int status) {
    struct worker *w = &workers[k];
    w->output[w->used] = 0;
    struct json_doc d;
    int good = WIFEXITED(status) && WEXITSTATUS(status) == 0 && !w->timed_out && !w->overflow;
    if (w->kind == 0) {
        probe_running = 0;
        int previously_ok = probe_ok;
        probe_ok = 0;
        if (good && json_parse(&d, w->output, w->used) == 0) {
            char cap[sizeof capabilities], state[sizeof cached_state];
            int ci = json_get(&d, 0, "capabilities"), si = json_get(&d, 0, "state");
            if (ci >= 0 && si >= 0 && d.t[ci].type == J_OBJECT && d.t[si].type == J_OBJECT &&
                json_copy(&d, ci, cap, sizeof cap) >= 0 &&
                json_copy(&d, si, state, sizeof state) >= 0 && strlen(state) < 18000) {
                int changed = strcmp(cap, capabilities) || strcmp(state, cached_state);
                strcpy(capabilities, cap);
                strcpy(cached_state, state);
                cached_at = time(NULL);
                probe_ok = 1;
                probe_status = "ready";
                startup_exhausted = 0;
                if (!previously_ok)
                    diagnostic("probe_ready", 0);
                if (changed)
                    ++sequence;
            }
        }
        if (!probe_ok) {
            if (w->timed_out)
                probe_failed("probe_timeout", PROBE_TIMEOUT);
            else if (w->overflow)
                probe_failed("probe_output_limit", OUTPUT);
            else if (!WIFEXITED(status))
                probe_failed("probe_signalled", WIFSIGNALED(status) ? WTERMSIG(status) : 0);
            else if (WEXITSTATUS(status) != 0)
                probe_failed("probe_exit_nonzero", WEXITSTATUS(status));
            else if (json_parse(&d, w->output, w->used) < 0)
                probe_failed("probe_invalid_json", 0);
            else
                probe_failed("probe_invalid_response", 0);
        }
    } else {
        struct job *j = &jobs[w->job];
        const char *last = w->output;
        for (char *p = w->output; *p; p++)
            if (*p == '\n' && p[1])
                last = p + 1;
        size_t length = strlen(last);
        while (length && last[length - 1] == '\n')
            --length;
        if (strcmp(j->status, "cancelled") != 0)
            strcpy(j->status, good ? "completed" : "failed");
        int parsed = json_parse(&d, last, length) == 0 && d.t[0].type == J_OBJECT &&
                     length < sizeof j->result;
        if (strcmp(j->status, "cancelled") == 0) {
            good = 0;
            if (!j->result[0])
                strcpy(j->result, "{\"error\":\"cancelled\"}");
        } else if (parsed) {
            memcpy(j->result, last, length);
            j->result[length] = 0;
            if (!good && json_get(&d, 0, "error") < 0)
                strcpy(j->result, w->timed_out ? "{\"error\":\"task_timeout\"}"
                                               : "{\"error\":\"operation_failed\"}");
        } else {
            good = 0;
            strcpy(j->status, "failed");
            strcpy(j->result, w->timed_out ? "{\"error\":\"task_timeout\"}"
                                           : "{\"error\":\"invalid_adapter_response\"}");
        }
        j->active = 0;
        ++sequence;
        next_probe = 0;
        if (w->owner >= 0 && conn[w->owner].fd == w->owner_fd &&
            conn[w->owner].identity == w->owner_identity && conn[w->owner].pending) {
            if (good)
                response(w->owner, 200, j->result);
            else
                response(w->owner, 409, j->result);
        }
    }
    if (w->fd >= 0)
        close(w->fd);
    memset(w, 0, sizeof *w);
    w->fd = -1;
}
static void read_worker(int k) {
    struct worker *w = &workers[k];
    if (w->fd < 0)
        return;
    ssize_t n = read(w->fd, w->output + w->used, sizeof w->output - w->used - 1);
    if (n > 0) {
        w->used += (size_t)n;
        w->output[w->used] = 0;
        if (w->kind == 2) {
            struct job *j = &jobs[w->job];
            if (strcmp(j->status, "cancelled")) {
                const char *stage = strstr(w->output, "\"stage\":\"playing\"") ? "playing"
                                    : strstr(w->output, "\"stage\":\"synthesizing\"")
                                        ? "synthesizing"
                                        : "running";
                if (strcmp(j->status, stage)) {
                    strcpy(j->status, stage);
                    ++sequence;
                }
            }
        }
        if (w->used >= sizeof w->output - 1) {
            w->overflow = 1;
            kill(-w->pid, SIGTERM);
            w->kill_at = monotime() + 2;
        }
    } else if (n == 0) {
        close(w->fd);
        w->fd = -1;
    }
}
static void read_local(void) {
    char data[LOCAL_MESSAGE];
    ssize_t n = recv(local_socket, data, sizeof data - 1, 0);
    if (n < 1)
        return;
    data[n] = 0;
    struct json_doc d;
    char type[32];
    if (json_parse(&d, data, (size_t)n) < 0 || text(&d, 0, "type", type, sizeof type) < 0)
        return;
    if (strcmp(type, "voice_begin") == 0) {
        for (int k = 0; k < WORKERS; k++) {
            if (workers[k].pid && workers[k].kind == 2 &&
                strcmp(jobs[workers[k].job].kind, "announce") == 0) {
                kill(-workers[k].pid, SIGTERM);
                workers[k].kill_at = monotime() + CLEANUP_GRACE;
                strcpy(jobs[workers[k].job].status, "cancelled");
                strcpy(jobs[workers[k].job].result, "{\"error\":\"voice_preempted\"}");
            }
        }
        voice_active = 1;
        voice_source[0] = 0;
        text(&d, 0, "source", voice_source, sizeof voice_source);
        if (json_copy(&d, json_get(&d, 0, "active_sources"), voice_sources, sizeof voice_sources) <
            0)
            strcpy(voice_sources, "[]");
        if (json_copy(&d, json_get(&d, 0, "volumes"), voice_volumes, sizeof voice_volumes) < 0)
            strcpy(voice_volumes, "{}");
        voice_uncertain = json_bool(&d, json_get(&d, 0, "uncertain"), 1);
        if (json_copy(&d, json_get(&d, 0, "observed_sources"), voice_observations,
                      sizeof voice_observations) < 0)
            strcpy(voice_observations, "{}");
        double pid;
        if (json_number(&d, json_get(&d, 0, "pid"), &pid) == 0 && pid > 1)
            voice_pid = (pid_t)pid;
    } else if (strcmp(type, "voice_end") == 0) {
        voice_active = 0;
        voice_pid = 0;
        voice_source[0] = 0;
        strcpy(voice_sources, "[]");
        strcpy(voice_volumes, "{}");
        strcpy(voice_observations, "{}");
    }
    ++sequence;
    next_probe = 0;
}
static void accept_client(void) {
    struct sockaddr_in peer;
    socklen_t length = sizeof peer;
    int fd = accept(listening, (struct sockaddr *)&peer, &length);
    if (fd < 0)
        return;
    int allowed = cfg.allowed == 0 || ((ntohl(peer.sin_addr.s_addr) & 0xFF000000) == 0x7F000000);
    for (int k = 0; k < cfg.allowed; k++)
        if ((ntohl(peer.sin_addr.s_addr) & cfg.allow[k].mask) == cfg.allow[k].network)
            allowed = 1;
    if (!allowed) {
        close(fd);
        return;
    }
    for (int i = 0; i < CLIENTS; i++)
        if (conn[i].fd < 0) {
            memset(&conn[i], 0, sizeof conn[i]);
            conn[i].fd = fd;
            conn[i].identity = ++connection_identity;
            conn[i].deadline = monotime() + 5;
            nonblock(fd);
            return;
        }
    close(fd);
}
static int server(void) {
    server_started = monotime();
    server_logging = 1;
    diagnostic("initializing", 0);
    snprintf(instance_id, sizeof instance_id, "%ld-%ld-%ld", (long)time(NULL), (long)getpid(),
             (long)(monotime() * 1000000));
    rate_at = monotime();
    for (int i = 0; i < CLIENTS; i++)
        conn[i].fd = -1;
    for (int k = 0; k < WORKERS; k++)
        workers[k].fd = -1;
    char pidfile[512];
    runtime_path(pidfile, sizeof pidfile, "xiaoai_bridge.pid");
    int lock = open(pidfile, O_CREAT | O_RDWR | O_NOFOLLOW, 0600);
    if (lock < 0)
        return initialization_failed("init_pid_open_failed");
    if (flock(lock, LOCK_EX | LOCK_NB) < 0)
        return initialization_failed("init_pid_lock_failed");
    if (fcntl(lock, F_SETFD, FD_CLOEXEC) < 0)
        return initialization_failed("init_pid_cloexec_failed");
    char pid[32];
    int pn = snprintf(pid, sizeof pid, "%ld\n", (long)getpid());
    if (ftruncate(lock, 0) < 0 || write(lock, pid, (size_t)pn) != pn) {
        return initialization_failed("init_pid_write_failed");
    }
    listening = socket(AF_INET, SOCK_STREAM, 0);
    if (listening < 0)
        return initialization_failed("init_http_socket_failed");
    int yes = 1;
    if (setsockopt(listening, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes) < 0)
        return initialization_failed("init_http_options_failed");
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons((uint16_t)cfg.port)};
    if (inet_pton(AF_INET, cfg.bind, &addr.sin_addr) != 1) {
        errno = EINVAL;
        return initialization_failed("init_http_address_failed");
    }
    if (bind(listening, (struct sockaddr *)&addr, sizeof addr) < 0)
        return initialization_failed("init_http_bind_failed");
    if (listen(listening, CLIENTS) < 0)
        return initialization_failed("init_http_listen_failed");
    if (nonblock(listening) < 0)
        return initialization_failed("init_http_nonblock_failed");
    local_socket = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (local_socket < 0)
        return initialization_failed("init_events_socket_failed");
    char socket_path[512];
    runtime_path(socket_path, sizeof socket_path, "xiaoai_bridge.events.sock");
    struct sockaddr_un un = {.sun_family = AF_UNIX};
    if (strlen(socket_path) >= sizeof un.sun_path) {
        errno = ENAMETOOLONG;
        return initialization_failed("init_events_path_failed");
    }
    strcpy(un.sun_path, socket_path);
    unlink(socket_path);
    if (bind(local_socket, (struct sockaddr *)&un, sizeof un) < 0)
        return initialization_failed("init_events_bind_failed");
    if (chmod(socket_path, 0600) < 0)
        return initialization_failed("init_events_permissions_failed");
    if (nonblock(local_socket) < 0)
        return initialization_failed("init_events_nonblock_failed");
    signal(SIGTERM, stop_signal);
    signal(SIGINT, stop_signal);
    signal(SIGPIPE, SIG_IGN);
    diagnostic("http_listening", 0);
    while (!stopping) {
        struct pollfd fds[2 + CLIENTS + WORKERS];
        int map[2 + CLIENTS + WORKERS], count = 0;
        fds[count] = (struct pollfd){listening, POLLIN, 0};
        map[count++] = -1;
        fds[count] = (struct pollfd){local_socket, POLLIN, 0};
        map[count++] = -2;
        for (int i = 0; i < CLIENTS; i++)
            if (conn[i].fd >= 0) {
                short events = conn[i].total                       ? POLLOUT
                               : conn[i].pending || conn[i].events ? 0
                                                                   : POLLIN;
                fds[count] = (struct pollfd){conn[i].fd, events, 0};
                map[count++] = i;
            }
        for (int k = 0; k < WORKERS; k++)
            if (workers[k].pid && workers[k].fd >= 0) {
                fds[count] = (struct pollfd){workers[k].fd, POLLIN, 0};
                map[count++] = CLIENTS + k;
            }
        if (poll(fds, (nfds_t)count, 500) < 0 && errno != EINTR) {
            diagnostic("poll_failed", errno);
            stopping = 1;
        }
        for (int k = 0; k < count; k++)
            if (fds[k].revents) {
                int index = map[k];
                if (index == -1) {
                    accept_client();
                    continue;
                }
                if (index == -2) {
                    read_local();
                    continue;
                }
                if (index >= CLIENTS) {
                    if (fds[k].revents & (POLLIN | POLLHUP))
                        read_worker(index - CLIENTS);
                    continue;
                }
                if (conn[index].fd < 0)
                    continue;
                if (fds[k].revents & (POLLERR | POLLNVAL)) {
                    close_conn(index);
                    continue;
                }
                if (conn[index].total && (fds[k].revents & POLLOUT)) {
                    ssize_t n = write(conn[index].fd, conn[index].output + conn[index].sent,
                                      conn[index].total - conn[index].sent);
                    if (n > 0)
                        conn[index].sent += (size_t)n;
                    else if (errno != EAGAIN && errno != EINTR) {
                        close_conn(index);
                        continue;
                    }
                    if (conn[index].sent == conn[index].total)
                        close_conn(index);
                } else if (!conn[index].pending && !conn[index].events && (fds[k].revents & POLLIN))
                    http_read(index);
                if (conn[index].fd >= 0 && (fds[k].revents & POLLHUP) && !conn[index].total)
                    close_conn(index);
            }
        double now = monotime();
        if (restart_pid && waitpid(restart_pid, NULL, WNOHANG) == restart_pid)
            restart_pid = 0;
        for (int k = 0; k < WORKERS; k++)
            if (workers[k].pid) {
                if (now > workers[k].deadline && !workers[k].kill_at) {
                    workers[k].timed_out = 1;
                    if (workers[k].kind == 0) {
                        probe_status = "probe_timeout";
                        diagnostic("probe_deadline_exceeded", PROBE_TIMEOUT);
                    }
                    kill(-workers[k].pid, SIGTERM);
                    workers[k].kill_at = now + (workers[k].kind == 2 ? CLEANUP_GRACE : 5);
                }
                if (workers[k].kill_at && now > workers[k].kill_at)
                    kill(-workers[k].pid, SIGKILL);
                int status;
                pid_t done = waitpid(workers[k].pid, &status, WNOHANG);
                if (done == workers[k].pid) {
                    while (1) {
                        size_t before = workers[k].used;
                        read_worker(k);
                        if (workers[k].used == before)
                            break;
                    }
                    finish_worker(k, status);
                }
            }
        for (int i = 0; i < CLIENTS; i++)
            if (conn[i].fd >= 0) {
                if (conn[i].events && (conn[i].since != sequence || now >= conn[i].deadline)) {
                    char out[160];
                    snprintf(out, sizeof out, "{\"sequence\":%llu,\"reset\":%s}",
                             (unsigned long long)sequence,
                             conn[i].since > sequence ? "true" : "false");
                    response(i, 200, out);
                } else if (now >= conn[i].deadline && !conn[i].pending)
                    close_conn(i);
            }
        if (voice_active && voice_pid > 1 && kill(voice_pid, 0) < 0 && errno == ESRCH) {
            voice_active = 0;
            voice_pid = 0;
            ++sequence;
        }
        if (now >= next_probe) {
            int running = 0;
            for (int k = 0; k < WORKERS; k++)
                if (workers[k].pid && workers[k].kind == 0)
                    running = 1;
            if (!running) {
                ++probe_attempt;
                if (worker_spawn(0, -1, -1, "probe", "{}") < 0)
                    probe_failed("probe_spawn_failed", errno);
                else {
                    probe_running = 1;
                    if (strcmp(probe_status, "pending") == 0)
                        probe_status = "running";
                    if (!cached_at)
                        diagnostic("probe_started", PROBE_TIMEOUT);
                    next_probe = now + PROBE_INTERVAL;
                }
            }
        }
        if (now - last_airplay_check >= 1) {
            last_airplay_check = now;
            char path[512];
            struct stat st;
            runtime_path(path, sizeof path, "airplay_led.last");
            if (stat(path, &st) == 0 && st.st_mtime != airplay_mtime) {
                airplay_mtime = st.st_mtime;
                ++sequence;
                next_probe = 0;
            }
        }
    }
    close(listening);
    close(local_socket);
    unlink(socket_path);
    for (int i = 0; i < CLIENTS; i++)
        close_conn(i);
    for (int k = 0; k < WORKERS; k++)
        if (workers[k].pid)
            kill(-workers[k].pid, SIGTERM);
    double deadline = monotime() + CLEANUP_GRACE;
    for (int k = 0; k < WORKERS; k++)
        if (workers[k].pid) {
            int status;
            while (waitpid(workers[k].pid, &status, WNOHANG) == 0 && monotime() < deadline) {
                struct timespec pause = {0, 100000000};
                nanosleep(&pause, NULL);
            }
            if (waitpid(workers[k].pid, &status, WNOHANG) == 0) {
                kill(-workers[k].pid, SIGKILL);
                waitpid(workers[k].pid, &status, 0);
            }
            close(workers[k].fd);
        }
    unlink(pidfile);
    close(lock);
    diagnostic("stopped", 0);
    return 0;
}
static int emit(const char *message) {
    struct json_doc d;
    if (strlen(message) >= LOCAL_MESSAGE || json_parse(&d, message, strlen(message)) < 0)
        return 2;
    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd < 0)
        return 2;
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    char path[512];
    runtime_path(path, sizeof path, "xiaoai_bridge.events.sock");
    if (strlen(path) >= sizeof addr.sun_path) {
        close(fd);
        return 2;
    }
    strcpy(addr.sun_path, path);
    ssize_t n = sendto(fd, message, strlen(message), 0, (struct sockaddr *)&addr, sizeof addr);
    close(fd);
    return n == (ssize_t)strlen(message) ? 0 : 1;
}
/* --health exit codes: 0 ready, 1 HTTP unavailable/rejected, 3 probe pending,
 * 4 probe failed but retrying, 5 initial retries exhausted. No response is echoed. */
static int health_wait(int fd, short events, double deadline) {
    while (monotime() < deadline) {
        struct pollfd p = {fd, events, 0};
        int remaining = (int)((deadline - monotime()) * 1000) + 1;
        int result = poll(&p, 1, remaining > 0 ? remaining : 1);
        if (result > 0)
            return !(p.revents & (POLLERR | POLLNVAL));
        if (result < 0 && errno != EINTR)
            break;
    }
    return 0;
}
static int health(void) {
    signal(SIGPIPE, SIG_IGN);
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
        return 1;
    double deadline = monotime() + 2;
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons((uint16_t)cfg.port)};
    inet_pton(AF_INET, strcmp(cfg.bind, "0.0.0.0") == 0 ? "127.0.0.1" : cfg.bind, &addr.sin_addr);
    if (nonblock(fd) < 0)
        goto unavailable;
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        if (errno != EINPROGRESS || !health_wait(fd, POLLOUT, deadline))
            goto unavailable;
        int error = 0;
        socklen_t length = sizeof error;
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &length) < 0 || error)
            goto unavailable;
    }
    char req[512];
    int n = snprintf(req, sizeof req,
                     "GET /api/health HTTP/1.1\r\nHost: "
                     "localhost\r\nAuthorization: Bearer %s\r\n\r\n",
                     cfg.token);
    size_t sent = 0;
    while (sent < (size_t)n && health_wait(fd, POLLOUT, deadline)) {
        ssize_t written = send(fd, req + sent, (size_t)n - sent, 0);
        if (written > 0)
            sent += (size_t)written;
        else if (written == 0 || (errno != EAGAIN && errno != EINTR))
            goto unavailable;
    }
    if (sent != (size_t)n)
        goto unavailable;
    char reply[1024];
    size_t used = 0;
    int ended = 0;
    while (used < sizeof reply - 1 && health_wait(fd, POLLIN, deadline)) {
        ssize_t received = read(fd, reply + used, sizeof reply - used - 1);
        if (received > 0)
            used += (size_t)received;
        else if (received == 0) {
            ended = 1;
            break;
        } else if (errno != EAGAIN && errno != EINTR)
            goto unavailable;
    }
    close(fd);
    if (!ended)
        return 1;
    reply[used] = 0;
    const char *body = strstr(reply, "\r\n\r\n");
    struct json_doc d;
    if (!body || json_parse(&d, body + 4, strlen(body + 4)) < 0 || d.t[0].type != J_OBJECT)
        return 1;
    if (strncmp(reply, "HTTP/1.1 200 ", 13) == 0)
        return json_bool(&d, json_get(&d, 0, "ready"), 0) ? 0 : 1;
    if (strncmp(reply, "HTTP/1.1 503 ", 13) != 0)
        return 1;
    if (json_bool(&d, json_get(&d, 0, "startup_failed"), 0))
        return 5;
    char status[48];
    if (text(&d, 0, "probe_status", status, sizeof status) < 0)
        return 3; /* Compatibility with an older running endpoint. */
    return strcmp(status, "pending") == 0 || strcmp(status, "running") == 0 ? 3 : 4;
unavailable:
    close(fd);
    return 1;
}
static int mpd_state(void) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        puts("unknown");
        return 0;
    }
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    strcpy(addr.sun_path, "/var/run/mpd.sock");
    struct timeval t = {1, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &t, sizeof t);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &t, sizeof t);
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        close(fd);
        puts("unknown");
        return 0;
    }
    char buf[4096];
    ssize_t n = read(fd, buf, sizeof buf - 1);
    if (n < 7 || memcmp(buf, "OK MPD ", 7)) {
        close(fd);
        puts("unknown");
        return 0;
    }
    if (write(fd, "status\n", 7) != 7) {
        close(fd);
        puts("unknown");
        return 0;
    }
    size_t used = 0;
    while (used < sizeof buf - 1) {
        n = read(fd, buf + used, sizeof buf - used - 1);
        if (n < 1)
            break;
        used += (size_t)n;
        buf[used] = 0;
        if (strstr(buf, "\nOK\n") || strncmp(buf, "ACK", 3) == 0)
            break;
    }
    close(fd);
    buf[used] = 0;
    char *state = strstr(buf, "state: ");
    if (state) {
        state += 7;
        if (strncmp(state, "play\n", 5) == 0) {
            puts("play");
            return 0;
        }
        if (strncmp(state, "pause\n", 6) == 0) {
            puts("pause");
            return 0;
        }
        if (strncmp(state, "stop\n", 5) == 0) {
            puts("stop");
            return 0;
        }
    }
    puts("unknown");
    return 0;
}
int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        puts(VERSION);
        return 0;
    }
    if (argc >= 3 && strcmp(argv[1], "--json-op") == 0)
        return bridge_json_cli(argc - 2, argv + 2);
    if (argc == 4 && strcmp(argv[1], "--trace-pcm") == 0)
        return bridge_trace_pcm(argv[2], argv[3]);
    const char *config = getenv("XIAOAI_BRIDGE_CONFIG");
    if (!config)
        config = "/data/xiaoai_bridge/config.json";
    int index = 1;
    if (argc > 2 && strcmp(argv[1], "--config") == 0) {
        config = argv[2];
        index = 3;
    }
    if (load_config(config) < 0) {
        fputs("invalid or insecure bridge configuration\n", stderr);
        return 2;
    }
    if (index < argc && strcmp(argv[index], "--check-config") == 0)
        return 0;
    if (index < argc && strcmp(argv[index], "--mpd-state") == 0)
        return mpd_state();
    if (index < argc && strcmp(argv[index], "--health") == 0)
        return health();
    if (index + 1 < argc && strcmp(argv[index], "--emit") == 0)
        return emit(argv[index + 1]);
    if (index < argc && strcmp(argv[index], "--snapshot") == 0) {
        char file[512], out[1024];
        runtime_path(file, sizeof file, "xiaoai_bridge.source.json");
        if (read_file(file, out, sizeof out) < 0)
            puts("{\"source\":null,\"uncertain\":true}");
        else
            puts(out);
        return 0;
    }
    if (index < argc) {
        fputs("unknown option\n", stderr);
        return 2;
    }
    umask(077);
    return server();
}
