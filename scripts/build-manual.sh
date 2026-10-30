#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Builds the administrator's manual, it and en, as PDF (as the app's
# scripts/build-manual.sh):
#
#   scripts/build-manual.sh [version]      -> build/manual/*.pdf
#
# Needs pandoc and a Chromium (Debian packages; no npm).

set -eu
cd "$(dirname "$0")/.."

VERSION=${1:-$(sed -n 's/^Versione \([0-9][0-9.]*\).*/\1/p' docs/manual/it/admin.md | head -n 1)}
OUT=build/manual
CHROME=$(command -v chromium || command -v chromium-headless-shell || command -v google-chrome || true)
[ -n "$CHROME" ] || { echo "no Chromium for the PDF" >&2; exit 1; }

rm -rf "$OUT"
mkdir -p "$OUT"
{ echo "<style>"; cat docs/manual/style.css; echo "</style>"; } > "$OUT/style.html"
cp -r docs/manual/img "$OUT/img"
for lang in it en; do
	case $lang in
		it) title="ActivityTrack Web — Manuale dell'amministratore"; name=Amministratore ;;
		en) title="ActivityTrack Web — Administrator's manual"; name=Administrator ;;
	esac
	mkdir -p "$OUT/$lang"
	pandoc -f gfm+implicit_figures docs/manual/$lang/admin.md -s --toc --toc-depth=2 -H "$OUT/style.html" \
		--metadata title="$title" --metadata subtitle="$VERSION" --metadata lang="$lang" \
		-o "$OUT/$lang/admin.html"
	"$CHROME" --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
		--print-to-pdf="$OUT/ActivityTrack-Web-$name-$VERSION.pdf" "file://$PWD/$OUT/$lang/admin.html" 2>/dev/null
	echo "$OUT/ActivityTrack-Web-$name-$VERSION.pdf"
done
