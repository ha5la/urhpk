#!/bin/sh

filename="$(date -uIseconds).cast"
asciinema rec "${filename}" -c 'tmux new-session irssi\; split-window ./puskas_logger.py\; select-layout even-horizontal\; new-window -d -n bg ./hamlib_supervisor.py\; split-window -t bg ./on4kst_irc_bridge.py'
