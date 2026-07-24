"""Campo criptografado para credenciais dos portais (docs.md §4.1).

Usa Fernet com chave derivada da SECRET_KEY — se a SECRET_KEY mudar,
os valores gravados deixam de ser legíveis (o campo devolve o texto cifrado).
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

PREFIXO = "fernet:"


def _fernet():
    chave = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(chave))


class CampoCriptografado(models.TextField):
    def get_prep_value(self, value):
        if value in (None, ""):
            return value
        if str(value).startswith(PREFIXO):
            return value
        return PREFIXO + _fernet().encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if not value or not value.startswith(PREFIXO):
            return value
        try:
            return _fernet().decrypt(value[len(PREFIXO) :].encode()).decode()
        except InvalidToken:
            return value
