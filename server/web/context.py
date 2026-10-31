# SPDX-License-Identifier: GPL-3.0-or-later
"""Template context of every page."""

from django.utils.functional import SimpleLazyObject

from . import data


def tags(request):
    """name -> colour of the tags the user sees (issue #133), read when a page shows tags."""
    user = request.user
    return {"tag_colors": SimpleLazyObject(lambda: data.tag_colors(user) if user.is_authenticated else {})}


def version(request):
    """The server's version, in the menu and on the login page: what the beta's reports ask for."""
    from atweb import __version__

    return {"server_version": __version__}
