"""Settings usados pelo pytest — sem redirecionamento SSL e com storage simples."""

import os

# Fora de DEBUG a SECRET_KEY é obrigatória (config/settings.py). O CI roda sem .env
# e sem DEBUG, então a chave descartável dos testes entra antes do import.
os.environ.setdefault("SECRET_KEY", "chave-descartavel-somente-para-testes")

from config.settings import *  # noqa: E402, F403

SECURE_SSL_REDIRECT = False
# client.login() dos testes chama authenticate() sem request, e o backend do axes
# exige request. Os testes que exercitam o bloqueio ligam com override_settings.
AXES_ENABLED = False
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
