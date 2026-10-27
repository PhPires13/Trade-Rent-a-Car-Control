"""Conferência e formato único de CPF, CNPJ, CNH e telefone do cadastro.

Esses dados vão para a FICI (indicação de condutor) e para o WhatsApp: um
dígito trocado vira multa no nome errado ou cobrança que não chega. O
cadastro grava sempre no mesmo formato ("529.982.247-25", "(31) 98888-7777"),
assim o mesmo CPF digitado com ou sem pontos não vira dois clientes.
"""

from django.core.exceptions import ValidationError

from apps.busca import so_digitos

MENSAGEM_CPF = "CPF inválido — confira os 11 números (ex.: 529.982.247-25)."
MENSAGEM_CPF_CNPJ = (
    "CPF ou CNPJ inválido — confira os números (CPF tem 11, CNPJ tem 14; um número trocado "
    "faz a multa ir para a pessoa errada)."
)
MENSAGEM_CNH = "Número da CNH inválido — são 11 números (campo Nº REGISTRO da CNH)."


def _digito(numeros, pesos):
    resto = sum(int(n) * p for n, p in zip(numeros, pesos, strict=False)) % 11
    return "0" if resto < 2 else str(11 - resto)


def cpf_valido(digitos):
    if len(digitos) != 11 or len(set(digitos)) == 1:
        return False
    primeiro = _digito(digitos[:9], range(10, 1, -1))
    segundo = _digito(digitos[:10], range(11, 1, -1))
    return digitos[9:] == primeiro + segundo


def cnpj_valido(digitos):
    if len(digitos) != 14 or len(set(digitos)) == 1:
        return False
    pesos = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    primeiro = _digito(digitos[:12], pesos)
    segundo = _digito(digitos[:13], [6, *pesos])
    return digitos[12:] == primeiro + segundo


def formatar_cpf_cnpj(texto):
    """'52998224725' → '529.982.247-25'; CNPJ → '11.222.333/0001-81'.

    Número que não tem 11 nem 14 dígitos volta como veio (cadastro antigo).
    """
    d = so_digitos(texto)
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return (texto or "").strip()


def cpf_cnpj(texto):
    """CPF ou CNPJ conferido e formatado; ValidationError se os números não fecham."""
    d = so_digitos(texto)
    if not (cpf_valido(d) or cnpj_valido(d)):
        raise ValidationError(MENSAGEM_CPF_CNPJ, code="cpf_cnpj_invalido")
    return formatar_cpf_cnpj(d)


def cpf(texto):
    """CPF (só pessoa física) conferido e formatado; vazio continua vazio."""
    if not (texto or "").strip():
        return ""
    d = so_digitos(texto)
    if not cpf_valido(d):
        raise ValidationError(MENSAGEM_CPF, code="cpf_invalido")
    return formatar_cpf_cnpj(d)


def cnh_numero(texto):
    """Número de registro da CNH: 11 números; vazio continua vazio."""
    if not (texto or "").strip():
        return ""
    d = so_digitos(texto)
    # letras no meio ("12345abc") não são uma CNH
    if len(d) != 11 or any(c.isalpha() for c in texto):
        raise ValidationError(MENSAGEM_CNH, code="cnh_invalida")
    return d


def formatar_telefone(texto):
    """'31988887777' ou '+55 31 98888-7777' → '(31) 98888-7777'.

    Sem DDD (ou com dígitos a mais) volta como veio — quem chama decide se aceita.
    """
    d = so_digitos(texto)
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return (texto or "").strip()
