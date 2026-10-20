#!/usr/bin/env bash
# Monta o pacote .deb do simple-opencode-usage.
# Uso: ./packaging/build-deb.sh
set -euo pipefail
umask 022

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="simple-opencode-usage"
VERSION="$(sed -n 's/^VERSION = "\([0-9][0-9.]*\)"/\1/p' "$ROOT/simple_opencode_usage.py" | head -n1)"
if [[ -z "$VERSION" ]]; then
    echo "erro: não foi possível ler a versão em simple_opencode_usage.py" >&2
    exit 1
fi

STAGE="$ROOT/dist/${PKG}_${VERSION}_all"
OUT="$ROOT/dist/${PKG}_${VERSION}_all.deb"

rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" \
    "$STAGE/usr/share/applications" \
    "$STAGE/usr/share/doc/$PKG" \
    "$STAGE/usr/share/$PKG"

# Executável
install -Dm755 "$ROOT/simple_opencode_usage.py" "$STAGE/usr/bin/$PKG"

# Ícones (tema hicolor + cópia local usada quando roda do código-fonte)
install -Dm644 "$ROOT/assets/simple-opencode-usage.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/$PKG.svg"
for size in 48 64 128; do
    install -Dm644 "$ROOT/assets/simple-opencode-usage-$size.png" \
        "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/$PKG.png"
done
install -Dm644 "$ROOT/assets/simple-opencode-usage-64.png" "$STAGE/usr/share/$PKG/$PKG-64.png"
install -Dm644 "$ROOT/assets/simple-opencode-usage-48.png" "$STAGE/usr/share/$PKG/$PKG-48.png"
install -Dm644 "$ROOT/assets/simple-opencode-usage.svg" "$STAGE/usr/share/$PKG/$PKG.svg"

# Entrada no menu de aplicativos (não é autostart: isso é opcional nas configurações)
install -Dm644 "$ROOT/packaging/simple-opencode-usage.desktop" \
    "$STAGE/usr/share/applications/$PKG.desktop"

# Documentação
install -Dm644 "$ROOT/README.md" "$STAGE/usr/share/doc/$PKG/README.md"
install -Dm644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/$PKG/LICENSE"
install -Dm644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/$PKG/copyright"

INSTALLED_SIZE="$(du -sk "$STAGE/usr" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $INSTALLED_SIZE
Depends: python3 (>= 3.9), python3-tk, python3-gi, gir1.2-glib-2.0
Recommends: gnome-shell-extension-appindicator | gnome-shell-extension-zorin-appindicator
Maintainer: Clifford Reis <cliffordreis@users.noreply.github.com>
Homepage: https://github.com/Cliffordreis/$PKG
Description: Floating OpenCode Go usage widget
 A small always-on-top card that shows the OpenCode Go rolling, weekly and
 monthly usage meters, with a tray icon (StatusNotifierItem), settings for
 size, opacity, refresh interval, API key and optional autostart.
EOF

(cd "$STAGE" && find usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums)

dpkg-deb --build --root-owner-group "$STAGE" "$OUT"
echo "Pacote gerado: $OUT"
dpkg-deb --info "$OUT" | sed 's/^/  /'
