"""Run the actual RAM-root helper with stubbed Darwin filesystem calls."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "third_party/iphone-dataprotection"
COMPILER = shutil.which("cc")


@unittest.skipUnless(sys.platform == "darwin" and COMPILER, "Darwin C headers required")
class RamdiskRootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        folder = Path(cls.temporary.name)
        cls.program = folder / "root-helper"
        source = (PAYLOAD / "source/ramdisk_tools/restored_external.c").read_text()
        helper = re.search(r"^static int make_ramdisk_writable\(void\).*?^\}",
                           source, re.MULTILINE | re.DOTALL)
        if helper is None:
            raise AssertionError("RAM-root helper not found")
        harness = folder / "root-helper.c"
        harness.write_text(r'''
#include <sys/types.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <hfs/hfs_mount.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <assert.h>
static const char *scenario;
static int queries, mounts;
static int test_statfs(const char *path, struct statfs *out) {
    assert(!strcmp(path, "/"));
    ++queries;
    if (!strcmp(scenario, "query-fails") ||
        (!strcmp(scenario, "verify-fails") && queries == 2)) {
        errno = EIO;
        return -1;
    }
    memset(out, 0, sizeof(*out));
    strcpy(out->f_mntonname, !strcmp(scenario, "wrong-target") ? "/mnt2" : "/");
    strcpy(out->f_mntfromname, "root_device");
    strcpy(out->f_fstypename, !strcmp(scenario, "wrong-fs") ? "apfs" : "hfs");
    out->f_flags = MNT_NOATIME | MNT_ROOTFS | MNT_LOCAL;
    if (strcmp(scenario, "writable") &&
        (queries == 1 || !strcmp(scenario, "stays-readonly")))
        out->f_flags |= MNT_RDONLY;
    return 0;
}
static int test_stat(const char *path, struct stat *out) {
    memset(out, 0, sizeof(*out));
    if (!strcmp(scenario, "identity-query-fails")) { errno = EIO; return -1; }
    if (!strcmp(path, "/")) {
        out->st_dev = !strcmp(scenario, "nand") ? 99 : 7;
    } else {
        assert(!strcmp(path, "/dev/md0"));
        out->st_rdev = 7;
        out->st_mode = !strcmp(scenario, "not-block-device") ? S_IFREG : S_IFBLK;
    }
    return 0;
}
static int test_mount(const char *type, const char *path, int flags, void *data) {
    struct hfs_mount_args *args = data;
    ++mounts;
    assert(!strcmp(type, "hfs") && !strcmp(path, "/"));
    assert(!strcmp(args->fspec, "/dev/md0"));
    assert((flags & MNT_UPDATE) && (flags & MNT_NOATIME) && !(flags & MNT_RDONLY));
    assert(!(flags & (MNT_FORCE | MNT_RELOAD)));
    assert(args->hfs_uid == (uid_t)-1 && args->hfs_gid == (gid_t)-1);
    assert(args->hfs_mask == (mode_t)-1 && args->hfs_encoding == (u_int32_t)-1);
    assert(args->hfs_timezone.tz_minuteswest == -1 && args->flags == 0);
    if (!strcmp(scenario, "mount-fails")) { errno = EBUSY; return -1; }
    return 0;
}
#define statfs(path, out) test_statfs(path, out)
#define stat(path, out) test_stat(path, out)
#define mount(type, path, flags, data) test_mount(type, path, flags, data)
''' + helper.group(0) + r'''
int main(int argc, char **argv) {
    assert(argc == 2);
    scenario = argv[1];
    int result = make_ramdisk_writable();
    printf("queries=%d mounts=%d\n", queries, mounts);
    return result;
}
''')
        subprocess.run([COMPILER, "-std=gnu99", "-Wall", "-Wextra", "-Werror",
                        "-I" + str(PAYLOAD / "include"), str(harness), "-o", str(cls.program)],
                       check=True, capture_output=True, text=True, timeout=30)

    def run_helper(self, scenario):
        return subprocess.run([str(self.program), scenario], capture_output=True,
                              text=True, timeout=5)

    def test_writable_root_skips_update_and_readonly_root_updates_once(self):
        for scenario, expected in (("writable", "queries=1 mounts=0"),
                                   ("readonly", "queries=2 mounts=1")):
            with self.subTest(scenario=scenario):
                result = self.run_helper(scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
                self.assertEqual(result.stderr, "")

    def test_unknown_root_or_query_failure_never_reaches_mount(self):
        for scenario in ("nand", "wrong-target", "wrong-fs", "query-fails", "identity-query-fails", "not-block-device"):
            with self.subTest(scenario=scenario):
                result = self.run_helper(scenario)
                self.assertEqual(result.returncode, 1)
                self.assertIn("mounts=0", result.stdout)
                self.assertTrue(result.stderr)

    def test_failed_or_ineffective_update_stops_startup(self):
        for scenario in ("mount-fails", "verify-fails", "stays-readonly"):
            with self.subTest(scenario=scenario):
                result = self.run_helper(scenario)
                self.assertEqual(result.returncode, 1)
                self.assertIn("mounts=1", result.stdout)
                self.assertTrue(result.stderr)
