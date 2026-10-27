"""Proposta #10 (sinistros): edição, formulário em blocos, aviso ao vivo e lista."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente
from apps.sinistros.models import AuxilioMotorista, Sinistro


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")


@pytest.fixture
def outro_veiculo(db):
    return Veiculo.objects.create(placa="ABC1D23", marca_modelo="Onix")


@pytest.fixture
def arlen(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def bruna(db):
    return Cliente.objects.create(nome="Bruna", cpf_cnpj="555.666.777-88")


@pytest.fixture
def alocacoes(veiculo, outro_veiculo, arlen, bruna):
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=arlen,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )
    Alocacao.objects.create(
        veiculo=outro_veiculo,
        cliente=bruna,
        data_inicio=date(2026, 7, 1),
        valor_semanal=Decimal("650.00"),
        km_entrega=0,
    )


@pytest.fixture
def sinistro(alocacoes, veiculo):
    # proteção acionada: o auxílio motorista só vale com evento aberto (D26)
    return Sinistro.objects.create(
        veiculo=veiculo, data=date(2026, 7, 10), envolvido="terceiro", acionou_protecao=True
    )


def _dados(sinistro, **mudancas):
    dados = {
        "veiculo": sinistro.veiculo_id,
        "data": sinistro.data.isoformat(),
        "cliente": sinistro.cliente_id or "",
        "tipo": sinistro.tipo,
        "envolvido": sinistro.envolvido,
        "responsabilidade": sinistro.responsabilidade,
        "descricao": sinistro.descricao,
        "boletim_ocorrencia": sinistro.boletim_ocorrencia,
        "data_evento": "",
        "franquia_valor": "",
        "responsavel_custo": sinistro.responsavel_custo,
        "status": sinistro.status,
        "observacoes": sinistro.observacoes,
    }
    dados.update(mudancas)
    return {chave: valor for chave, valor in dados.items() if valor is not None}


def test_pagina_editar_mostra_dados_e_situacao_no_topo(usuario_logado, sinistro):
    resposta = usuario_logado.get(reverse("sinistros:editar", args=[sinistro.pk]))
    html = resposta.content.decode()
    assert resposta.status_code == 200
    assert 'value="2026-07-10"' in html  # a data aparece no campo de data
    assert html.index("Situação") < html.index("O que aconteceu")
    assert "Auxílio motorista" in html


def test_editar_muda_status_bo_franquia_e_volta_com_filtros(usuario_logado, sinistro):
    volta = reverse("sinistros:lista") + "?status=aberto&veiculo=QXQ"
    resposta = usuario_logado.post(
        reverse("sinistros:editar", args=[sinistro.pk]),
        _dados(
            sinistro,
            status="concluido",
            boletim_ocorrencia="12345/2026",
            acionou_protecao="on",
            data_evento="2026-07-11",
            franquia_valor="1.250,00",
            volta=volta,
        ),
    )
    assert resposta.status_code == 302
    assert resposta.url == volta
    sinistro.refresh_from_db()
    assert sinistro.status == "concluido"
    assert sinistro.boletim_ocorrencia == "12345/2026"
    assert sinistro.franquia_valor == Decimal("1250.00")
    assert sinistro.data_evento == date(2026, 7, 11)


def test_editar_nao_redireciona_para_fora_do_site(usuario_logado, sinistro):
    resposta = usuario_logado.post(
        reverse("sinistros:editar", args=[sinistro.pk]),
        _dados(sinistro, volta="https://golpe.example.com/"),
    )
    assert resposta.url == reverse("sinistros:lista")


def test_desmarcar_protecao_apaga_franquia_e_data_do_evento(usuario_logado, sinistro):
    sinistro.acionou_protecao = True
    sinistro.franquia_valor = Decimal("500")
    sinistro.data_evento = date(2026, 7, 11)
    sinistro.save()
    usuario_logado.post(
        reverse("sinistros:editar", args=[sinistro.pk]),
        _dados(sinistro, franquia_valor="500,00", data_evento="2026-07-11"),
    )
    sinistro.refresh_from_db()
    assert sinistro.acionou_protecao is False
    assert sinistro.franquia_valor is None
    assert sinistro.data_evento is None


def test_trocar_carro_na_edicao_recalcula_motorista(usuario_logado, sinistro, outro_veiculo, bruna):
    assert sinistro.cliente.nome == "Arlen"
    usuario_logado.post(
        reverse("sinistros:editar", args=[sinistro.pk]),
        _dados(sinistro, veiculo=outro_veiculo.pk),
    )
    sinistro.refresh_from_db()
    assert sinistro.cliente == bruna


def test_motorista_escolhido_na_mao_e_respeitado(usuario_logado, sinistro, bruna):
    # mudou a data e trocou o motorista na mão: vale o que a pessoa escolheu
    usuario_logado.post(
        reverse("sinistros:editar", args=[sinistro.pk]),
        _dados(sinistro, data="2026-07-12", cliente=bruna.pk),
    )
    sinistro.refresh_from_db()
    assert sinistro.cliente == bruna


def test_novo_em_blocos_com_rotulos_simples(usuario_logado, veiculo):
    html = usuario_logado.get(reverse("sinistros:novo")).content.decode()
    for titulo in ("O que aconteceu", "Acionou a proteção?", "Quem paga"):
        assert titulo in html
    assert "Associado (nosso condutor)" not in html
    assert "Só o nosso carro (associado)" in html
    assert 'x-show="protecao"' in html  # franquia só aparece com a caixa marcada
    assert reverse("sinistros:quem_estava") in html


def test_novo_continua_preenchendo_motorista_pela_data(usuario_logado, alocacoes, veiculo, arlen):
    resposta = usuario_logado.post(
        reverse("sinistros:novo"),
        {
            "veiculo": veiculo.pk,
            "data": "2026-07-10",
            "tipo": "colisao",
            "envolvido": "associado",
            "responsabilidade": "indefinida",
            "responsavel_custo": "empresa",
            "status": "aberto",
        },
        follow=True,
    )
    assert "motorista: Arlen" in resposta.content.decode()
    assert Sinistro.objects.get().cliente == arlen


def test_quem_estava_mostra_o_motorista_da_data(usuario_logado, alocacoes, veiculo):
    url = reverse("sinistros:quem_estava")
    html = usuario_logado.get(url, {"veiculo": veiculo.pk, "data": "2026-07-10"}).content.decode()
    assert "estava com" in html and "Arlen" in html
    html = usuario_logado.get(url, {"veiculo": veiculo.pk, "data": "2026-06-01"}).content.decode()
    assert "não estava alugado" in html
    html = usuario_logado.get(url, {"veiculo": "", "data": "lixo"}).content.decode()
    assert "Escolha o carro e a data" in html


def test_lista_tem_editar_e_auxilio_volta_com_filtros(usuario_logado, sinistro):
    lista = reverse("sinistros:lista") + "?status=aberto"
    html = usuario_logado.get(lista).content.decode()
    assert reverse("sinistros:editar", args=[sinistro.pk]) + "?volta=" in html
    resposta = usuario_logado.post(
        reverse("sinistros:solicitar_auxilio", args=[sinistro.pk]),
        {"valor": "1.518,00", "volta": lista},
    )
    assert resposta.url == lista
    assert AuxilioMotorista.objects.get().valor == Decimal("1518.00")


def test_auxilio_pela_pagina_de_editar_volta_para_ela(usuario_logado, sinistro):
    editar = reverse("sinistros:editar", args=[sinistro.pk])
    resposta = usuario_logado.post(
        reverse("sinistros:solicitar_auxilio", args=[sinistro.pk]), {"valor": "", "volta": editar}
    )
    assert resposta.url == editar
    assert "Marcar recebido" in usuario_logado.get(editar).content.decode()


# --- revisão adversarial -------------------------------------------------------


def test_quem_estava_com_veiculo_invalido_nao_da_erro(usuario_logado):
    url = reverse("sinistros:quem_estava")
    resposta = usuario_logado.get(url, {"veiculo": "abc", "data": "2026-07-10"})
    assert resposta.status_code == 200
    assert "Escolha o carro e a data" in resposta.content.decode()


def test_franquia_negativa_e_recusada(usuario_logado, sinistro):
    resposta = usuario_logado.post(
        reverse("sinistros:editar", args=[sinistro.pk]),
        _dados(sinistro, acionou_protecao="on", franquia_valor="-500,00"),
    )
    assert resposta.status_code == 200
    assert "não pode ser negativo" in resposta.content.decode()
    sinistro.refresh_from_db()
    assert sinistro.franquia_valor is None


def test_franquia_invalida_com_caixa_desmarcada_nao_barra_o_salvar(usuario_logado, sinistro):
    # o campo está escondido: um erro nele travaria o salvar sem a pessoa ver por quê
    resposta = usuario_logado.post(
        reverse("sinistros:editar", args=[sinistro.pk]),
        _dados(sinistro, status="concluido", franquia_valor="abc"),
    )
    assert resposta.status_code == 302
    sinistro.refresh_from_db()
    assert sinistro.status == "concluido"
    assert sinistro.franquia_valor is None


def test_data_no_futuro_e_recusada(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        reverse("sinistros:novo"),
        {
            "veiculo": veiculo.pk,
            # ano plausível: um ano absurdo cai antes no "Confira o ano"
            "data": (date.today() + timedelta(days=30)).isoformat(),
            "tipo": "colisao",
            "envolvido": "associado",
            "responsabilidade": "indefinida",
            "responsavel_custo": "empresa",
            "status": "aberto",
        },
    )
    assert resposta.status_code == 200
    assert "não pode ser no futuro" in resposta.content.decode()
    assert not Sinistro.objects.exists()


def test_pedido_de_auxilio_repetido_nao_duplica(usuario_logado, sinistro):
    url = reverse("sinistros:solicitar_auxilio", args=[sinistro.pk])
    usuario_logado.post(url, {"valor": "1.518,00"})
    usuario_logado.post(url, {"valor": "1.518,00"})
    assert AuxilioMotorista.objects.count() == 1


def test_auxilio_so_para_colisao_e_sem_valor_negativo(usuario_logado, sinistro):
    url = reverse("sinistros:solicitar_auxilio", args=[sinistro.pk])
    usuario_logado.post(url, {"valor": "-1.518,00"})
    assert not AuxilioMotorista.objects.exists()
    Sinistro.objects.filter(pk=sinistro.pk).update(tipo="roubo")
    usuario_logado.post(url, {"valor": "1.518,00"})
    assert not AuxilioMotorista.objects.exists()


def test_receber_auxilio_repetido_nao_muda_data_nem_valor(usuario_logado, sinistro):
    auxilio = AuxilioMotorista.objects.create(
        sinistro=sinistro,
        valor=Decimal("1518.00"),
        status=AuxilioMotorista.Status.RECEBIDO,
        data_recebimento=date(2026, 8, 1),
    )
    usuario_logado.post(
        reverse("sinistros:receber_auxilio", args=[auxilio.pk]), {"valor": "9.999,00"}
    )
    auxilio.refresh_from_db()
    assert auxilio.valor == Decimal("1518.00")
    assert auxilio.data_recebimento == date(2026, 8, 1)


def test_receber_sem_valor_nenhum_pede_o_valor(usuario_logado, sinistro):
    auxilio = AuxilioMotorista.objects.create(
        sinistro=sinistro, status=AuxilioMotorista.Status.SOLICITADO
    )
    resposta = usuario_logado.post(
        reverse("sinistros:receber_auxilio", args=[auxilio.pk]), {"valor": ""}, follow=True
    )
    assert "Informe o valor recebido" in resposta.content.decode()
    auxilio.refresh_from_db()
    assert auxilio.status == AuxilioMotorista.Status.SOLICITADO


def test_editar_exige_login(client, sinistro):
    resposta = client.get(reverse("sinistros:editar", args=[sinistro.pk]))
    assert resposta.status_code == 302
    assert resposta.url.startswith(reverse("login"))
