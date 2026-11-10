"""Etapa 18b — achados dos testes no navegador (clientes e condutores)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente, CondutorAutorizado

HOJE = timezone.localdate()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Leigo Teste Um", cpf_cnpj="529.982.247-25")


def test_erro_no_condutor_mantem_o_que_foi_digitado(usuario_logado, cliente):
    resposta = usuario_logado.post(
        f"/clientes/{cliente.pk}/condutores/novo/",
        {"nome": "José", "cpf": "123", "cnh_numero": "abc", "contato": "qualquer"},
    )
    # não volta com redirect: a ficha abre com os dados e o erro junto do campo
    assert resposta.status_code == 200
    form = resposta.context["form_condutor"]
    assert form.errors
    texto = resposta.text
    assert 'value="José"' in texto
    assert 'value="qualquer"' in texto
    assert "autofocus" in texto
    assert not CondutorAutorizado.objects.exists()


def test_condutor_certo_continua_redirecionando(usuario_logado, cliente):
    resposta = usuario_logado.post(
        f"/clientes/{cliente.pk}/condutores/novo/", {"nome": "Maria", "contato": "31 99999-0000"}
    )
    assert resposta.status_code == 302
    assert CondutorAutorizado.objects.filter(cliente=cliente, nome="Maria").exists()


def test_edicao_de_cliente_nao_le_a_cnh_sozinha(usuario_logado, cliente, settings):
    from apps.pessoas import cnh

    settings.ANTHROPIC_API_KEY = "teste"
    if not cnh.disponivel():
        pytest.skip("leitura da CNH depende de outra configuração")
    edicao = usuario_logado.get(f"/clientes/{cliente.pk}/editar/").text
    assert "Comece por aqui" not in edicao
    assert "const editando = true" in edicao
    novo = usuario_logado.get("/clientes/novo/").text
    assert "Comece por aqui" in novo
    assert "const editando = false" in novo


def test_historico_do_cliente_cabe_no_celular(usuario_logado, cliente):
    veiculo = Veiculo.objects.create(placa="QWE8G90", marca_modelo="Gol", km_atual=10_000)
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("650"),
        km_entrega=10_000,
    )
    alocacao.encerrar(data_termino=HOJE - timedelta(days=5), km_devolucao=12_000)
    texto = usuario_logado.get(f"/clientes/{cliente.pk}/").text
    # período numa coluna só e sem a coluna "Situação" (sempre "Encerrada")
    assert "Período" in texto
    assert '<th class="px-3 py-2">Situação</th>' not in texto
    assert f"→ {HOJE - timedelta(days=5):%d/%m/%Y}" in texto


def test_leitura_da_cnh_na_edicao_ignora_maiusculas_acentos_e_pontuacao(
    usuario_logado, cliente, settings
):
    # a CNH vem "JOAO DA SILVA" / "123.456.789-00": não é diferença do cadastro
    # "João da Silva" / "12345678900" — senão a lista de diferenças aparecia sempre
    from apps.pessoas import cnh

    settings.ANTHROPIC_API_KEY = "teste"
    if not cnh.disponivel():
        pytest.skip("leitura da CNH depende de outra configuração")
    edicao = usuario_logado.get(f"/clientes/{cliente.pk}/editar/").text
    assert "comparavel(atual) !== comparavel(valor)" in edicao
    assert 'normalize("NFD")' in edicao
    # validade aparece como dd/mm/aaaa na lista de diferenças
    assert "legivel(d.antes)" in edicao
