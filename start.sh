#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

DATA_DIR="${SOLIXAUTO_HOME:-$HOME/solix-automation}"
VENV_DIR="${SOLIXAUTO_VENV:-$DATA_DIR/venv}"
VENV_PYTHON="$VENV_DIR/bin/python"
REPO_URL="https://github.com/thomluther/anker-solix-api.git"

echo
echo "=============================================================="
echo " Anker SOLIX to Shelly automation"
echo "=============================================================="
echo

works() {
    [ -x "$1" ] && "$1" -c "import anker_solix_api.api, yaml, aiohttp" >/dev/null 2>&1
}

if works "$VENV_PYTHON"; then
    echo "Using: $VENV_PYTHON"
    echo
    exec "$VENV_PYTHON" "$HERE/solixauto.py" setup
fi

find_base_python() {
    for name in python3.14 python3.13 python3.12 python3; do
        if command -v "$name" >/dev/null 2>&1; then
            local path version
            path="$(command -v "$name")"
            version="$("$path" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)"
            if [ "$version" -ge 312 ]; then
                echo "$path"
                return 0
            fi
        fi
    done
    return 1
}

BASE="$(find_base_python || true)"

if [ -z "${BASE:-}" ]; then
    echo "Python 3.12 or newer is required and was not found."
    echo
    if command -v brew >/dev/null 2>&1; then
        echo "Install it with:   brew install python@3.13"
    elif command -v apt >/dev/null 2>&1; then
        echo "Install it with:   sudo apt install python3 python3-venv python3-pip git"
    elif command -v dnf >/dev/null 2>&1; then
        echo "Install it with:   sudo dnf install python3 python3-pip git"
    else
        echo "Install Python 3.12 or newer from https://www.python.org/downloads/"
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
        echo "Install git with your package manager, for example:"
        echo "    sudo apt install git"
    fi
    echo
    echo "Then run this script again."
    exit 1
fi

if [ -d "$VENV_DIR" ]; then
    echo "The environment at $VENV_DIR is incomplete."
    echo "It will be rebuilt. Nothing else is touched."
else
    echo "This project runs in its own Python environment, so that installing or"
    echo "removing packages elsewhere on this machine cannot break your"
    echo "automation."
    echo
    echo "It will be created at:"
    echo "    $VENV_DIR"
fi

echo
echo "Base interpreter: $BASE"
echo
printf "Set it up now? [Y/n]: "
read -r reply
case "${reply:-y}" in
    [nN]*) echo "Nothing was changed."; exit 0 ;;
esac

echo
echo "Creating the environment..."
mkdir -p "$DATA_DIR"
rm -rf "$VENV_DIR"

if ! "$BASE" -m venv "$VENV_DIR"; then
    echo
    echo "Could not create a virtual environment."
    if command -v apt >/dev/null 2>&1; then
        echo "On Debian and Ubuntu this usually means python3-venv is missing:"
        echo "    sudo apt install python3-venv"
    fi
    exit 1
fi

if [ ! -x "$VENV_PYTHON" ]; then
    echo "The environment was created but has no interpreter at $VENV_PYTHON"
    exit 1
fi

echo "Installing packages. This takes a minute."
echo

"$VENV_PYTHON" -m pip install --upgrade pip --quiet
"$VENV_PYTHON" -m pip install -r "$HERE/requirements.txt" --quiet
"$VENV_PYTHON" -m pip install "git+$REPO_URL" --quiet

# The upstream Anker library does not declare its runtime dependencies, so a
# plain install imports the package but fails on first real use.
"$VENV_PYTHON" -m pip install aiofiles cryptography paho-mqtt --quiet

if ! works "$VENV_PYTHON"; then
    echo
    echo "The install finished but the Anker library still will not import."
    echo "Run this to see the error:"
    echo "    $VENV_PYTHON -c 'import anker_solix_api.api'"
    exit 1
fi

echo
echo "Environment ready."
echo
echo "From now on, run commands with:"
echo "    $VENV_PYTHON solixauto.py <command>"
echo
exec "$VENV_PYTHON" "$HERE/solixauto.py" setup
