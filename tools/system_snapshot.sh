#!/bin/bash
# system_snapshot.sh OUTDIR : small text snapshot of machine state worth keeping (crontab, launchd, brew, tool versions, repo inventory)
O="${1:?outdir}"; mkdir -p "$O"
crontab -l > "$O/crontab.txt" 2>&1
launchctl list 2>/dev/null | grep -v "com.apple" > "$O/launchctl-user.txt"
command -v brew >/dev/null && brew list --versions > "$O/brew.txt" 2>&1
pip3 list 2>/dev/null > "$O/pip3.txt"; npm ls -g --depth=0 2>/dev/null > "$O/npm-global.txt"
ls -1 ~ > "$O/home-listing.txt"; ls -1 ~/Documents ~/Desktop ~/Downloads > "$O/user-dirs-listing.txt" 2>&1
git config --global -l 2>/dev/null | sed -E 's#(://)[^/@ ]+@#\1#' > "$O/git-global.txt"
df -h ~ > "$O/df.txt"; sw_vers > "$O/sw_vers.txt"; date -u > "$O/captured_at.txt"
