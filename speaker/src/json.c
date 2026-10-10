/* A bounded strict JSON parser. No allocations, duplicate keys or embedded
 * NULs. */
#include "json.h"
#include <ctype.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int hex(char c) {
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}
static void space(const struct json_doc *d, size_t *p) {
    while (*p < d->len && strchr(" \t\r\n", d->s[*p]))
        ++*p;
}
static int utf8(const char *s, size_t len, size_t *p) {
    unsigned char c = (unsigned char)s[*p];
    if (c < 128) {
        ++*p;
        return 0;
    }
    int n = c >= 0xC2 && c <= 0xDF   ? 2
            : c >= 0xE0 && c <= 0xEF ? 3
            : c >= 0xF0 && c <= 0xF4 ? 4
                                     : 0;
    if (!n || *p + (size_t)n > len)
        return -1;
    uint32_t cp = c & ((1u << (7 - n)) - 1u);
    for (int i = 1; i < n; i++) {
        unsigned char b = (unsigned char)s[*p + (size_t)i];
        if ((b & 0xC0) != 0x80)
            return -1;
        cp = (cp << 6) | (b & 0x3F);
    }
    if ((n == 3 && cp < 0x800) || (n == 4 && cp < 0x10000) || cp > 0x10FFFF ||
        (cp >= 0xD800 && cp <= 0xDFFF))
        return -1;
    *p += (size_t)n;
    return 0;
}
static int parse_value(struct json_doc *d, size_t *p, int depth);
static int parse_string(struct json_doc *d, size_t *p) {
    if (d->used >= JSON_MAX_TOKENS)
        return -1;
    int i = d->used++;
    d->t[i] = (struct json_token){J_STRING, (int)*p, 0, 0, 0};
    ++*p;
    while (*p < d->len) {
        unsigned char c = (unsigned char)d->s[*p];
        if (c == '"') {
            ++*p;
            d->t[i].end = (int)*p;
            d->t[i].next = d->used;
            return i;
        }
        if (c < 0x20)
            return -1;
        if (c == '\\') {
            ++*p;
            if (*p >= d->len)
                return -1;
            char esc = d->s[(*p)++];
            if (esc == 'u') {
                if (*p + 4 > d->len)
                    return -1;
                int code = 0;
                for (int k = 0; k < 4; k++) {
                    int h = hex(d->s[(*p)++]);
                    if (h < 0)
                        return -1;
                    code = (code << 4) | h;
                }
                if (!code)
                    return -1;
                if (code >= 0xD800 && code <= 0xDBFF) {
                    if (*p + 6 > d->len || d->s[*p] != '\\' || d->s[*p + 1] != 'u')
                        return -1;
                    *p += 2;
                    int low = 0;
                    for (int k = 0; k < 4; k++) {
                        int h = hex(d->s[(*p)++]);
                        if (h < 0)
                            return -1;
                        low = (low << 4) | h;
                    }
                    if (low < 0xDC00 || low > 0xDFFF)
                        return -1;
                } else if (code >= 0xDC00 && code <= 0xDFFF)
                    return -1;
            } else if (!strchr("\"\\/bfnrt", esc))
                return -1;
        } else if (utf8(d->s, d->len, p) < 0)
            return -1;
    }
    return -1;
}
static int parse_value(struct json_doc *d, size_t *p, int depth) {
    space(d, p);
    if (depth > 24 || *p >= d->len || d->used >= JSON_MAX_TOKENS)
        return -1;
    char c = d->s[*p];
    if (c == '"')
        return parse_string(d, p);
    int i = d->used++;
    d->t[i] = (struct json_token){J_NULL, (int)*p, 0, 0, 0};
    if (c == '{' || c == '[') {
        char close = c == '{' ? '}' : ']';
        d->t[i].type = c == '{' ? J_OBJECT : J_ARRAY;
        ++*p;
        space(d, p);
        if (*p < d->len && d->s[*p] == close) {
            ++*p;
            goto end;
        }
        for (;;) {
            if (c == '{') {
                if (*p >= d->len || d->s[*p] != '"')
                    return -1;
                int key = parse_string(d, p);
                if (key < 0)
                    return -1;
                char name[256], old[256];
                if (json_string(d, key, name, sizeof name) < 0)
                    return -1;
                for (int k = i + 1; k < key;) {
                    if (json_string(d, k, old, sizeof old) < 0 || strcmp(name, old) == 0)
                        return -1;
                    k = d->t[k + 1].next;
                }
                space(d, p);
                if (*p >= d->len || d->s[(*p)++] != ':')
                    return -1;
            }
            if (parse_value(d, p, depth + 1) < 0)
                return -1;
            ++d->t[i].count;
            space(d, p);
            if (*p >= d->len)
                return -1;
            c = d->t[i].type == J_OBJECT ? '{' : '[';
            char sep = d->s[(*p)++];
            if (sep == close)
                break;
            if (sep != ',')
                return -1;
            space(d, p);
        }
    } else if (c == 't' || c == 'f' || c == 'n') {
        const char *word = c == 't' ? "true" : c == 'f' ? "false" : "null";
        size_t n = strlen(word);
        if (*p + n > d->len || memcmp(d->s + *p, word, n))
            return -1;
        *p += n;
        d->t[i].type = c == 'n' ? J_NULL : J_BOOL;
    } else {
        d->t[i].type = J_NUMBER;
        if (d->s[*p] == '-')
            ++*p;
        if (*p >= d->len)
            return -1;
        if (d->s[*p] == '0')
            ++*p;
        else {
            if (d->s[*p] < '1' || d->s[*p] > '9')
                return -1;
            while (*p < d->len && isdigit((unsigned char)d->s[*p]))
                ++*p;
        }
        if (*p < d->len && d->s[*p] == '.') {
            ++*p;
            if (*p >= d->len || !isdigit((unsigned char)d->s[*p]))
                return -1;
            while (*p < d->len && isdigit((unsigned char)d->s[*p]))
                ++*p;
        }
        if (*p < d->len && (d->s[*p] == 'e' || d->s[*p] == 'E')) {
            ++*p;
            if (*p < d->len && (d->s[*p] == '+' || d->s[*p] == '-'))
                ++*p;
            if (*p >= d->len || !isdigit((unsigned char)d->s[*p]))
                return -1;
            while (*p < d->len && isdigit((unsigned char)d->s[*p]))
                ++*p;
        }
    }
end:
    d->t[i].end = (int)*p;
    d->t[i].next = d->used;
    return i;
}
int json_parse(struct json_doc *d, const char *s, size_t len) {
    d->s = s;
    d->len = len;
    d->used = 0;
    if (len > 131072 || memchr(s, 0, len))
        return -1;
    size_t p = 0;
    if (parse_value(d, &p, 0) != 0)
        return -1;
    space(d, &p);
    return p == len ? 0 : -1;
}
int json_string(const struct json_doc *d, int i, char *out, size_t cap) {
    if (i < 0 || i >= d->used || d->t[i].type != J_STRING || !cap)
        return -1;
    size_t n = 0;
    for (int p = d->t[i].start + 1; p < d->t[i].end - 1; p++) {
        unsigned char c = (unsigned char)d->s[p];
        if (c == '\\') {
            c = (unsigned char)d->s[++p];
            if (c == 'u') {
                uint32_t code = 0;
                for (int k = 0; k < 4; k++)
                    code = (code << 4) | (unsigned)hex(d->s[++p]);
                if (code >= 0xD800 && code <= 0xDBFF) {
                    p += 2;
                    uint32_t low = 0;
                    for (int k = 0; k < 4; k++)
                        low = (low << 4) | (unsigned)hex(d->s[++p]);
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                }
                unsigned char bytes[4];
                int count = 1;
                if (code < 0x80)
                    bytes[0] = (unsigned char)code;
                else if (code < 0x800) {
                    count = 2;
                    bytes[0] = 0xC0 | (code >> 6);
                    bytes[1] = 0x80 | (code & 63);
                } else if (code < 0x10000) {
                    count = 3;
                    bytes[0] = 0xE0 | (code >> 12);
                    bytes[1] = 0x80 | ((code >> 6) & 63);
                    bytes[2] = 0x80 | (code & 63);
                } else {
                    count = 4;
                    bytes[0] = 0xF0 | (code >> 18);
                    bytes[1] = 0x80 | ((code >> 12) & 63);
                    bytes[2] = 0x80 | ((code >> 6) & 63);
                    bytes[3] = 0x80 | (code & 63);
                }
                if (n + (size_t)count >= cap)
                    return -1;
                for (int k = 0; k < count; k++)
                    out[n++] = (char)bytes[k];
                continue;
            }
            if (c == 'b')
                c = '\b';
            else if (c == 'f')
                c = '\f';
            else if (c == 'n')
                c = '\n';
            else if (c == 'r')
                c = '\r';
            else if (c == 't')
                c = '\t';
        }
        if (n + 1 >= cap)
            return -1;
        out[n++] = (char)c;
    }
    out[n] = 0;
    return (int)n;
}
int json_get(const struct json_doc *d, int object, const char *key) {
    if (object < 0 || object >= d->used || d->t[object].type != J_OBJECT)
        return -1;
    char name[256];
    for (int i = object + 1; i < d->t[object].next; i = d->t[i + 1].next) {
        if (json_string(d, i, name, sizeof name) >= 0 && strcmp(name, key) == 0)
            return i + 1;
    }
    return -1;
}
int json_bool(const struct json_doc *d, int i, int fallback) {
    if (i < 0 || i >= d->used || d->t[i].type != J_BOOL)
        return fallback;
    return d->s[d->t[i].start] == 't';
}
int json_copy(const struct json_doc *d, int i, char *out, size_t cap) {
    if (i < 0 || i >= d->used)
        return -1;
    size_t n = (size_t)(d->t[i].end - d->t[i].start);
    if (n >= cap)
        return -1;
    memcpy(out, d->s + d->t[i].start, n);
    out[n] = 0;
    return (int)n;
}
int json_number(const struct json_doc *d, int i, double *out) {
    char buf[64], *end;
    if (i < 0 || i >= d->used || d->t[i].type != J_NUMBER || json_copy(d, i, buf, sizeof buf) < 0)
        return -1;
    double n = strtod(buf, &end);
    if (*end || !isfinite(n))
        return -1;
    *out = n;
    return 0;
}
int json_quote(const char *s, char *out, size_t cap) {
    size_t n = 0;
    if (cap < 3)
        return -1;
    out[n++] = '"';
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c < ' ' || c == '"' || c == '\\') {
            if (n + 6 >= cap)
                return -1;
            if (c == '"' || c == '\\') {
                out[n++] = '\\';
                out[n++] = (char)c;
            } else {
                int k = snprintf(out + n, cap - n, "\\u%04x", c);
                n += (size_t)k;
            }
        } else {
            if (n + 2 >= cap)
                return -1;
            out[n++] = (char)c;
        }
    }
    if (n + 2 > cap)
        return -1;
    out[n++] = '"';
    out[n] = 0;
    return (int)n;
}
