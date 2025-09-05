from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from django.contrib import messages
from django.utils import timezone
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import User, ActivityLog, PendingRegistration

class CustomUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = '__all__'

class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('email', 'full_name', 'phone', 'role', 'pg_admin')
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['pg_admin'].queryset = User.objects.filter(role='pg_admin')
        
        # Make pg_admin required only for staff users
        if 'role' in self.data and self.data['role'] == 'pg_staff':
            self.fields['pg_admin'].required = True
            
        # Make phone field not required
        self.fields['phone'].required = False
        
    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get('role')
        pg_admin = cleaned_data.get('pg_admin')
        
        if role == 'pg_staff' and not pg_admin:
            raise forms.ValidationError({
                'pg_admin': 'PG Staff must have a PG Admin assigned.'
            })
            
        # Validate phone number format if provided
        phone = cleaned_data.get('phone')
        if phone and (not phone.isdigit() or len(phone) != 10):
            raise forms.ValidationError({
                'phone': 'Phone number must be 10 digits.'
            })
            
        return cleaned_data

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm
    
    list_display = (
        'email', 'full_name', 'role', 'pg_admin', 'is_staff', 'is_active',
        'has_logged_in', 'last_login', 'reset_email_sent', 'password_reset_sent'
    )
    list_filter = ('role', 'is_staff', 'is_active', 'last_login')
    search_fields = ('email', 'full_name', 'phone')
    ordering = ('-date_joined',)
    readonly_fields = ('last_login', 'date_joined', 'password_reset_sent_at', 'hierarchical_id')
    
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        (_('Personal Info'), {'fields': ('full_name', 'phone', 'profile_picture')}),
        (_('Role Information'), {
            'fields': ('role', 'pg_admin'),
            'description': _('For PG Staff, please select a PG Admin. PG Admins can manage multiple staff members.')
        }),
        (_('Permissions'), {
            'fields': (
                'is_active', 
                'is_staff', 
                'is_superuser',
                'groups', 
                'user_permissions'
            ),
        }),
        (_('Important Dates'), {
            'fields': (
                'last_login', 
                'date_joined',
                'password_reset_sent_at',
                'hierarchical_id'
            ),
        }),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'email',
                'password1',
                'password2',
                'full_name',
                'phone',
                'role',
                'pg_admin',
                'is_staff',
                'is_active'
            )
        }),
    )
    
    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if 'pg_admin' in form.base_fields:
            form.base_fields['pg_admin'].queryset = User.objects.filter(role='pg_admin')
            
            # If editing an existing user who is a staff member, make pg_admin required
            if obj and obj.role == 'pg_staff':
                form.base_fields['pg_admin'].required = True
            
            # If creating a new user, make pg_admin required conditionally based on role
            if not obj and 'role' in request.POST and request.POST['role'] == 'pg_staff':
                form.base_fields['pg_admin'].required = True
                
        return form
    
    def save_model(self, request, obj, form, change):
        # If user is a PG Admin, ensure pg_admin is None
        if obj.role == 'pg_admin':
            obj.pg_admin = None
        super().save_model(request, obj, form, change)
    
    def password_reset_sent(self, obj):
        if obj.password_reset_sent_at:
            return obj.password_reset_sent_at.strftime('%Y-%m-%d %H:%M:%S')
        return "Never"
    password_reset_sent.short_description = 'Password Reset Sent'
    
    # Boolean checkmark: has the user ever logged in?
    def has_logged_in(self, obj):
        return bool(obj.last_login)
    has_logged_in.boolean = True
    has_logged_in.short_description = 'Has logged in'
    
    # Boolean checkmark: has a password reset email been sent?
    def reset_email_sent(self, obj):
        return bool(getattr(obj, 'password_reset_sent_at', None))
    reset_email_sent.boolean = True
    reset_email_sent.short_description = 'Reset email sent'
    
    def send_password_reset_email(self, request, queryset):
        for user in queryset:
            if not user.email:
                self.message_user(
                    request,
                    f"User {user.full_name or user.email} has no email address set. Cannot send reset email.",
                    level=messages.WARNING
                )
                continue
                
            # Generate password reset token
            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            
            # Build reset URL
            reset_url = request.build_absolute_uri(
                reverse('password_reset_confirm', kwargs={
                    'uidb64': uid,
                    'token': token
                })
            )
            
            # Send email
            subject = 'Password Reset Requested'
            message = render_to_string('emails/password_reset_email.html', {
                'user': user,
                'reset_url': reset_url,
                'site_name': 'PG Management System',
            })
            
            try:
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    html_message=message,
                    fail_silently=False,
                )
                user.password_reset_sent_at = timezone.now()
                user.save(update_fields=['password_reset_sent_at'])
                self.message_user(
                    request,
                    f"Password reset email sent to {user.email}",
                    level=messages.SUCCESS
                )
            except Exception as e:
                self.message_user(
                    request,
                    f"Failed to send email to {user.email}: {str(e)}",
                    level=messages.ERROR
                )
    
    send_password_reset_email.short_description = "Send password reset email to selected users"
    
    actions = [send_password_reset_email]
    
    def get_actions(self, request):
        actions = super().get_actions(request)
        if 'delete_selected' in actions:
            del actions['delete_selected']
        return actions
    
    class ActivityLogInline(admin.TabularInline):
        model = ActivityLog
        extra = 0
        fields = ("action", "description", "timestamp")
        readonly_fields = ("action", "description", "timestamp")
        can_delete = False
        ordering = ("-timestamp",)
        verbose_name = "Activity"
        verbose_name_plural = "Recent Activities"
        max_num = 0
        show_change_link = False
        
        def has_add_permission(self, request, obj=None):
            return False
    
    inlines = [ActivityLogInline]

@admin.register(PendingRegistration)
class PendingRegistrationAdmin(admin.ModelAdmin):
    list_display = ("email", "role", "pg_admin", "email_otp", "email_otp_expires_at", "email_otp_attempts", "created_at")
    list_filter = ("role", "pg_admin", "created_at")
    search_fields = ("email", "full_name", "phone")
    autocomplete_fields = ("pg_admin",)
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("user", "action", "short_description", "timestamp")
    list_filter = ("action", "timestamp")
    search_fields = ("user__email", "user__full_name", "description")
    ordering = ("-timestamp",)
    date_hierarchy = "timestamp"
    readonly_fields = ("user", "action", "description", "timestamp", "meta")

    fieldsets = (
        (None, {"fields": ("user", "action", "timestamp")} ),
        ("Details", {"fields": ("description", "meta")}),
    )

    def short_description(self, obj):
        desc = obj.description or ""
        return (desc[:77] + "...") if len(desc) > 80 else desc
    short_description.short_description = "Description"
    
    # Make ActivityLog read-only in admin (system-generated)
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
