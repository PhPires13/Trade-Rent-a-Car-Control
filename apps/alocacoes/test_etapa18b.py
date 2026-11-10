"""Etapa 18b — achados dos testes no navegador (alocações).

Franquia "5.000", cliente que desiste com o carro na oficina, KM da vistoria de
entrega, KM da retirada errado, ND da avaria, CNH vencida, caução menor,
vistoria depois da troca, contrato no celular e cartão de entrega antigo.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.alocacoes.entrega import passos_da_entrega
from apps.alocacoes.km_aviso import suspeita_de_km
from apps.alocacoes.models import Alocacao, TrocaTemporaria, Vistoria
from apps.financeiro import services as financeiro
from apps.financeiro.models import ItemNotaDebito, NotaDebito
from apps.frota.models import Veiculo
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="SHN2E34", marca_modelo="Gol", km_atual=50_000)


@pytest.fixture
def substituto(db):
    return Veiculo.objects.create(placa="RBC5F67", marca_modelo="Onix", km_atual=143_140)


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Diego Lima", cpf_cnpj="529.982.247-25")


@pytest.fixture
def alocacao(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=20),
        valor_semanal=Decimal("650.00"),
        km_entrega=50_000,
    )


@pytest.fixture
def troca(alocacao, substituto):
    return TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE - timedelta(days=10),
        km_retirada=143_140,
    )


def _oficina(veiculo, dias=10):
    return Manutencao.objects.create(
        veiculo=veiculo,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=dias),
        data_entrada=HOJE - timedelta(days=dias),
        descricao="Funilaria",
    )


def _dados_nova(veiculo, cliente, **extra):
    dados = {
        "veiculo": veiculo.pk,
        "cliente": cliente.pk,
        "data_inicio": HOJE.isoformat(),
        "valor_semanal": "650,00",
        "km_entrega": "50.000",
        "limite_km": "limitado",
        "franquia_km_mensal": "3.000",
        "taxa_km_excedido": "0,50",
        "token": "t-nova",
    }
    dados.update(extra)
    return dados


# --- Franquia mensal "3.000" virava 3 km (crítico) ---------------------------


def test_nova_alocacao_grava_franquia_com_ponto_de_milhar(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.post("/alocacoes/nova/", _dados_nova(veiculo, cliente))
    assert resposta.status_code == 302
    assert Alocacao.objects.get().franquia_km_mensal == 3_000


def test_nova_alocacao_recusa_franquia_baixa_demais(usuario_logado, veiculo, cliente):
    resposta = usuario_logado.post(
        "/alocacoes/nova/", _dados_nova(veiculo, cliente, franquia_km_mensal="3")
    )
    assert resposta.status_code == 200
    assert "baixa demais" in resposta.text
    assert not Alocacao.objects.exists()


def test_editar_grava_franquia_5000_e_nao_5(usuario_logado, alocacao):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/",
        {
            "valor_semanal": "650,00",
            "dia_vencimento": alocacao.dia_vencimento,
            "caucao_valor": "",
            "limite_km": "limitado",
            "franquia_km_mensal": "5.000",
            "taxa_km_excedido": "0,50",
            "observacoes": "",
        },
    )
    assert resposta.status_code == 302
    alocacao.refresh_from_db()
    assert alocacao.franquia_km_mensal == 5_000


def test_admin_grava_franquia_com_ponto_de_milhar(alocacao):
    from apps.alocacoes.admin import AlocacaoAdminForm

    campo = AlocacaoAdminForm.base_fields["franquia_km_mensal"]
    assert campo.clean("35.000") == 35_000


# --- Cliente desiste com o carro principal na oficina (alta) ------------------


def test_encerrar_com_substituto_devolve_junto_e_nao_mexe_na_oficina(
    usuario_logado, alocacao, troca, veiculo, substituto
):
    oficina = _oficina(veiculo)
    tela = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/")
    assert "Antes de encerrar, devolva" not in tela.text
    assert "continua na oficina" in tela.text
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "143.900", "troca": troca.pk},
    )
    assert resposta.status_code == 302
    alocacao.refresh_from_db()
    troca.refresh_from_db()
    oficina.refresh_from_db()
    veiculo.refresh_from_db()
    substituto.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ENCERRADA
    # o principal não rodou: fica com o KM que tinha; a oficina segue aberta
    assert alocacao.km_devolucao == 50_000
    assert oficina.data_saida is None
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO
    assert troca.data_devolucao == HOJE and troca.km_devolucao == 143_900
    assert substituto.status == Veiculo.Status.DISPONIVEL
    assert substituto.km_atual == 143_900


def test_encerrar_com_substituto_pede_o_campo_da_troca(usuario_logado, alocacao, troca):
    # tela antiga (sem o campo da troca): não encerra com o KM do carro errado
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "143.900"},
        follow=True,
    )
    alocacao.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ATIVA
    assert "confira o KM dele" in resposta.text


def test_encerrar_com_substituto_erro_nao_grava_nada(usuario_logado, alocacao, troca):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "100", "troca": troca.pk},
    )
    assert resposta.status_code == 200
    troca.refresh_from_db()
    assert troca.data_devolucao is None


def test_devolver_substituto_oferece_encerrar_quando_o_principal_esta_na_oficina(
    usuario_logado, alocacao, troca, veiculo
):
    _oficina(veiculo)
    texto = usuario_logado.get(f"/alocacoes/troca/{troca.pk}/devolver/").text
    assert f"/alocacoes/{alocacao.pk}/encerrar/" in texto
    assert "devolvendo de vez" in texto


# --- KM da retirada do substituto errado (média) ------------------------------


def test_troca_mostra_o_erro_de_km_junto_do_campo(usuario_logado, alocacao, substituto):
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/troca/",
        {
            "veiculo_substituto": substituto.pk,
            "data_retirada": HOJE.isoformat(),
            "km_retirada": "14.314",
            "observacoes": "",
        },
    )
    assert resposta.status_code == 200
    form = resposta.context["form"]
    assert "Você digitou 14.314 km" in " ".join(form.errors["km_retirada"])
    html = resposta.text
    # a caixa "Confirmo o KM" vem logo depois do campo, antes de Observações
    assert html.index('name="confirmar_km"') < html.index('name="motivo"')


def test_devolucao_corrige_o_km_da_retirada(usuario_logado, alocacao, substituto):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=HOJE,
        km_retirada=14_314,  # era 143.140 (confirmado errado)
    )
    url = f"/alocacoes/troca/{troca.pk}/devolver/"
    resposta = usuario_logado.post(
        url, {"data_devolucao": HOJE.isoformat(), "km_devolucao": "143.200"}
    )
    assert resposta.status_code == 200
    assert "O KM da retirada (14.314) parece errado" in resposta.text
    assert 'name="km_retirada"' in resposta.text
    resposta = usuario_logado.post(
        url,
        {"data_devolucao": HOJE.isoformat(), "km_devolucao": "143.200", "km_retirada": "143.140"},
        follow=True,
    )
    troca.refresh_from_db()
    assert troca.km_retirada == 143_140
    assert troca.km_devolucao == 143_200
    assert "KM da retirada do RBC5F67 corrigido" in resposta.text


def test_km_da_retirada_corrigido_nao_pode_passar_da_devolucao(usuario_logado, troca):
    usuario_logado.post(
        f"/alocacoes/troca/{troca.pk}/devolver/",
        {"data_devolucao": HOJE.isoformat(), "km_devolucao": "143.200", "km_retirada": "150.000"},
    )
    troca.refresh_from_db()
    assert troca.data_devolucao is None
    assert troca.km_retirada == 143_140


def test_aviso_de_km_no_mesmo_dia_nao_fala_por_dia():
    aviso = suspeita_de_km(143_200, HOJE, 14_314, HOJE, "na retirada")
    assert "por dia" not in aviso
    aviso = suspeita_de_km(143_200, HOJE, 14_314, HOJE - timedelta(days=5), "na retirada")
    assert "por dia" in aviso


# --- KM da vistoria de entrega corrige o KM de partida (média) ---------------


def _vistoria(usuario_logado, alocacao, veiculo, km, tipo="entrada", token="tv"):
    return usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {
            "veiculo": veiculo.pk,
            "tipo": tipo,
            "data": HOJE.isoformat(),
            "km": km,
            "token": token,
        },
        follow=True,
    )


def test_vistoria_de_entrega_ajusta_o_km_da_entrega(usuario_logado, veiculo, cliente):
    alocacao = Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE,
        valor_semanal=Decimal("650.00"),
        km_entrega=105_380,
    )
    resposta = _vistoria(usuario_logado, alocacao, veiculo, "105.400")
    alocacao.refresh_from_db()
    assert alocacao.km_entrega == 105_400
    assert "KM de entrega do contrato ajustado para 105.400" in resposta.text


def test_vistoria_de_entrega_antiga_nao_mexe_no_km_da_entrega(usuario_logado, alocacao, veiculo):
    # entrega há 20 dias: a vistoria de "entrega" de hoje é outra coisa
    _vistoria(usuario_logado, alocacao, veiculo, "50.500")
    alocacao.refresh_from_db()
    assert alocacao.km_entrega == 50_000


def test_vistoria_de_entrega_do_substituto_ajusta_a_retirada(usuario_logado, alocacao, substituto):
    troca = TrocaTemporaria.objects.create(
        alocacao=alocacao, veiculo_substituto=substituto, data_retirada=HOJE, km_retirada=143_140
    )
    resposta = _vistoria(usuario_logado, alocacao, substituto, "143.150")
    troca.refresh_from_db()
    assert troca.km_retirada == 143_150
    assert "KM da retirada do substituto RBC5F67 ajustado" in resposta.text


# --- ND da avaria já emitida (baixa) ------------------------------------------


def test_encerrar_troca_o_aviso_da_avaria_pela_nd_emitida(usuario_logado, alocacao, veiculo):
    Vistoria.objects.create(
        alocacao=alocacao, veiculo=veiculo, tipo="entrada", data=alocacao.data_inicio, km=50_000
    )
    Vistoria.objects.create(
        alocacao=alocacao,
        veiculo=veiculo,
        tipo="saida",
        data=HOJE,
        km=51_000,
        avarias="Risco na porta",
    )
    url = f"/alocacoes/{alocacao.pk}/encerrar/"
    assert "data-marca-nova" in usuario_logado.get(url).text
    nota = NotaDebito.objects.create(cliente=alocacao.cliente, data_emissao=HOJE)
    ItemNotaDebito.objects.create(nota_debito=nota, descricao="Avaria", valor=Decimal("350"))
    texto = usuario_logado.get(url).text
    assert "data-marca-nova" not in texto
    assert f"ND {nota.numero:03d} emitida" in texto


# --- KM já confirmado na vistoria de devolução (baixa) ------------------------


def test_encerrar_nao_pede_de_novo_o_km_confirmado_na_vistoria(usuario_logado, alocacao, veiculo):
    Vistoria.objects.create(alocacao=alocacao, veiculo=veiculo, tipo="saida", data=HOJE, km=80_000)
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "80.000"},
    )
    assert resposta.status_code == 302
    alocacao.refresh_from_db()
    assert alocacao.status == Alocacao.Status.ENCERRADA


def test_encerrar_ainda_pede_confirmacao_de_km_diferente_da_vistoria(
    usuario_logado, alocacao, veiculo
):
    Vistoria.objects.create(alocacao=alocacao, veiculo=veiculo, tipo="saida", data=HOJE, km=80_000)
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/encerrar/",
        {"data_termino": HOJE.isoformat(), "km_devolucao": "90.000"},
    )
    assert resposta.status_code == 200
    assert 'name="confirmar_km"' in resposta.text


# --- CNH vencida avisada ao escolher o cliente (baixa) -----------------------


def test_nova_alocacao_avisa_cnh_vencida_ao_escolher(usuario_logado, veiculo, cliente):
    cliente.cnh_validade = HOJE - timedelta(days=26)
    cliente.save()
    resposta = usuario_logado.get("/alocacoes/nova/")
    dados = resposta.context["dados_clientes"][str(cliente.pk)]
    assert dados["cnh"].startswith(f"CNH vencida desde {cliente.cnh_validade:%d/%m/%Y}")
    assert "data-aviso-cnh" in resposta.text


# --- Caução combinada menor que a depositada (baixa) --------------------------


def test_editar_caucao_menor_que_a_depositada_pede_confirmacao(usuario_logado, alocacao):
    alocacao.caucao_valor = Decimal("600")
    alocacao.save()
    financeiro.abrir_caucao(alocacao, Decimal("600.00"), HOJE, "pix")
    dados = {
        "valor_semanal": "650,00",
        "dia_vencimento": alocacao.dia_vencimento,
        "caucao_valor": "400,00",
        "limite_km": "ilimitado",
        "franquia_km_mensal": "",
        "taxa_km_excedido": "",
        "observacoes": "",
    }
    url = f"/alocacoes/{alocacao.pk}/editar/"
    resposta = usuario_logado.post(url, dados)
    assert resposta.status_code == 200
    assert "Já foram depositados R$ 600,00" in resposta.text
    assert 'name="confirmar_caucao"' in resposta.text
    resposta = usuario_logado.post(url, {**dados, "confirmar_caucao": "1"})
    assert resposta.status_code == 302
    alocacao.refresh_from_db()
    assert alocacao.caucao_valor == Decimal("400")


# --- Vistoria depois de devolver o substituto (baixa) -------------------------


def test_vistoria_mostra_substituto_devolvido_com_a_data(usuario_logado, alocacao, troca):
    troca.devolver(data_devolucao=HOJE - timedelta(days=2), km_devolucao=143_500)
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/nova/")
    texto = resposta.text
    assert f"(substituto até {HOJE - timedelta(days=2):%d/%m})" in texto
    assert "Com carro substituto, vistorie" not in texto
    # na devolução, só o carro que está com o motorista
    resposta = usuario_logado.get(f"/alocacoes/{alocacao.pk}/vistoria/nova/?tipo=saida")
    placas = [v.placa for v in resposta.context["form"].fields["veiculo"].queryset]
    assert placas == ["SHN2E34"]


# --- Contrato no celular e cartão de entrega antigo (baixa/média) -------------


def test_contrato_quebra_os_brancos_no_celular(usuario_logado, alocacao):
    texto = usuario_logado.get(f"/alocacoes/{alocacao.pk}/contrato/").text
    assert "overflow-wrap: anywhere" in texto


def test_cartao_da_entrega_nao_aparece_em_alocacao_antiga(alocacao):
    alocacao.data_inicio = HOJE - timedelta(days=30)
    alocacao.save()
    assert passos_da_entrega(alocacao) is None


def test_cartao_da_entrega_nao_aparece_em_contrato_que_ja_rodava(alocacao):
    alocacao.data_inicio = HOJE - timedelta(days=3)
    alocacao.cobrar_a_partir_de = HOJE
    alocacao.save()
    assert passos_da_entrega(alocacao) is None


def test_cartao_da_entrega_aparece_em_entrega_recente(alocacao):
    alocacao.data_inicio = HOJE - timedelta(days=3)
    alocacao.save()
    assert passos_da_entrega(alocacao)


# --- Revisão adversarial (etapa 18b) ------------------------------------------


def test_franquia_escondida_no_ilimitado_nao_trava_a_nova_alocacao(
    usuario_logado, veiculo, cliente
):
    # digitou "3" e depois mudou para ilimitado: o campo some da tela, o erro não
    # apareceria e a pessoa ficaria presa no formulário
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        _dados_nova(veiculo, cliente, limite_km="ilimitado", franquia_km_mensal="3"),
    )
    assert resposta.status_code == 302
    assert Alocacao.objects.get().limite_km == Alocacao.LimiteKm.ILIMITADO


def test_admin_recusa_franquia_baixa_so_quando_limitado(alocacao):
    from django.forms import modelform_factory

    from apps.alocacoes.admin import AlocacaoAdminForm

    Formulario = modelform_factory(
        Alocacao,
        form=AlocacaoAdminForm,
        fields=[
            "valor_semanal",
            "dia_vencimento",
            "limite_km",
            "franquia_km_mensal",
            "taxa_km_excedido",
        ],
    )

    def form(limite, franquia):
        return Formulario(
            instance=alocacao,
            data={
                "valor_semanal": "650.00",
                "dia_vencimento": alocacao.dia_vencimento,
                "limite_km": limite,
                "franquia_km_mensal": franquia,
                "taxa_km_excedido": "0.50",
            },
        )

    limitado = form("limitado", "5")
    assert not limitado.is_valid()
    assert "baixa demais" in str(limitado.errors["franquia_km_mensal"])
    assert "franquia_km_mensal" not in form("ilimitado", "5").errors


def test_caucao_menor_confirmada_continua_marcada_se_outro_campo_der_erro(usuario_logado, alocacao):
    alocacao.caucao_valor = Decimal("600")
    alocacao.save()
    financeiro.abrir_caucao(alocacao, Decimal("600.00"), HOJE, "pix")
    resposta = usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/editar/",
        {
            "valor_semanal": "0",  # erro em outro campo
            "dia_vencimento": alocacao.dia_vencimento,
            "caucao_valor": "400,00",
            "limite_km": "ilimitado",
            "franquia_km_mensal": "",
            "taxa_km_excedido": "",
            "observacoes": "",
            "confirmar_caucao": "1",
        },
    )
    assert resposta.status_code == 200
    assert "Já foram depositados" not in resposta.text
    assert 'name="confirmar_caucao" value="1" checked' in resposta.text


def test_nd_so_de_multa_nao_esconde_o_convite_da_avaria(usuario_logado, alocacao, veiculo):
    from apps.multas.models import Multa

    Vistoria.objects.create(
        alocacao=alocacao, veiculo=veiculo, tipo="entrada", data=alocacao.data_inicio, km=50_000
    )
    Vistoria.objects.create(
        alocacao=alocacao,
        veiculo=veiculo,
        tipo="saida",
        data=HOJE,
        km=51_000,
        avarias="Risco na porta",
    )
    multa = Multa.objects.create(
        veiculo=veiculo, data_infracao=HOJE - timedelta(days=5), valor=Decimal("130.16")
    )
    nota = NotaDebito.objects.create(cliente=alocacao.cliente, data_emissao=HOJE)
    ItemNotaDebito.objects.create(
        nota_debito=nota, descricao="Multa", valor=Decimal("130.16"), multa=multa
    )
    texto = usuario_logado.get(f"/alocacoes/{alocacao.pk}/encerrar/").text
    assert "data-marca-nova" in texto
    assert "data-nd-emitida" not in texto


def _alocacao_de_hoje(veiculo, cliente, km_entrega=105_400):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE,
        valor_semanal=Decimal("650.00"),
        km_entrega=km_entrega,
    )


def _vistoria_confirmada(usuario_logado, alocacao, veiculo, km):
    return usuario_logado.post(
        f"/alocacoes/{alocacao.pk}/vistoria/nova/",
        {
            "veiculo": veiculo.pk,
            "tipo": "entrada",
            "data": HOJE.isoformat(),
            "km": km,
            "confirmar_km": "on",
            "token": "tvc",
        },
        follow=True,
    )


def test_vistoria_de_entrega_com_km_menor_baixa_a_entrega_e_o_carro(
    usuario_logado, veiculo, cliente
):
    alocacao = _alocacao_de_hoje(veiculo, cliente)
    resposta = _vistoria_confirmada(usuario_logado, alocacao, veiculo, "105.380")
    alocacao.refresh_from_db()
    veiculo.refresh_from_db()
    assert alocacao.km_entrega == 105_380
    assert veiculo.km_atual == 105_380
    assert "ajustado para 105.380 (estava 105.400)" in resposta.text


def test_vistoria_com_digito_faltando_nao_baixa_a_entrega_abaixo_do_que_o_carro_ja_teve(
    usuario_logado, veiculo, cliente
):
    # a pessoa confirmou 10.540 (faltou um dígito), mas o carro foi comprado com
    # 100.000 km: o contrato não pode partir de 10.540 (cobraria 95 mil km a mais)
    veiculo.km_compra = 100_000
    veiculo.save()
    alocacao = _alocacao_de_hoje(veiculo, cliente)
    resposta = _vistoria_confirmada(usuario_logado, alocacao, veiculo, "10.540")
    alocacao.refresh_from_db()
    veiculo.refresh_from_db()
    assert alocacao.km_entrega == 105_400
    assert veiculo.km_atual == 105_400
    assert "KM de entrega do contrato continua 105.400" in resposta.text
    assert Vistoria.objects.filter(km=10_540).exists()  # a vistoria em si fica gravada


def test_vistoria_de_entrega_depois_da_leitura_mensal_nao_mexe_na_entrega(
    usuario_logado, veiculo, cliente
):
    from apps.km.models import RegistroKm

    alocacao = _alocacao_de_hoje(veiculo, cliente)
    RegistroKm.objects.create(
        veiculo=veiculo, mes_referencia=HOJE.replace(day=1), data_leitura=HOJE, km=105_450
    )
    resposta = _vistoria(usuario_logado, alocacao, veiculo, "105.420")
    alocacao.refresh_from_db()
    assert alocacao.km_entrega == 105_400
    assert "ajustado" not in resposta.text
