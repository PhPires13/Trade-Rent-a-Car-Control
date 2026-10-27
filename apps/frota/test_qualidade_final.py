"""Regressões da revisão final de qualidade da frota (venda, cadastro, IPVA, hub, ficha)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.financeiro.models import ItemNotaDebito, NotaDebito
from apps.frota import desmobilizacao
from apps.frota.alertas import pendencias_do_veiculo
from apps.frota.models import PagamentoIpva, Veiculo
from apps.frota.views import VeiculoForm
from apps.manutencao.models import Manutencao
from apps.multas.models import Multa
from apps.pessoas.models import Cliente

HOJE = date.today()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(
        placa="QFT1A23",
        marca_modelo="Onix",
        km_atual=50_000,
        km_compra=40_000,
        data_aquisicao=date(2025, 1, 10),
    )


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def _vender(cliente_http, veiculo, **dados):
    corpo = {
        "data": HOJE.isoformat(),
        "valor": "35000,00",
        "comprador": "",
        "custos": "",
        "km": "",
    }
    corpo.update(dados)
    return cliente_http.post(f"/frota/veiculo/{veiculo.pk}/vender/", corpo)


def _dados_form(veiculo=None, **extra):
    dados = {
        "placa": veiculo.placa if veiculo else "RRR1B22",
        "marca_modelo": veiculo.marca_modelo if veiculo else "Gol",
        "uso": veiculo.uso if veiculo else Veiculo.Uso.LOCACAO,
        "km_atual": veiculo.km_atual if veiculo else "",
        "chave_reserva": Veiculo.ChaveReserva.DUVIDA,
    }
    if veiculo:
        dados["km_compra"] = veiculo.km_compra or ""
        dados["data_aquisicao"] = veiculo.data_aquisicao or ""
    dados.update(extra)
    return dados


# --- Venda --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dados, mensagem",
    [
        ({"data": (HOJE + timedelta(days=30)).isoformat()}, "não pode ser depois de hoje"),
        ({"data": "2024-12-31"}, "não pode ser antes da compra (10/01/2025)"),
        ({"km": "1.000"}, "KM da venda menor que o KM atual"),
    ],
)
def test_venda_recusa_data_ou_km_impossiveis(usuario_logado, veiculo, dados, mensagem):
    resposta = _vender(usuario_logado, veiculo, **dados)
    assert resposta.status_code == 200
    assert mensagem in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.DISPONIVEL
    assert veiculo.data_venda is None


def test_venda_valida_continua_funcionando(usuario_logado, veiculo):
    resposta = _vender(usuario_logado, veiculo, km="52.000")
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.VENDIDO
    assert veiculo.km_atual == 52_000


def test_venda_bloqueada_com_carro_na_oficina(usuario_logado, veiculo):
    veiculo.status = Veiculo.Status.EM_MANUTENCAO
    veiculo.save()
    manutencao = Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=3),
        data_entrada=HOJE - timedelta(days=3),
    )
    # a tela explica e leva ao "Carro voltou da oficina"
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/vender/").content.decode()
    assert "O carro ainda está na oficina" in html
    assert f"/manutencao/voltou/{manutencao.pk}/" in html
    # e o POST também é recusado
    resposta = _vender(usuario_logado, veiculo)
    assert "O carro ainda está na oficina" in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO


# --- Cadastro do carro ----------------------------------------------------------------


def test_km_atual_menor_que_km_da_compra_e_recusado(db):
    form = VeiculoForm(_dados_form(km_compra="60000", km_atual="50000"))
    assert not form.is_valid()
    assert "não pode ser menor que o KM na compra (60.000)" in form.errors["km_atual"][0]


@pytest.mark.parametrize("km_atual", ["", "0"])
def test_carro_novo_sem_km_atual_usa_km_da_compra(db, km_atual):
    form = VeiculoForm(_dados_form(km_compra="60000", km_atual=km_atual))
    assert form.is_valid(), form.errors
    assert form.save().km_atual == 60_000


def test_mensalidade_de_protecao_exige_data_de_compra(db):
    form = VeiculoForm(_dados_form(mensalidade_protecao="250,00"))
    assert not form.is_valid()
    assert "data de compra" in form.errors["data_aquisicao"][0]


def test_ficha_avisa_protecao_nao_calculada_sem_data(usuario_logado, db):
    carro = Veiculo.objects.create(
        placa="SEM1D00", marca_modelo="Gol", mensalidade_protecao=Decimal("250.00")
    )
    html = usuario_logado.get(f"/frota/veiculo/{carro.pk}/ficha/").content.decode()
    assert "sem data de compra: proteção não calculada" in html
    assert desmobilizacao.montar_ficha(carro).despesa_protecao_estimada == 0


def test_fora_de_locacao_com_lancamentos_pede_confirmacao(usuario_logado, veiculo):
    PagamentoIpva.objects.create(veiculo=veiculo, ano=2026, valor=Decimal("1200.00"))
    url = f"/frota/veiculo/{veiculo.pk}/editar/"
    resposta = usuario_logado.post(url, _dados_form(veiculo, uso=Veiculo.Uso.FORA_LOCACAO))
    assert resposta.status_code == 200
    assert "vão sair dos relatórios dos meses passados" in resposta.content.decode()
    veiculo.refresh_from_db()
    assert veiculo.uso == Veiculo.Uso.LOCACAO

    resposta = usuario_logado.post(
        url, _dados_form(veiculo, uso=Veiculo.Uso.FORA_LOCACAO, confirmar_fora_locacao="on")
    )
    assert resposta.status_code == 302
    veiculo.refresh_from_db()
    assert veiculo.uso == Veiculo.Uso.FORA_LOCACAO


def test_fora_de_locacao_sem_lancamentos_nao_pede_confirmacao(veiculo):
    form = VeiculoForm(_dados_form(veiculo, uso=Veiculo.Uso.FORA_LOCACAO), instance=veiculo)
    assert form.is_valid(), form.errors


# --- Hub e despesas -----------------------------------------------------------------


def test_hub_nao_conta_fici_de_multa_cancelada(usuario_logado, veiculo):
    Multa.objects.create(
        veiculo=veiculo,
        data_infracao=HOJE,
        fici_status=Multa.Fici.PENDENTE,
        resultado=Multa.Resultado.CANCELADA,
    )
    html = usuario_logado.get("/frota/").content.decode()
    assert "FICI pendente" not in html


def test_multa_da_empresa_ja_em_nd_nao_e_despesa(veiculo, cliente):
    multa = Multa.objects.create(
        veiculo=veiculo,
        data_infracao=HOJE,
        valor=Decimal("195.23"),
        responsavel=Multa.Responsavel.EMPRESA,
    )
    assert multa in desmobilizacao.multas_que_sao_despesa()
    nd = NotaDebito.objects.create(cliente=cliente, data_emissao=HOJE)
    ItemNotaDebito.objects.create(
        nota_debito=nd, descricao="Multa", valor=Decimal("195.23"), multa=multa
    )
    assert multa not in desmobilizacao.multas_que_sao_despesa()


# --- IPVA ---------------------------------------------------------------------------


def _post_ipva(cliente_http, veiculo, **linha):
    dados = {"pagamentos_ipva-TOTAL_FORMS": "1", "pagamentos_ipva-INITIAL_FORMS": "0"}
    dados.update({f"pagamentos_ipva-0-{k}": v for k, v in linha.items()})
    return cliente_http.post(f"/frota/veiculo/{veiculo.pk}/ipva/", dados)


def test_ipva_ano_com_2_digitos_e_parcela_zero_recusados(usuario_logado, veiculo):
    resposta = _post_ipva(usuario_logado, veiculo, ano="26", parcela="0", valor="1.200,00")
    html = resposta.content.decode()
    assert "Use o ano com 4 dígitos" in html
    assert "A parcela começa em 1" in html
    assert not PagamentoIpva.objects.exists()


def test_ipva_reenviado_da_mensagem_e_nao_erro_500(usuario_logado, veiculo):
    PagamentoIpva.objects.create(veiculo=veiculo, ano=2026, parcela=1, valor=Decimal("1200"))
    resposta = _post_ipva(usuario_logado, veiculo, ano="2026", parcela="1", valor="1.200,00")
    assert resposta.status_code == 200
    assert "O IPVA 2026 parcela 1 deste carro já está lançado" in resposta.content.decode()
    assert PagamentoIpva.objects.count() == 1


def test_ipva_sem_nada_preenchido_nao_diz_salvo(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/frota/veiculo/{veiculo.pk}/ipva/",
        {
            "pagamentos_ipva-TOTAL_FORMS": "1",
            "pagamentos_ipva-INITIAL_FORMS": "0",
            # a linha nova já vem com ano e parcela sugeridos; a pessoa só clicou em Salvar
            "pagamentos_ipva-0-ano": str(HOJE.year),
            "pagamentos_ipva-0-parcela": "1",
        },
    )
    html = resposta.content.decode()
    assert resposta.status_code == 200
    assert "Nada foi lançado" in html
    assert "IPVA do QFT1A23 salvo" not in html


def test_ipva_linha_nova_exige_valor(usuario_logado, veiculo):
    resposta = _post_ipva(
        usuario_logado, veiculo, ano=str(HOJE.year), parcela="1", vencimento="2026-12-10"
    )
    assert "Informe o valor do IPVA" in resposta.content.decode()
    assert not PagamentoIpva.objects.exists()


def test_ipva_linha_nova_sugere_proxima_parcela_livre(usuario_logado, veiculo):
    PagamentoIpva.objects.create(veiculo=veiculo, ano=HOJE.year, parcela=1, valor=Decimal("400"))
    resposta = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/ipva/")
    nova = resposta.context["formset"].forms[-1]
    assert nova.initial["parcela"] == 2


# --- Página do carro ----------------------------------------------------------------


def test_pendencias_incluem_rastreador_e_bateria(veiculo):
    veiculo.rastreador_vigencia_fim = HOJE - timedelta(days=1)
    veiculo.bateria_garantia_fim = HOJE + timedelta(days=10)
    veiculo.save()
    pendencias = {p["texto"]: p for p in pendencias_do_veiculo(veiculo)}
    assert pendencias["Rastreador"]["grave"] is True
    assert pendencias["Garantia da bateria"]["grave"] is False
    assert pendencias["Rastreador"]["acao"] == "Atualizar a data"


def test_carro_na_oficina_tem_botao_voltou(usuario_logado, veiculo):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE,
        data_entrada=HOJE - timedelta(days=2),
    )
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Carro voltou da oficina" in html
    assert f"/manutencao/voltou/{manutencao.pk}/" in html
    assert any(p["acao"] == "Carro voltou da oficina" for p in pendencias_do_veiculo(veiculo))


def test_hub_sem_jargao_e_foto_com_alt(usuario_logado, veiculo):
    veiculo.foto = "veiculos/nao-existe.jpg"
    veiculo.save()
    html = usuario_logado.get("/frota/").content.decode()
    assert "Quando vender cada carro" in html
    assert "+ Novo carro" in html
    assert "Situação" in html
    assert 'alt="Foto do QFT1A23"' in html
    assert "onerror" in html


def test_venda_recusada_nao_mostra_resultado_final_falso(usuario_logado, veiculo):
    """A ficha remontada após o erro não pode usar os dados da venda recusada."""
    resposta = _vender(usuario_logado, veiculo, data=(HOJE + timedelta(days=30)).isoformat())
    assert resposta.status_code == 200
    assert resposta.context["veiculo"].data_venda is None
    assert resposta.context["veiculo"].valor_venda is None
    assert resposta.context["ficha"].resultado_final is None
    veiculo.refresh_from_db()
    assert veiculo.status != Veiculo.Status.VENDIDO


def test_venda_recusada_por_campo_nao_deixa_valor_no_objeto(veiculo):
    with pytest.raises(desmobilizacao.ValidationError):
        desmobilizacao.registrar_venda(
            veiculo, data=HOJE + timedelta(days=5), valor=Decimal("30000"), km=60_000
        )
    assert veiculo.data_venda is None and veiculo.valor_venda is None
    assert veiculo.km_atual == 50_000


def test_tela_de_venda_limita_data_e_km(usuario_logado, veiculo):
    html = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/vender/").content.decode()
    assert f'max="{HOJE:%Y-%m-%d}"' in html
    assert 'min="2025-01-10"' in html
    assert 'min="50000"' in html
