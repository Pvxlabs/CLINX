/* Fixed identity drop for HostExecutor subprocesses, never an authority API.
 * Installed root:clinx-transport 4750 under a root-controlled release.
 * Original Codex filesystem identity from root-owned deployment configuration.
 * No caller-selected identity/environment or inherited FD.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <grp.h>
#include <fcntl.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/prctl.h>
#include <unistd.h>

int main(int argc, char **argv) {
    struct passwd *account = getpwnam("clinx-transport");
    if (!account || getuid() != account->pw_uid || geteuid() != 0 || argc < 2) {
        fputs("WORKER_LAUNCHER_PEER_DENIED\n", stderr); return 126;
    }
    int identity = open("/var/lib/clinx-rust/config/worker-user", O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    char name[256] = {0};
    if (identity < 0 || fstat(identity, &info) || info.st_uid != 0
        || (info.st_mode & 0022) || !S_ISREG(info.st_mode)
        || info.st_size < 1 || info.st_size >= (off_t)sizeof(name)) return 126;
    ssize_t count = read(identity, name, sizeof(name) - 1);
    close(identity);
    if (count != info.st_size) return 126;
    name[strcspn(name, "\r\n")] = 0;
    if (!name[0] || strspn(name, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") != strlen(name)
        || !strcmp(name, "clinx-authority") || !strcmp(name, "clinx-api") || !strcmp(name, "clinx-transport")) return 126;
    account = getpwnam(name);
    if (!account || account->pw_uid == 0) return 126;
    uid_t uid = account->pw_uid; gid_t gid = account->pw_gid;
    char *home = strdup(account->pw_dir);
    if (!home) return 126;
    if (close_range(3, ~0U, 0) || initgroups(name, gid) || setresgid(gid, gid, gid)
        || setresuid(uid, uid, uid) || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        || prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) || getuid() != uid || geteuid() != uid) {
        perror("WORKER_IDENTITY_DROP_FAILED"); return 126;
    }
    if (clearenv() || setenv("PATH", "/usr/local/bin:/usr/bin:/bin", 1)
        || setenv("HOME", home, 1)
        || setenv("USER", name, 1) || setenv("LOGNAME", name, 1)
        || setenv("LANG", "C.UTF-8", 1)) return 126;
    execvp(argv[1], &argv[1]); perror("WORKER_EXEC_FAILED"); return 127;
}
