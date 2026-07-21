#!/bin/sh

filename="$(date -uIseconds).cast"
asciinema rec "${filename}" -c 'tmux new-session irssi\; split-window ./puskas_logger.py\; select-layout even-horizontal\; new-window -d ./hamlib_supervisor.py'
