#!/usr/bin/env bash
# Create the small Retinue label set on novique-ai/retinue.
# Safe to re-run (gh label create --force updates color/description).
# Requires: gh auth with repo administration on that repository.
set -euo pipefail

REPO="${REPO:-novique-ai/retinue}"

label() {
  local name="$1" color="$2" desc="$3"
  gh label create "$name" --repo "$REPO" --color "$color" --description "$desc" --force
}

label "bug" "d73a4a" "Broken behavior"
label "enhancement" "a2eeef" "Agreed new behavior"
label "documentation" "0075ca" "Docs only"
label "question" "d876e3" "Setup help or needs a conversation"
label "good first issue" "7057ff" "New contributor, one sitting"
label "help wanted" "008672" "Maintainer wants a volunteer"
label "ui/ux" "fbca04" "retinue-web or rooms UX"
label "linux" "0e8a16" "Linux-specific"
label "wayland" "1d76db" "Wayland / compositor"
label "x11" "5319e7" "X11-specific"
label "kde" "3e4eb8" "KDE / Plasma"
label "gnome" "4a86cf" "GNOME"
label "packaging" "e99695" "Install, distro, prefix, pip UI path"
label "ai-provider" "b60205" "Model preset or provider"
label "rooms" "5319e7" "Turn-taking / rooms adapter"
label "voice" "d4c5f9" "STT/TTS"
label "upstream" "c5def5" "Belongs in or came from Hermes Agent"
label "priority: p1" "b60205" "Should land soon"
label "priority: p2" "fbca04" "Normal"

echo "labels applied on $REPO"
