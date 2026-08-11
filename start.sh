#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV_DIR="${SOLIXAUTO_VENV:-$HOME/solix-automation/venv}"
REPO_URL="https://github.com/thomluther/anker-solix-api.git"

echo
echo "=============================================================="
echo " Anker SOLIX to Shelly automation - setup"
echo "=============================================================="
echo

works() {
    [ -x "$1" ] && "$1" -c "import anker_solix_api.api" >/dev/null 2>&1
}

usable_python() {
    local candidates=(
        "$VENV_DIR/bin/python"
        "$HOME/anker-solix-mqtt/venv/bin/python"
        "$HERE/venv/bin/python"
    )
    for candidate in "${candidates[@]}"; do
        if works "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    for name in python3.14 python3.13 python3.12 python3; do
        if command -v "$name" >/dev/null 2>&1 && works "$(command -v "$name")"; then
            command -v "$name"
            return 0
        fi
    done
    return 1
}

base_python() {
    if [ -x "$VENV_DIR/bin/python" ]; then
        echo "$VENV_DIR/bin/python"
        return 0
    fi
    for name in python3.14 python3.13 python3.12 python3; do
        if command -v "$name" >/dev/null 2>&1; then
            local version
            version="$("$name" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)"
            if [ "$version" -ge 312 ]; then
                command -v "$name"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON="$(usable_python || true)"

if [ -n "${PYTHON:-}" ]; then
    echo "Using Python: $PYTHON"
    echo
    exec "$PYTHON" "$HERE/solixauto.py" setup
fi

echo "Nothing on this machine can talk to Anker yet."
echo

BASE="$(base_python || true)"

if [ -z "${BASE:-}" ]; then
    echo "Python 3.12 or newer is required and was not found."
    echo
    if command -v brew >/dev/null 2>&1; then
        echo "Install it with:   brew install python@3.13"
    elif command -v apt >/dev/null 2>&1; then
        echo "Install it with:   sudo apt install python3 python3-venv python3-pip git"
    else
        echo "Install Python 3.12+ from https://www.python.org/downloads/"
    fi
    echo
    echo "Then run this script again."
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "Git is required to fetch the Anker library, and was not found."
    echo
    if [ "$(uname)" = "Darwin" ]; then
        echo "Install the Xcode command line tools:   xcode-select --install"
    else
        echo "Install git with your package manager, for example: sudo apt install git"
    fi
    echo
    echo "Then run this script again."
    exit 1
fi

echo "This will create a self-contained Python environment and install"
echo "everything needed. Nothing outside these two locations is touched:"
echo
echo "    $VENV_DIR"
echo "    $HOME/solix-automation"
echo
printf "Set it up now? [Y/n]: "
read -r reply
case "${reply:-y}" in
    [nN]*) echo "Nothing was changed."; exit 0 ;;
esac

echo
echo "Creating the environment with $BASE ..."
mkdir -p "$(dirname "$VENV_DIR")"
"$BASE" -m venv "$VENV_DIR"

VENV_PYTHON="$VENV_DIR/bin/python"
[ -x "$VENV_PYTHON" ] || VENV_PYTHON="$VENV_DIR/Scripts/python.exe"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Could not create the environment at $VENV_DIR"
    exit 1
fi

echo "Installing packages. This takes a minute."
echo
"$VENV_PYTHON" -m pip install --upgrade pip --quiet
"$VENV_PYTHON" -m pip install -r "$HERE/requirements.txt" --quiet
"$VENV_PYTHON" -m pip install "git+$REPO_URL" --quiet

# The upstream package does not declare its runtime dependencies, so a plain
# install of it imports the package but fails on first real use.
"$VENV_PYTHON" -m pip install aiofiles cryptography paho-mqtt --quiet

if ! works "$VENV_PYTHON"; then
    echo
    echo "Some dependencies are still missing. The setup wizard will resolve"
    echo "the rest as it goes."
fi

echo
echo "Environment ready: $VENV_PYTHON"
echo
echo "Tip: from now on you can run commands as"
echo "    $VENV_PYTHON solixauto.py <command>"
echo
exec "$VENV_PYTHON" "$HERE/solixauto.py" setup
