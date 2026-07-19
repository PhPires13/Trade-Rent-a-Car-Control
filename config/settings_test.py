"""Settings usados pelo pytest — sem redirecionamento SSL e com storage simples."""

from config.settings import *  # noqa: F403

SECURE_SSL_REDIRECT = False
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
