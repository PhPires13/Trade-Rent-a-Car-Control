"""Retomada da etapa 17 — datas e seletores em todos os formulários.

Abrir a edição e salvar sem mexer em nada tem de manter todas as datas
(inclusive as opcionais); ano digitado pela metade no celular ("0026") é
recusado com mensagem simples; "10/05/26" vira 2026.
"""

from datetime import date
from decimal import Decimal

import pytest
from django import forms
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao
from apps.campos import OPCAO_VAZIA, FormularioBR, ler_data
from apps.financeiro.models import Caucao, MovimentacaoCaucao, NotaDebito
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro
from config.test_qualidade_final import _ler

# ---------- leitura de datas ----------


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("2026-05-10", date(2026, 5, 10)),
        ("10/05/2026", date(2026, 5, 10)),
        ("10/05/26", date(2026, 5, 10)),
        (" 1/5/26 ", date(2026, 5, 1)),
    ],
)
def test_ler_data_aceita_calendario_e_digitado(texto, esperado):
    assert ler_data(texto) == esperado


@pytest.mark.parametrize("texto", ["0026-05-10", "1999-12-31", "10/05/99", "", "abc", "31/02/2026"])
def test_ler_data_recusa_com_mensagem(texto):
    with pytest.raises(ValidationError) as erro:
        ler_data(texto, "Data de emissão")
    assert erro.value.messages[0].startswith(("Data de emissão inválida", "Confira o ano"))


class _Form(FormularioBR, forms.Form):
    data = forms.DateField(required=False)
    carro = forms.ModelChoiceField(queryset=Veiculo.objects.all(), required=False)


@pytest.mark.django_db
@pytest.mark.parametrize("digitado", ["10/05/26", "10/05/2026", "2026-05-10"])
def test_formulario_aceita_ano_com_2_digitos(digitado):
    form = _Form(data={"data": digitado})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["data"] == date(2026, 5, 10)


@pytest.mark.django_db
def test_formulario_recusa_ano_0026_vindo_do_celular():
    form = _Form(data={"data": "0026-05-10"})
    assert not form.is_valid()
    assert form.errors["data"] == ["Confira o ano (ex.: 2026)."]


# ---------- ida e volta das telas de edição ----------


@pytest.fixture
def base(db):
    veiculo = Veiculo.objects.create(
        placa="QXQ6C10",
        marca_modelo="Gol",
        km_atual=50_000,
        data_aquisicao=date(2024, 3, 15),
        licenciamento_vencimento=date(2027, 4, 30),
        bateria_garantia_fim=date(2027, 1, 20),
    )
    cliente = Cliente.objects.create(
        nome="Arlen Souza",
        cpf_cnpj="111.444.777-35",
        cnh_validade=date(2029, 5, 10),
        telefone="(31) 98888-7777",
    )
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        cobrar_a_partir_de=date(2026, 7, 8),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )
    return veiculo, cliente, alocacao


def _ida_e_volta(client, url, datas):
    """Abre a edição, confere as datas no calendário e salva sem mexer em nada."""
    leitor = _ler(client.get(url).content.decode())
    for nome, valor in datas.items():
        assert leitor.datas.get(nome) == valor.isoformat(), (nome, leitor.datas)
    resposta = client.post(url, leitor.dados)
    assert resposta.status_code == 302, resposta.content.decode()[:3000]


def test_veiculo_salvo_sem_mexer_mantem_datas_opcionais(usuario_logado, base):
    veiculo = base[0]
    datas = {
        "data_aquisicao": veiculo.data_aquisicao,
        "licenciamento_vencimento": veiculo.licenciamento_vencimento,
        "bateria_garantia_fim": veiculo.bateria_garantia_fim,
    }
    _ida_e_volta(usuario_logado, f"/frota/veiculo/{veiculo.pk}/editar/", datas)
    veiculo.refresh_from_db()
    for nome, valor in datas.items():
        assert getattr(veiculo, nome) == valor


def test_cliente_salvo_sem_mexer_mantem_validade_da_cnh(usuario_logado, base):
    cliente = base[1]
    _ida_e_volta(
        usuario_logado, f"/clientes/{cliente.pk}/editar/", {"cnh_validade": date(2029, 5, 10)}
    )
    cliente.refresh_from_db()
    assert cliente.cnh_validade == date(2029, 5, 10)


def test_alocacao_salva_sem_mexer_mantem_datas(usuario_logado, base):
    alocacao = base[2]
    leitor = _ler(usuario_logado.get(f"/alocacoes/{alocacao.pk}/editar/").content.decode())
    for nome, valor in leitor.datas.items():
        assert valor == getattr(alocacao, nome).isoformat()
    usuario_logado.post(f"/alocacoes/{alocacao.pk}/editar/", leitor.dados)
    alocacao.refresh_from_db()
    assert alocacao.data_inicio == date(2026, 7, 1)
    assert alocacao.cobrar_a_partir_de == date(2026, 7, 8)


