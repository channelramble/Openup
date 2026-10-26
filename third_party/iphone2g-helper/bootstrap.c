/* Openup, MIT; USB interface sequence follows iphone-dataprotection's
 * restored_external.c (BSD-3-Clause, see NOTICE.txt). This bootstrap updates
 * only the already verified HFS RAM root and volatile USB/network services.
 * It never opens a NAND device, runs a restore service, or writes NVRAM.
 */
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <sys/sockio.h>
#include <netinet/in.h>
#include <net/if.h>
#include <hfs/hfs_mount.h>
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOCFPlugIn.h>
#include "../iphone-dataprotection/source/ramdisk_tools/IOUSBDeviceControllerLib.h"
int run_sshd(void);

static int writable_ram_root(void) {
    struct stat root, md;
    struct statfs fs;
    struct hfs_mount_args args;
    if (stat("/", &root) || stat("/dev/md0", &md) || statfs("/", &fs) ||
        !S_ISBLK(md.st_mode) || root.st_dev != md.st_rdev ||
        strcmp(fs.f_fstypename, "hfs") || strcmp(fs.f_mntonname, "/") ||
        !(fs.f_flags & MNT_ROOTFS)) return 1;
    if (!(fs.f_flags & MNT_RDONLY)) return 0;
    memset(&args, 0, sizeof(args));
    args.fspec = "/dev/md0";
    args.hfs_uid = (uid_t)-1; args.hfs_gid = (gid_t)-1;
    args.hfs_mask = (mode_t)-1; args.hfs_encoding = (uint32_t)-1;
    args.hfs_timezone.tz_minuteswest = -1;
    if (mount("hfs", "/", (fs.f_flags & MNT_VISFLAGMASK & ~MNT_RDONLY) | MNT_UPDATE, &args)) return 1;
    return statfs("/", &fs) || (fs.f_flags & MNT_RDONLY) ? 1 : 0;
}

static CFStringRef session_serial(void) {
    char value[41];
    struct stat info, root;
    int fd;
    unsigned int i;
    /* Stock 3.1.3 /etc is a symlink to private/etc. Check the canonical
       directories and their RAM device numbers instead of following it. */
    if (stat("/", &root) || lstat("/private", &info) || !S_ISDIR(info.st_mode) ||
        info.st_dev != root.st_dev || lstat("/private/etc", &info) ||
        !S_ISDIR(info.st_mode) || info.st_dev != root.st_dev) return NULL;
    fd = open("/private/etc/openup-2g-session", O_RDONLY | O_NOFOLLOW);
    if (fd < 0) return NULL;
    if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_size != 40 ||
        info.st_dev != root.st_dev || info.st_uid != 0 || (info.st_mode & 0777) != 0600 ||
        read(fd, value, 41) != 40) {
        close(fd); return NULL;
    }
    close(fd);
    value[40] = 0;
    for (i = 0; i < 40; ++i)
        if (!((value[i] >= '0' && value[i] <= '9') || (value[i] >= 'a' && value[i] <= 'f'))) return NULL;
    return CFStringCreateWithCString(kCFAllocatorDefault, value, kCFStringEncodingASCII);
}

static int loopback(void) {
    struct ifreq request;
    struct ifaliasreq address;
    int fd = socket(AF_INET, SOCK_DGRAM, 0), result;
    if (fd < 0) return 1;
    memset(&request, 0, sizeof(request));
    strcpy(request.ifr_name, "lo0");
    if (ioctl(fd, SIOCGIFFLAGS, &request)) { close(fd); return 1; }
    request.ifr_flags |= IFF_UP;
    if (ioctl(fd, SIOCSIFFLAGS, &request)) { close(fd); return 1; }
    memset(&address, 0, sizeof(address));
    strcpy(address.ifra_name, "lo0");
    ((struct sockaddr_in *)&address.ifra_addr)->sin_family = AF_INET;
    ((struct sockaddr_in *)&address.ifra_addr)->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ((struct sockaddr_in *)&address.ifra_addr)->sin_len = sizeof(struct sockaddr_in);
    ((struct sockaddr_in *)&address.ifra_mask)->sin_family = AF_INET;
    ((struct sockaddr_in *)&address.ifra_mask)->sin_addr.s_addr = htonl(IN_CLASSA_NET);
    ((struct sockaddr_in *)&address.ifra_mask)->sin_len = sizeof(struct sockaddr_in);
    result = ioctl(fd, SIOCAIFADDR, &address);
    if (result && errno == EEXIST) result = 0;
    close(fd);
    return result ? 1 : 0;
}

