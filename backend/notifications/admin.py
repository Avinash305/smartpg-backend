from django.contrib import admin
from .models import Notification

# Register your models here.

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "recipient", "level", "unread", "created_at")
    list_filter = ("level", "unread", "created_at")
    search_fields = ("event", "title", "message", "recipient__email", "recipient__username")
    autocomplete_fields = ("recipient", "actor", "pg_admin", "building")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "read_at")
    list_select_related = ("recipient", "actor", "pg_admin", "building")
