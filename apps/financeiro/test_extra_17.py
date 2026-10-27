"""Achados extras da etapa 17 no financeiro: cada correção tem um teste aqui."""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.alocacoes.models import Alocacao, Vistoria
from apps.campos import ler_data
from apps.financeiro import services, whatsapp
from apps.financeiro.models import Caucao, Cobranca, MovimentacaoCaucao, NotaDebito
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Rodrigo Fluxo", cpf_cnpj="111.222.333-44", telefone="31987654321"
    )


@pytest.fixture
def alocacao(cliente):
    veiculo = Veiculo.objects.create(placa="RTB2C03", marca_modelo="Gol")
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=30),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
        caucao_valor=Decimal("1200.00"),
    )


def _mov(caucao, tipo, valor):
    return MovimentacaoCaucao.objects.create(
        caucao=caucao, tipo=tipo, valor=Decimal(valor), data=HOJE - timedelta(days=1)
    )


def _cobranca(cliente, alocacao=None, vencimento=None, valor="650.00", origem=None, **extra):
    cobranca = Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=origem or Cobranca.Origem.ALUGUEL,
        descricao=extra.pop("descricao", "Aluguel semanal RTB2C03"),
        valor=Decimal(valor),
        vencimento=vencimento or HOJE,
        **extra,
    )
    cobranca.atualizar_status()
    return cobranca


# ---------- caução devolvida não sugere depósito ----------


def test_caucao_devolvida_nao_mostra_falta_nem_preenche_deposito(usuario_logado, alocacao):
    caucao = Caucao.objects.get(alocacao=alocacao)
    _mov(caucao, "recebimento", "1000.00")
    _mov(caucao, "reforco", "200.00")
    Alocacao.objects.filter(pk=alocacao.pk).update(
        status=Alocacao.Status.ENCERRADA, data_termino=HOJE
    )
    _mov(caucao, "devolucao", "1200.00")
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert "falta R$" not in html
    assert "Falta depositar" not in html
    assert 'value="1.200,00"' not in html
    assert "acertada ✓" in html


def test_caucao_ativa_falta_e_combinado_menos_depositado(usuario_logado, alocacao):
    caucao = Caucao.objects.get(alocacao=alocacao)
    _mov(caucao, "recebimento", "1200.00")
    _cobranca(alocacao.cliente, alocacao, vencimento=HOJE - timedelta(days=3))
    cobranca = Cobranca.objects.get()
    services.descontar_da_caucao(caucao, cobranca, Decimal("650.00"), HOJE)
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    # o desconto não faz a tela pedir um novo depósito de R$ 650
    assert "falta R$" not in html
    assert "completa ✓" in html


def test_aviso_acima_do_possivel_usa_ponto_de_milhar(usuario_logado, alocacao):
    caucao = Caucao.objects.get(alocacao=alocacao)
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert 'toLocaleString("pt-BR"' in html
    assert 'toFixed(2).replace(".", ",")' not in html


# ---------- desconto de avaria sem cobrança: caminho para a ND ----------


def test_caucao_sem_cobranca_em_aberto_leva_a_emitir_nd(usuario_logado, alocacao):
    caucao = Caucao.objects.get(alocacao=alocacao)
    _mov(caucao, "recebimento", "1200.00")
    html = usuario_logado.get(f"/financeiro/caucoes/{caucao.pk}/").content.decode()
    assert "data-desconto-sem-cobranca" in html
    assert (
        f"/financeiro/nd/nova/?cliente={alocacao.cliente.pk}&voltar=/financeiro/caucoes/"
        f"{caucao.pk}/" in html
    )
    assert "semCobranca" in html


def test_encerrar_avisa_marca_nova_na_devolucao(usuario_logado, alocacao):
    Vistoria.objects.create(
        alocacao=alocacao,
        veiculo=alocacao.veiculo,
        tipo="entrada",
        data=alocacao.data_inicio,
        km=0,
        avarias="risco para-choque",
    )
    Vistoria.objects.create(
        alocacao=alocacao,
        veiculo=alocacao.veiculo,
        tipo="saida",
        data=HOJE,
        km=1000,
        avarias="risco para-choque; amassado porta direita",
    )
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").content.decode()
    assert "Marca nova na devolução" in html
    assert f"/financeiro/nd/nova/?cliente={alocacao.cliente.pk}&voltar=" in html


def test_encerrar_sem_marca_nova_nao_avisa(usuario_logado, alocacao):
    for tipo in ("entrada", "saida"):
        Vistoria.objects.create(
            alocacao=alocacao,
            veiculo=alocacao.veiculo,
            tipo=tipo,
            data=HOJE,
            km=0,
            avarias="Risco para-choque",
        )
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").content.decode()
    assert "Marca nova na devolução" not in html


