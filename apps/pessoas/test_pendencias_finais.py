"""Etapa 16: telefone sem DDD não vira link de WhatsApp inválido."""

import pytest

from apps.financeiro.whatsapp import MENSAGEM_TELEFONE_SEM_DDD, telefone_whatsapp
from apps.pessoas.models import Cliente
from apps.pessoas.views import ClienteForm


def _dados(telefone):
    return {
        "nome": "Bruno Lima",
        "cpf_cnpj": "123.456.789-09",
        "telefone": telefone,
        "status": Cliente.Status.ATIVO,
    }


@pytest.mark.parametrize(
    ("telefone", "esperado"),
    [
        ("(31) 98888-7777", "5531988887777"),
        ("(31) 3333-4444", "553133334444"),
        ("+55 (21) 98888-7777", "5521988887777"),
        ("55 31 3333-4444", "553133334444"),
        ("98888-7777", ""),  # sem DDD
        ("3333-4444", ""),
        ("12 3456 7890 12", ""),  # 12 dígitos sem o 55
        ("", ""),
    ],
)
def test_telefone_whatsapp_exige_ddd(telefone, esperado):
    assert telefone_whatsapp(telefone) == esperado


def test_cadastro_recusa_telefone_sem_ddd(db):
    form = ClienteForm(data=_dados("98888-7777"))
    assert not form.is_valid()
    assert form.errors["telefone"] == [MENSAGEM_TELEFONE_SEM_DDD]


@pytest.mark.parametrize("telefone", ["(31) 98888-7777", "+55 31 98888-7777", ""])
def test_cadastro_aceita_telefone_com_ddd_ou_vazio(db, telefone):
    form = ClienteForm(data=_dados(telefone))
    assert form.is_valid(), form.errors


def test_cadastro_antigo_sem_ddd_abre_e_so_valida_ao_salvar(usuario_logado, db):
    cliente = Cliente.objects.create(
        nome="Antigo", cpf_cnpj="987.654.321-00", telefone="98888-7777"
    )
    resposta = usuario_logado.get(f"/clientes/{cliente.pk}/editar/")
    assert resposta.status_code == 200
    # a ficha também abre, sem botão de WhatsApp quebrado
    ficha = usuario_logado.get(f"/clientes/{cliente.pk}/")
    assert ficha.status_code == 200
    assert "wa.me/" not in ficha.content.decode()


def test_admin_recusa_telefone_sem_ddd(db):
    from django.contrib.admin.sites import site

    form_class = site._registry[Cliente].get_form(None)
    form = form_class(data=_dados("98888-7777"))
    assert not form.is_valid()
    assert form.errors["telefone"] == [MENSAGEM_TELEFONE_SEM_DDD]
