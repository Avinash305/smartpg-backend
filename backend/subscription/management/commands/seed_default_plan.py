from django.core.management.base import BaseCommand
from subscription.models import SubscriptionPlan


class Command(BaseCommand):
    help = "Seed or update the default 'Basic' subscription plan (INR 149/month)."

    def handle(self, *args, **options):
        slug = "basic"
        name = "Basic"

        desired_limits = {
            # Simple counts
            "buildings": 1,
            "floors": 5,
            "rooms": 5,
            "beds": 7,
            "staff": 1,
            # Max caps used by enforcement paths in views
            "max_buildings": 1,
            "max_floors": 5,
            "max_rooms": 5,
            "max_beds": 7,
            "max_staff": 1,
        }

        desired_features = {
            # Toggle feature flags as needed in future
        }

        desired_prices = {
            # Interval code to price map; monthly only per requirement
            "1m": 149,
        }

        defaults = {
            "name": name,
            "currency": "INR",
            "price_monthly": 149,
            # Keep yearly unset (0) unless requirements specify otherwise
            "price_yearly": 0,
            "is_active": True,
            "features": desired_features,
            "limits": desired_limits,
            "available_intervals": ["1m"],
            "prices": desired_prices,
        }

        plan, created = SubscriptionPlan.objects.get_or_create(slug=slug, defaults=defaults)

        if not created:
            # Update existing plan with latest desired values but do not flip is_active if user changed it manually
            plan.name = name
            plan.currency = defaults["currency"]
            plan.price_monthly = defaults["price_monthly"]
            # Only update price_yearly if it's currently zero to avoid overriding an intentional value
            if not plan.price_yearly:
                plan.price_yearly = defaults["price_yearly"]
            # Merge limits to avoid dropping any custom keys user may have added
            merged_limits = dict(plan.limits or {})
            merged_limits.update(desired_limits)
            plan.features = defaults["features"]
            plan.limits = merged_limits
            plan.available_intervals = defaults["available_intervals"]
            plan.prices = defaults["prices"]
            plan.save(update_fields=[
                "name",
                "currency",
                "price_monthly",
                "price_yearly",
                "features",
                "limits",
                "available_intervals",
                "prices",
            ])

        action = "created" if created else "updated"
        self.stdout.write(self.style.SUCCESS(
            f"Successfully {action} plan '{name}' (slug='{slug}') at INR 149/month with limits: "
            f"buildings=1, floors=5, rooms=5, beds=7, staff=1 (and max_* caps set)."
        ))
