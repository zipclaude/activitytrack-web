# SPDX-License-Identifier: GPL-3.0-or-later
"""Content-Security-Policy of the pages (docs/SECURITY.md)."""

# scripts and styles from this site only (style attributes carry the colours);
# images also as data: (the favicon); no frames, no plugins, forms to this site
POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def content_security_policy(get_response):
    def middleware(request):
        response = get_response(request)
        # attachments set their own, stricter one
        response.headers.setdefault("Content-Security-Policy", POLICY)
        return response
    return middleware
