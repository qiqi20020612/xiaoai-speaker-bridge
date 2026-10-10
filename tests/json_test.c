#include "../speaker/src/json.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
int main(void) {
    struct json_doc doc;
    char out[128];
    const char *good = "{\"text\":\"你好\\uD83D\\uDE00\",\"n\":30,\"b\":false,\"a\":[1,2]}";
    assert(json_parse(&doc, good, strlen(good)) == 0);
    assert(json_string(&doc, json_get(&doc, 0, "text"), out, sizeof out) > 0);
    assert(strcmp(out, "你好😀") == 0);
    assert(json_bool(&doc, json_get(&doc, 0, "b"), 1) == 0);
    const char *bad[] = {"{\"a\":1,\"a\":2}", "{\"a\":1,}",          "[1,]",        "01",
                         "true false",        "{\"s\":\"\\u0000\"}", "\"\\uD800\"", "{\"x\":nan}",
                         "\"\xC0\x80\""};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++)
        assert(json_parse(&doc, bad[i], strlen(bad[i])) < 0);
    char quote[64];
    assert(json_quote("hello\n\"", quote, sizeof quote) > 0);
    assert(strcmp(quote, "\"hello\\u000a\\\"\"") == 0);
    puts("strict JSON and UTF-8 checks passed");
    return 0;
}
