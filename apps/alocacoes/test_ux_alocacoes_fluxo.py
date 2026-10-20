"""Entrega e devolução guiadas + sugestões do cadastro na nova alocação (propostas #7 e #15)."""

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.alocacoes.models import Alocacao, Vistoria
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="FLX1A23", marca_modelo="Onix", km_atual=48_900)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(
        nome="Bruna",
        cpf_cnpj="222.333.444-55",
        dia_vencimento=Cliente.DiaSemana.QUARTA,
        caucao_referencia=Decimal("1000.00"),
    )


def _alocar(veiculo, cliente, **extra):
    dados = {
        "veiculo": veiculo,
        "cliente": cliente,
        "data_inicio": date.today(),
        "valor_semanal": Decimal("650.00"),
        "km_entrega": veiculo.km_atual,
    }
    dados.update(extra)
    return Alocacao.objects.create(**dados)


def _json(html, elemento):
    inicio = html.index(f'id="{elemento}"')
    inicio = html.index(">", inicio) + 1
    return json.loads(html[inicio : html.index("</script>", inicio)])


# ---------- #7: sugestões do cadastro na nova alocação ----------


def test_nova_alocacao_sugere_dia_e_caucao_do_cliente(usuario_logado, veiculo, cliente):
    html = usuario_logado.get(f"/alocacoes/nova/?cliente={cliente.pk}").content.decode()
    # quarta = 2 já selecionada e caução pré-preenchida, com o aviso de sugestão
    assert '<option value="2" selected>' in html
    assert 'value="1.000,00"' in html
    assert "Sugerido pelo cadastro do cliente" in html
    dados = _json(html, "dados-clientes")
    assert dados[str(cliente.pk)] == {"dia": "2", "dia_nome": "Quarta-feira", "caucao": "1.000,00"}


def test_nova_alocacao_preenche_km_do_carro_e_textos_dos_selects(usuario_logado, veiculo, cliente):
    html = usuario_logado.get(f"/alocacoes/nova/?veiculo={veiculo.pk}").content.decode()
    assert 'name="km_entrega" value="48900"' in html
    assert _json(html, "dados-veiculos")[str(veiculo.pk)]["km"] == 48_900
    assert "Escolha o carro…" in html
    assert "Escolha o cliente…" in html
    assert "Mesmo dia da semana do início" in html
    assert "---------" not in html


def test_sugestao_continua_editavel(usuario_logado, veiculo, cliente):
    """O valor que vale é o digitado na alocação, não o do cadastro."""
    usuario_logado.post(
        "/alocacoes/nova/",
        {
            "veiculo": veiculo.pk,
            "cliente": cliente.pk,
            "data_inicio": date.today().isoformat(),
            "valor_semanal": "650,00",
            "dia_vencimento": str(Cliente.DiaSemana.SEXTA),
            "caucao_valor": "",
            "km_entrega": "48900",
            "limite_km": "ilimitado",
        },
    )
    alocacao = Alocacao.objects.get(veiculo=veiculo)
    assert alocacao.dia_vencimento == Cliente.DiaSemana.SEXTA
    assert alocacao.caucao_valor is None


# ---------- #15: entrega guiada ----------


def test_alocar_leva_para_a_ficha_com_os_proximos_passos(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        {
            "veiculo": veiculo.pk,
            "cliente": cliente.pk,
            "data_inicio": date.today().isoformat(),
            "valor_semanal": "650,00",
            "dia_vencimento": "",
            "caucao_valor": "1.000,00",
            "km_entrega": "48900",
            "limite_km": "ilimitado",
        },
    )
    assert resposta.url == f"/clientes/{cliente.pk}/"
    alocacao = Alocacao.objects.get(veiculo=veiculo)
    # a 1ª semana já nasce, sem esperar a rotina diária
    assert alocacao.cobrancas.filter(origem=Cobranca.Origem.ALUGUEL).count() == 1
    html = usuario_logado.get(resposta.url).content.decode()
    assert "Próximos passos da entrega" in html
    assert f"/alocacoes/{alocacao.pk}/contrato/" in html
    assert f"/alocacoes/{alocacao.pk}/vistoria/nova/?tipo=entrada" in html
    assert "Receber a caução (R$ 1.000,00)" in html
    assert f"/financeiro/caucoes/{alocacao.caucao.pk}/" in html
    assert "Receber a 1ª semana" in html
    assert "faltam 4 de 4" in html


def test_passos_se_marcam_sozinhos_e_cartao_some(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente, caucao_valor=Decimal("1000.00"))
    financeiro.gerar_cobrancas_semanais()
    ficha = f"/clientes/{cliente.pk}/"

    usuario_logado.get(f"/alocacoes/{alocacao.pk}/contrato/")  # abriu para imprimir
    Vistoria.objects.create(alocacao=alocacao, tipo=Vistoria.Tipo.ENTRADA, data=date.today())
    html = usuario_logado.get(ficha).content.decode()
    assert "faltam 2 de 4" in html
    assert html.count('aria-label="Feito"') == 2

    financeiro.abrir_caucao(alocacao, valor_recebido=Decimal("1000.00"))
    primeira = alocacao.cobrancas.get()
    financeiro.registrar_recebimento(
        cliente=cliente,
        data=date.today(),
        valor=primeira.valor,
        forma="pix",
        aplicacoes=[(primeira, primeira.valor)],
    )
    assert "Próximos passos da entrega" not in usuario_logado.get(ficha).content.decode()


def test_sem_caucao_o_passo_da_caucao_nao_aparece(usuario_logado, veiculo, cliente):
    _alocar(veiculo, cliente)
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert "Receber a caução" not in html
    assert "faltam 3 de 3" in html


