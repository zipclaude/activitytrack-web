# SPDX-License-Identifier: GPL-3.0-or-later
import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Organization, Profile, User


class Command(BaseCommand):
    help = "Creates the organisation and its first administrator (once, after installing)."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True, help="name of the organisation")
        parser.add_argument("--admin", required=True, help="username of the administrator")
        parser.add_argument("--email", default="")
        parser.add_argument("--password", help="for scripts; asked when missing")

    def handle(self, *args, org, admin, email, password, **options):
        if Organization.objects.exists():
            raise CommandError("the organisation already exists")
        if not password:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                raise CommandError("the passwords differ")
        # the rules of the web pages (settings.AUTH_PASSWORD_VALIDATORS), for scripts too
        try:
            validate_password(password, User(username=admin, email=email))
        except ValidationError as e:
            raise CommandError(" ".join(e.messages)) from None
        with transaction.atomic():
            o = Organization.objects.create(name=org)
            User.objects.create_superuser(username=admin, email=email, password=password, org=o, profile=Profile.ADMIN)
        self.stdout.write(f"Organisation {org!r} and administrator {admin!r} created.")
