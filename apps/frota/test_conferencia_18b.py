"""Conferência final da etapa 18b — ciclo de locação visto pela frota.

Revisão adversarial do diff desde 46ebf80 (locação, pessoas, frota, KM e
manutenção). Nenhum defeito novo apareceu; estes testes guardam o que foi
conferido para que uma mudança futura não o quebre:

- as telas de lista e de detalhe não fazem uma consulta a mais por carro
  (a frota cresce e o Paulo usa no celular);
- encerrar devolvendo o substituto, com o KM da retirada corrigido na hora,
  deixa cada carro no status certo (o do contrato segue "Em manutenção");
- devolver o substituto com "o cliente volta para o carro dele" fecha a ida
  à oficina, e o carro do contrato continua "Alocado";
- a venda não pode ser antes do último lançamento do carro, e KM fora do
  normal pede "Confirmo o KM".
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria, Vistoria
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="RUJ3I28", marca_modelo="Onix", km_atual=143_140)


@pytest.fixture
def alocacao(veiculo):
    cliente = Cliente.objects.create(nome="Arlen", cpf_cnpj="529.982.247-25")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=20),
        valor_semanal=Decimal("650.00"),
        dia_vencimento=HOJE.weekday(),
        km_entrega=50_000,
    )


def _troca(alocacao, substituto, km_retirada):
    troca = TrocaTemporaria(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=5),
        km_retirada=km_retirada,
    )
    troca.save()
    return troca


def _na_oficina(veiculo):
    return Manutencao.objects.create(
        veiculo=veiculo,
        data=HOJE - timedelta(days=5),
        data_entrada=HOJE - timedelta(days=5),
        descricao="Motor",
    )


# --- nenhuma tela faz uma consulta a mais por carro --------------------------------


def _carros_alugados(quantos, primeiro):
    mes_passado = HOJE.replace(day=1) - timedelta(days=1)
    for i in range(primeiro, primeiro + quantos):
        carro = Veiculo.objects.create(
            placa=f"ZZZ{i}A{10 + i}",
            marca_modelo="Gol",
            km_atual=40_000,
            data_aquisicao=HOJE - timedelta(days=400),
            km_compra=10_000,
        )
        cliente = Cliente.objects.create(nome=f"Cliente {i}", cpf_cnpj=f"{i:011d}")
        alocacao = Alocacao.objects.create(
            veiculo=carro,
            cliente=cliente,
            data_inicio=HOJE - timedelta(days=40),
            valor_semanal=Decimal("650"),
            km_entrega=40_000,
        )
        reserva = Veiculo.objects.create(
            placa=f"YYY{i}B{10 + i}", marca_modelo="Onix", km_atual=1_000
        )
        _troca(alocacao, reserva, 1_000)
        Manutencao.objects.create(
            veiculo=carro,
            data=HOJE - timedelta(days=3),
            data_entrada=HOJE - timedelta(days=3),
            descricao="Freio",
            km=40_100,
        )
        Vistoria.objects.create(
            alocacao=alocacao, tipo=Vistoria.Tipo.ENTRADA, data=alocacao.data_inicio, km=40_000
        )
        RegistroKm.objects.create(
            veiculo=carro, mes_referencia=mes_passado, data_leitura=mes_passado, km=40_500
        )
    return Veiculo.objects.get(placa=f"ZZZ{primeiro}A{10 + primeiro}")


@pytest.mark.parametrize(
    "endereco",
    [
        "/",
        "/alocacoes/",
        "/alocacoes/nova/",
        "/km/",
        "/frota/",
        "/clientes/",
        "/manutencao/preventivas/",
    ],
)
def test_listas_nao_fazem_uma_consulta_por_carro(usuario_logado, endereco):
    _carros_alugados(2, 1)
    usuario_logado.get(endereco)  # aquece o cache da sessão e afins
    with CaptureQueriesContext(connection) as poucos:
        assert usuario_logado.get(endereco).status_code == 200
    _carros_alugados(4, 10)
    with CaptureQueriesContext(connection) as muitos:
        assert usuario_logado.get(endereco).status_code == 200
    assert len(muitos) <= len(poucos) + 2, (endereco, len(poucos), len(muitos))


def test_fichas_nao_crescem_com_a_frota(usuario_logado):
    carro = _carros_alugados(1, 1)
    alocacao = carro.alocacoes.get()
    enderecos = [
        f"/frota/veiculo/{carro.pk}/",
        f"/clientes/{alocacao.cliente_id}/",
        f"/alocacoes/{alocacao.pk}/encerrar/",
        f"/km/historico/{carro.pk}/",
    ]
    contagem = {}
    for endereco in enderecos:
        with CaptureQueriesContext(connection) as consultas:
            assert usuario_logado.get(endereco).status_code == 200
        contagem[endereco] = len(consultas)
    _carros_alugados(4, 10)
    for endereco in enderecos:
        with CaptureQueriesContext(connection) as consultas:
            assert usuario_logado.get(endereco).status_code == 200
        assert len(consultas) <= contagem[endereco] + 2, (endereco, contagem[endereco])


# --- encerrar devolvendo o substituto ------------------------------------------------


def test_encerrar_com_substituto_e_retirada_corrigida_deixa_cada_carro_no_lugar(
    usuario_logado, alocacao, veiculo, substituto
):
    troca = _troca(alocacao, substituto, 14_314)  # digitado errado: era 143.140
    _na_oficina(veiculo)
    endereco = f"/alocacoes/{alocacao.pk}/encerrar/"
    dados = {"data_termino": HOJE.isoformat(), "km_devolucao": "143.200", "troca": troca.pk}

    resposta = usuario_logado.post(endereco, dados)
    assert resposta.status_code == 200
    assert 'name="km_retirada"' in resposta.content.decode()  # oferece corrigir a retirada

    resposta = usuario_logado.post(endereco, {**dados, "km_retirada": "143.140"})
    assert resposta.status_code == 302
    troca.refresh_from_db()
    alocacao.refresh_from_db()
    substituto.refresh_from_db()
    veiculo.refresh_from_db()
    assert troca.km_retirada == 143_140
    assert troca.data_devolucao == HOJE
    assert alocacao.status == Alocacao.Status.ENCERRADA
    assert alocacao.km_devolucao == 50_000  # o carro do contrato não rodou
    assert substituto.status == Veiculo.Status.DISPONIVEL
    assert substituto.km_atual == 143_200
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO  # segue na oficina, sem cliente

    # aba velha mandando de novo: aviso, sem erro 500 nem acerto em dobro
    assert usuario_logado.post(endereco, dados).status_code == 302


def test_devolver_substituto_confirmando_a_volta_fecha_a_oficina(
    usuario_logado, alocacao, veiculo, substituto
):
    troca = _troca(alocacao, substituto, 143_140)
    oficina = _na_oficina(veiculo)
    resposta = usuario_logado.post(
        f"/alocacoes/troca/{troca.pk}/devolver/",
        {"data_devolucao": HOJE.isoformat(), "km_devolucao": "143.500", "confirmar_oficina": "1"},
    )
    assert resposta.status_code == 302
    oficina.refresh_from_db()
    veiculo.refresh_from_db()
    substituto.refresh_from_db()
    assert oficina.data_saida == HOJE
    assert veiculo.status == Veiculo.Status.ALOCADO  # o cliente voltou para ele
    assert substituto.status == Veiculo.Status.DISPONIVEL


def test_vistoria_de_devolucao_durante_a_troca_so_oferece_o_substituto(
    usuario_logado, alocacao, substituto
):
    _troca(alocacao, substituto, 143_140)
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/nova/?tipo=saida")
    assert list(resposta.context["form"].fields["veiculo"].queryset) == [substituto]


# --- venda ------------------------------------------------------------------------


def test_venda_respeita_o_ultimo_lancamento_e_confere_o_km(usuario_logado, alocacao, veiculo):
    alocacao.encerrar(HOJE - timedelta(days=2), 50_500)
    endereco = f"/frota/veiculo/{veiculo.pk}/vender/"

    resposta = usuario_logado.post(
        endereco,
        {"data": (HOJE - timedelta(days=3)).isoformat(), "valor": "30.000,00", "km": "50.500"},
    )
    assert resposta.status_code == 200
    assert "não pode ser antes disso" in resposta.content.decode()

    resposta = usuario_logado.post(
        endereco, {"data": HOJE.isoformat(), "valor": "30.000,00", "km": "505.000"}
    )
    assert "Confirmo o KM" in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.status != Veiculo.Status.VENDIDO

    resposta = usuario_logado.post(
        endereco, {"data": HOJE.isoformat(), "valor": "30.000,00", "km": "50.600"}
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.VENDIDO
    assert veiculo.km_atual == 50_600
