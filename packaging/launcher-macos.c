#include <mach-o/dyld.h>
#include <CoreFoundation/CoreFoundation.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    char executable[PATH_MAX], path[PATH_MAX], python[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) || !realpath(executable, path)) return 1;
    char *slash = strrchr(path, '/');
    if (!slash) return 1;
    *slash = '\0';
    if (snprintf(python, sizeof(python), "%s/../Resources/app/runtime/bin/python3", path) >= (int)sizeof(python) ||
        snprintf(script, sizeof(script), "%s/../Resources/app/desktop.py", path) >= (int)sizeof(script)) return 1;
    execl(python, python, "-I", "-B", script, (char *)NULL);
    CFUserNotificationDisplayAlert(0, kCFUserNotificationStopAlertLevel, NULL, NULL, NULL,
        CFSTR("Bruteforce Legacy iOS"), CFSTR("The bundled runtime could not start. Extract the complete app and try again."),
        CFSTR("OK"), NULL, NULL, NULL);
    return 1;
}
