#include "socket.h"
#include "../util/log.h"
#include <stdio.h>

#define MAX_CONNS 64

struct conn {
    int fd;
};

/* int ghost(void) { return 0; } */
static int open_socket(const char *host, int port)
{
    if (port < 0) {
        return -1;
    }
    log_info("opening %s", host);
    return connect_to(host, port);
}

int close_socket(struct conn *c);
