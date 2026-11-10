"""Etapa 18c — sobras do financeiro deixadas pela etapa 18.

1. O painel recusa cancelar a franquia de sinistro apontando o caminho certo
   (botão "Cancelar esta cobrança" no sinistro); o conserto cobrado do cliente
   (repasse de manutenção) se cancela no painel — a manutenção manda "cancelar
   a cobrança no Financeiro" antes de apagar e não tem botão próprio.
2. A rotina diária (marcar_atrasos) não desfaz um pagamento gravado enquanto
   ela roda: relê a cobrança travada antes de gravar o status.
3. Retaguarda: valores em R$ ("R$ 1.234,50") nas listas do financeiro.
4. Um termo só nas telas do financeiro: "Caução retida" e "Crédito do cliente".
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services
from apps.financeiro.models import Cobranca, MovimentoCredito
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Célia Sobras", cpf_cnpj="222.333.444-55")


@pytest.fixture
def alocacao(cliente):
    veiculo = Veiculo.objects.create(placa="SBR1A18", marca_modelo="Onix", km_atual=40_000)
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=20),
        valor_semanal=Decimal("650.00"),
        km_entrega=40_000,
        caucao_valor=Decimal("1000.00"),
    )


def _cobranca(cliente, origem, valor="1234.50", vencimento=None):
    return Cobranca.objects.create(
        cliente=cliente,
        origem=origem,
        descricao=f"Cobrança {origem}",
        valor=Decimal(valor),
        vencimento=vencimento or HOJE + timedelta(days=3),
    )


def _cancelar(cliente_http, cobranca):
    return cliente_http.post(f"/financeiro/cobranca/{cobranca.pk}/cancelar/", follow=True)


# ---------- 1. cancelar no painel: o caminho certo ----------


def test_franquia_de_sinistro_recusada_aponta_o_botao_do_sinistro(usuario_logado, cliente):
    franquia = _cobranca(cliente, Cobranca.Origem.REPASSE_SINISTRO)
    html = _cancelar(usuario_logado, franquia).content.decode()
    assert "Cancelar esta cobrança" in html
    assert "menu Sinistros" in html
    franquia.refresh_from_db()
    assert franquia.status == Cobranca.Status.PENDENTE


def test_aluguel_continua_sem_cancelar_pelo_painel(usuario_logado, cliente):
    aluguel = _cobranca(cliente, Cobranca.Origem.ALUGUEL)
    html = _cancelar(usuario_logado, aluguel).content.decode()
    assert "podem ser canceladas" in html
    aluguel.refresh_from_db()
    assert aluguel.status == Cobranca.Status.PENDENTE


def test_conserto_cobrado_tem_botao_no_painel(usuario_logado, cliente):
    conserto = _cobranca(cliente, Cobranca.Origem.REPASSE_MANUTENCAO)
    html = usuario_logado.get("/financeiro/?status=todas").content.decode()
    assert f"/financeiro/cobranca/{conserto.pk}/cancelar/" in html


def test_conserto_cancelado_no_painel_libera_apagar_a_manutencao(usuario_logado, cliente, alocacao):
    conserto = _cobranca(cliente, Cobranca.Origem.REPASSE_MANUTENCAO)
    manutencao = Manutencao.objects.create(
        veiculo=alocacao.veiculo,
        tipo="corretiva",
        data=HOJE - timedelta(days=2),
        descricao="Para-choque",
        cobranca_repasse=conserto,
    )
    # antes de cancelar, a manutenção manda cancelar no Financeiro
    usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/")
    assert Manutencao.objects.filter(pk=manutencao.pk).exists()

    html = _cancelar(usuario_logado, conserto).content.decode()
    assert "Cancelada (não será cobrada)" in html
    assert "apague a manutenção" in html
    conserto.refresh_from_db()
    assert conserto.status == Cobranca.Status.CANCELADA

    usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/")
    assert not Manutencao.objects.filter(pk=manutencao.pk).exists()


def test_conserto_com_pagamento_nao_se_cancela(usuario_logado, cliente):
    conserto = _cobranca(cliente, Cobranca.Origem.REPASSE_MANUTENCAO, valor="300.00")
    services.registrar_recebimento(
        cliente, HOJE, Decimal("100"), "pix", [(conserto, Decimal("100"))]
    )
    html = _cancelar(usuario_logado, conserto).content.decode()
    assert "com pagamento aplicado não pode ser cancelada" in html
    conserto.refresh_from_db()
    assert conserto.status == Cobranca.Status.PARCIAL


# ---------- 2. rotina diária x pagamento ao mesmo tempo ----------


def test_marcar_atrasos_nao_desfaz_pagamento_gravado_enquanto_roda(cliente, monkeypatch):
    vencida = _cobranca(cliente, Cobranca.Origem.ALUGUEL, "650.00", HOJE - timedelta(days=3))
    original = Cobranca.com_quitacao_anotada

    def lista_velha_e_paga_no_meio(queryset):
        # a rotina lê a lista (sem pagamento) e, logo depois, o Receber quita a cobrança
        velha = list(original(queryset))
        monkeypatch.setattr(Cobranca, "com_quitacao_anotada", original)
        services.registrar_recebimento(
            cliente,
            HOJE,
            Decimal("650"),
            "pix",
            [(Cobranca.objects.get(pk=vencida.pk), Decimal("650"))],
        )
        return velha

    monkeypatch.setattr(Cobranca, "com_quitacao_anotada", lista_velha_e_paga_no_meio)
    services.marcar_atrasos(HOJE)

    vencida.refresh_from_db()
    assert vencida.status == Cobranca.Status.PAGO
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.ATIVO


@pytest.mark.parametrize("mudanca", [Cobranca.Status.CANCELADA, Cobranca.Status.JUDICIAL])
def test_marcar_atrasos_nao_desfaz_cancelar_ou_judicial_feito_enquanto_roda(
    cliente, monkeypatch, mudanca
):
    # sem reler a cobrança, o status velho (Pendente) da lista virava Atrasado
    # por cima do Cancelada/Judicial gravado no painel nesse meio tempo
    vencida = _cobranca(cliente, Cobranca.Origem.OUTRO, "650.00", HOJE - timedelta(days=3))
    original = Cobranca.com_quitacao_anotada

    def lista_velha_e_muda_no_meio(queryset):
        velha = list(original(queryset))
        Cobranca.objects.filter(pk=vencida.pk).update(status=mudanca)
        return velha

    monkeypatch.setattr(Cobranca, "com_quitacao_anotada", lista_velha_e_muda_no_meio)
    services.marcar_atrasos(HOJE)

    vencida.refresh_from_db()
    assert vencida.status == mudanca


def test_marcar_atrasos_continua_marcando_o_atraso(cliente):
    vencida = _cobranca(cliente, Cobranca.Origem.ALUGUEL, "650.00", HOJE - timedelta(days=3))
    services.marcar_atrasos(HOJE)
    vencida.refresh_from_db()
    assert vencida.status == Cobranca.Status.ATRASADO
    cliente.refresh_from_db()
    assert cliente.status == Cliente.Status.INADIMPLENTE


# ---------- 3. Retaguarda em R$ ----------


@pytest.mark.parametrize(
    "lista", ["cobranca", "recebimento", "notadebito", "caucao", "movimentocredito"]
)
def test_retaguarda_mostra_valores_em_reais(admin_client, cliente, alocacao, lista):
    cobranca = _cobranca(cliente, Cobranca.Origem.OUTRO, "1234.50")
    services.registrar_recebimento(
        cliente, HOJE, Decimal("1234.50"), "pix", [(cobranca, Decimal("1234.50"))]
    )
    services.emitir_nota_debito(cliente, HOJE, [("Avaria", Decimal("1234.50"))])
    services.abrir_caucao(alocacao, Decimal("1234.50"), HOJE, "pix")
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("1234.50"), data=HOJE
    )
    html = admin_client.get(f"/admin/financeiro/{lista}/").content.decode()
    assert "R$ 1.234,50" in html
    assert "1234.50" not in html.replace('value="1234.50"', "")


# ---------- 4. um termo só ----------


def test_extrato_chama_de_caucao_retida_e_credito_do_cliente(usuario_logado, cliente, alocacao):
    services.abrir_caucao(alocacao, Decimal("1000"), HOJE, "pix")
    html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert "Caução retida" in html
    assert "Crédito do cliente" in html
    assert "Caução guardada" not in html


def test_receber_chama_de_credito_do_cliente(usuario_logado, cliente):
    _cobranca(cliente, Cobranca.Origem.ALUGUEL, "650.00")
    MovimentoCredito.objects.create(
        cliente=cliente, tipo=MovimentoCredito.Tipo.ENTRADA, valor=Decimal("50.00"), data=HOJE
    )
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    assert "Crédito do cliente: <strong>R$ 50,00</strong>" in html
    assert "crédito guardado" not in html.lower()


def test_telas_do_financeiro_sem_os_termos_antigos():
    pasta = Path(settings.BASE_DIR) / "templates" / "financeiro"
    for arquivo in pasta.glob("*.html"):
        texto = arquivo.read_text(encoding="utf-8").lower()
        for antigo in (
            "crédito guardado",
            "caução guardada",
            "saldo guardado",
            "dinheiro guardado",
        ):
            assert antigo not in texto, f"{arquivo.name}: “{antigo}”"
