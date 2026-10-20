"""GUI event hooks for backend source already verified by its owning adapter."""


_HELPERS = r'''
bl_ios_ui() {
    if [[ ${BL_IOS_UI_PROTOCOL:-} == 1 && -n ${BL_IOS_PYTHON:-} ]]; then
        "$BL_IOS_PYTHON" -m ios_timeout.desktop_protocol "$@"
    fi
}

bl_ios_ui_input() {
    bl_ios_ui_context="$1"
    case "$1" in
        "Press Enter"* )
            bl_ios_ui prompt continue "Continue when ready" "$1"
            ;;
        *: | *": " | *\? | *"? " )
            bl_ios_ui prompt auto "Recovery tool" "$1"
            ;;
        * )
            bl_ios_ui progress "Recovery tool" "$1"
            ;;
    esac
}

bl_ios_ui_buttons() {
    local remaining
    case "$1" in
        "* Hold "*" buttons."* )
            remaining="${sec:-10}"
            bl_ios_ui progress "Enter DFU mode" "${1#\* }" --remaining "$remaining"
            ;;
        "* Release "*"keep holding "* )
            remaining=8
            if [[ " ${FUNCNAME[*]} " == *" bl_ios_early_prepare "* ]]; then
                remaining=15
            fi
            bl_ios_ui progress "Enter DFU mode" "${1#\* }" --remaining "$remaining"
            ;;
        "* USB entry mode detected. Release both buttons now." )
            bl_ios_ui progress "Phone detected" "${1#\* }" --remaining 0
            ;;
    esac
}
'''

_LEGACY_SELECT = '''    if [[ ${BL_IOS_UI_PROTOCOL:-} == 1 ]]; then
        local bl_ios_choice bl_ios_number=1
        local bl_ios_options=()
        for bl_ios_choice in "$@"; do
            bl_ios_options+=(--option "$bl_ios_choice" "$bl_ios_number")
            ((bl_ios_number++))
        done
        bl_ios_ui prompt choice "Choose the next step" \\
            "${bl_ios_ui_context:-Choose an option to continue.}" "${bl_ios_options[@]}"
    fi
    if [[ ${BL_IOS_UI_PROTOCOL:-} == 1 || $menu_old == 1 ]]; then
        select opt in "$@"; do'''


def adapt_prompts(source, backend):
    """Add emit-only hooks; normal CLI input behavior remains unchanged.

    The caller must verify the full upstream source hash before applying its
    compatibility edits and this helper. Exact local matches prevent silently
    adapting a changed function layout.
    """
    if backend not in {"bruteforce", "legacy"}:
        raise ValueError("Unknown backend for GUI prompt hooks.")

    def replace(before, after):
        nonlocal source
        if source.count(before) != 1:
            raise ValueError("Backend GUI prompt hook did not match exactly.")
        source = source.replace(before, after)

    if backend == "bruteforce":
        replace(
            'input() { echo "${color_Y}[Input] ${1}${color_N}"; }',
            _HELPERS + '''
input() {
    bl_ios_ui_input "$1"
    echo "${color_Y}[Input] ${1}${color_N}"
}''',
        )
        replace(
            'print() { echo "${color_B}${1}${color_N}"; }',
            '''print() {
    bl_ios_ui_buttons "$1"
    echo "${color_B}${1}${color_N}"
}''',
        )
    else:
        replace(
            '''input() {
    echo "${color_Y}[Input] ${1}${color_N}"
}''',
            _HELPERS + '''
input() {
    bl_ios_ui_input "$1"
    echo "${color_Y}[Input] ${1}${color_N}"
}''',
        )
        replace(
            '''print() {
    echo "${color_B}${1}${color_N}"
}''',
            '''print() {
    bl_ios_ui_buttons "$1"
    echo "${color_B}${1}${color_N}"
}''',
        )
        replace(
            '''    if [[ $menu_old == 1 ]]; then
        select opt in "$@"; do''',
            _LEGACY_SELECT,
        )
    return source
