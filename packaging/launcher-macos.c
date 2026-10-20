#include <mach-o/dyld.h>
#include <CoreFoundation/CoreFoundation.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 1) return 1;
    char executable[PATH_MAX], path[PATH_MAX], python[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) || !realpath(executable, path)) return 1;
    char *slash = strrchr(path, '/');
    if (!slash) return 1;
    *slash = '\0';
    if (snprintf(python, sizeof(python), "%s/../Resources/app/runtime/bin/python3", path) >= (int)sizeof(python) ||
        snprintf(script, sizeof(script), "%s/../Resources/app/desktop.py", path) >= (int)sizeof(script)) return 1;
    char **arguments = calloc((size_t)argc + 4, sizeof(*arguments));
    if (!arguments) return 1;
    arguments[0] = python;
    arguments[1] = "-I";
    arguments[2] = "-B";
    arguments[3] = script;
    for (int i = 1; i < argc; i++) arguments[i + 3] = argv[i];
    execv(python, arguments);
    free(arguments);
    CFUserNotificationDisplayAlert(0, kCFUserNotificationStopAlertLevel, NULL, NULL, NULL,
        CFSTR("Bruteforce Legacy iOS"), CFSTR("The bundled runtime could not start. Extract the complete app and try again."),
        CFSTR("OK"), NULL, NULL, NULL);
    return 1;
}