def test_dispensar_esconde_o_cartao(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente)
    resposta = usuario_logado.post(f"/alocacoes/{alocacao.pk}/entrega/dispensar/")
    assert resposta.url == f"/clientes/{cliente.pk}/"
    alocacao.refresh_from_db()
    assert alocacao.passos_entrega_dispensados
    assert "Próximos passos da entrega" not in usuario_logado.get(resposta.url).content.decode()


def test_dispensar_por_get_nao_altera(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente)
    usuario_logado.get(f"/alocacoes/{alocacao.pk}/entrega/dispensar/")
    alocacao.refresh_from_db()
    assert not alocacao.passos_entrega_dispensados


def test_vistoria_ja_chega_com_o_tipo_escolhido(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente)
    html = usuario_logado.get(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/?tipo=saida"
    ).content.decode()
    assert '<option value="saida" selected>' in html


# ---------- #15: devolução em passos ----------


def test_encerrar_destaca_vistoria_de_devolucao_que_falta(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente)
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").content.decode()
    assert "Ainda não feita" in html
    assert f"/alocacoes/{alocacao.pk}/vistoria/nova/?tipo=saida" in html
    assert "Sem débitos em aberto hoje" in html


def test_encerrar_preenche_km_e_data_da_vistoria_e_compara_avarias(
    usuario_logado, veiculo, cliente
):
    alocacao = _alocar(veiculo, cliente, data_inicio=date.today() - timedelta(days=30))
    Vistoria.objects.create(
        alocacao=alocacao,
        tipo=Vistoria.Tipo.ENTRADA,
        data=alocacao.data_inicio,
        avarias="Risco na porta traseira",
    )
    ontem = date.today() - timedelta(days=1)
    Vistoria.objects.create(
        alocacao=alocacao,
        tipo=Vistoria.Tipo.SAIDA,
        data=ontem,
        km=52_300,
        avarias="Para-choque amassado",
    )
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").content.decode()
    assert 'value="52300"' in html
    assert f'value="{ontem:%Y-%m-%d}"' in html
    assert "Preenchido com o KM da vistoria de devolução" in html
    assert "Risco na porta traseira" in html
    assert "Para-choque amassado" in html
    assert "Ainda não feita" not in html


def test_encerrar_mostra_o_que_o_cliente_deve(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente, data_inicio=date.today() - timedelta(days=10))
    Cobranca.objects.create(
        cliente=cliente,
        alocacao=alocacao,
        origem=Cobranca.Origem.OUTRO,
        descricao="Lavagem",
        valor=Decimal("80.00"),
        vencimento=date.today() - timedelta(days=2),
    )
    html = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").content.decode()
    assert "Bruna deve R$ 80,00 hoje" in html
    assert f"/financeiro/receber/?cliente={cliente.pk}" in html


# ---------- lista vira cartões no celular ----------


def test_lista_tem_cartoes_para_celular_e_tabela_para_desktop(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente)
    html = usuario_logado.get("/alocacoes/").content.decode()
    assert 'class="space-y-3 sm:hidden"' in html
    assert "hidden overflow-x-auto" in html and "sm:block" in html
    # as ações aparecem nos dois formatos
    assert html.count(f"/alocacoes/{alocacao.pk}/encerrar/") == 2


# ---------- revisão: casos de borda ----------


def test_caucao_paga_em_parte_ainda_falta(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente, caucao_valor=Decimal("1000.00"))
    financeiro.abrir_caucao(alocacao, valor_recebido=Decimal("400.00"))
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert "Recebido R$ 400,00 de R$ 1.000,00 — faltam R$ 600,00." in html
    assert "faltam 4 de 4" in html


def test_primeira_semana_paga_em_parte_mostra_o_que_falta(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente)
    financeiro.gerar_cobrancas_semanais()
    primeira = alocacao.cobrancas.get()
    financeiro.registrar_recebimento(
        cliente=cliente,
        data=date.today(),
        valor=Decimal("200.00"),
        forma="pix",
        aplicacoes=[(primeira, Decimal("200.00"))],
    )
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert "Pago em parte: faltam R$ 450,00 de R$ 650,00" in html
    assert "Receber a 1ª semana" in html


def test_vistoria_volta_para_a_tela_de_onde_veio(usuario_logado, veiculo, cliente):
    alocacao = _alocar(veiculo, cliente)
    ficha = f"/clientes/{cliente.pk}/"
    dados = {"tipo": "entrada", "data": date.today().isoformat(), "combustivel": "cheio"}
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/?tipo=entrada&voltar={ficha}", dados
    )
    assert resposta.status_code == 302
    assert resposta.url == ficha
    # endereço de fora do sistema é ignorado (vai para a linha do tempo)
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/?voltar=https://exemplo.com/", dados
    )
    assert resposta.status_code == 302
    assert resposta.url == reverse("alocacoes:timeline", args=[veiculo.pk])


def test_caucao_zero_no_cadastro_nao_vira_sugestao(usuario_logado, veiculo, cliente):
    cliente.caucao_referencia = Decimal("0")
    cliente.save()
    html = usuario_logado.get(f"/alocacoes/nova/?cliente={cliente.pk}").content.decode()
    assert 'value="0,00"' not in html
    assert _json(html, "dados-clientes")[str(cliente.pk)]["caucao"] == ""


def test_parametros_invalidos_na_nova_alocacao_nao_quebram(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.get("/alocacoes/nova/?veiculo=abc&cliente=99999")
    assert resposta.status_code == 200


def test_ficha_mostra_preferencia_quando_dia_do_contrato_e_outro(usuario_logado, veiculo, cliente):
    _alocar(veiculo, cliente, dia_vencimento=Cliente.DiaSemana.SEXTA)
    html = usuario_logado.get(f"/clientes/{cliente.pk}/").content.decode()
    assert "(preferência do cadastro: quarta-feira)" in html
