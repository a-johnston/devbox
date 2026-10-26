#!/bin/bash
# Keep a recognizable pane leader while the harness runs. No credentials or
# launch arguments are written to disk; tmux receives them as separate argv.
case "$1" in
    alive)
        pids=$(tmux list-panes -s -t agent -F '#{pane_pid}') || exit 1
        for pid in $pids; do
            if IFS= read -r -d '' command 2>/dev/null < "/proc/$pid/cmdline" \
                && [[ $command == devbox-harness ]]; then
                exit 0
            fi
        done
        exit 1
        ;;
    start)
        shift
        workdir=$1
        count=$2
        shift 2
        environment=()
        for ((i=0; i<count; i++)); do
            name=$1
            shift
            # A surviving tmux server has an old environment. Override declared
            # secrets per session, including ones that are now unset. Values
            # never cross the host CLI boundary or enter saved launch policy.
            environment+=(-e "$name=${!name-}")
        done
        tmux -u -f "${BASH_SOURCE[0]%/*}/tmux.conf" new-session \
            -d -s agent -c "$workdir" "${environment[@]}" -- \
            bash "${BASH_SOURCE[0]}" run "$@" || exit 1
        # Do not return to the host until the pane leader is recognizable; an
        # immediate attach must not mistake an in-flight launch for a dead one.
        for ((i=0; i<20; i++)); do
            bash "${BASH_SOURCE[0]}" alive && exit 0
            sleep 0.05
        done
        exit 1
        ;;
    run)
        shift
        # The trailing exit prevents bash from replacing itself with the harness.
        exec -a devbox-harness bash -c '"$@"; exit $?' -- "$@"
        ;;
    *) exit 2 ;;
esac
