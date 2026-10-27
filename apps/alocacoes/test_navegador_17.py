"""Etapa 17 (testes no navegador) — alocações: oficina fechada na devolução do
substituto, KM com ponto de milhar, vistoria com resumo de erros, entrega guiada
voltando à ficha, linha do tempo e textos do encerramento."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.entrega import passos_da_entrega, situacao_da_devolucao
from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.alocacoes.services import linha_do_tempo
from apps.financeiro.models import Caucao, Cobranca
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ7R29", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen Souza", cpf_cnpj="529.982.247-25")


@pytest.fixture
def alocacao(veiculo, cliente):
    veiculo.status = Veiculo.Status.ALOCADO
    veiculo.save()
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=20),
        valor_semanal=650,
        km_entrega=50_000,
    )


@pytest.fixture
def troca(alocacao):
    substituto = Veiculo.objects.create(placa="RNB7R29", marca_modelo="Voyage", km_atual=39_100)
    return TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=11),
        km_retirada=39_100,
    )


# --- devolver o substituto com o carro principal "na oficina" ---------------


def test_confirmar_volta_do_cliente_fecha_a_oficina_do_principal(
    usuario_logado, alocacao, troca, veiculo
):
    oficina = Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=11),
        data_entrada=HOJE - timedelta(days=11),
        descricao="Para-choque",
    )
    devolucao = HOJE - timedelta(days=1)
    resposta = usuario_logado.post(
        f"/alocacoes/troca/{troca.pk}/devolver/",
        {
            "data_devolucao": devolucao.isoformat(),
            "km_devolucao": "39.600",
            "confirmar_oficina": "1",
        },
        follow=True,
    )
    oficina.refresh_from_db()
    troca.refresh_from_db()
    assert troca.data_devolucao == devolucao
    assert troca.km_devolucao == 39_600
    # os dias parado (auxílio) param de contar na volta do cliente para o carro
    assert oficina.data_saida == devolucao
    assert oficina.dias_parado == 10
    assert f"saiu da oficina em {devolucao:%d/%m/%Y}" in resposta.content.decode()


def test_sem_confirmar_a_oficina_continua_aberta(usuario_logado, troca, veiculo):
    oficina = Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=11),
        data_entrada=HOJE - timedelta(days=11),
        descricao="Para-choque",
    )
    usuario_logado.post(
        f"/alocacoes/troca/{troca.pk}/devolver/",
        {"data_devolucao": HOJE.isoformat(), "km_devolucao": "39600"},
    )
    oficina.refresh_from_db()
    troca.refresh_from_db()
    assert troca.data_devolucao is None
    assert oficina.data_saida is None


def test_tela_de_devolver_explica_que_a_oficina_fecha(usuario_logado, troca, veiculo):
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=3),
        data_entrada=HOJE - timedelta(days=3),
        descricao="Pneu",
    )
    html = usuario_logado.get(f"/alocacoes/troca/{troca.pk}/devolver/").content.decode()
    assert "sai da oficina na data da devolução" in html


# --- KM com ponto de milhar --------------------------------------------------


def test_encerrar_aceita_km_com_ponto_de_milhar(usuario_logado, alocacao):
    usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "52.300", "confirmar_km": "1"},
    )
    alocacao.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ENCERRADA
    assert alocacao.km_devolucao == 52_300


def test_encerrar_com_km_torto_explica_e_nao_encerra(usuario_logado, alocacao):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "52.3"},
    )
    assert resposta.status_code == 200
    assert "só com números" in resposta.content.decode()
    alocacao.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ATIVA


def test_nova_alocacao_le_km_de_entrega_com_milhar(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        {
            "veiculo": veiculo.pk,
            "cliente": cliente.pk,
            "data_inicio": HOJE.isoformat(),
            "valor_semanal": "650,00",
            "dia_vencimento": "",
            "km_entrega": "50.000",
            "limite_km": "ilimitado",
        },
    )
    assert resposta.status_code == 302
    # "50.000" era lido como 50 pelo campo de número padrão
    assert Alocacao.objects.get().km_entrega == 50_000


def test_troca_le_km_de_retirada_com_milhar(usuario_logado, alocacao):
    substituto = Veiculo.objects.create(placa="SUB1A23", marca_modelo="Onix", km_atual=35_000)
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/troca/",
        {
            "veiculo_substituto": substituto.pk,
            "data_retirada": HOJE.isoformat(),
            "km_retirada": "35.000",
        },
    )
    assert resposta.status_code == 302
    assert TrocaTemporaria.objects.get().km_retirada == 35_000


def test_km_do_encerramento_sem_type_number(usuario_logado, alocacao):
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").content.decode()
    assert 'name="km_devolucao"' in html and 'type="number" name="km_devolucao"' not in html


def test_km_da_devolucao_do_substituto_sem_type_number(usuario_logado, troca):
    html = usuario_logado.get(f"/alocacoes/troca/{troca.pk}/devolver/").content.decode()
    assert 'name="km_devolucao"' in html and 'type="number" name="km_devolucao"' not in html


# --- vistoria: resumo de erros no topo ---------------------------------------


def test_vistoria_com_erro_mostra_resumo_no_topo(usuario_logado, alocacao):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {
            "veiculo": alocacao.veiculo_id,
            "tipo": "entrada",
            "data": (HOJE + timedelta(days=2)).isoformat(),
            "km": "50000",
            "token": "t1",
        },
    )
    html = resposta.content.decode()
    assert resposta.status_code == 200
    assert "Não foi possível salvar" in html


def test_vistoria_aceita_km_com_milhar(usuario_logado, alocacao):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {
            "veiculo": alocacao.veiculo_id,
            "tipo": "entrada",
            "data": HOJE.isoformat(),
            "km": "50.000",
            "token": "t2",
        },
    )
    assert resposta.status_code == 302
    assert alocacao.vistorias.get().km == 50_000


# --- entrega guiada volta para a ficha ---------------------------------------


def test_passos_de_caucao_e_receber_levam_voltar_para_a_ficha(alocacao):
    alocacao.caucao_valor = Decimal("1000")
    alocacao.save()
    Caucao.objects.get_or_create(alocacao=alocacao)
    passos = passos_da_entrega(alocacao)
    ficha = f"/clientes/{alocacao.cliente_id}/"
    por_botao = {p["botao"]: p["url"] for p in passos}
    assert "voltar=%2Fclientes%2F" in por_botao["Registrar caução"]
    assert "voltar=%2Fclientes%2F" in por_botao["Receber pagamento"]
    assert ficha.replace("/", "%2F") in por_botao["Receber pagamento"]


# --- linha do tempo, títulos e textos -----------------------------------------


def test_linha_do_tempo_mostra_km_com_milhar(alocacao, veiculo):
    textos = " ".join(e["descricao"] for e in linha_do_tempo(veiculo))
    assert "50.000 km" in textos
    assert "50000 km" not in textos


def test_titulos_com_nome_do_sistema(usuario_logado, alocacao, veiculo):
    html = usuario_logado.get(f"/alocacoes/veiculo/{veiculo.pk}/linha-do-tempo/").content.decode()
    assert "Linha do tempo — QXQ7R29 — Trade Rent a Car</title>" in html
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/editar/").content.decode()
    assert "Editar alocação — QXQ7R29 — Trade Rent a Car</title>" in html


def test_checklist_do_substituto_tem_botao_do_carro_original(usuario_logado, alocacao, troca):
    html = usuario_logado.get(
        f"/alocacoes/{alocacao.pk}/vistoria/imprimir/?veiculo={troca.veiculo_substituto_id}"
    ).content.decode()
    assert "Imprimir checklist do QXQ7R29 (carro original)" in html


def test_encerrar_nao_diz_deve_hoje_quando_so_vence_depois(usuario_logado, alocacao):
    vencimento = HOJE + timedelta(days=4)
    Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.NOTA_DEBITO,
        valor=Decimal("145.00"),
        vencimento=vencimento,
        descricao="ND de multa",
    )
    situacao = situacao_da_devolucao(alocacao)
    assert situacao["debitos_vencidos"] == 0
    assert situacao["proximo_vencimento"] == vencimento
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").content.decode()
    assert "deve R$" not in html
    assert f"em aberto (vence {vencimento:%d/%m})" in html
