#!/usr/bin/env bash
# ==============================================================================
# AERAM: build Mocktail with AERAM's input-latency patch
#
# Clones upstream Mocktail at a pinned commit, applies native/mocktail/*.patch (in order),
# builds it and installs it under ~/.local (bin/mocktail + lib/mocktail/).
# Anything already installed there is backed up first. AERAM prefers this build
# over a system (AUR/.deb/.rpm) Mocktail once the marker file below exists.
#
# The patch adds:
#   - MOCKTAIL_HOST_CURSOR: desktop hardware cursor instead of Roblox's
#     frame-rendered arrow (AERAM sets it for main accounts)
#   - 1 ms input polling instead of 4 ms when presentation is unthrottled
#   - removal of the Android client's 240 FPS clamp
#   - a 100 us rest after empty engine polls instead of a spinning core
#   - login checks: solved generic challenges close, web windows get 5 s
#   - faster CPU texture conversion (used where the GPU driver lacks ETC2,
#     e.g. NVIDIA): streaming reads from GPU-mapped memory, parallel writes
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AERAM_DIR="$(dirname "${SCRIPT_DIR}")"
PATCH_DIR="${AERAM_DIR}/native/mocktail"

UPSTREAM_URL="https://github.com/komaruworld/mocktail.git"
UPSTREAM_COMMIT="$(tr -d '[:space:]' < "${PATCH_DIR}/UPSTREAM_COMMIT")"

PREFIX="${HOME}/.local"
SRC_DIR="${AERAM_MOCKTAIL_SRC:-${HOME}/.cache/aeram/mocktail-src}"
BUILD_DIR="${SRC_DIR}-build"
MARKER="${PREFIX}/lib/mocktail/aeram-mocktail-patch"
JOBS="$(nproc 2>/dev/null || echo 4)"

print_build_deps() {
    echo "  Install Mocktail's build dependencies, then run this script again:"
    echo "    Arch:   sudo pacman -S --needed base-devel cmake git ninja pkgconf lld sdl3 sdl3_ttf \\"
    echo "              curl openssl nlohmann-json libyaml libelf minizip capstone gtk4 \\"
    echo "              libadwaita webkitgtk-6.0 libutf8proc fontconfig libglvnd \\"
    echo "              libplacebo vulkan-headers vulkan-icd-loader zlib"
    echo "    Others: see the 'Build from source' section of ${UPSTREAM_URL%.git}"
}

for tool in git cmake c++ pkg-config; do
    if ! command -v "${tool}" > /dev/null 2>&1; then
        echo "  [!] '${tool}' not found."
        print_build_deps
        exit 1
    fi
done

echo "  * Fetching upstream Mocktail ${UPSTREAM_COMMIT:0:7}..."
if [ ! -d "${SRC_DIR}/.git" ]; then
    mkdir -p "$(dirname "${SRC_DIR}")"
    git clone --quiet "${UPSTREAM_URL}" "${SRC_DIR}"
fi
if ! git -C "${SRC_DIR}" cat-file -e "${UPSTREAM_COMMIT}^{commit}" 2> /dev/null; then
    git -C "${SRC_DIR}" fetch --quiet origin
fi
# The checkout is AERAM-owned: discard any previous patch application.
git -C "${SRC_DIR}" checkout --quiet --force "${UPSTREAM_COMMIT}"
git -C "${SRC_DIR}" clean --quiet -fdx
git -C "${SRC_DIR}" submodule --quiet update --init --recursive

echo "  * Applying AERAM patches..."
for patch in "${PATCH_DIR}"/*.patch; do
    git -C "${SRC_DIR}" apply --whitespace=nowarn "${patch}"
    echo "    - $(basename "${patch}")"
done

echo "  * Building (this takes a few minutes)..."
if ! cmake -S "${SRC_DIR}" -B "${BUILD_DIR}" -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_TESTING=OFF -DCMAKE_INSTALL_PREFIX="${PREFIX}" > "${BUILD_DIR}.log" 2>&1 \
    || ! cmake --build "${BUILD_DIR}" -j"${JOBS}" >> "${BUILD_DIR}.log" 2>&1; then
    echo "  [!] Build failed. Last lines of ${BUILD_DIR}.log:"
    tail -n 15 "${BUILD_DIR}.log" | sed 's/^/      /'
    print_build_deps
    exit 1
fi

if pgrep -x mocktail > /dev/null 2>&1 || pgrep -f '/mocktail_acc_' > /dev/null 2>&1; then
    echo "  [!] Close all running Roblox/Mocktail instances first, then run this script again."
    exit 1
fi

# Keep whatever was installed before (a release build, or a custom one).
if [ -e "${PREFIX}/bin/mocktail" ] || [ -e "${PREFIX}/lib/mocktail/mocktail" ]; then
    BACKUP="${PREFIX}/lib/mocktail-backup/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "${BACKUP}"
    [ -e "${PREFIX}/bin/mocktail" ] && cp -a "${PREFIX}/bin/mocktail" "${BACKUP}/bin-mocktail"
    find "${PREFIX}/lib/mocktail" -maxdepth 1 -type f ! -name 'mocktail_acc_*' \
        -exec cp -a {} "${BACKUP}/" \; 2> /dev/null || true
    echo "  * Previous Mocktail backed up to ${BACKUP}"
    # A wrapper script or symlink here would shadow the new ELF binary.
    rm -f "${PREFIX}/bin/mocktail"
fi

cmake --install "${BUILD_DIR}" > /dev/null
echo "${UPSTREAM_COMMIT} $(cat "${PATCH_DIR}"/*.patch | sha256sum | cut -c1-16)" > "${MARKER}"
echo "  [✓] Installed AERAM-patched Mocktail to ${PREFIX}/bin/mocktail"