# ---------- ND: ?cliente= e volta para o acerto ----------


def test_nd_nova_aceita_cliente_na_url(usuario_logado, cliente):
    html = usuario_logado.get(f"/financeiro/nd/nova/?cliente={cliente.pk}").content.decode()
    assert f'<option value="{cliente.pk}" selected>' in html
    assert f'cliente: "{cliente.pk}"' in html


def test_nd_nova_ignora_cliente_invalido(usuario_logado, cliente):
    resposta = usuario_logado.get("/financeiro/nd/nova/?cliente=999999")
    assert resposta.status_code == 200
    assert " selected>" not in resposta.content.decode().split('id="nd-cliente"')[1][:200]


def test_nd_emitida_volta_para_a_caucao(usuario_logado, alocacao):
    caucao = Caucao.objects.get(alocacao=alocacao)
    voltar = f"/financeiro/caucoes/{caucao.pk}/"
    html = usuario_logado.get(
        f"/financeiro/nd/nova/?cliente={alocacao.cliente.pk}&voltar={voltar}"
    ).content.decode()
    assert "Voltar ao acerto da caução" in html
    resposta = usuario_logado.post(
        "/financeiro/nd/nova/",
        {
            "token": "t1",
            "cliente": alocacao.cliente.pk,
            "data_emissao": HOJE.isoformat(),
            "item_descricao": ["Avaria porta direita"],
            "item_valor": ["300,00"],
            "voltar": voltar,
        },
    )
    assert NotaDebito.objects.count() == 1
    assert resposta.status_code == 302 and resposta.url == voltar


def test_nd_nao_volta_para_outro_site(usuario_logado, cliente):
    resposta = usuario_logado.post(
        "/financeiro/nd/nova/",
        {
            "token": "t2",
            "cliente": cliente.pk,
            "data_emissao": HOJE.isoformat(),
            "item_descricao": ["Avaria"],
            "item_valor": ["300,00"],
            "voltar": "//malicioso.example/",
        },
    )
    assert resposta.url == "/financeiro/nd/"


def test_usar_credito_pela_caucao_mostra_volta(usuario_logado, alocacao):
    cliente = alocacao.cliente
    caucao = Caucao.objects.get(alocacao=alocacao)
    semana = _cobranca(cliente, alocacao, vencimento=HOJE - timedelta(days=3))
    voltar = f"/financeiro/caucoes/{caucao.pk}/"
    resposta = usuario_logado.post(
        f"/financeiro/receber/?cliente={cliente.pk}&usar_credito=1&voltar={voltar}",
        {
            "cliente": cliente.pk,
            "data": HOJE.isoformat(),
            "forma": "pix",
            "valor": "650,00",
            f"aplicar_{semana.pk}": "650,00",
        },
    )
    assert resposta.status_code == 302
    assert "voltar=%2Ffinanceiro%2Fcaucoes" in resposta.url
    html = usuario_logado.get(resposta.url).content.decode()
    assert "Voltar ao acerto da caução" in html


# ---------- Receber depois de lançar ----------


def test_receber_com_recibo_esconde_formulario_e_aviso_repetido(usuario_logado, cliente):
    semana = _cobranca(cliente, vencimento=HOJE - timedelta(days=3))
    resposta = usuario_logado.post(
        f"/financeiro/receber/?cliente={cliente.pk}",
        {
            "cliente": cliente.pk,
            "data": HOJE.isoformat(),
            "forma": "pix",
            "valor": "650,00",
            f"aplicar_{semana.pk}": "650,00",
        },
    )
    resposta = usuario_logado.get(resposta.url)
    html = resposta.content.decode()
    assert "Pagamento lançado" in html
    assert "Confirmar recebimento" not in html
    assert "Lançar outro pagamento" in html
    # o aviso continua no contexto, mas não aparece duas vezes na tela
    assert any("lançado para" in str(m) for m in resposta.context["messages"])
    assert "Recebimento de R$ 650,00 lançado" not in html


def test_receber_vence_hoje_nao_e_vencido(usuario_logado, cliente):
    _cobranca(cliente, vencimento=HOJE)
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    assert "Para pagar até hoje:" in html
    assert "Vencido:" not in html


def test_receber_atrasada_continua_vencido(usuario_logado, cliente):
    _cobranca(cliente, vencimento=HOJE - timedelta(days=2))
    html = usuario_logado.get(f"/financeiro/receber/?cliente={cliente.pk}").content.decode()
    assert "Vencido:" in html


# ---------- textos ----------


