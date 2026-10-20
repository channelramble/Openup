/* Freestanding Linux x86-64 launcher. No libc or other runtime dependency. */
static long syscall3(long number, long a, long b, long c) {
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a), "S"(b), "d"(c)
                     : "rcx", "r11", "memory");
    return result;
}

__attribute__((noreturn)) static void fail(void) {
    static const char message[] = "Could not open the bundled runtime. Extract the complete application folder.\n";
    syscall3(1, 2, (long)message, sizeof(message) - 1);
    syscall3(60, 1, 0, 0);
    __builtin_unreachable();
}

static void append(char *out, const char *prefix, const char *suffix) {
    long index = 0;
    while (*prefix && index < 4094) out[index++] = *prefix++;
    while (*suffix && index < 4094) out[index++] = *suffix++;
    if (*prefix || *suffix) fail();
    out[index] = 0;
}

__attribute__((noreturn, used)) void launch(long *stack) {
    long argc = stack[0];
    char **argv = (char **)(stack + 1);
    char **environment = argv + argc + 1;
    static char root[4096], python[4096], script[4096];
    long count = syscall3(89, (long)"/proc/self/exe", (long)root, 4095);
    if (count <= 0 || count >= 4095 || argc > 96) fail();
    root[count] = 0;
    while (count > 0 && root[count] != '/') count--;
    if (!count) fail();
    root[count] = 0;
    append(python, root, "/runtime/bin/python3");
    append(script, root, "/desktop.py");
    char *arguments[101];
    arguments[0] = python; arguments[1] = "-I"; arguments[2] = "-B"; arguments[3] = script;
    for (long i = 1; i < argc; i++) arguments[i + 3] = argv[i];
    arguments[argc + 3] = 0;
    syscall3(59, (long)python, (long)arguments, (long)environment);
    fail();
}

__asm__(".global _start\n.type _start,@function\n_start:\n"
        "mov %rsp,%rdi\nandq $-16,%rsp\ncall launch\n");
