#!/bin/sh
# Openup's original-iPhone SSH RAM disk startup. The native helper verifies
# the actual m68 hardware and RAM root before preparing RAM and USB only.
# NAND volumes are mounted read-only later by the verified host workflow.
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

# The helper retains its open USB interface while supervising foreground SSH.
# Exiting it before SSH would discard the device-controller owner process.
exec /usr/local/bin/openup-iphone2g --bootstrap
