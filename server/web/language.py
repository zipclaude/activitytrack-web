# SPDX-License-Identifier: GPL-3.0-or-later
"""
The language of a page, as in the app and the PC interfaces: the policy's ui.language for the logged-in user,
else the one chosen in Preferences (the cookie), else the browser's, else English. An unknown browser language
is English, not Italian: Italian is the language of the message ids (settings.LANGUAGE_CODE), which is why it has
no catalogue of its own, and not the fallback.
"""

from django.conf import settings
from django.utils import translation
from django.utils.translation.trans_real import parse_accept_lang_header

FALLBACK = "en"


def _supported(code):
    """The language of the site for [code] ("de-AT" is "de"), else None."""
    try:
        found = translation.get_supported_language_variant(code)
    except LookupError:
        return None
    return found if found in dict(settings.LANGUAGES) else None


def policy_language(user):
    """The language the user's policy imposes (policy key ui.language), else None."""
    if user is None or not user.is_authenticated:
        return None
    from policies.effective import merged

    code = merged(user).get("ui", {}).get("language")
    return code if code in dict(settings.LANGUAGES) else None


def choose(request):
    """Policy, then the cookie of the Preferences page, then the browser's languages, then English."""
    code = policy_language(getattr(request, "user", None))
    code = code or _supported(request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME, ""))
    if code:
        return code
    for accepted, _q in parse_accept_lang_header(request.META.get("HTTP_ACCEPT_LANGUAGE", "")):
        if accepted == "*":
            break
        code = _supported(accepted)
        if code:
            return code
    return FALLBACK


def middleware(get_response):
    """After the authentication: Django's LocaleMiddleware has already chosen one, without the policy."""
    def handle(request):
        code = choose(request)
        translation.activate(code)
        request.LANGUAGE_CODE = code
        return get_response(request)
    return handle
