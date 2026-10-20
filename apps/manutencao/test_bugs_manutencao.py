"""Correções da revisão de bugs — frente manutenção (C03, C04-repasse, C05, C24, D08, D18)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import Cobranca
from apps.financeiro.services import proximo_dia_pagamento
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.manutencao.repasse import gerar_repasse
from apps.manutencao.services import (
    StatusPreventiva,
    preventivas_em_alerta,
    preventivas_sem_registro,
    resumo_preventivas,
)
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro

HOJE = date.today()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="BUG1M23", marca_modelo="Onix", km_atual=100_000)


@pytest.fixture
def cliente_a(db):
    return Cliente.objects.create(nome="Ana Bateu", cpf_cnpj="111.222.333-44")


@pytest.fixture
def cliente_b(db):
    return Cliente.objects.create(nome="Bruno Depois", cpf_cnpj="222.333.444-55")


@pytest.fixture
def oleo(db):
    item, _ = ItemPreventiva.objects.get_or_create(
        nome="Troca de óleo e filtro", defaults={"intervalo_km_padrao": 10_000}
    )
    ItemPreventiva.objects.exclude(pk=item.pk).update(ativo=False)  # isola o plano
    return item


def _alocacao_encerrada(veiculo, cliente, inicio, fim):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=inicio,
        data_termino=fim,
        status=Alocacao.Status.ENCERRADA,
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )


def _alocacao_ativa(veiculo, cliente, inicio):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=inicio,
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )


def _conserto(veiculo, **extra):
    dados = {
        "veiculo": veiculo,
        "tipo": Manutencao.Tipo.CORRETIVA,
        "data": HOJE,
        "descricao": "Funilaria",
        "responsavel": Manutencao.Responsavel.CLIENTE,
        "valor_cobrado_cliente": Decimal("2000.00"),
    }
    dados.update(extra)
    return Manutencao.objects.create(**dados)


# ---------- C03: repasse cobra quem causou o dano ----------


def test_repasse_com_sinistro_cobra_o_cliente_do_sinistro_apos_devolucao(veiculo, cliente_a):
    _alocacao_encerrada(veiculo, cliente_a, HOJE - timedelta(days=30), HOJE - timedelta(days=18))
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=19), envolvido="terceiro"
    )
    assert sinistro.cliente == cliente_a
    manutencao = _conserto(veiculo, sinistro=sinistro)  # conserto hoje: ninguém com o carro
    cobranca = gerar_repasse(manutencao)
    assert cobranca.cliente == cliente_a


def test_repasse_com_sinistro_nao_cobra_o_motorista_seguinte(veiculo, cliente_a, cliente_b):
    _alocacao_encerrada(veiculo, cliente_a, HOJE - timedelta(days=30), HOJE - timedelta(days=16))
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=20), envolvido="terceiro"
    )
    _alocacao_ativa(veiculo, cliente_b, HOJE - timedelta(days=15))
    manutencao = _conserto(veiculo, sinistro=sinistro)
    assert gerar_repasse(manutencao).cliente == cliente_a


def test_repasse_sem_sinistro_usa_a_data_de_entrada_na_oficina(veiculo, cliente_a, cliente_b):
    _alocacao_encerrada(veiculo, cliente_a, HOJE - timedelta(days=30), HOJE - timedelta(days=16))
    _alocacao_ativa(veiculo, cliente_b, HOJE - timedelta(days=15))
    manutencao = _conserto(
        veiculo, data_entrada=HOJE - timedelta(days=20), data_saida=HOJE - timedelta(days=1)
    )
    assert gerar_repasse(manutencao).cliente == cliente_a


def test_historico_mostra_o_cliente_antes_de_confirmar(usuario_logado, veiculo, cliente_a):
    _alocacao_encerrada(veiculo, cliente_a, HOJE - timedelta(days=30), HOJE - timedelta(days=18))
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=19), envolvido="terceiro"
    )
    _conserto(veiculo, sinistro=sinistro)
    html = usuario_logado.get(f"/manutencao/historico/{veiculo.pk}/").content.decode()
    assert "Ana Bateu" in html
    assert "Cobrar do cliente" in html


# ---------- C04 (repasse): vence no próximo dia de pagamento ----------


def test_repasse_vence_no_proximo_dia_de_pagamento(veiculo, cliente_a):
    _alocacao_ativa(veiculo, cliente_a, HOJE - timedelta(days=10))
    manutencao = _conserto(veiculo)
    cobranca = gerar_repasse(manutencao)
    assert cobranca.vencimento > HOJE
    assert cobranca.vencimento == proximo_dia_pagamento(cliente_a, HOJE)


def test_repasse_respeita_vencimento_informado(veiculo, cliente_a):
    _alocacao_ativa(veiculo, cliente_a, HOJE - timedelta(days=10))
    vencimento = HOJE + timedelta(days=3)
    assert gerar_repasse(_conserto(veiculo), vencimento=vencimento).vencimento == vencimento


# ---------- C05: previsão desconta os dias desde a última leitura ----------


def _leitura(veiculo, data_leitura, km):
    registro = RegistroKm(
        veiculo=veiculo,
        mes_referencia=data_leitura.replace(day=1),
        data_leitura=data_leitura,
        km=km,
    )
    registro.save()
    return registro


def test_previsao_projeta_o_km_desde_a_ultima_leitura(oleo, db):
    veiculo = Veiculo.objects.create(placa="RUN3D00", marca_modelo="Gol", km_compra=40_000)
    Manutencao.objects.create(
        veiculo=veiculo, tipo="preventiva", item=oleo, data=date(2026, 8, 1), km=50_000
    )
    _leitura(veiculo, date(2026, 9, 1), 43_000)
    _leitura(veiculo, date(2026, 10, 1), 52_000)  # 9.000 km em 30 dias = 300 km/dia
    veiculo.refresh_from_db()
    (p,) = resumo_preventivas(veiculo, hoje=date(2026, 10, 1))
    assert p.dias_restantes == 27  # no dia da leitura: 8.000 km ÷ 300
    assert p.status == StatusPreventiva.OK
    (p,) = resumo_preventivas(veiculo, hoje=date(2026, 10, 20))
    assert p.faltam_km == 8_000  # KM confirmado não muda
    assert p.km_projetado == 57_700
    assert p.dias_restantes == 8  # 19 dias depois, faltam ~2.300 km
    assert p.status == StatusPreventiva.PROXIMA


def test_projecao_passou_do_ponto_fica_proxima_com_zero_dias(oleo, db):
    veiculo = Veiculo.objects.create(placa="RUN3D01", marca_modelo="Gol", km_compra=40_000)
    Manutencao.objects.create(
        veiculo=veiculo, tipo="preventiva", item=oleo, data=date(2026, 8, 1), km=50_000
    )
    _leitura(veiculo, date(2026, 9, 1), 43_000)
    _leitura(veiculo, date(2026, 10, 1), 52_000)
    veiculo.refresh_from_db()
    (p,) = resumo_preventivas(veiculo, hoje=date(2026, 11, 5))
    assert p.dias_restantes == 0
    assert p.status == StatusPreventiva.PROXIMA  # vencida só com o KM confirmado


# ---------- D18: item nunca registrado conta do KM da compra ----------


def test_sem_registro_conta_o_primeiro_ciclo_do_km_da_compra(oleo, db):
    veiculo = Veiculo.objects.create(
        placa="USA1D00", marca_modelo="Gol", km_compra=47_000, km_atual=70_000
    )
    (p,) = resumo_preventivas(veiculo)
    assert p.sem_registro
    assert p.km_proximo == 57_000
    assert p.faltam_km == -13_000
    assert p.status == StatusPreventiva.VENCIDA
    assert [v for v, _ in preventivas_em_alerta()] == [veiculo]


def test_sem_registro_dentro_do_ciclo_continua_sem_registro(oleo, db):
    veiculo = Veiculo.objects.create(
        placa="USA1D01", marca_modelo="Gol", km_compra=47_000, km_atual=48_000
    )
    (p,) = resumo_preventivas(veiculo)
    assert p.faltam_km == 9_000
    assert p.status == StatusPreventiva.SEM_REGISTRO  # não fica "Em dia" sem troca confirmada
    assert [v for v, _ in preventivas_sem_registro()] == [veiculo]


def test_sem_registro_e_sem_km_da_compra_nao_calcula(oleo, db):
    veiculo = Veiculo.objects.create(placa="USA1D02", marca_modelo="Gol", km_atual=80_000)
    (p,) = resumo_preventivas(veiculo)
    assert p.faltam_km is None
    assert p.status == StatusPreventiva.SEM_REGISTRO


def test_tela_preventivas_sem_registro_vencida_pede_a_ultima_troca(usuario_logado, oleo, db):
    Veiculo.objects.create(placa="USA1D03", marca_modelo="Gol", km_compra=47_000, km_atual=70_000)
    html = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert "USA1D03" in html
    assert "Informar última troca" in html
    assert "ultima=1" in html


def test_texto_itens_em_dia_sem_erro_de_plural(usuario_logado, oleo, db):
    for placa in ("PLU1R00", "PLU1R01"):
        v = Veiculo.objects.create(placa=placa, marca_modelo="Gol", km_atual=52_000)
        Manutencao.objects.create(
            veiculo=v, tipo="preventiva", item=oleo, data=HOJE, km=50_000, descricao="óleo"
        )
    html = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert "itemns" not in html
    assert "2 itens em dia" in html


# ---------- C24: manutenção sem KM no histórico ----------


def test_historico_sem_km_mostra_traco(usuario_logado, veiculo):
    Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=HOJE, descricao="Funilaria sem km"
    )
    html = usuario_logado.get(f"/manutencao/historico/{veiculo.pk}/").content.decode()
    assert "None" not in html


# ---------- D08: carro livre na oficina fica "Em manutenção" ----------


def _dados(**extra):
    dados = {
        "tipo": "corretiva",
        "data": HOJE.isoformat(),
        "km": "100000",
        "descricao": "Troca da embreagem",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
    }
    dados.update(extra)
    return dados


def test_carro_livre_entra_na_oficina_e_vira_em_manutencao(usuario_logado, veiculo):
    usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(data_entrada=HOJE.isoformat())
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO
    manutencao = veiculo.manutencoes.get()
    usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/",
        {"data_saida": HOJE.isoformat(), "custo_real": "100,00", "pagamento_custo": "pago"},
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL


def test_carro_alugado_na_oficina_continua_alocado(usuario_logado, veiculo, cliente_a):
    _alocacao_ativa(veiculo, cliente_a, HOJE - timedelta(days=5))
    veiculo.refresh_from_db()
    usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(data_entrada=HOJE.isoformat())
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.ALOCADO


def test_manutencao_ja_encerrada_nao_muda_status(usuario_logado, veiculo):
    usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        _dados(data_entrada=HOJE.isoformat(), data_saida=HOJE.isoformat()),
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL


def test_editar_adicionando_entrada_manda_para_a_oficina(usuario_logado, veiculo):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=HOJE, km=100_000, descricao="Embreagem"
    )
    usuario_logado.post(
        f"/manutencao/editar/{manutencao.pk}/", _dados(data_entrada=HOJE.isoformat())
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO


# ---------- revisão adversarial ----------


def test_repasse_com_objeto_desatualizado_nao_cobra_duas_vezes(veiculo, cliente_a):
    """Dois cliques em "Cobrar do cliente": a 2ª tentativa lê o banco, não a cópia antiga."""
    _alocacao_ativa(veiculo, cliente_a, HOJE - timedelta(days=5))
    manutencao = _conserto(veiculo)
    copia_antiga = Manutencao.objects.get(pk=manutencao.pk)
    gerar_repasse(manutencao)
    with pytest.raises(ValidationError):
        gerar_repasse(copia_antiga)
    assert Cobranca.objects.filter(origem=Cobranca.Origem.REPASSE_MANUTENCAO).count() == 1


def test_repasse_de_sinistro_sem_cliente_usa_quem_estava_na_entrada(veiculo, cliente_a, cliente_b):
    _alocacao_encerrada(veiculo, cliente_a, HOJE - timedelta(days=30), HOJE - timedelta(days=11))
    _alocacao_ativa(veiculo, cliente_b, HOJE - timedelta(days=10))
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=40), envolvido="terceiro"
    )
    assert sinistro.cliente is None  # ninguém com o carro no dia do sinistro
    manutencao = _conserto(veiculo, sinistro=sinistro, data_entrada=HOJE - timedelta(days=15))
    assert gerar_repasse(manutencao).cliente == cliente_a


def test_repasse_vence_depois_de_hoje_mesmo_para_ex_cliente(veiculo, cliente_a):
    """Ex-cliente sem alocação ativa: vence no dia dele (ou em 7 dias), nunca hoje."""
    _alocacao_encerrada(veiculo, cliente_a, HOJE - timedelta(days=30), HOJE - timedelta(days=18))
    sinistro = Sinistro.objects.create(
        veiculo=veiculo, data=HOJE - timedelta(days=19), envolvido="terceiro"
    )
    cobranca = gerar_repasse(_conserto(veiculo, sinistro=sinistro))
    assert cobranca.vencimento > HOJE


def test_apagar_entrada_por_engano_devolve_o_carro_a_disponivel(usuario_logado, veiculo):
    usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(data_entrada=HOJE.isoformat())
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO
    manutencao = veiculo.manutencoes.get()
    usuario_logado.post(f"/manutencao/editar/{manutencao.pk}/", _dados())  # sem entrada
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL


def test_carro_com_duas_idas_abertas_so_volta_quando_a_ultima_fecha(usuario_logado, veiculo):
    for _ in range(2):
        usuario_logado.post(
            f"/manutencao/registrar/{veiculo.pk}/", _dados(data_entrada=HOJE.isoformat())
        )
    primeira, segunda = veiculo.manutencoes.order_by("pk")
    saida = {"data_saida": HOJE.isoformat(), "custo_real": "100,00", "pagamento_custo": "pago"}
    usuario_logado.post(f"/manutencao/voltou/{primeira.pk}/", saida)
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO
    usuario_logado.post(f"/manutencao/voltou/{segunda.pk}/", saida)
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL


def test_carro_em_manutencao_nao_aparece_para_nova_alocacao(usuario_logado, veiculo):
    usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(data_entrada=HOJE.isoformat())
    )
    resposta = usuario_logado.get("/alocacoes/nova/")
    assert veiculo not in resposta.context["form"].fields["veiculo"].queryset


def test_vencida_pelo_km_da_compra_entra_no_painel(usuario_logado, oleo, db):
    Veiculo.objects.create(placa="CMP9R00", marca_modelo="HB20", km_compra=47_000, km_atual=70_000)
    html = usuario_logado.get("/").content.decode()
    assert "CMP9R00" in html
