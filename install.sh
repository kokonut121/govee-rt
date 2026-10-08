#!/bin/sh
# govee-rt installer for macOS and Linux. Usage:
#   curl -fsSL https://raw.githubusercontent.com/kokonut121/govee-rt/main/install.sh | sh
# Installs uv (if missing), then govee-rt as an isolated command-line tool with its own Python,
# then the audio loopback driver (BlackHole on macOS). Safe to re-run; it upgrades in place.
set -eu

SOURCE="${GOVEE_RT_SOURCE:-git+https://github.com/kokonut121/govee-rt}"
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    bold "Installing uv (a fast Python tool installer, https://docs.astral.sh/uv) ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

bold "Installing govee-rt ..."
uv tool install --force --python 3.12 "$SOURCE"
uv tool update-shell >/dev/null 2>&1 || true

case "$(uname -s)" in
Darwin)
    if [ ! -d /Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver ]; then
        if command -v brew >/dev/null 2>&1; then
            bold "Installing BlackHole 2ch (lets the lights hear what your Mac plays) ..."
            brew install blackhole-2ch || echo "BlackHole install failed; get it from https://existential.audio/blackhole/"
        else
            echo "Next, install BlackHole 2ch so the lights can hear your Mac:"
            echo "  https://existential.audio/blackhole/   (or install Homebrew, then: brew install blackhole-2ch)"
        fi
    fi
    ;;
Linux)
    if ! (ldconfig -p 2>/dev/null | grep -q libportaudio); then
        echo "Audio capture needs PortAudio:  sudo apt install libportaudio2   (Fedora: sudo dnf install portaudio)"
    fi
    ;;
esac

echo
bold "Done. Start it with:  govee-rt"
command -v govee-rt >/dev/null 2>&1 || echo "(open a new terminal window first so it's on your PATH)"
