#ifndef XIAOAI_JSON_OPS_H
#define XIAOAI_JSON_OPS_H
/* Fixed local JSON operations. No configuration load, network or mixer access. */
int bridge_json_cli(int argc, char **argv);
int bridge_trace_pcm(const char *path, const char *cycle);
#endif