static int watchdog(void) {
    io_service_t service = IOServiceGetMatchingService(kIOMasterPortDefault, IOServiceMatching("IOWatchDogTimer"));
    int zero = 0;
    CFNumberRef number;
    IOReturn result;
    if (!service) return 0;
    number = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &zero);
    if (!number) { IOObjectRelease(service); return 1; }
    result = IORegistryEntrySetCFProperties(service, number);
    CFRelease(number); IOObjectRelease(service);
    return result != kIOReturnSuccess;
}

static int usb(CFStringRef serial) {
    IOUSBDeviceDescriptionRef description = NULL;
    IOUSBDeviceControllerRef controller = NULL;
    io_service_t service = 0;
    IOCFPlugInInterface **plugin = NULL;
    void *interface = NULL;
    IOReturn (**table)(void *, ...);
    CFMutableDictionaryRef match, property;
    SInt32 score = 0;
    unsigned int tries;
    int result = 1;
    description = IOUSBDeviceDescriptionCreateFromDefaults(kCFAllocatorDefault);
    if (!description) goto done;
    IOUSBDeviceDescriptionSetSerialString(description, serial);
    for (tries = 0; tries < 20; ++tries) {
        if (IOUSBDeviceControllerCreate(kCFAllocatorDefault, &controller) == kIOReturnSuccess) break;
        sleep(1);
    }
    if (!controller || IOUSBDeviceControllerSetDescription(controller, description) != kIOReturnSuccess) goto done;
    for (tries = 0; tries < 20; ++tries) {
        match = IOServiceMatching("IOUSBDeviceInterface");
        property = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
        if (!match || !property) {
            if (match) CFRelease(match);
            if (property) CFRelease(property);
            goto done;
        }
        CFDictionarySetValue(property, CFSTR("USBDeviceFunction"), CFSTR("PTP"));
        CFDictionarySetValue(match, CFSTR("IOPropertyMatch"), property);
        CFRelease(property);
        service = IOServiceGetMatchingService(kIOMasterPortDefault, match);
        if (service) break;
        sleep(1);
    }
    if (!service) goto done;
    if (IOCreatePlugInInterfaceForService(service,
        CFUUIDGetConstantUUIDWithBytes(NULL, 0x9e,0x72,0x21,0x7e,0x8a,0x60,0x11,0xdb,0xbf,0x57,0x00,0x0d,0x93,0x6d,0x06,0xd2),
        kIOCFPlugInInterfaceID, &plugin, &score) != kIOReturnSuccess || !plugin) goto done;
    if ((*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(
        CFUUIDGetConstantUUIDWithBytes(NULL, 0xea,0x33,0xba,0x4f,0x8a,0x60,0x11,0xdb,0x84,0xdb,0x00,0x0d,0x93,0x6d,0x06,0xd2)),
        &interface) != 0 || !interface) goto done;
    table = *((IOReturn (***)(void *, ...))interface);
    if (!table || !table[0x10/4] || !table[0x2c/4] || !table[0x30/4] || !table[0x34/4] || !table[0x44/4]) goto done;
    if (table[0x10/4](interface, 0) != kIOReturnSuccess ||
        table[0x2c/4](interface, 0xff, 0) != kIOReturnSuccess ||
        table[0x30/4](interface, 0x50, 0) != kIOReturnSuccess ||
        table[0x34/4](interface, 0x43, 0) != kIOReturnSuccess ||
        table[0x44/4](interface, 0) != kIOReturnSuccess) goto done;
    result = 0;
done:
    /* Match the proven source's plugin lifecycle: keep the configured USB
       interface open; destroying the plugin does not close that interface. */
    if (plugin) IODestroyPlugInInterface(plugin);
    if (service) IOObjectRelease(service);
    /* Successful USB objects intentionally live for the owner process's
       lifetime, as in the original implementation. Closing their Mach ports
       before SSH exits can take the USB interface away from the host. */
    if (result && controller) CFRelease(controller);
    if (result && description) CFRelease(description);
    return result;
}

int bootstrap(void) {
    CFStringRef serial;
    int result;
    alarm(45); /* Bounds even a stalled driver call; failure prevents SSH. */
    if (writable_ram_root() || watchdog() || loopback()) return 1;
    serial = session_serial();
    if (!serial) return 1;
    result = usb(serial);
    CFRelease(serial);
    alarm(0);
    return result ? result : run_sshd();
}
