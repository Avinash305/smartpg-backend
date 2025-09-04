# smartpg-backend

## Production Readiness Guide

Follow this checklist to run the Django backend safely in production.

### 1) Required environment variables
Create a `.env` file next to `backend/backend/settings.py` project root (same folder as `manage.py`). At minimum set:

```
# Core
SECRET_KEY=change-this-to-a-long-unique-random-string
DEBUG=False
ALLOWED_HOSTS=your.domain.com,api.your.domain.com

# Database (choose one)
# Option A: DATABASE_URL (recommended)
# e.g. Postgres: postgres://USER:PASSWORD@HOST:5432/DBNAME
DATABASE_URL=postgres://postgres:password@localhost:5432/pgms
DB_SSL=True

# Option B: native settings (if not using DATABASE_URL)
POSTGRES_DB=pgms
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your-password
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432

# Redis / Celery
REDIS_URL=redis://localhost:6379/0

# CORS/CSRF
CORS_ALLOWED_ORIGINS=https://your-frontend.example
CORS_ALLOW_CREDENTIALS=False
CSRF_TRUSTED_ORIGINS=https://your-frontend.example

# Email (SMTP)
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=your@email
EMAIL_HOST_PASSWORD=your-app-password
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=SmartPG <no-reply@your.domain>

# Security headers (optional overrides)
SECURE_SSL_REDIRECT=True
SECURE_HSTS_SECONDS=31536000
X_FRAME_OPTIONS=DENY
SECURE_REFERRER_POLICY=strict-origin-when-cross-origin
SESSION_COOKIE_SAMESITE=Lax
CSRF_COOKIE_SAMESITE=Lax

# Storage backend (optional)
# MEDIA_BACKEND=local  # or s3 or cloudinary
# AWS_ACCESS_KEY_ID=
# AWS_SECRET_ACCESS_KEY=
# AWS_STORAGE_BUCKET_NAME=
# AWS_S3_REGION_NAME=
# CLOUDINARY_URL=

# Razorpay (optional)
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=

# Sentry (optional)
SENTRY_DSN=
SENTRY_ENVIRONMENT=production
SENTRY_TRACES_SAMPLE_RATE=0.0
SENTRY_PROFILES_SAMPLE_RATE=0.0
```

### 2) Install dependencies

```
cd backend
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3) Collect static, run migrations, create superuser

```
python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py createsuperuser
```

### 4) Start the server

- WSGI (Gunicorn):
```
gunicorn backend.wsgi:application --bind 0.0.0.0:8000 --workers 3
```

- ASGI (recommended for websockets / async):
```
gunicorn backend.asgi:application -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --workers 3
```

### 5) Reverse proxy (Nginx example)

```
server {
    listen 80;
    server_name your.domain.com;

    location /static/ {
        alias /path/to/project/backend/staticfiles/;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### 6) Systemd service (example)

```
[Unit]
Description=smartpg-backend
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/path/to/project/backend
Environment="DJANGO_SETTINGS_MODULE=backend.settings"
EnvironmentFile=/path/to/project/backend/.env
ExecStart=/path/to/project/backend/.venv/bin/gunicorn backend.asgi:application -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000 --workers 3
Restart=always

[Install]
WantedBy=multi-user.target
```

### 7) Health and metrics

- Health check: `GET /health/`
- Prometheus metrics: `GET /metrics` (from `django_prometheus`)

### 8) Celery (optional tasks)

Run workers and beat if you use scheduled tasks:

```
celery -A backend worker -l info
celery -A backend beat -l info
```

### 9) Troubleshooting

- If `DEBUG=False`, ensure:
  - `ALLOWED_HOSTS` includes your domain/IP
  - `SECRET_KEY` is set
  - Database is reachable (via `DATABASE_URL` or Postgres env vars)
- If static files 404 in production, confirm:
  - `python manage.py collectstatic` ran successfully
  - Nginx serves `/static/` from `backend/staticfiles/`
- If CORS errors occur, verify `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS`.