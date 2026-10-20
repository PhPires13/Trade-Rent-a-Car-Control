"""Faixa de pendências e bloco "Vender este carro" no detalhe do veículo (proposta #16, opção A)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.alertas import pendencias_do_veiculo
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.multas.models import Multa
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro

HOJE = date(2026, 10, 20)


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=95_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def _km_do_mes(veiculo, dia=HOJE):
    RegistroKm.objects.create(
        veiculo=veiculo,
        mes_referencia=dia.replace(day=1),
        data_leitura=dia,
        km=veiculo.km_atual,
    )


def _textos(veiculo, hoje=HOJE):
    return [p["texto"] for p in pendencias_do_veiculo(veiculo, hoje=hoje)]


def test_carro_em_dia_nao_tem_pendencias(veiculo):
    _km_do_mes(veiculo)
    assert pendencias_do_veiculo(veiculo, hoje=HOJE) == []


def test_km_do_mes_nao_anotado_aparece_com_link_para_a_lista(veiculo):
    pendencias = pendencias_do_veiculo(veiculo, hoje=HOJE)
    assert [p["texto"] for p in pendencias] == ["KM de outubro ainda não anotado"]
    assert pendencias[0]["url"] == "/km/?mes=2026-10"


def test_preventiva_vencida_vem_primeiro_e_proxima_depois(veiculo):
    _km_do_mes(veiculo)
    oleo = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    alinhamento = ItemPreventiva.objects.get(nome="Alinhamento")
    Manutencao.objects.create(
        veiculo=veiculo, item=alinhamento, tipo="preventiva", data=date(2026, 5, 1), km=85_500
    )  # próxima aos 95.500 → faltam 500
    Manutencao.objects.create(
        veiculo=veiculo, item=oleo, tipo="preventiva", data=date(2026, 5, 1), km=84_000
    )  # próxima aos 94.000 → passou 1.000
    veiculo.refresh_from_db()
    pendencias = pendencias_do_veiculo(veiculo, hoje=HOJE)
    assert pendencias[0]["texto"] == "Troca de óleo e filtro: vencida"
    assert pendencias[0]["grave"] is True
    assert "1.000 km" in pendencias[0]["detalhe"]
    assert pendencias[1]["texto"] == "Alinhamento: está chegando a hora"
    assert pendencias[1]["url"] == f"/manutencao/registrar/{veiculo.pk}/"


def test_ipva_e_licenciamento_dentro_de_30_dias(veiculo):
    _km_do_mes(veiculo)
    veiculo.ipva_ano = 2026
    veiculo.ipva_valor = Decimal("1480.00")
    veiculo.ipva_vencimento = HOJE + timedelta(days=10)
    veiculo.licenciamento_vencimento = HOJE - timedelta(days=1)
    veiculo.save()
    pendencias = {p["texto"]: p for p in pendencias_do_veiculo(veiculo, hoje=HOJE)}
    assert "IPVA 2026 em aberto" in pendencias
    assert "vence em 10 dias" in pendencias["IPVA 2026 em aberto"]["detalhe"]
    assert "R$ 1.480,00" in pendencias["IPVA 2026 em aberto"]["detalhe"]
    assert pendencias["Licenciamento"]["grave"] is True

    veiculo.ipva_pago_em = HOJE
    veiculo.licenciamento_vencimento = HOJE + timedelta(days=60)
    veiculo.save()
    assert pendencias_do_veiculo(veiculo, hoje=HOJE) == []


def test_multa_sem_motorista_e_sinistro_aberto(veiculo):
    _km_do_mes(veiculo)
    Multa.objects.create(
        veiculo=veiculo, data_infracao=date(2026, 10, 1), fici_prazo=HOJE + timedelta(days=3)
    )
    Multa.objects.create(
        veiculo=veiculo, data_infracao=date(2026, 9, 1), fici_status=Multa.Fici.INDICADO
    )
    Sinistro.objects.create(veiculo=veiculo, data=date(2026, 10, 5), envolvido="cliente")
    Sinistro.objects.create(
        veiculo=veiculo,
        data=date(2026, 3, 5),
        envolvido="cliente",
        status=Sinistro.Status.CONCLUIDO,
    )
    pendencias = {p["texto"]: p for p in pendencias_do_veiculo(veiculo, hoje=HOJE)}
    multa = pendencias["1 multa sem indicar o motorista (FICI)"]
    assert multa["grave"] is True  # prazo em 3 dias
    assert multa["url"] == "/multas/?veiculo=QXQ6C10&tarefa=indicar"
    assert pendencias["1 sinistro em aberto"]["url"] == "/sinistros/?veiculo=QXQ6C10"


def test_carro_vendido_ou_fora_de_locacao_sem_preventiva_nem_km(veiculo):
    veiculo.uso = Veiculo.Uso.FORA_LOCACAO
    veiculo.save()
    assert _textos(veiculo) == []
    veiculo.uso = Veiculo.Uso.LOCACAO
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.save()
    assert _textos(veiculo) == []


def test_detalhe_mostra_a_faixa_com_botao_para_resolver(usuario_logado, veiculo):
    conteudo = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Pendências deste carro" in conteudo
    assert "ainda não anotado" in conteudo
    assert "Anotar o KM" in conteudo


def test_detalhe_em_dia_mostra_tudo_certo(usuario_logado, veiculo):
    _km_do_mes(veiculo, date.today())
    conteudo = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Tudo em dia com este carro" in conteudo
    assert "Pendências deste carro" not in conteudo


def test_textos_novos_do_detalhe(usuario_logado, veiculo):
    conteudo = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Lucro do carro e quando vender" in conteudo
    assert "Mensalidade da proteção (Auto Truck)" in conteudo
    assert "desmobilização" not in conteudo


def test_venda_liberada_explica_e_pede_confirmacao(usuario_logado, veiculo):
    conteudo = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Vender este carro" in conteudo
    assert "não pode ser desfeita" in conteudo
    assert "Sim, registrar a venda" in conteudo
    assert f"/frota/veiculo/{veiculo.pk}/vender/" in conteudo


def test_venda_bloqueada_com_carro_alugado(usuario_logado, veiculo, cliente):
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )
    conteudo = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Este carro está com Arlen desde 01/07/2026" in conteudo
    assert f"/alocacoes/{alocacao.pk}/encerrar/" in conteudo
    assert f"/frota/veiculo/{veiculo.pk}/vender/" not in conteudo


def test_venda_bloqueada_para_o_substituto_em_troca(usuario_logado, veiculo, cliente):
    substituto = Veiculo.objects.create(placa="RNB9J66", marca_modelo="Voyage", km_atual=50_000)
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=95_000,
    )
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=date(2026, 7, 10),
        km_retirada=50_000,
    )
    conteudo = usuario_logado.get(f"/frota/veiculo/{substituto.pk}/").content.decode()
    assert "emprestado a Arlen (troca) desde 10/07/2026" in conteudo
    assert f"/alocacoes/troca/{troca.pk}/devolver/" in conteudo
    assert f"/frota/veiculo/{substituto.pk}/vender/" not in conteudo


def test_carro_vendido_nao_mostra_faixa_nem_venda(usuario_logado, veiculo):
    veiculo.status = Veiculo.Status.VENDIDO
    veiculo.save()
    conteudo = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Pendências deste carro" not in conteudo
    assert "Tudo em dia" not in conteudo
    assert "Vender este carro" not in conteudo


def test_multa_anulada_nao_cobra_fici(veiculo):
    """Mesma regra do atalho "Indicar condutor": multa anulada não precisa de FICI."""
    _km_do_mes(veiculo)
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=date(2026, 10, 1),
        resultado=Multa.Resultado.ADVERTENCIA,
    )
    assert _textos(veiculo) == []


def test_link_da_multa_abre_so_as_que_faltam_indicar(usuario_logado, veiculo):
    Multa.objects.create(veiculo=veiculo, data_infracao=date(2026, 10, 1))
    url = next(p["url"] for p in pendencias_do_veiculo(veiculo) if "FICI" in p["texto"])
    resposta = usuario_logado.get(url)
    assert resposta.status_code == 200
    assert resposta.context["tarefa"] == "indicar"
    assert list(resposta.context["multas"])


def test_preventiva_quase_no_dia_nao_fala_em_0_dias(veiculo):
    from unittest import mock

    from apps.manutencao.services import StatusPreventiva

    item = ItemPreventiva.objects.get(nome="Alinhamento")
    prev = StatusPreventiva(
        item=item,
        intervalo_km=10_000,
        ultima=None,
        km_proximo=95_050,
        faltam_km=50,
        media_km_dia=200,
    )
    with mock.patch("apps.manutencao.services.resumo_preventivas", return_value=[prev]):
        pendencias = pendencias_do_veiculo(veiculo, hoje=HOJE)
    detalhe = pendencias[0]["detalhe"]
    assert "0 dias" not in detalhe and "1 dias" not in detalhe
    assert "próximos dias" in detalhe


def test_detalhe_texto_da_venda_nao_promete_receita(usuario_logado, veiculo):
    conteudo = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "não conta para o DAS" in conteudo
    assert "entra como receita" not in conteudo


def test_detalhe_de_carro_inexistente_da_404(usuario_logado, db):
    assert usuario_logado.get("/frota/veiculo/999999/").status_code == 404


def test_detalhe_exige_login(client, veiculo):
    resposta = client.get(f"/frota/veiculo/{veiculo.pk}/")
    assert resposta.status_code == 302
