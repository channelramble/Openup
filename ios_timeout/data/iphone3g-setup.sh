#!/bin/sh
# Managed iPhone 3G recovery startup. Only the RAM disk is writable.
# The included restored_external omits automatic filesystem repair.
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
exec >/dev/console 2>&1

hold_ramdisk() {
    echo "Recovery stopped. The phone remains in its RAM disk until rebooted."
    while :; do
        if [ -x /usr/local/bin/bl-ios-wait ]; then
            /usr/local/bin/bl-ios-wait --sleep 60 && continue
        fi
        # A missing or damaged helper must not turn this hold into a busy loop.
        kill -STOP "$$"
    done
}

echo "Starting iPhone 3G ARMv6 recovery."
if [ ! -x /usr/local/bin/restored_external.sshrd ] || [ ! -x /usr/bin/bruteforce ] || [ ! -x /usr/local/bin/bl-ios-wait ]; then
    echo "Required recovery executables are missing."
    hold_ramdisk
fi

/usr/local/bin/restored_external.sshrd &
daemon_pid=$!
attempt=0
ready=0
while [ "$attempt" -lt 60 ]; do
    if ! kill -0 "$daemon_pid" 2>/dev/null; then
        echo "Recovery USB service stopped."
        hold_ramdisk
    fi
    if [ -e /dev/disk0s2 ] || [ -e /dev/disk0s2s1 ] || [ -e /dev/disk0s1s2 ]; then
        ready=1
        break
    fi
    /usr/local/bin/bl-ios-wait --sleep 1 || hold_ramdisk
    attempt=$((attempt + 1))
done
if [ "$ready" -ne 1 ]; then
    echo "Data partition was not detected. Recovery was not started."
    hold_ramdisk
fi

cd /var/root || hold_ramdisk
# The recovery executable itself mounts the data partition read-only.
# Never run the upstream mount.sh (which repairs and mounts it read/write).
/usr/bin/bruteforce -u
recovery_status=$?
echo "Recovery process finished with status $recovery_status."
wait "$daemon_pid"
hold_ramdisk
