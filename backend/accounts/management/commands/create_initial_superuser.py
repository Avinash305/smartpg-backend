from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
import os


class Command(BaseCommand):
    help = "Create an initial superuser if not exists"

    def handle(self, *args, **kwargs):
        User = get_user_model()
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "admin77@gmail.com")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "Hunter@123")
        full_name = os.environ.get("DJANGO_SUPERUSER_FULL_NAME", "Avinash")
        phone = os.environ.get("DJANGO_SUPERUSER_PHONE", "7026943730")

        existing = User.objects.filter(email=email).first()
        if existing:
            # Ensure flags are set for an existing account
            changed = False
            if not existing.is_superuser:
                existing.is_superuser = True
                changed = True
            if not existing.is_staff:
                existing.is_staff = True
                changed = True
            if full_name and existing.full_name != full_name:
                existing.full_name = full_name
                changed = True
            if phone and getattr(existing, 'phone', None) != phone:
                existing.phone = phone
                changed = True
            if changed:
                existing.save()
                self.stdout.write(self.style.SUCCESS("Existing user updated with superuser/staff flags"))
            else:
                self.stdout.write(self.style.WARNING("Superuser already exists"))
            return

        User.objects.create_superuser(email=email, password=password, full_name=full_name, phone=phone)
        self.stdout.write(self.style.SUCCESS("Superuser created"))
