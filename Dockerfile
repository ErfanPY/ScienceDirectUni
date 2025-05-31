# Dockerfile (Django Web App for _base project)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Install build essentials for some python packages, git for potential VCS installs
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \ # For psycopg2 if using PostgreSQL
    # Add any other system dependencies your project might have
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers with their dependencies
# For a single celery worker, this is fine. If web also needed playwright, it'd be here too.
# We'll put Playwright installation in the Celery Dockerfile primarily.
# If web app ever needs playwright directly:
# RUN playwright install --with-deps chromium

COPY . /app/

# Set DJANGO_SETTINGS_MODULE if not already managed by wsgi/asgi
ENV DJANGO_SETTINGS_MODULE=_base.settings

# Run collectstatic if you have custom static files for admin or your app
# RUN python manage.py collectstatic --noinput --clear

EXPOSE 8000

# Command to run the application using Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "_base.wsgi:application"]
