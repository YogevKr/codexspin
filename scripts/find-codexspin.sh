# GUI-launched sessions get a minimal PATH without common CLI install paths.
find_codexspin() {
  command -v codexspin 2>/dev/null && return 0
  for candidate in "$HOME/.local/bin/codexspin" /opt/homebrew/bin/codexspin /usr/local/bin/codexspin; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}
