#ifndef XIAOAI_AIRPLAY_H
#define XIAOAI_AIRPLAY_H
#include <stddef.h>
#ifndef AIRPLAY_ARTWORK_DIRECTORY
#define AIRPLAY_ARTWORK_DIRECTORY "/tmp/shairport-sync/.cache/coverart"
#endif
#define AIRPLAY_ARTWORK_LIMIT (2 * 1024 * 1024)
/* Parse one already collected RemoteControl GetAll reply. No D-Bus or file I/O. */
int bridge_airplay_metadata(void);
int bridge_airplay_artwork_id(const char *name);
/* Caller owns the returned FD. Walk every directory without following symlinks. */
int bridge_airplay_artwork_open(const char *directory, const char *id, size_t *size,
                                const char **mime);
#endif