def test_nd_de_multa_sem_codigo_sem_espaco_duplo(cliente):
    from apps.multas.models import Multa
    from apps.multas.services import gerar_nd_de_multas

    veiculo = Veiculo.objects.create(placa="RTX1A11", marca_modelo="Gol")
    multa = Multa.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_infracao=date(2026, 5, 1),
        descricao="MS-TESTE futuro",
        valor=Decimal("100.00"),
    )
    assert multa.repasse == "A cobrar"
    nd = gerar_nd_de_multas(cliente, [multa], HOJE)
    assert nd.itens.get().descricao == "Multa MS-TESTE futuro (01/05/2026)"


def test_whatsapp_franquia_do_sinistro_sem_repeticao(cliente):
    cobranca = _cobranca(
        cliente,
        vencimento=HOJE + timedelta(days=2),
        valor="1200.00",
        origem=Cobranca.Origem.REPASSE_SINISTRO,
        descricao="Franquia do sinistro RTD4E07 de 15/10/2026",
    )
    texto = whatsapp.mensagem_cobranca(cobranca, HOJE)
    assert "repasse de sinistro" not in texto
    assert "A franquia do sinistro RTD4E07 de 15/10/2026 vence em" in texto


def test_quem_deve_mostra_extrato_mesmo_com_whatsapp(usuario_logado, cliente):
    _cobranca(cliente, vencimento=HOJE - timedelta(days=3))
    html = usuario_logado.get("/financeiro/quem-deve/").content.decode()
    assert "💬 WhatsApp" in html
    assert "data-ver-extrato" in html


def test_extrato_usa_ano_com_4_digitos(usuario_logado, cliente):
    _cobranca(cliente, vencimento=date(2026, 11, 3))
    html = usuario_logado.get(f"/financeiro/cliente/{cliente.pk}/").content.decode()
    assert "03/11/2026" in html
    assert "03/11/26<" not in html


@pytest.mark.parametrize(
    "url,titulo",
    [
        ("/financeiro/quem-deve/", "Financeiro — Quem deve"),
        ("/financeiro/receber/", "Financeiro — Receber"),
        ("/financeiro/nd/", "Financeiro — Notas de débito"),
        ("/financeiro/", "Financeiro — Cobranças"),
    ],
)
def test_cada_tela_do_financeiro_tem_seu_titulo(usuario_logado, url, titulo):
    assert titulo in usuario_logado.get(url).content.decode()


# ---------- datas digitadas à mão ----------


@pytest.mark.parametrize("texto", ["27-10-2026", "27.10.2026", "27/10/2026", "2026-10-27"])
def test_ler_data_aceita_formatos_comuns(texto):
    assert ler_data(texto) == date(2026, 10, 27)


def test_ler_data_sem_ano_usa_o_ano_corrente():
    assert ler_data("27/10") == date(HOJE.year, 10, 27)


def test_ler_data_invalida_da_exemplo():
    with pytest.raises(ValidationError, match="ex.: 27/10/2026"):
        ler_data("amanhã", "Data de vencimento")


# ---------- rotina diária: uma alocação ruim não barra as outras ----------


def test_rotina_segue_nas_outras_alocacoes(db):
    for numero in range(2):
        cliente = Cliente.objects.create(nome=f"C{numero}", cpf_cnpj=f"111.222.333-4{numero}")
        veiculo = Veiculo.objects.create(placa=f"RTA0B0{numero}", marca_modelo="Gol")
        Alocacao.objects.create(
            veiculo=veiculo,
            cliente=cliente,
            data_inicio=HOJE - timedelta(days=2),
            valor_semanal=Decimal("650.00"),
            km_entrega=0,
        )
    Cobranca.objects.all().delete()
    original = services._valor_do_periodo
    chamadas = []

    def falha_na_primeira(*args, **kwargs):
        chamadas.append(1)
        if len(chamadas) == 1:
            raise RuntimeError("contrato com dado ruim")
        return original(*args, **kwargs)

    with (
        mock.patch.object(services, "_valor_do_periodo", side_effect=falha_na_primeira),
        pytest.raises(CommandError),
    ):
        call_command("rotina_diaria")
    assert Cobranca.objects.filter(origem=Cobranca.Origem.ALUGUEL).count() == 1


def test_gerar_so_da_alocacao_nova(db, alocacao):
    outro = Cliente.objects.create(nome="Outro", cpf_cnpj="999.222.333-44")
    outra = Alocacao.objects.create(
        veiculo=Veiculo.objects.create(placa="RTZ9Z99", marca_modelo="Gol"),
        cliente=outro,
        data_inicio=HOJE - timedelta(days=2),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    Cobranca.objects.all().delete()
    services.gerar_cobrancas_semanais(alocacao=outra)
    assert set(Cobranca.objects.values_list("alocacao_id", flat=True)) == {outra.pk}
