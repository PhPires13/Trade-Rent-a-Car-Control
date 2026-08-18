"""Configurações do projeto Trade Rent a Car.

Variáveis de ambiente (arquivo .env em desenvolvimento):
- SECRET_KEY, DEBUG, ALLOWED_HOSTS, DATABASE_URL, CSRF_TRUSTED_ORIGINS
- CREDENCIAIS_KEY (opcional): chave das credenciais dos portais de multas
"""

from datetime import timedelta
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

CHAVE_DE_DESENVOLVIMENTO = "dev-only-insecure-key"


def chave_secreta(valor, debug):
    """SECRET_KEY obrigatória em produção; em desenvolvimento cai numa chave fixa.

    Sem isso, um deploy que esquecesse a variável subiria com uma chave pública
    do repositório — que também deriva a criptografia das credenciais dos
    portais (apps/multas/fields.py).
    """
    if valor:
        return valor
    if not debug:
        raise ImproperlyConfigured(
            "SECRET_KEY não definida. Com DEBUG=False é obrigatório definir a variável "
            'de ambiente SECRET_KEY (gere com: python -c "from django.core.management.'
            'utils import get_random_secret_key; print(get_random_secret_key())").'
        )
    return CHAVE_DE_DESENVOLVIMENTO


DEBUG = env("DEBUG")
SECRET_KEY = chave_secreta(env("SECRET_KEY", default=""), DEBUG)
# Chave das credenciais dos portais (apps/multas/fields.py). Separada da SECRET_KEY
# para que rotacionar a SECRET_KEY não torne as senhas gravadas ilegíveis. Se não
# for definida, usa a SECRET_KEY — e aí não pode mais ser trocada sem recadastro.
CREDENCIAIS_KEY = env("CREDENCIAIS_KEY", default=SECRET_KEY)
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "simple_history",
    "axes",
    "apps.frota",
    "apps.pessoas",
    "apps.alocacoes",
    "apps.financeiro",
    "apps.km",
    "apps.manutencao",
    "apps.multas",
    "apps.sinistros",
    "apps.relatorios",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Bloqueio de força bruta no login (django-axes). Trava por usuário, não por IP:
# atrás do proxy do Railway o IP ou colapsa num só (um bot travaria os donos) ou
# vem do X-Forwarded-For, que é falsificável. O AxesStandaloneBackend vem primeiro.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_LOCKOUT_PARAMETERS = ["username"]
AXES_FAILURE_LIMIT = 6
AXES_COOLOFF_TIME = timedelta(minutes=15)  # destrava sozinho, nunca bloqueio permanente
AXES_RESET_ON_SUCCESS = True
AXES_COOLOFF_MESSAGE = (
    "Muitas tentativas de senha erradas. Por segurança, o acesso a esta conta ficou "
    "bloqueado por 15 minutos — tente de novo mais tarde."
)
# W006 sugere travar também por IP; aqui é decisão contrária deliberada (ver acima).
SILENCED_SYSTEM_CHECKS = ["axes.W006"]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "painel"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Fotos e documentos enviados (carro, motorista, CNH). Servidos por view autenticada
# (config.views.midia) — CNH é documento sensível, nunca pode virar URL pública.
# Em produção (Railway), aponte MEDIA_ROOT para um volume persistente (ex.: /data/midia).
MEDIA_URL = "midia/"
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "midia"))

# Leitura automática da CNH no cadastro (apps/pessoas/cnh.py) — opcional:
# sem ANTHROPIC_API_KEY o cadastro segue funcionando, só sem o preenchimento automático.
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
CNH_MODELO = env("CNH_MODELO", default="claude-opus-5")

# Chave Pix da empresa — entra na mensagem de cobrança pronta do WhatsApp (opcional).
CHAVE_PIX = env("CHAVE_PIX", default="")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # HSTS: depois da primeira visita o navegador só usa https, sem o salto em
    # http que um atacante na mesma rede Wi-Fi interceptaria. 30 dias, sem
    # subdomínios e sem preload (preload é irreversível na prática).
    SECURE_HSTS_SECONDS = 2592000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False
