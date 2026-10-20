// Only used by the Mac development credential regression; never bundled.
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
volatile int fixture_version = VERSION;
int main(int argc, char **argv) {
    if (argc != 2 || fixture_version != VERSION) return 2;
    pid_t child = fork();
    if (child == -1) return 3;
    if (child == 0) {
        char *const arguments[] = { argv[1], NULL };
        char *const environment[] = { NULL };
        execve(argv[1], arguments, environment);
        _exit(4);
    }
    int status;
    while (waitpid(child, &status, 0) == -1) if (errno != EINTR) return 5;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 6;
}
