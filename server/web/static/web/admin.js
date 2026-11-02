// SPDX-License-Identifier: GPL-3.0-or-later
// The Django admin in the user's theme and colour (templates/admin/base_site.html):
// set on <html> before the page draws, as base.html does for the site's pages.
'use strict';
{
	const s = document.currentScript.dataset;
	// "system" leaves it to the browser (dark_mode.css follows prefers-color-scheme)
	if (s.theme === 'light' || s.theme === 'dark') document.documentElement.dataset.theme = s.theme;
	if (s.accent) document.documentElement.dataset.accent = s.accent;
}
