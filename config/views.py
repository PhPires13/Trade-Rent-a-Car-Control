from datetime import date, timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.static import serve

from apps.alocacoes.models import TrocaTemporaria
from apps.busca import (
    MINIMO_DIGITOS,
    nome_casa,
    numero_casa,
    parece_numero,
    sem_acento,
    so_digitos,
    so_letras_e_digitos,
)
from apps.financeiro.devedores import resumo_do_dia
from apps.financeiro.models import ZERO, Cobranca
from apps.frota.alertas import vigencias_a_vencer
from apps.frota.desmobilizacao import ranking_da_frota
from apps.frota.models import Veiculo, normalizar_placa
from apps.km.models import veiculos_com_leitura_pendente
from apps.manutencao.services import preventivas_em_alerta
from apps.multas.models import Multa
from apps.multas.services import alertas_fici
from apps.pessoas.models import Cliente, CondutorAutorizado
from apps.sinistros.models import Sinistro


def midia(request, caminho):
    """Serve fotos e documentos enviados — SEMPRE atrás do login.

    O LoginRequiredMiddleware cobre esta view como qualquer outra; CNH e fotos
    de clientes nunca podem virar URL pública (por isso não usamos static()).
    """
    return serve(request, caminho, document_root=settings.MEDIA_ROOT)


#: Por grupo; com mais que isso a tela pede para refinar a busca.
LIMITE_BUSCA = 20


def _buscar_clientes(texto, digitos):
    """Clientes por nome, CPF/CNPJ, telefone ou nome/CPF de condutor autorizado.

    Devolve [{"cliente", "motivo"}] — o motivo diz por que apareceu quando não
    foi pelo nome ("condutor autorizado: Zé"), e os condutores sem cliente.
    """
    achados = {}
    for cliente in Cliente.objects.order_by("nome"):
        if nome_casa(texto, cliente.nome):
            achados[cliente.pk] = {"cliente": cliente, "motivo": ""}
        elif digitos and numero_casa(digitos, cliente.cpf_cnpj):
            achados[cliente.pk] = {"cliente": cliente, "motivo": "CPF/CNPJ"}
        elif digitos and numero_casa(digitos, cliente.telefone):
            achados[cliente.pk] = {"cliente": cliente, "motivo": "telefone"}
    sem_cliente = []
    for condutor in CondutorAutorizado.objects.select_related("cliente"):
        casou = nome_casa(texto, condutor.nome) or (
            digitos
            and (numero_casa(digitos, condutor.cpf) or numero_casa(digitos, condutor.contato))
        )
        if not casou:
            continue
        if condutor.cliente is None:
            sem_cliente.append(condutor)
        elif condutor.cliente_id not in achados:
            achados[condutor.cliente_id] = {
                "cliente": condutor.cliente,
                "motivo": f"condutor autorizado: {condutor.nome}",
            }
    clientes = sorted(achados.values(), key=lambda a: sem_acento(a["cliente"].nome))
    return clientes[:LIMITE_BUSCA], sem_cliente[:LIMITE_BUSCA]


def _buscar_por_codigo(modelo, campos, codigo, relacionados):
    """Registros cujo número (AIT, BO...) contém o código, ignorando pontuação."""
    vazio = Q()
    for campo in campos:
        vazio &= Q(**{campo: ""})
    achados = []
    for registro in modelo.objects.exclude(vazio).select_related(*relacionados):
        if any(codigo in so_letras_e_digitos(getattr(registro, campo)) for campo in campos):
            achados.append(registro)
            if len(achados) == LIMITE_BUSCA:
                break
    return achados


def buscar(request):
    """Busca global da barra do topo (proposta #18).

    Ignora acento e maiúscula; acha carro (placa/modelo), cliente (nome,
    CPF/CNPJ, telefone, condutor autorizado), multa (AIT/nº de processamento)
    e sinistro (nº do BO). Um único resultado vai direto para a ficha.
    """
    consulta = request.GET.get("q", "").strip()
    veiculos, clientes, condutores, multas, sinistros = [], [], [], [], []
    if consulta:
        texto = sem_acento(consulta)
        digitos = so_digitos(consulta) if parece_numero(consulta) else ""
        # "-" ou " - " viram placa vazia, e placa vazia acharia a frota inteira
        placa = normalizar_placa(consulta)
        veiculos = [
            v
            for v in Veiculo.objects.order_by("placa")
            if (placa and placa in v.placa) or nome_casa(texto, v.marca_modelo)
        ][:LIMITE_BUSCA]
        clientes, condutores = _buscar_clientes(texto, digitos)
        codigo = so_letras_e_digitos(consulta)
        # AIT/BO sempre têm número; sem isso um nome ("joao") varreria multas à toa
        if len(so_digitos(codigo)) >= MINIMO_DIGITOS:
            multas = _buscar_por_codigo(
                Multa, ["ait", "num_processamento"], codigo, ["veiculo", "cliente"]
            )
            sinistros = _buscar_por_codigo(
                Sinistro, ["boletim_ocorrencia"], codigo, ["veiculo", "cliente"]
            )
        destinos = (
            [reverse("frota:detalhe", args=[v.pk]) for v in veiculos]
            + [reverse("pessoas:detalhe", args=[c["cliente"].pk]) for c in clientes]
            + [reverse("pessoas:condutor_editar", args=[c.pk]) for c in condutores]
            + [reverse("multas:editar", args=[m.pk]) for m in multas]
            + [reverse("sinistros:editar", args=[s.pk]) for s in sinistros]
        )
        if len(destinos) == 1:
            return redirect(destinos[0])
    grupos = [veiculos, clientes, condutores, multas, sinistros]
    return render(
        request,
        "buscar.html",
        {
            "consulta": consulta,
            "veiculos": veiculos,
            "clientes": clientes,
            "condutores": condutores,
            "multas": multas,
            "sinistros": sinistros,
            "total": sum(len(g) for g in grupos),
            "cortado": any(len(g) == LIMITE_BUSCA for g in grupos),
        },
    )


