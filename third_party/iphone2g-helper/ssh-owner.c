/* Keep the USB-owning process alive for exactly the foreground SSH lifetime. */
#include <errno.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef OPENUP_SSHD_PATH
#define OPENUP_SSHD_PATH "/sbin/sshd"
#endif

static volatile sig_atomic_t ssh_child = 0;
static volatile sig_atomic_t stop_signal = 0;

static void stop_ssh(int signal_number) {
    stop_signal = signal_number;
    if (ssh_child > 0) kill((pid_t)ssh_child, signal_number);
}

int run_sshd(void) {
    struct sigaction action;
    pid_t child, waited;
    int status = 0;
    sigset_t blocked, previous;
    /* Avoid the fork/handler race: install handlers and publish the child PID
       before accepting termination. The child restores default handlers. */
    sigemptyset(&blocked); sigaddset(&blocked, SIGTERM); sigaddset(&blocked, SIGINT);
    if (sigprocmask(SIG_BLOCK, &blocked, &previous)) return 1;
    action.sa_handler = stop_ssh;
    sigemptyset(&action.sa_mask); action.sa_flags = 0;
    if (sigaction(SIGTERM, &action, 0) || sigaction(SIGINT, &action, 0)) {
        sigprocmask(SIG_SETMASK, &previous, 0); return 1;
    }
    child = fork();
    if (child == 0) {
        char *const arguments[] = {OPENUP_SSHD_PATH, "-D", "-e", 0};
        action.sa_handler = SIG_DFL;
        sigaction(SIGTERM, &action, 0); sigaction(SIGINT, &action, 0);
        sigprocmask(SIG_SETMASK, &previous, 0);
        execv(OPENUP_SSHD_PATH, arguments);
        _exit(127);
    }
    if (child < 0) { sigprocmask(SIG_SETMASK, &previous, 0); return 1; }
    ssh_child = child;
    sigprocmask(SIG_SETMASK, &previous, 0);
    do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    ssh_child = 0;
    if (waited != child) return 1;
    if (stop_signal) return 128 + stop_signal;
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return WIFSIGNALED(status) ? 128 + WTERMSIG(status) : 1;
}