def test_manutencao_salva_sem_mexer_mantem_datas(usuario_logado, base):
    veiculo = base[0]
    manutencao = Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=date(2026, 9, 20),
        km=50_100,
        descricao="Freio",
        data_entrada=date(2026, 9, 18),
        data_saida=date(2026, 9, 20),
    )
    _ida_e_volta(
        usuario_logado,
        f"/manutencao/editar/{manutencao.pk}/",
        {"data": date(2026, 9, 20), "data_saida": date(2026, 9, 20)},
    )
    manutencao.refresh_from_db()
    assert (manutencao.data, manutencao.data_entrada, manutencao.data_saida) == (
        date(2026, 9, 20),
        date(2026, 9, 18),
        date(2026, 9, 20),
    )


def test_multa_salva_sem_mexer_mantem_datas(usuario_logado, base):
    veiculo = base[0]
    multa = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 7, 10),
        descricao="Avançar sinal",
        fici_prazo=date(2026, 8, 1),
    )
    _ida_e_volta(
        usuario_logado,
        f"/multas/{multa.pk}/editar/",
        {"data_infracao": date(2026, 7, 10), "fici_prazo": date(2026, 8, 1)},
    )
    multa.refresh_from_db()
    assert (multa.data_infracao, multa.fici_prazo) == (date(2026, 7, 10), date(2026, 8, 1))


def test_sinistro_salvo_sem_mexer_mantem_datas(usuario_logado, base):
    veiculo, cliente, _ = base
    sinistro = Sinistro.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data=date(2026, 9, 15),
        envolvido=Sinistro.Envolvido.ASSOCIADO,
        acionou_protecao=True,
        data_evento=date(2026, 9, 16),
    )
    pagina = usuario_logado.get(f"/sinistros/{sinistro.pk}/editar/").content.decode()
    assert "---------" not in pagina
    _ida_e_volta(
        usuario_logado,
        f"/sinistros/{sinistro.pk}/editar/",
        {"data": date(2026, 9, 15), "data_evento": date(2026, 9, 16)},
    )
    sinistro.refresh_from_db()
    assert (sinistro.data, sinistro.data_evento) == (date(2026, 9, 15), date(2026, 9, 16))


def test_correcao_de_km_mostra_a_data_no_calendario(usuario_logado, base):
    veiculo = base[0]
    registro = RegistroKm.objects.create(
        veiculo=veiculo,
        mes_referencia=date(2026, 9, 1),
        data_leitura=date(2026, 9, 28),
        km=52_000,
    )
    pagina = usuario_logado.get(f"/km/corrigir/{registro.pk}/").content.decode()
    assert _ler(pagina).datas["data_leitura"] == "2026-09-28"


# ---------- telas sem formulário Django: caução e ND ----------


def test_caucao_recusa_ano_0026_e_aceita_dd_mm_aa(usuario_logado, base):
    caucao = Caucao.objects.create(alocacao=base[2])
    url = f"/financeiro/caucoes/{caucao.pk}/"
    dados = {"tipo": MovimentacaoCaucao.Tipo.RECEBIMENTO, "valor": "500,00", "data": "0026-07-02"}
    dados["forma"] = "pix"
    resposta = usuario_logado.post(url, dados, follow=True)
    assert "Confira o ano" in resposta.content.decode()
    assert not caucao.movimentacoes.exists()

    usuario_logado.post(url, {**dados, "data": "02/07/26"})
    assert caucao.movimentacoes.get().data == date(2026, 7, 2)


def test_nd_recusa_emissao_com_ano_0026(usuario_logado, base):
    cliente = base[1]
    dados = {
        "cliente": cliente.pk,
        "data_emissao": "0026-10-20",
        "item_descricao": ["Lavagem"],
        "item_valor": ["50,00"],
    }
    resposta = usuario_logado.post("/financeiro/nd/nova/", dados, follow=True)
    assert "Confira o ano" in resposta.content.decode()
    assert not NotaDebito.objects.exists()


# ---------- seletores ----------


def test_telas_com_seletor_nao_mostram_tracinhos(usuario_logado, base):
    for url in [
        "/frota/veiculo/novo/",
        "/alocacoes/nova/",
        "/multas/nova/",
        "/sinistros/novo/",
        "/manutencao/plano/",
        "/multas/orgaos/novo/",
        "/frota/fornecedores/",
        f"/sinistros/novo/?veiculo={base[0].pk}",
    ]:
        resposta = usuario_logado.get(url)
        assert resposta.status_code == 200, url
        assert "---------" not in resposta.content.decode(), url


def test_opcao_vazia_amigavel():
    assert _Form().fields["carro"].empty_label == OPCAO_VAZIA
