// Minimal macOS launcher so a child Python process may use Bluetooth and audio input.
//
// macOS (TCC) attributes Bluetooth access to the "responsible" process. A python started
// from a CLI tool without an Info.plist (for example the Claude Code agent) is killed with
// SIGABRT on first CoreBluetooth use. Wrapped in GoveeBLE.app, which declares
// NSBluetoothAlwaysUsageDescription and is launched through LaunchServices (`open`), this
// launcher becomes the responsible process, macOS asks once for Bluetooth (and microphone,
// which covers loopback devices like BlackHole) permission, and the python child inherits it.
// Without NSMicrophoneUsageDescription, audio input silently returns zeros.
//
// Usage (see tools/build_ble_app.sh):
//   open -W -n -a build/GoveeBLE.app --stdout out.log --stderr out.log --args <python> <args...>
//
// It spawns (does not exec) the child so responsibility stays with this bundle, forwards
// SIGINT/SIGTERM/SIGHUP so the child's cleanup (rt_disable) runs, and exits with its status.
// LaunchServices starts apps in "/", so GOVEE_RT_CWD (set by ble_run.sh) restores the caller's
// working directory.

#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: GoveeBLE <program> [args...]\n");
        return 2;
    }
    const char *cwd = getenv("GOVEE_RT_CWD");
    if (cwd && *cwd && chdir(cwd) != 0) perror("GoveeBLE: chdir");
    signal(SIGINT, forward);
    signal(SIGTERM, forward);
    signal(SIGHUP, forward);

    int rc = posix_spawn(&child, argv[1], NULL, NULL, &argv[1], environ);
    if (rc != 0) {
        fprintf(stderr, "GoveeBLE: cannot spawn %s: %s\n", argv[1], strerror(rc));
        return 127;
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        // EINTR from a forwarded signal; keep waiting for the child to finish its cleanup
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}
