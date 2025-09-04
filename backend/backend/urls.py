"""
URL configuration for backend project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from payment.views import CashflowView
from django.http import JsonResponse

def health_view(_request):
    return JsonResponse({
        'status': 'ok',
        'app': 'pg-management-backend',
        'debug': settings.DEBUG,
    })

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('accounts.urls')),
    path('api/properties/', include('properties.urls')),
    path('api/tenants/', include('tenants.urls')),
    path('api/bookings/', include('bookings.urls')),
    path('api/payments/', include('payment.urls')),
    path('api/notifications/', include('notifications.urls')),
    path('api/subscription/', include('subscription.urls')),
    path('api/dashboard/cashflow/', CashflowView.as_view()),
    # Health & Metrics
    path('health/', health_view),
    # Root landing endpoint
    path('', lambda _r: JsonResponse({'service': 'smartpg-backend', 'status': 'ok'})),
    path('', include('django_prometheus.urls')),  # exposes /metrics
]

# Serve media files during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
