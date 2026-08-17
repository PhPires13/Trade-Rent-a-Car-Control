"""Campo criptografado para credenciais dos portais (docs.md §4.1).

Usa Fernet com chave derivada de settings.CREDENCIAIS_KEY (que por padrão é a
SECRET_KEY). Se a chave usada na gravação não for a mesma da leitura, o valor
não é recuperável: o campo devolve vazio e registra um aviso no log — nunca
devolve o texto cifrado disfarçado de senha, que acabaria salvo por cima da
credencial boa na primeira edição do cadastro.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

PREFIXO = "fernet:"

logger = logging.getLogger(__name__)


def _fernet():
    chave = hashlib.sha256(settings.CREDENCIAIS_KEY.encode()).digest()
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
            modelo = self.model._meta.label if hasattr(self, "model") else "?"
            logger.warning(
                "Credencial ilegível em %s.%s: a CREDENCIAIS_KEY atual não confere com "
                "a usada na gravação. O valor foi tratado como vazio — recadastre a "
                "credencial do portal ou restaure a chave anterior.",
                modelo,
                self.name,
            )
            return ""
