#!/bin/sh
# Keep the reported result below diagnostics on small phone consoles.
# Shell builtins only: no additional ramdisk utility or saved transcript.
export LC_ALL=C

show_recovery_result() {
    passcode=
    reported=0
    no_passcode=0
    invalid=0
    status=125
    completed=0
    cr=$(printf '\r')
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%"$cr"}
        case "$line" in
            __OPENUP_RECOVERY_EXIT__:[0-9]*)
                value=${line#__OPENUP_RECOVERY_EXIT__:}
                case "$value" in
                    ''|*[!0-9]*) completed=0 ;;
                    *) if [ "${#value}" -le 3 ] && [ "$value" -le 255 ]; then
                           status=$value
                           completed=1
                       else
                           completed=0
                       fi ;;
                esac
                continue
                ;;
        esac
        # A completion marker is valid only when it is the last input record.
        completed=0
        printf '%s\n' "$line"
        case "$line" in
            'Found passcode : '*)
                value=${line#'Found passcode : '}
                case "$value" in
                    ''|*[![:print:]]*) invalid=1 ;;
                    *) if [ "${#value}" -gt 128 ] ||
                          { [ "$reported" -eq 1 ] && [ "$value" != "$passcode" ]; }; then
                           invalid=1
                       else
                           passcode=$value
                           reported=1
                       fi ;;
                esac
                ;;
            'No passcode set') no_passcode=1 ;;
            'Invalid passcode !'|'FAIL:'*|'FAILed '*) invalid=1 ;;
        esac
    done
    if [ "$completed" -ne 1 ]; then
        printf '\nRecovery was interrupted.\nNo confirmed passcode.\n'
        return 125
    fi
    if [ "$status" -ne 0 ]; then
        printf '\nRecovery failed (status %s).\nNo confirmed passcode.\n' "$status"
        return "$status"
    fi
    if [ "$invalid" -ne 0 ] || { [ "$reported" -eq 1 ] && [ "$no_passcode" -eq 1 ]; }; then
        printf '\nNo confirmed passcode.\n'
        return 1
    fi
    if [ "$reported" -eq 1 ]; then
        printf '\nRecovered passcode:\n%s\n' "$passcode"
    elif [ "$no_passcode" -eq 1 ]; then
        printf '\nNo passcode is set.\n'
    else
        printf '\nNo passcode was recovered.\n'
    fi
}

{
    /usr/bin/openup-recovery-engine "$@"
    recovery_status=$?
    printf '\n__OPENUP_RECOVERY_EXIT__:%s\n' "$recovery_status"
} 2>&1 | show_recovery_result
