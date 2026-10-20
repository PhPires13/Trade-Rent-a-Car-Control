"""Comparação de texto para as buscas — sem acento, sem maiúscula, só dígitos.

No celular quase ninguém digita acento ("Joao", "Conceicao"), e CPF/telefone
chegam com ou sem pontuação (copiados do WhatsApp, de carta de multa...).
A comparação é feita em Python: a base é pequena (~20 carros, centenas de
clientes) e assim vale igual no SQLite (testes) e no Postgres, sem extensão.
"""

import re
import unicodedata

#: Menos dígitos que isso é ambíguo demais para CPF/telefone ("12" casaria todo mundo).
MINIMO_DIGITOS = 4


def sem_acento(texto):
    """'João da CONCEIÇÃO' → 'joao da conceicao' (espaços repetidos viram um)."""
    decomposto = unicodedata.normalize("NFKD", texto or "")
    sem_marcas = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_marcas.casefold().split())


def so_digitos(texto):
    return re.sub(r"\D", "", texto or "")


def so_letras_e_digitos(texto):
    """Para códigos (AIT, BO): 'R-123.456 / 2025' → 'R1234562025'."""
    return re.sub(r"[^0-9a-z]", "", sem_acento(texto)).upper()


def parece_numero(consulta):
    """Consulta só com dígitos e pontuação (CPF, CNPJ, telefone), sem letras."""
    return not re.search(r"[^\W\d_]", consulta) and len(so_digitos(consulta)) >= MINIMO_DIGITOS


def variantes_telefone(digitos):
    """WhatsApp copia com o +55 do Brasil; o cadastro costuma estar sem.

    Só 12/13 dígitos (55 + DDD + fixo/celular): um CNPJ (14) que começa com
    55 não pode perder os dois primeiros números e casar com outro cadastro.
    """
    variantes = {digitos}
    if digitos.startswith("55") and len(digitos) in (12, 13):
        variantes.add(digitos[2:])
    return variantes


def nome_casa(consulta_normalizada, nome):
    return bool(consulta_normalizada) and consulta_normalizada in sem_acento(nome)


def numero_casa(digitos_consulta, valor):
    """O número digitado aparece (só dígitos) dentro do número gravado."""
    digitos_valor = so_digitos(valor)
    return bool(digitos_valor) and any(
        d in digitos_valor for d in variantes_telefone(digitos_consulta)
    )
