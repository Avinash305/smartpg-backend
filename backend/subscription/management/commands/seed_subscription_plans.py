from decimal import Decimal
from django.core.management.base import BaseCommand
from subscription.models import SubscriptionPlan


PLANS = [
    {
        'name': 'Free',
        'slug': 'free',
        'currency': 'INR',
        'price_monthly': Decimal('0'),
        'price_yearly': Decimal('0'),
        'is_active': True,
        'features': {
            'bookings': True,
            'payments': True,
            'reports': False,
            'tenant_media': True,
            'staff_media': True,
        },
        'limits': {
            'max_buildings': 1,
            'max_floors_per_building': 2,
            'max_rooms_per_floor': 5,
            'max_beds_per_room': 4,
            'max_staff': 1,
            'max_tenants': 25,
            'max_tenant_media_per_tenant': 1,
        },
    },
    {
        'name': 'Standard',
        'slug': 'standard',
        'currency': 'INR',
        'price_monthly': Decimal('999'),
        'price_yearly': Decimal('9990'),
        'is_active': True,
        'features': {
            'bookings': True,
            'payments': True,
            'reports': True,
            'tenant_media': True,
            'staff_media': True,
        },
        'limits': {
            'max_buildings': 5,
            'max_floors_per_building': 10,
            'max_rooms_per_floor': 20,
            'max_beds_per_room': 6,
            'max_staff': 5,
            'max_tenants': 500,
            'max_tenant_media_per_tenant': 2,
        },
    },
    {
        'name': 'Pro',
        'slug': 'pro',
        'currency': 'INR',
        'price_monthly': Decimal('1999'),
        'price_yearly': Decimal('19990'),
        'is_active': True,
        'features': {
            'bookings': True,
            'payments': True,
            'reports': True,
            'tenant_media': True,
            'staff_media': True,
        },
        'limits': {
            'max_buildings': 50,
            'max_floors_per_building': 50,
            'max_rooms_per_floor': 100,
            'max_beds_per_room': 10,
            'max_staff': 50,
            'max_tenants': 10000,
            'max_tenant_media_per_tenant': 2,
        },
    },
]


class Command(BaseCommand):
    help = 'Seed initial subscription plans (Free, Standard, Pro)'

    def handle(self, *args, **options):
        created = 0
        updated = 0
        for data in PLANS:
            slug = data['slug']
            obj, was_created = SubscriptionPlan.objects.get_or_create(slug=slug, defaults=data)
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"Created plan: {obj.name}"))
            else:
                # Update fields to keep definitions current
                for field in ['name', 'currency', 'price_monthly', 'price_yearly', 'is_active', 'features', 'limits']:
                    setattr(obj, field, data[field])
                obj.save()
                updated += 1
                self.stdout.write(self.style.WARNING(f"Updated plan: {obj.name}"))
        self.stdout.write(self.style.SUCCESS(f"Done. Created: {created}, Updated: {updated}"))
