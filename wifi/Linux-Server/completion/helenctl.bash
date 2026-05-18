# Bash completion for helenctl. Install:
#   sudo cp completion/helenctl.bash /etc/bash_completion.d/helenctl
# Or per-user:
#   mkdir -p ~/.local/share/bash-completion/completions
#   cp completion/helenctl.bash ~/.local/share/bash-completion/completions/helenctl

_helenctl() {
    local cur prev words cword
    _init_completion || return

    local top="status start stop restart reload logs health check diag \
               backup restore upgrade cert cluster role policy emergency \
               roles metrics version help"
    local roles_names="sfu relay recording file_transfer metrics federation auto_degrade"
    local policy_modes="auto chat_only audio_only video_ok no_sfu_p2p_only no_relay"

    case ${COMP_CWORD} in
        1)
            COMPREPLY=( $(compgen -W "${top}" -- "${cur}") )
            return ;;
        2)
            case "${prev}" in
                cert)     COMPREPLY=( $(compgen -W "rotate" -- "${cur}") ); return ;;
                cluster)  COMPREPLY=( $(compgen -W "list join leave" -- "${cur}") ); return ;;
                role)     COMPREPLY=( $(compgen -W "${roles_names}" -- "${cur}") ); return ;;
                policy)   COMPREPLY=( $(compgen -W "${policy_modes}" -- "${cur}") ); return ;;
                emergency)COMPREPLY=( $(compgen -W "freeze exit" -- "${cur}") ); return ;;
                logs)     COMPREPLY=( $(compgen -W "-f -n" -- "${cur}") ); return ;;
                upgrade|restore|backup|diag)
                          _filedir; return ;;
            esac
            ;;
        3)
            case "${words[1]}" in
                role) COMPREPLY=( $(compgen -W "on off" -- "${cur}") ); return ;;
            esac
            ;;
    esac
}
complete -F _helenctl helenctl
