# SPDX-License-Identifier: GPL-3.0-or-later
"""
The test runner: the test client asks for Italian, as the pages' tests have always read them in Italian.
Without a language the server answers in English (web.language), so a test that wants another one says so
with HTTP_ACCEPT_LANGUAGE, which wins over this default.
"""

from django.test import SimpleTestCase
from django.test.client import Client
from django.test.runner import DiscoverRunner


class ItalianClient(Client):
    def __init__(self, *args, **defaults):
        defaults.setdefault("HTTP_ACCEPT_LANGUAGE", "it")
        super().__init__(*args, **defaults)


class Runner(DiscoverRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        SimpleTestCase.client_class = ItalianClient
