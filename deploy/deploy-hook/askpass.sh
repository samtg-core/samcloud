#!/bin/sh
# GIT_ASKPASS for the deploy receiver: answers git's credential prompts from the
# 0600 token file named by DEPLOY_HOOK_GITHUB_TOKEN_FILE, so the token is never on
# a command line and never handed to a stored credential helper (#582).
case "$1" in
    Username*) echo x-access-token ;;
    *) cat "${DEPLOY_HOOK_GITHUB_TOKEN_FILE:?DEPLOY_HOOK_GITHUB_TOKEN_FILE unset}" ;;
esac
