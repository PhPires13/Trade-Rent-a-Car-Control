"""Etapa 18 (multas): ND de multa excluída no meio do caminho, "Vendedor anterior"
no dia da compra e a sigla ND escrita por extenso."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro.models import NotaDebito
from apps.frota.models import Veiculo
from apps.multas import services
from apps.multas.forms import MultaEdicaoForm, MultaForm
from apps.multas.models import Multa
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Ana", cpf_cnpj="111.222.333-44")


@pytest.fixture
def veiculo(cliente):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=100),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    return veiculo


def _multa(veiculo, **campos):
    campos.setdefault("data_infracao", HOJE - timedelta(days=20))
    campos.setdefault("valor", Decimal("150.00"))
    campos.setdefault("descricao", "Avançar sinal")
    return Multa.objects.create(veiculo=veiculo, **campos)


# --- ND de multas: multa excluída em outra aba não gera ND vazia nem incompleta ---


def test_multa_excluida_antes_de_gerar_nd_nao_sai_nd_vazia(veiculo, cliente):
    multa = _multa(veiculo)
    Multa.objects.filter(pk=multa.pk).delete()
    with pytest.raises(ValidationError, match="alterada ou excluída"):
        services.gerar_nd_de_multas(cliente, [multa], HOJE)
    assert not NotaDebito.objects.exists()


def test_uma_de_duas_multas_excluida_nao_gera_nd_incompleta(veiculo, cliente):
    fica, some = _multa(veiculo), _multa(veiculo, descricao="Velocidade")
    Multa.objects.filter(pk=some.pk).delete()
    with pytest.raises(ValidationError, match="Abra a tela de novo"):
        services.gerar_nd_de_multas(cliente, [fica, some], HOJE)
    assert not NotaDebito.objects.exists()


def test_mesma_multa_repetida_na_lista_gera_nd_normal(veiculo, cliente):
    multa = _multa(veiculo)
    nd = services.gerar_nd_de_multas(cliente, [multa, multa], HOJE)
    assert nd.itens.count() == 1


# --- "Vendedor anterior" no próprio dia da compra ---


def _dados_novos(veiculo, dia, **extra):
    dados = {
        "veiculo": veiculo.pk,
        "data_infracao": dia.isoformat(),
        "descricao": "Avançar sinal",
        "valor": "100,00",
        "tipo_condutor": Multa.TipoCondutor.CLIENTE,
        "responsavel": Multa.Responsavel.VENDEDOR,
    }
    dados.update(extra)
    return dados


def test_vendedor_aceito_na_infracao_do_dia_da_compra(veiculo):
    veiculo.data_aquisicao = HOJE - timedelta(days=30)
    veiculo.save(update_fields=["data_aquisicao"])
    form = MultaForm(_dados_novos(veiculo, veiculo.data_aquisicao))
    assert form.is_valid(), form.errors
    assert form.save().responsavel == Multa.Responsavel.VENDEDOR


def test_vendedor_recusado_depois_do_dia_da_compra(veiculo):
    veiculo.data_aquisicao = HOJE - timedelta(days=30)
    veiculo.save(update_fields=["data_aquisicao"])
    form = MultaForm(_dados_novos(veiculo, veiculo.data_aquisicao + timedelta(days=1)))
    assert not form.is_valid()
    assert "antes do dia da infração" in str(form.errors["responsavel"])


def test_corrigir_data_para_o_dia_da_compra_mantem_vendedor(veiculo):
    veiculo.data_aquisicao = HOJE - timedelta(days=30)
    veiculo.save(update_fields=["data_aquisicao"])
    multa = _multa(
        veiculo,
        data_infracao=veiculo.data_aquisicao - timedelta(days=2),
        responsavel=Multa.Responsavel.VENDEDOR,
    )
    dados = {
        "veiculo": veiculo.pk,
        "data_infracao": veiculo.data_aquisicao.isoformat(),
        "cliente": "",
        "descricao": multa.descricao,
        "valor": "150,00",
        "tipo_condutor": multa.tipo_condutor,
        "responsavel": multa.responsavel,
        "resultado": multa.resultado,
        "fici_status": multa.fici_status,
        "pagamento": multa.pagamento,
        "pago_por": "",
    }
    form = MultaEdicaoForm(dados, instance=multa)
    assert form.is_valid(), form.errors
    form.save()
    multa.refresh_from_db()
    assert multa.responsavel == Multa.Responsavel.VENDEDOR


# --- sigla ND por extenso ---


def test_lista_e_mensagem_escrevem_nota_de_debito_por_extenso(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.get("/multas/")
    assert "Gerar nota de débito (ND) das multas" in resposta.content.decode()
    multa = _multa(veiculo)
    tela = usuario_logado.get(f"/multas/gerar-nd/?cliente={cliente.pk}")
    token = tela.context["token"]
    resposta = usuario_logado.post(
        "/multas/gerar-nd/",
        {"cliente": cliente.pk, "multa": [multa.pk], "token": token},
        follow=True,
    )
    textos = [str(m) for m in resposta.context["messages"]]
    assert any(t.startswith("Nota de débito (ND) 001 emitida") for t in textos), textos


# --- revisão adversarial: a tela de Gerar ND também avisa (não só o serviço) ---


def _post_gerar_nd(usuario_logado, cliente, multas):
    tela = usuario_logado.get(f"/multas/gerar-nd/?cliente={cliente.pk}")
    return usuario_logado.post(
        "/multas/gerar-nd/",
        {"cliente": cliente.pk, "multa": [m.pk for m in multas], "token": tela.context["token"]},
        follow=True,
    )


def test_tela_avisa_multa_marcada_e_excluida_em_outra_aba(usuario_logado, veiculo, cliente):
    fica, some = _multa(veiculo), _multa(veiculo, descricao="Velocidade")
    tela = usuario_logado.get(f"/multas/gerar-nd/?cliente={cliente.pk}")
    Multa.objects.filter(pk=some.pk).delete()  # outra aba
    resposta = usuario_logado.post(
        "/multas/gerar-nd/",
        {"cliente": cliente.pk, "multa": [fica.pk, some.pk], "token": tela.context["token"]},
        follow=True,
    )
    textos = [str(m) for m in resposta.context["messages"]]
    assert any("foi alterada, excluída" in t for t in textos), textos
    assert not NotaDebito.objects.exists()  # antes: ND só com a outra, sem aviso


def test_tela_avisa_multa_ja_cobrada_em_outra_aba(usuario_logado, veiculo, cliente):
    primeira, segunda = _multa(veiculo), _multa(veiculo, descricao="Velocidade")
    tela_b = usuario_logado.get(f"/multas/gerar-nd/?cliente={cliente.pk}")
    _post_gerar_nd(usuario_logado, cliente, [primeira])  # aba A emite só a primeira
    assert NotaDebito.objects.count() == 1
    resposta = usuario_logado.post(  # aba B, aberta antes, com as duas marcadas
        "/multas/gerar-nd/",
        {
            "cliente": cliente.pk,
            "multa": [primeira.pk, segunda.pk],
            "token": tela_b.context["token"],
        },
        follow=True,
    )
    textos = [str(m) for m in resposta.context["messages"]]
    assert any("já entrou em outra nota de débito" in t for t in textos), textos
    assert NotaDebito.objects.count() == 1
    # a lista que volta já mostra só a que falta, e o mesmo código serve de novo
    assert [m.pk for m in resposta.context["multas"]] == [segunda.pk]
    resposta = usuario_logado.post(
        "/multas/gerar-nd/",
        {"cliente": cliente.pk, "multa": [segunda.pk], "token": resposta.context["token"]},
        follow=True,
    )
    assert NotaDebito.objects.count() == 2


def test_desmarcar_todas_continua_pedindo_uma_multa(usuario_logado, veiculo, cliente):
    _multa(veiculo)
    resposta = _post_gerar_nd(usuario_logado, cliente, [])
    textos = [str(m) for m in resposta.context["messages"]]
    assert any("Selecione ao menos uma multa" in t for t in textos), textos
    assert not NotaDebito.objects.exists()


def test_cartao_do_celular_sem_valor_mostra_informar_o_valor(usuario_logado, veiculo):
    # na aba "Pagar ao órgão" a multa sem valor aparecia sem botão nenhum no celular
    _multa(veiculo, valor=None, fici_status=Multa.Fici.INDICADO)
    html = usuario_logado.get("/multas/?tarefa=pagar").content.decode()
    assert "Informar o valor" in html


def test_cartao_com_valor_continua_com_marcar_paga(usuario_logado, veiculo):
    _multa(veiculo, fici_status=Multa.Fici.INDICADO)
    html = usuario_logado.get("/multas/?tarefa=pagar").content.decode()
    assert "Informar o valor" not in html
    assert "Marcar paga" in html
