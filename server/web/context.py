# SPDX-License-Identifier: GPL-3.0-or-later
"""Template context of every page."""

from django.utils.functional import SimpleLazyObject

from . import data


def tags(request):
    """name -> colour of the tags the user sees (issue #133), read when a page shows tags."""
    user = request.user
    return {"tag_colors": SimpleLazyObject(lambda: data.tag_colors(user) if user.is_authenticated else {})}
