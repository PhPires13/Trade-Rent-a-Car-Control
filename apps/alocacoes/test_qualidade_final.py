"""Qualidade final — alocações: datas, KM, troca, vistoria, encerramento e telas."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.alocacoes.entrega import situacao_da_devolucao
from apps.alocacoes.models import Alocacao, TrocaTemporaria, Vistoria
from apps.alocacoes.views import AlocacaoForm, _tipo_sugerido
from apps.financeiro.models import Cobranca
from apps.frota.models import Categoria, Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QLF1A11", marca_modelo="Onix", km_atual=10_000)


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="QLF2B22", marca_modelo="HB20", km_atual=39_100)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Ana Souza", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("700.00"),
        km_entrega=10_000,
    )


def _dados(veiculo, cliente, **extra):
    dados = {
        "veiculo": veiculo.pk,
        "cliente": cliente.pk,
        "data_inicio": HOJE.isoformat(),
        "valor_semanal": "700,00",
        "km_entrega": str(veiculo.km_atual),
        "limite_km": "ilimitado",
    }
    dados.update(extra)
    return dados


# ---------- nova alocação: datas ----------


def test_data_de_inicio_no_futuro_e_recusada(veiculo, cliente):
    futura = (HOJE + timedelta(days=30)).isoformat()
    form = AlocacaoForm(_dados(veiculo, cliente, data_inicio=futura))
    assert not form.is_valid()
    assert "não pode ser no futuro" in str(form.errors["data_inicio"])


def test_data_de_inicio_no_futuro_recusada_tambem_no_modelo(veiculo, cliente):
    alocacao = Alocacao(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE + timedelta(days=1),
        valor_semanal=Decimal("700"),
        km_entrega=10_000,
    )
    with pytest.raises(ValidationError) as erro:
        alocacao.full_clean()
    assert "data_inicio" in erro.value.message_dict


def test_ano_9999_nao_da_erro_500(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        _dados(veiculo, cliente, data_inicio="9999-12-31", cobrar_a_partir_de="9999-12-31"),
    )
    assert resposta.status_code == 200
    assert not Alocacao.objects.exists()


def test_data_com_ano_errado_no_passado_e_recusada(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        _dados(veiculo, cliente, data_inicio="1926-07-01", cobrar_a_partir_de="1926-07-01"),
    )
    assert resposta.status_code == 200
    assert "Confira o ano" in resposta.content.decode()
    assert not Cobranca.objects.exists()


def test_muitas_semanas_atrasadas_pedem_confirmacao(usuario_logado, veiculo, cliente):
    inicio = (HOJE - timedelta(days=7 * 30)).isoformat()
    dados = _dados(veiculo, cliente, data_inicio=inicio, cobrar_a_partir_de=inicio)
    resposta = usuario_logado.post("/alocacoes/nova/", dados)
    assert resposta.status_code == 200
    assert "semanas atrasadas" in resposta.content.decode()
    assert not Alocacao.objects.exists()
    resposta = usuario_logado.post("/alocacoes/nova/", {**dados, "confirmar_semanas": "on"})
    assert resposta.status_code == 302
    assert Alocacao.objects.count() == 1


# ---------- nova alocação: KM, cliente com carro, toque duplo, preço ----------


def test_km_menor_que_o_do_carro_pede_confirmacao(usuario_logado, veiculo, cliente):
    dados = _dados(veiculo, cliente, km_entrega="1000")
    resposta = usuario_logado.post("/alocacoes/nova/", dados)
    assert resposta.status_code == 200
    assert "já marcou 10.000 km" in resposta.content.decode()
    resposta = usuario_logado.post("/alocacoes/nova/", {**dados, "confirmar_km": "on"})
    assert resposta.status_code == 302


def test_cliente_que_ja_tem_carro_pede_confirmacao(usuario_logado, alocacao, cliente):
    outro = Veiculo.objects.create(placa="QLF3C33", marca_modelo="Kwid", km_atual=5_000)
    dados = _dados(outro, cliente)
    resposta = usuario_logado.post("/alocacoes/nova/", dados)
    assert resposta.status_code == 200
    assert "já está com o carro QLF1A11" in resposta.content.decode()
    resposta = usuario_logado.post("/alocacoes/nova/", {**dados, "confirmar_dois_carros": "on"})
    assert resposta.status_code == 302


def test_lista_de_clientes_mostra_quem_ja_tem_carro(usuario_logado, alocacao):
    html = usuario_logado.get("/alocacoes/nova/").content.decode()
    assert "Ana Souza — já com QLF1A11" in html


def test_toque_duplo_em_alocar_nao_mostra_erro(usuario_logado, veiculo, cliente):
    dados = _dados(veiculo, cliente, token="abc123")
    primeira = usuario_logado.post("/alocacoes/nova/", dados)
    segunda = usuario_logado.post("/alocacoes/nova/", dados, follow=True)
    assert primeira.status_code == 302
    assert Alocacao.objects.count() == 1
    assert "já tinha sido registrada" in segunda.content.decode()


def test_formulario_com_erro_nao_queima_o_token(usuario_logado, veiculo, cliente):
    dados = _dados(veiculo, cliente, token="tok1", valor_semanal="")
    assert usuario_logado.post("/alocacoes/nova/", dados).status_code == 200
    resposta = usuario_logado.post("/alocacoes/nova/", {**dados, "valor_semanal": "700,00"})
    assert resposta.status_code == 302
    assert Alocacao.objects.count() == 1


def test_valor_semanal_vem_com_o_preco_da_categoria(usuario_logado, veiculo):
    veiculo.categoria = Categoria.objects.create(
        nome="Econômico", valor_semanal_referencia=Decimal("650.00")
    )
    veiculo.save()
    resposta = usuario_logado.get(f"/alocacoes/nova/?veiculo={veiculo.pk}")
    assert resposta.context["form"]["valor_semanal"].value() in (Decimal("650.00"), "650,00")


def test_cobrar_a_partir_de_fica_recolhido(usuario_logado, veiculo):
    html = usuario_logado.get("/alocacoes/nova/").content.decode()
    assert "Contrato que já rodava antes do sistema?" in html
    assert 'name="token"' in html


# ---------- troca temporária ----------


def test_troca_com_retirada_no_futuro_e_recusada(usuario_logado, alocacao, substituto):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/troca/",
        {
            "veiculo_substituto": substituto.pk,
            "data_retirada": (HOJE + timedelta(days=20)).isoformat(),
            "km_retirada": "39100",
        },
    )
    assert resposta.status_code == 200
    assert not TrocaTemporaria.objects.exists()


def test_troca_com_km_menor_pede_confirmacao(usuario_logado, alocacao, substituto):
    dados = {
        "veiculo_substituto": substituto.pk,
        "data_retirada": HOJE.isoformat(),
        "km_retirada": "30000",
    }
    resposta = usuario_logado.post(f"/alocacoes/{alocacao.pk}/troca/", dados)
    assert resposta.status_code == 200
    assert "já marcou 39.100 km" in resposta.content.decode()
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/troca/", {**dados, "confirmar_km": "on"}
    )
    assert resposta.status_code == 302


def test_troca_mostra_km_dos_candidatos_e_rotulo_claro(usuario_logado, alocacao, substituto):
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/troca/")
    html = resposta.content.decode()
    assert "KM do substituto ao sair" in html
    assert resposta.context["km_candidatos"][str(substituto.pk)] == 39_100


def test_troca_sem_carro_disponivel_explica(usuario_logado, alocacao):
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/troca/").content.decode()
    assert "Nenhum carro disponível para servir de substituto agora" in html


def test_devolver_substituto_com_principal_na_oficina_pede_confirmacao(
    usuario_logado, alocacao, veiculo, substituto
):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=5),
        km_retirada=39_100,
    )
    Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=5),
        data_entrada=HOJE - timedelta(days=5),
    )
    url = f"/alocacoes/troca/{troca.pk}/devolver/"
    assert "ainda consta na oficina" in usuario_logado.get(url).content.decode()
    dados = {"data_devolucao": HOJE.isoformat(), "km_devolucao": "39500"}
    usuario_logado.post(url, dados)
    troca.refresh_from_db()
    assert troca.data_devolucao is None
    usuario_logado.post(url, {**dados, "confirmar_oficina": "1"})
    troca.refresh_from_db()
    assert troca.data_devolucao == HOJE


def test_devolver_substituto_com_km_abaixo_da_vistoria_pede_confirmacao(
    usuario_logado, alocacao, substituto
):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=5),
        km_retirada=39_100,
    )
    Vistoria.objects.create(
        alocacao=alocacao, veiculo=substituto, tipo="saida", data=HOJE, km=40_000
    )
    url = f"/alocacoes/troca/{troca.pk}/devolver/"
    usuario_logado.post(url, {"data_devolucao": HOJE.isoformat(), "km_devolucao": "39500"})
    troca.refresh_from_db()
    assert troca.data_devolucao is None


# ---------- encerramento ----------


def test_encerrar_antes_do_fim_da_troca_e_recusado(alocacao, substituto):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=10),
        km_retirada=39_100,
    )
    troca.devolver(HOJE - timedelta(days=2), 39_500)
    with pytest.raises(ValidationError, match="ficou com o substituto"):
        alocacao.encerrar(HOJE - timedelta(days=15), 11_000)


def test_vistoria_de_devolucao_antiga_nao_sugere_data(usuario_logado, alocacao):
    Vistoria.objects.create(
        alocacao=alocacao, tipo="saida", data=HOJE - timedelta(days=40), km=10_500
    )
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/")
    assert resposta.context["data_sugerida"] == HOJE


def test_devolucao_seguida_de_nova_entrega_nao_conta(alocacao):
    Vistoria.objects.create(
        alocacao=alocacao, tipo="saida", data=HOJE - timedelta(days=40), km=10_500
    )
    Vistoria.objects.create(
        alocacao=alocacao, tipo="entrada", data=HOJE - timedelta(days=30), km=10_600
    )
    assert situacao_da_devolucao(alocacao)["vistoria_devolucao"] is None


def test_vistoria_de_devolucao_recente_sugere_data_e_avisa(usuario_logado, alocacao):
    Vistoria.objects.create(
        alocacao=alocacao, tipo="saida", data=HOJE - timedelta(days=1), km=12_000
    )
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/")
    assert resposta.context["data_sugerida"] == HOJE - timedelta(days=1)
    assert "Preenchido com a data da vistoria de devolução" in resposta.content.decode()


def test_encerrar_com_km_abaixo_da_vistoria_pede_confirmacao(usuario_logado, alocacao):
    Vistoria.objects.create(alocacao=alocacao, tipo="saida", data=HOJE, km=13_000)
    url = f"/alocacoes/{alocacao.pk}/encerrar/"
    usuario_logado.post(url, {"data_termino": HOJE.isoformat(), "km_devolucao": "11000"})
    alocacao.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ATIVA
    resposta = usuario_logado.post(
        url,
        {"data_termino": HOJE.isoformat(), "km_devolucao": "11000", "confirmar_km": "1"},
    )
    alocacao.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ENCERRADA
    assert resposta.url == f"/clientes/{alocacao.cliente_id}/"


# ---------- vistoria ----------


def test_vistoria_no_futuro_ou_antes_do_inicio_e_recusada(usuario_logado, alocacao):
    url = f"/alocacoes/{alocacao.pk}/vistoria/nova/"
    for data in (HOJE + timedelta(days=40), HOJE - timedelta(days=300)):
        resposta = usuario_logado.post(
            url, {"tipo": "saida", "data": data.isoformat(), "km": "10500"}
        )
        assert resposta.status_code == 200
    assert not Vistoria.objects.exists()


def test_vistoria_com_km_menor_que_o_da_entrega_pede_confirmacao(usuario_logado, alocacao):
    url = f"/alocacoes/{alocacao.pk}/vistoria/nova/"
    dados = {"tipo": "entrada", "data": HOJE.isoformat(), "km": "50"}
    assert usuario_logado.post(url, dados).status_code == 200
    assert not Vistoria.objects.exists()
    usuario_logado.post(url, {**dados, "confirmar_km": "on"})
    assert Vistoria.objects.count() == 1


def test_vistoria_reenviada_nao_duplica(usuario_logado, alocacao):
    url = f"/alocacoes/{alocacao.pk}/vistoria/nova/"
    dados = {"tipo": "entrada", "data": HOJE.isoformat(), "km": "10000", "token": "v1"}
    usuario_logado.post(url, dados)
    usuario_logado.post(url, dados)
    assert Vistoria.objects.count() == 1


def test_vistoria_sugere_km_e_tipo(usuario_logado, alocacao, veiculo):
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/nova/")
    form = resposta.context["form"]
    assert form["tipo"].value() == Vistoria.Tipo.ENTRADA
    assert form["km"].value() == 10_000
    Vistoria.objects.create(alocacao=alocacao, tipo="entrada", data=HOJE, km=10_000)
    Vistoria.objects.create(alocacao=alocacao, tipo="saida", data=HOJE, km=10_100)
    assert _tipo_sugerido(alocacao, veiculo) == Vistoria.Tipo.ENTRADA


# ---------- editar ----------


def test_editar_valor_refaz_a_semana_em_aberto(usuario_logado, veiculo, cliente):
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE,
        valor_semanal=Decimal("6500.00"),
        km_entrega=10_000,
    )
    from apps.financeiro.services import gerar_cobrancas_semanais

    gerar_cobrancas_semanais()
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/",
        {
            "valor_semanal": "650,00",
            "dia_vencimento": str(alocacao.dia_vencimento),
            "limite_km": "ilimitado",
        },
        follow=True,
    )
    cobranca = alocacao.cobrancas.get()
    assert cobranca.valor == Decimal("650.00")
    assert "foi refeita" in resposta.content.decode()


def test_editar_dia_de_vencimento_sem_opcao_vazia(usuario_logado, alocacao):
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/editar/")
    escolhas = [valor for valor, _ in resposta.context["form"].fields["dia_vencimento"].choices]
    assert "" not in escolhas


# ---------- contrato ----------


def test_contrato_volta_para_a_ficha_e_avisa_dados_da_empresa(usuario_logado, alocacao, settings):
    settings.EMPRESA_CNPJ = ""
    ficha = f"/clientes/{alocacao.cliente_id}/"
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/contrato/?voltar={ficha}").content
    html = html.decode()
    assert f'href="{ficha}"' in html
    assert "Dados da empresa não configurados" in html


def test_contrato_ignora_voltar_externo(usuario_logado, alocacao):
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/contrato/?voltar=//exemplo.com/x")
    assert resposta.context["voltar"] == "/alocacoes/"


# ---------- lista: vencido x a vencer ----------


def test_lista_separa_vencido_de_a_vencer(usuario_logado, alocacao, cliente):
    Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        descricao="Franquia",
        valor=Decimal("1200.00"),
        vencimento=HOJE + timedelta(days=2),
    )
    html = usuario_logado.get("/alocacoes/").content.decode()
    assert "R$ 1.200,00 a vencer" in html
    assert "R$ 1.200,00 vencido" not in html


# ---------- revisão adversarial ----------


def test_contrato_de_anos_atras_passa_com_cobrar_a_partir_de_recente(
    usuario_logado, veiculo, cliente
):
    """Carga inicial (D01): contrato que roda há 2 anos não pode ser barrado pelo "ano errado"."""
    dados = _dados(
        veiculo,
        cliente,
        data_inicio=(HOJE - timedelta(days=730)).isoformat(),
        cobrar_a_partir_de=(HOJE - timedelta(days=3)).isoformat(),
    )
    resposta = usuario_logado.post("/alocacoes/nova/", dados)
    assert resposta.status_code == 302
    alocacao = Alocacao.objects.get()
    assert alocacao.data_inicio == HOJE - timedelta(days=730)
    # só o período desde "cobrar a partir de" — nada dos 2 anos anteriores
    assert not Cobranca.objects.filter(
        alocacao=alocacao, vencimento__lt=HOJE - timedelta(days=3)
    ).exists()
    assert Cobranca.objects.filter(alocacao=alocacao).count() <= 2


def test_inicio_de_anos_atras_sem_cobrar_a_partir_de_pede_conferir_o_ano(
    usuario_logado, veiculo, cliente
):
    dados = _dados(veiculo, cliente, data_inicio=(HOJE - timedelta(days=730)).isoformat())
    resposta = usuario_logado.post("/alocacoes/nova/", dados)
    assert resposta.status_code == 200
    assert "Confira o ano" in resposta.content.decode()
    assert not Alocacao.objects.exists()


def test_token_reusado_com_outro_carro_nao_finge_que_alocou(usuario_logado, veiculo, cliente):
    """Voltar com o botão do celular e trocar o carro: nada foi criado com esses dados."""
    outro = Veiculo.objects.create(placa="QLF4D44", marca_modelo="Mobi", km_atual=2_000)
    outro_cliente = Cliente.objects.create(nome="Bruno Lima", cpf_cnpj="555.666.777-88")
    assert (
        usuario_logado.post("/alocacoes/nova/", _dados(veiculo, cliente, token="t1")).status_code
        == 302
    )
    resposta = usuario_logado.post("/alocacoes/nova/", _dados(outro, outro_cliente, token="t1"))
    html = resposta.content.decode()
    assert resposta.status_code == 200
    assert "já tinha sido registrada" not in html
    assert "já tinha sido usado" in html
    assert Alocacao.objects.count() == 1
    novo_token = resposta.context["token"]
    assert novo_token != "t1"
    resposta = usuario_logado.post(
        "/alocacoes/nova/", _dados(outro, outro_cliente, token=novo_token)
    )
    assert resposta.status_code == 302
    assert Alocacao.objects.count() == 2


def test_vistoria_de_devolucao_antiga_nao_preenche_o_km(usuario_logado, alocacao):
    """KM de semanas atrás no encerramento deixaria de cobrar o que o carro rodou depois."""
    Vistoria.objects.create(
        alocacao=alocacao, tipo="saida", data=HOJE - timedelta(days=40), km=10_500
    )
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/")
    assert resposta.context["km_sugerido"] == ""
    assert "Essa vistoria já tem alguns dias" in resposta.content.decode()


def test_vistoria_de_devolucao_recente_preenche_o_km(usuario_logado, alocacao):
    Vistoria.objects.create(
        alocacao=alocacao, tipo="saida", data=HOJE - timedelta(days=1), km=12_000
    )
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/")
    assert resposta.context["km_sugerido"] == 12_000
    assert "Essa vistoria já tem alguns dias" not in resposta.content.decode()