def painel(request):
    """Painel inicial — dinheiro, frota e alertas do dia (docs.md §5).

    Cada bloco consulta em lote: o número de queries não acompanha o tamanho
    da frota nem o de cobranças (config/tests.py mede os dois).
    """
    hoje = date.today()
    veiculos = Veiculo.objects.exclude(status=Veiculo.Status.VENDIDO)
    status_no_painel = [
        (valor, rotulo)
        for valor, rotulo in Veiculo.Status.choices
        if valor != Veiculo.Status.VENDIDO
    ]
    locacao = Q(uso=Veiculo.Uso.LOCACAO)
    contagem = veiculos.aggregate(
        total=Count("pk"),
        locacao=Count("pk", filter=locacao),
        alugados=Count("pk", filter=locacao & Q(status=Veiculo.Status.ALOCADO)),
        **{f"status_{valor}": Count("pk", filter=Q(status=valor)) for valor, _ in status_no_painel},
    )

    semana = (hoje - timedelta(days=hoje.weekday()), hoje + timedelta(days=6 - hoje.weekday()))
    da_semana = list(
        Cobranca.com_quitacao_anotada(
            Cobranca.objects.filter(vencimento__range=semana).exclude(
                status=Cobranca.Status.CANCELADA
            )
        )
    )
    atrasadas = Cobranca.com_quitacao_anotada(
        Cobranca.objects.filter(status=Cobranca.Status.ATRASADO)
    )
    semana_total = sum((c.valor for c in da_semana), ZERO)
    semana_recebido = sum((c.total_quitado for c in da_semana), ZERO)

    # prefetch das manutenções: auxilio_disponivel lê dias_parado de cada
    # sinistro, o que consultava uma vez por sinistro aberto
    sinistros_abertos = (
        Sinistro.objects.exclude(status=Sinistro.Status.CONCLUIDO)
        .select_related("veiculo")
        .prefetch_related("manutencoes")
    )
    fichas, _ = ranking_da_frota(hoje)
    contexto = {
        "hoje": hoje,
        "total_veiculos": contagem["total"],
        # (valor, rótulo, quantidade) — o valor vira o filtro do card no hub da frota
        "veiculos_por_status": [
            (valor, rotulo, contagem[f"status_{valor}"]) for valor, rotulo in status_no_painel
        ],
        "ocupacao": (
            round(100 * contagem["alugados"] / contagem["locacao"]) if contagem["locacao"] else None
        ),
        "trocas_em_andamento": TrocaTemporaria.objects.filter(
            data_devolucao__isnull=True
        ).select_related("alocacao__cliente", "veiculo_substituto"),
        "leituras_pendentes": veiculos_com_leitura_pendente(hoje),
        "alertas_preventivas": preventivas_em_alerta(),
        "semana_total": semana_total,
        "semana_recebido": semana_recebido,
        "percentual_semana": (
            min(100, round(100 * semana_recebido / semana_total)) if semana_total else 0
        ),
        "atrasado_total": sum((c.saldo for c in atrasadas), ZERO),
        "inadimplentes": Cliente.objects.filter(status=Cliente.Status.INADIMPLENTE),
        "nds_abertas": Cobranca.objects.filter(
            origem=Cobranca.Origem.NOTA_DEBITO, status__in=Cobranca.STATUS_EM_ABERTO
        ).count(),
        "fici_em_alerta": alertas_fici(hoje),
        "auxilios_disponiveis": [s for s in sinistros_abertos if s.auxilio_disponivel],
        "candidatos_venda": [f for f in fichas if f.nivel in ("preparar", "vender")],
        "vigencias": vigencias_a_vencer(hoje),
        # bloco "Hoje": quem paga hoje e quem está devendo (proposta #8)
        **resumo_do_dia(hoje),
    }
    return render(request, "painel.html", contexto)
