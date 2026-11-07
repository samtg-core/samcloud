# Source through Hermes terminal.shell_init_files. Its login shell can replace
# the launcher's PATH. This is an explicit helper directory, not a credential.
if [ -n "${SAMCLOUD_HELPER_DIR:-}" ] && [ -d "$SAMCLOUD_HELPER_DIR" ]; then
    case ":$PATH:" in
        *:"$SAMCLOUD_HELPER_DIR":*) ;;
        *) export PATH="$SAMCLOUD_HELPER_DIR:$PATH" ;;
    esac
fi
