/* Openup, MIT. Read-only identity and key835 query for the 3.1.3 m68 RAM disk.
 * IOAESAccelerator ABI and key835 derivation: Jean-Baptiste Bedrune and
 * Jean Sigwald, iphone-dataprotection, revision 572dd5cd8c07 (see NOTICE.txt).
 * Identity/key queries are read-only. --bootstrap updates only the verified
 * RAM root and volatile USB/network setup; no NAND repair or passcode attempts.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/mount.h>
#include <sys/sysctl.h>
#include <IOKit/IOKitLib.h>

/* The pre-iOS-4 ABI ends at mask: 72 bytes on 32-bit ARM. */
struct aes_request {
    void *cleartext;
    void *ciphertext;
    uint32_t size;
    uint8_t iv[16];
    uint32_t mode;
    uint32_t bits;
    uint8_t keybuf[32];
    uint32_t mask;
};
typedef char aes_request_size_must_be_72[(sizeof(struct aes_request) == 72) ? 1 : -1];
int bootstrap(void);

static int ram_root(void) {
    struct stat root, md;
    struct statfs fs;
    return stat("/", &root) == 0 && stat("/dev/md0", &md) == 0 &&
        statfs("/", &fs) == 0 && S_ISBLK(md.st_mode) &&
        root.st_dev == md.st_rdev && strcmp(fs.f_fstypename, "hfs") == 0;
}

static int query(const char *name, char value[64]) {
    size_t size = 64;
    memset(value, 0, 64);
    if (sysctlbyname(name, value, &size, NULL, 0) || size < 2 || size > 64 ||
        value[size - 1] != '\0') return 0;
    return 1;
}

static int key835(void) {
    io_service_t service;
    io_connect_t connection = 0;
    struct aes_request request;
    size_t size = sizeof(request);
    IOReturn result;
    unsigned char *input = valloc(16), *output = valloc(16);
    unsigned int i;
    if (!input || !output) { free(input); free(output); return 1; }
    memset(input, 1, 16);
    memset(output, 0, 16);
    memset(&request, 0, sizeof(request));
    request.cleartext = input;
    request.ciphertext = output;
    request.size = 16;
    request.bits = 128;
    request.mask = 0x7d0; /* UID hardware key; AES-CBC, zero IV, encrypt. */
    service = IOServiceGetMatchingService(kIOMasterPortDefault, IOServiceMatching("IOAESAccelerator"));
    if (!service) { free(input); free(output); return 1; }
    result = IOServiceOpen(service, mach_task_self(), 0, &connection);
    IOObjectRelease(service);
    if (result != kIOReturnSuccess) { free(input); free(output); return 1; }
    result = IOConnectCallStructMethod(connection, 1, &request, size, &request, &size);
    IOServiceClose(connection);
    if (result == kIOReturnSuccess && size == sizeof(request)) {
        for (i = 0; i < 16; ++i) printf("%02x", output[i]);
        puts("");
    }
    memset(input, 0, 16); memset(output, 0, 16); memset(&request, 0, sizeof(request));
    free(input); free(output);
    return result == kIOReturnSuccess && size == sizeof(request) ? 0 : 1;
}

int main(int argc, char **argv) {
    char product[64], board[64];
    if (argc != 2 || (strcmp(argv[1], "--identity") && strcmp(argv[1], "--key835") && strcmp(argv[1], "--bootstrap"))) return 2;
    if (!ram_root() || !query("hw.machine", product) || !query("hw.model", board) ||
        strcmp(product, "iPhone1,1") || strcasecmp(board, "m68ap")) {
        fputs("Expected an original iPhone running the m68 SSH RAM disk.\n", stderr);
        return 1;
    }
    if (!strcmp(argv[1], "--identity")) {
        puts("{\"product\":\"iPhone1,1\",\"board\":\"m68ap\",\"schema\":1}");
        return 0;
    }
    if (!strcmp(argv[1], "--bootstrap")) {
        int result = bootstrap();
        if (result) {
            fputs("Original-iPhone USB/SSH bootstrap failed; no restore service was started.\n", stderr);
            return result;
        }
        return 0;
    }
    if (key835()) {
        fputs("The RAM disk could not read the legacy hardware key.\n", stderr);
        return 1;
    }
    return 0;
}
