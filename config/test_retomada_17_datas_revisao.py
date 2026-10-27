"""Revisão da frente de datas (etapa 17).

As leituras de data fora do formulário Django usam as mesmas regras do
FormularioBR (AAAA-MM-DD, DD/MM/AAAA, DD/MM/AA; ano 2000..hoje+10).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.alocacoes.views import _data_do_post
from apps.campos import ler_data
from apps.financeiro.models import NotaDebito
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.multas.views import _vencimento_nd
from apps.pessoas.models import Cliente
from config.test_qualidade_final import _ler


def test_ler_data_recusa_ano_longe_demais_no_futuro():
    longe = date(timezone.localdate().year + 11, 1, 1)
    with pytest.raises(ValidationError) as erro:
        ler_data(longe.isoformat())
    assert erro.value.messages == ["Confira o ano (ex.: 2026)."]


def test_data_do_post_da_alocacao_entende_dd_mm_aa():
    # sem isto, "01/07/26" virava None e a confirmação de semanas atrasadas (D01) sumia
    assert _data_do_post({"data_inicio": "01/07/26"}, "data_inicio") == date(2026, 7, 1)
    assert _data_do_post({"data_inicio": "0026-07-01"}, "data_inicio") is None
    assert _data_do_post({}, "data_inicio") is None


@pytest.fixture
def base(db):
    veiculo = Veiculo.objects.create(placa="QXQ6C11", marca_modelo="Gol", km_atual=50_000)
    cliente = Cliente.objects.create(
        nome="Bruno Lima", cpf_cnpj="529.982.247-25", telefone="(31) 97777-6666"
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


def test_vencimento_da_nd_de_multas_aceita_dd_mm_aa_e_recusa_ano_torto(base):
    cliente = base[1]
    amanha = timezone.localdate() + timedelta(days=1)
    pedido = RequestFactory().post("/", {"vencimento": amanha.strftime("%d/%m/%y")})
    assert _vencimento_nd(cliente, pedido)["data"] == amanha
    pedido = RequestFactory().post("/", {"vencimento": f"0026-{amanha:%m-%d}"})
    assert _vencimento_nd(cliente, pedido)["data"] is None


def test_alocacao_editada_sem_mexer_redireciona(usuario_logado, base):
    alocacao = base[2]
    url = f"/alocacoes/{alocacao.pk}/editar/"
    leitor = _ler(usuario_logado.get(url).content.decode())
    resposta = usuario_logado.post(url, leitor.dados)
    assert resposta.status_code == 302, (resposta.context["form"].errors, leitor.dados)
    alocacao.refresh_from_db()
    assert (alocacao.data_inicio, alocacao.cobrar_a_partir_de) == (
        date(2026, 7, 1),
        date(2026, 7, 8),
    )


def test_contrato_limitado_editado_sem_mexer_continua_limitado(usuario_logado, base):
    # o <select> do limite dependia só do Alpine: sem "selected" no HTML, salvar sem o
    # JavaScript carregado trocava o contrato limitado para ilimitado
    alocacao = base[2]
    Alocacao.objects.filter(pk=alocacao.pk).update(
        limite_km="limitado", franquia_km_mensal=3000, taxa_km_excedido=Decimal("0.50")
    )
    url = f"/alocacoes/{alocacao.pk}/editar/"
    leitor = _ler(usuario_logado.get(url).content.decode())
    assert leitor.dados["limite_km"] == "limitado"
    assert usuario_logado.post(url, leitor.dados).status_code == 302
    alocacao.refresh_from_db()
    assert (alocacao.limite_km, alocacao.franquia_km_mensal) == ("limitado", 3000)


def test_nd_vencimento_invalido_tem_mensagem_certa(usuario_logado, base):
    dados = {
        "cliente": base[1].pk,
        "data_emissao": timezone.localdate().isoformat(),
        "vencimento": "abc",
        "item_descricao": ["Lavagem"],
        "item_valor": ["50,00"],
    }
    pagina = usuario_logado.post("/financeiro/nd/nova/", dados, follow=True).content.decode()
    assert "Não deu para emitir: Data de vencimento inválida" in pagina
    assert not NotaDebito.objects.exists()


def test_km_pendente_recusa_ano_0026_sem_gravar(usuario_logado, base):
    veiculo = base[0]
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"km": "51.000", "data_leitura": "0026-10-01"},
        follow=True,
    )
    assert "Confira o ano" in resposta.content.decode()
    assert not RegistroKm.objects.exists()
