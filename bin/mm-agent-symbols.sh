#!/usr/bin/env bash
# The symbols the shell scripts show or strip, as named constants: the one
# shell file that knows the characters (the Python side has symbols.py).
# UTF-8 bytes as octal escapes, so this file holds no emoji and works in bash 3.2.
# shellcheck disable=SC2034  # sourced by mm-agent-lib.sh; the callers use them

MM_CROWN=$'\360\237\221\221'  # crown
MM_INBOX=$'\360\237\223\250'  # incoming envelope
MM_SIGNOFF=$'\342\234\213'  # raised hand
MM_LISTENING=$'\360\237\221\202'  # ear
MM_UPDATE=$'\342\254\206'  # up arrow
