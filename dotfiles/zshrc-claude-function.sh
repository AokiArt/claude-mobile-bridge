claude() {
  if [ -n "$TMUX" ] || ! command -v tmux >/dev/null 2>&1; then
    command claude "$@"
    return
  fi
  local name base n=2
  name=$(basename "$PWD"); [ -z "$name" ] && name=main
  base=$name
  while tmux has-session -t "=$name" 2>/dev/null; do name="$base-$n"; ((n++)); done
  local -a cmda; cmda=(command claude "$@")
  if tmux new-session -d -s "$name" -c "$PWD" "${(j: :)${(@q)cmda}}" 2>/dev/null; then
    echo "↪️ tmux 窗口「$name」；脱离 Ctrl-B D（脱离后手机仍可遥控）"
    tmux attach -t "=$name"
  else
    echo "[tmux 起不来，退回普通窗口]" >&2
    command claude "$@"
  fi
}
