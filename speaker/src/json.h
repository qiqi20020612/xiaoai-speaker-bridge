#ifndef XIAOAI_JSON_H
#define XIAOAI_JSON_H
#include <stddef.h>
#define JSON_MAX_TOKENS 2048

enum json_type { J_OBJECT, J_ARRAY, J_STRING, J_NUMBER, J_BOOL, J_NULL };
struct json_token {
    enum json_type type;
    int start, end, next, count;
};
struct json_doc {
    const char *s;
    size_t len;
    int used;
    struct json_token t[JSON_MAX_TOKENS];
};
int json_parse(struct json_doc *d, const char *s, size_t len);
int json_get(const struct json_doc *d, int object, const char *key);
int json_string(const struct json_doc *d, int index, char *out, size_t cap);
int json_bool(const struct json_doc *d, int index, int fallback);
int json_number(const struct json_doc *d, int index, double *out);
int json_copy(const struct json_doc *d, int index, char *out, size_t cap);
int json_quote(const char *s, char *out, size_t cap);
#endif
