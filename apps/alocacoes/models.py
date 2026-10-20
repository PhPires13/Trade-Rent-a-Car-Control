from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MaxValueValidator
from django.db import models, transaction
from django.db.models import Max
from simple_history.models import HistoricalRecords

from apps.campos import validar_tamanho_arquivo
from apps.formatos import brl, milhar
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

#: Nenhum carro de aplicativo chega perto disso — acima, é dígito a mais (D27).
#: Também evita o erro 500 do Postgres em número maior que o inteiro do banco.
KM_MAXIMO = 2_000_000
VALIDADOR_KM = MaxValueValidator(
    KM_MAXIMO, message=f"KM acima de {milhar(KM_MAXIMO)} — confira os dígitos."
)
#: Foto ou PDF do checklist — .html/.svg rodariam script no navegador do dono (C17).
EXTENSOES_VISTORIA = ["jpg", "jpeg", "png", "webp", "pdf"]


def _validar_data_nao_futura(data, rotulo="devolução"):
    """C02: término/devolução no futuro deixava o ex-cliente sendo cobrado até lá."""
    if data > date.today():
        raise ValidationError(f"A data de {rotulo} não pode ser no futuro.")


def _validar_km_maximo(km):
    if km > KM_MAXIMO:
        raise ValidationError(f"KM acima de {milhar(KM_MAXIMO)} — confira os dígitos.")


def ultima_liberacao_do_carro(veiculo, excluir_alocacao=None, excluir_troca=None):
    """Última data em que o carro voltou de um motorista (término ou fim de troca).

    C13: nova alocação/troca não pode começar antes disso — senão multas e
    sinistros desses dias vão para o motorista errado (cliente_vigente).
    """
    alocacoes = Alocacao.objects.filter(veiculo=veiculo)
    trocas = TrocaTemporaria.objects.filter(veiculo_substituto=veiculo)
    if excluir_alocacao is not None:
        alocacoes = alocacoes.exclude(pk=excluir_alocacao)
    if excluir_troca is not None:
        trocas = trocas.exclude(pk=excluir_troca)
    datas = [
        alocacoes.aggregate(m=Max("data_termino"))["m"],
        trocas.aggregate(m=Max("data_devolucao"))["m"],
    ]
    datas = [d for d in datas if d]
    return max(datas) if datas else None


class Alocacao(models.Model):
    """Vínculo veículo↔cliente com valor semanal (docs.md §4.2).

    `caucao_valor` é só o valor acordado em contrato; o dinheiro de fato
    (depósito, descontos, devolução) fica em financeiro.Caucao (docs.md §4.4).
    """

    class Status(models.TextChoices):
        ATIVA = "ativa", "Ativa"
        ENCERRADA = "encerrada", "Encerrada"

    class LimiteKm(models.TextChoices):
        ILIMITADO = "ilimitado", "Ilimitado"
        LIMITADO = "limitado", "Limitado"

    veiculo = models.ForeignKey(
        Veiculo, verbose_name="veículo", on_delete=models.PROTECT, related_name="alocacoes"
    )
    cliente = models.ForeignKey(
        Cliente, verbose_name="cliente", on_delete=models.PROTECT, related_name="alocacoes"
    )
    data_inicio = models.DateField("data de início")
    data_termino = models.DateField("data de término", null=True, blank=True)
    cobrar_a_partir_de = models.DateField(
        "cobrar aluguel a partir de",
        null=True,
        blank=True,
        help_text="Só para contrato que já rodava antes do sistema: as semanas antes "
        "desta data não são lançadas (já foram acertadas por fora). "
        "Vazio = cobra desde a data de início.",
    )
    valor_semanal = models.DecimalField("valor semanal (R$)", max_digits=8, decimal_places=2)
    dia_vencimento = models.IntegerField(
        "dia de vencimento semanal",
        choices=Cliente.DiaSemana.choices,
        help_text="Padrão: dia da semana do início da locação",
    )
    caucao_valor = models.DecimalField(
        "caução acordada (R$)", max_digits=8, decimal_places=2, null=True, blank=True
    )
    km_entrega = models.PositiveIntegerField("KM na entrega", validators=[VALIDADOR_KM])
    km_devolucao = models.PositiveIntegerField(
        "KM na devolução", null=True, blank=True, validators=[VALIDADOR_KM]
    )
    limite_km = models.CharField(
        "limite de km", max_length=10, choices=LimiteKm.choices, default=LimiteKm.ILIMITADO
    )
    franquia_km_mensal = models.PositiveIntegerField(
        "franquia mensal (km)", null=True, blank=True, help_text="Somente quando limitado"
    )
    taxa_km_excedido = models.DecimalField(
        "taxa por km excedido (R$)", max_digits=6, decimal_places=2, null=True, blank=True
    )
    status = models.CharField("status", max_length=10, choices=Status.choices, default=Status.ATIVA)
    observacoes = models.TextField("observações", blank=True)
    # Cartão "Próximos passos da entrega" na ficha do cliente: o contrato se marca
    # sozinho ao ser aberto para imprimir; o cartão some ao concluir ou dispensar.
    contrato_aberto_em = models.DateField("contrato aberto para imprimir em", null=True, blank=True)
    passos_entrega_dispensados = models.BooleanField(
        "próximos passos da entrega dispensados", default=False
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = "alocação"
        verbose_name_plural = "alocações"
        ordering = ["-data_inicio"]
        constraints = [
            models.UniqueConstraint(
                fields=["veiculo"],
                condition=models.Q(status="ativa"),
                name="uma_alocacao_ativa_por_veiculo",
            )
        ]

    def __str__(self):
        return f"{self.veiculo.placa} → {self.cliente.nome} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if self.dia_vencimento is None and self.data_inicio:
            self.dia_vencimento = self.data_inicio.weekday()
        criando = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if criando and self.status == self.Status.ATIVA:
                self.veiculo.status = Veiculo.Status.ALOCADO
                if self.km_entrega > self.veiculo.km_atual:
                    self.veiculo.km_atual = self.km_entrega
                self.veiculo.save(update_fields=["status", "km_atual"])

    def clean(self):
        erros = {}
        if self._state.adding:
            if self.veiculo_id and self.veiculo.status != Veiculo.Status.DISPONIVEL:
                erros["veiculo"] = (
                    f"Veículo {self.veiculo.placa} não está disponível "
                    f"(status: {self.veiculo.get_status_display()})."
                )
            if self.veiculo_id and self.veiculo.uso != Veiculo.Uso.LOCACAO:
                erros["veiculo"] = "Veículo fora de locação não pode ser alocado."
            if self.cliente_id and self.cliente.status == Cliente.Status.INATIVO:
                erros["cliente"] = "Cliente inativo não pode receber veículo."
            if self.veiculo_id and self.data_inicio:
                liberado = ultima_liberacao_do_carro(self.veiculo, excluir_alocacao=self.pk)
                if liberado and self.data_inicio < liberado:
                    erros["data_inicio"] = (
                        f"O carro {self.veiculo.placa} só voltou do motorista anterior em "
                        f"{liberado:%d/%m/%Y}. A data de início não pode ser antes disso."
                    )
        # D28: valor semanal sempre cobrado; nenhum valor negativo (também no Admin).
        if self.valor_semanal is not None and self.valor_semanal <= 0:
            erros["valor_semanal"] = "O valor semanal precisa ser maior que zero."
        for campo in ("caucao_valor", "taxa_km_excedido"):
            valor = getattr(self, campo)
            if valor is not None and valor < 0:
                erros[campo] = "O valor não pode ser negativo."
        if self.status == self.Status.ENCERRADA:
            # Vale também na edição (Admin): encerrar é pelo fluxo "Encerrar alocação".
            if not self.data_termino:
                erros["data_termino"] = "Alocação encerrada precisa da data de término."
            if self.km_devolucao is None:
                erros["km_devolucao"] = "Alocação encerrada precisa do KM de devolução."
        if self.limite_km == self.LimiteKm.LIMITADO and not self.franquia_km_mensal:
            erros["franquia_km_mensal"] = "Informe a franquia mensal para limite de km."
        cobrar = self.cobrar_a_partir_de
        if cobrar and self.data_inicio and cobrar < self.data_inicio:
            erros["cobrar_a_partir_de"] = (
                f"Não pode ser antes do início da locação ({self.data_inicio:%d/%m/%Y})."
            )
        if erros:
            raise ValidationError(erros)

    @transaction.atomic
    def encerrar(self, data_termino, km_devolucao):
        """Encerra a alocação, libera o veículo e cancela semanas não usadas (docs.md §4.2).

        A cobrança é pré-paga: a semana que começaria no dia do término (ou
        depois) não é devida — se ainda não tem pagamento, é cancelada. O
        acerto de caução é feito na tela da caução (docs.md §4.4).
        """
        if self.status != self.Status.ATIVA:
            raise ValidationError("Alocação já encerrada.")
        if self.trocas.filter(data_devolucao__isnull=True).exists():
            raise ValidationError("Devolva o carro substituto antes de encerrar a alocação.")
        if data_termino < self.data_inicio:
            raise ValidationError(
                f"Data de término anterior ao início da alocação ({self.data_inicio:%d/%m/%Y})."
            )
        _validar_data_nao_futura(data_termino)
        if km_devolucao < self.km_entrega:
            raise ValidationError("KM de devolução menor que o KM de entrega.")
        _validar_km_maximo(km_devolucao)
        self.data_termino = data_termino
        self.km_devolucao = km_devolucao
        self.status = self.Status.ENCERRADA
        self.save()
        self.acerto_semanas_nao_usadas = self._cancelar_semanas_nao_usadas(data_termino)
        # semana cancelada que já estava atrasada não pode deixar o ex-cliente
        # inadimplente até a rotina da madrugada
        from apps.financeiro.services import atualizar_inadimplencia

        atualizar_inadimplencia(self.cliente)
        veiculo = self.veiculo
        if veiculo.status == Veiculo.Status.ALOCADO:
            veiculo.status = Veiculo.Status.DISPONIVEL
        if km_devolucao > veiculo.km_atual:
            veiculo.km_atual = km_devolucao
        veiculo.save(update_fields=["status", "km_atual"])

    def _cancelar_semanas_nao_usadas(self, data_termino):
        """D03: semanas com vencimento a partir da devolução não são devidas.

        Cobrança pré-paga: a semana que começa no dia do término (ou depois) é
        cancelada mesmo que já tenha pagamento. O que foi pago nela volta para
        o cliente — recebimento vira crédito do cliente (como sobra daquele
        pagamento) e desconto de caução volta para a caução, para entrar no
        acerto. Pagamento de mês anterior não é mexido (o DAS daquele mês pode
        já ter sido declarado): a semana fica com o valor do que foi pago, sem
        dívida, e o aviso pede para o dono decidir a devolução.

        Devolve {"credito", "caucao", "mes_anterior"} em R$ para os avisos.
        """
        from apps.financeiro.models import (  # import local evita ciclo de import
            Cobranca,
            MovimentacaoCaucao,
            MovimentoCredito,
        )

        inicio_do_mes = date.today().replace(day=1)
        acerto = {"credito": 0, "caucao": 0, "mes_anterior": 0}
        canceladas = []
        nao_usadas = Cobranca.com_quitacao_anotada(
            self.cobrancas.filter(origem=Cobranca.Origem.ALUGUEL)
            # semana de transição do dia de vencimento (D04) vence antes, mas paga
            # dias depois da devolução: também não é devida
            .filter(
                models.Q(vencimento__gte=data_termino) | models.Q(periodo_inicio__gte=data_termino)
            )
            .exclude(status__in=[Cobranca.Status.CANCELADA, Cobranca.Status.JUDICIAL])
        ).prefetch_related("aplicacoes__recebimento", "descontos_caucao")
        for cobranca in nao_usadas:
            aplicacoes = list(cobranca.aplicacoes.all())
            descontos = [
                d
                for d in cobranca.descontos_caucao.all()
                if d.tipo == MovimentacaoCaucao.Tipo.DESCONTO
            ]
            antigos = [a for a in aplicacoes if a.recebimento.data < inicio_do_mes] + [
                d for d in descontos if d.data < inicio_do_mes
            ]
            if antigos:
                pago_antes = sum((item.valor for item in antigos), start=0)
                acerto["mes_anterior"] += pago_antes
                # o resto (deste mês) volta normalmente; a semana fica só com o antigo
                for aplicacao in aplicacoes:
                    if aplicacao.recebimento.data >= inicio_do_mes:
                        acerto["credito"] += self._aplicacao_vira_credito(
                            aplicacao, MovimentoCredito
                        )
                for desconto in descontos:
                    if desconto.data >= inicio_do_mes:
                        acerto["caucao"] += desconto.valor
                        desconto.delete()
                cobranca.valor = pago_antes
                cobranca.observacoes = (
                    cobranca.observacoes + "\n" if cobranca.observacoes else ""
                ) + (
                    f"Semana não usada (carro devolvido em {data_termino:%d/%m/%Y}): "
                    f"valor baixado para {brl(pago_antes)}, pago em mês anterior."
                )
                cobranca.status = Cobranca.Status.PAGO
                cobranca.save(update_fields=["valor", "observacoes", "status"])
                continue
            for aplicacao in aplicacoes:
                acerto["credito"] += self._aplicacao_vira_credito(aplicacao, MovimentoCredito)
            for desconto in descontos:
                acerto["caucao"] += desconto.valor
                desconto.delete()
            cobranca.status = Cobranca.Status.CANCELADA
            cobranca.save(update_fields=["status"])
            canceladas.append(cobranca.pk)
        # encargo de atraso de semana que deixou de ser devida também cai (o que
        # já tem pagamento fica, para o dono decidir) — uma consulta para todos
        encargos = Cobranca.com_quitacao_anotada(
            Cobranca.objects.filter(cobranca_origem_id__in=canceladas).exclude(
                status__in=[Cobranca.Status.CANCELADA, Cobranca.Status.JUDICIAL]
            )
        )
        for encargo in encargos if canceladas else []:
            if encargo.total_quitado <= 0:
                encargo.status = Cobranca.Status.CANCELADA
                encargo.save(update_fields=["status"])
        return acerto

    def _aplicacao_vira_credito(self, aplicacao, MovimentoCredito):
        """Tira o pagamento da semana cancelada e o guarda como crédito do cliente."""
        valor = aplicacao.valor
        MovimentoCredito.objects.create(
            cliente_id=self.cliente_id,
            tipo=MovimentoCredito.Tipo.ENTRADA,
            valor=valor,
            data=aplicacao.recebimento.data,
            recebimento=aplicacao.recebimento,
            observacoes=(
                f"Semana não usada de {aplicacao.cobranca.vencimento:%d/%m/%Y} "
                f"(carro {self.veiculo.placa} devolvido)"
            ),
        )
        aplicacao.delete()
        return valor

    @property
    def troca_ativa(self):
        """Troca em aberto (no máximo uma, por constraint).

        Filtra em Python: assim a listagem com prefetch_related("trocas") usa o
        cache — um .filter() aqui refazia a consulta linha a linha.
        """
        return next((t for t in self.trocas.all() if t.data_devolucao is None), None)


class TrocaTemporaria(models.Model):
    """Carro substituto emprestado durante conserto, sem encerrar a alocação (docs.md §4.2)."""

    alocacao = models.ForeignKey(
        Alocacao, verbose_name="alocação", on_delete=models.PROTECT, related_name="trocas"
    )
    veiculo_substituto = models.ForeignKey(
        Veiculo,
        verbose_name="veículo substituto",
        on_delete=models.PROTECT,
        related_name="trocas_como_substituto",
    )
    data_retirada = models.DateField("data de retirada")
    data_devolucao = models.DateField("data de devolução", null=True, blank=True)
    km_retirada = models.PositiveIntegerField("KM na retirada", validators=[VALIDADOR_KM])
    km_devolucao = models.PositiveIntegerField(
        "KM na devolução", null=True, blank=True, validators=[VALIDADOR_KM]
    )
    motivo = models.CharField(
        "motivo", max_length=200, blank=True, help_text="Ex.: manutenção do carro principal"
    )
    valor_semanal_ajustado = models.DecimalField(
        "valor semanal durante a troca (R$)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Vazio = mantém o valor da alocação; muda quando a categoria é diferente",
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "troca temporária"
        verbose_name_plural = "trocas temporárias"
        ordering = ["-data_retirada"]
        constraints = [
            models.UniqueConstraint(
                fields=["alocacao"],
                condition=models.Q(data_devolucao__isnull=True),
                name="uma_troca_aberta_por_alocacao",
            ),
            models.UniqueConstraint(
                fields=["veiculo_substituto"],
                condition=models.Q(data_devolucao__isnull=True),
                name="uma_troca_aberta_por_substituto",
            ),
        ]

    def __str__(self):
        return (
            f"{self.alocacao.cliente.nome} com {self.veiculo_substituto.placa} "
            f"desde {self.data_retirada:%d/%m/%Y}"
        )

    def save(self, *args, **kwargs):
        criando = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if criando:
                substituto = self.veiculo_substituto
                substituto.status = Veiculo.Status.ALOCADO
                if self.km_retirada > substituto.km_atual:
                    substituto.km_atual = self.km_retirada
                substituto.save(update_fields=["status", "km_atual"])
            # Semana já lançada e ainda não paga passa a dividir os dias entre
            # o carro do contrato e o substituto (decisão D02).
            from apps.financeiro.services import reajustar_aluguel_pela_troca

            reajustar_aluguel_pela_troca(self)

    def clean(self):
        erros = {}
        if self._state.adding and self.veiculo_substituto_id:
            if self.veiculo_substituto.status != Veiculo.Status.DISPONIVEL:
                erros["veiculo_substituto"] = (
                    f"Substituto {self.veiculo_substituto.placa} não está disponível."
                )
            elif self.data_retirada:
                liberado = ultima_liberacao_do_carro(self.veiculo_substituto, excluir_troca=self.pk)
                if liberado and self.data_retirada < liberado:
                    erros["data_retirada"] = (
                        f"O substituto {self.veiculo_substituto.placa} só voltou do motorista "
                        f"anterior em {liberado:%d/%m/%Y}. A retirada não pode ser antes disso."
                    )
        if self._state.adding and self.alocacao_id and self.data_retirada:
            if self.data_retirada < self.alocacao.data_inicio:
                erros["data_retirada"] = (
                    f"A troca não pode começar antes do início da locação "
                    f"({self.alocacao.data_inicio:%d/%m/%Y})."
                )
        if self.valor_semanal_ajustado is not None and self.valor_semanal_ajustado <= 0:
            erros["valor_semanal_ajustado"] = (
                "O valor semanal precisa ser maior que zero (deixe vazio para manter o da locação)."
            )
        # Valem também na edição (Admin) — reabrir a troca passa por aqui.
        if self.alocacao_id and self.data_devolucao is None:
            if self.alocacao.status != Alocacao.Status.ATIVA:
                erros["alocacao"] = "A alocação precisa estar ativa."
            if (
                self.alocacao.trocas.filter(data_devolucao__isnull=True)
                .exclude(pk=self.pk)
                .exists()
            ):
                erros["alocacao"] = "Já existe uma troca em andamento nesta alocação."
        if (
            self.veiculo_substituto_id
            and self.alocacao_id
            and self.veiculo_substituto_id == self.alocacao.veiculo_id
        ):
            erros["veiculo_substituto"] = "O substituto não pode ser o próprio carro."
        if erros:
            raise ValidationError(erros)

    @transaction.atomic
    def devolver(self, data_devolucao, km_devolucao):
        if self.data_devolucao:
            raise ValidationError("Substituto já devolvido.")
        if data_devolucao < self.data_retirada:
            raise ValidationError(
                f"Data de devolução anterior à retirada ({self.data_retirada:%d/%m/%Y})."
            )
        _validar_data_nao_futura(data_devolucao)
        if km_devolucao < self.km_retirada:
            raise ValidationError("KM de devolução menor que o KM de retirada.")
        _validar_km_maximo(km_devolucao)
        self.data_devolucao = data_devolucao
        self.km_devolucao = km_devolucao
        self.save()
        substituto = self.veiculo_substituto
        # Só libera quem está alocado — não sobrescreve Vendido/Em manutenção/Inativo.
        if substituto.status == Veiculo.Status.ALOCADO:
            substituto.status = Veiculo.Status.DISPONIVEL
        if km_devolucao > substituto.km_atual:
            substituto.km_atual = km_devolucao
        substituto.save(update_fields=["status", "km_atual"])


class Vistoria(models.Model):
    """Checklist de entrega/devolução do carro (km, combustível, marcas, notas).

    O papel continua existindo: o sistema imprime o checklist em branco, o
    preenchimento é feito à mão na entrega/devolução, e a foto do formulário
    preenchido pode ser lida para carregar os dados aqui (validação humana antes
    de salvar — mesmo fluxo da CNH e do CRLV).
    """

    class Tipo(models.TextChoices):
        # Os valores gravados ("entrada"/"saida") ficam como estão; só o texto
        # mostrado mudou — "entrada" confundia, porque na entrega o carro SAI do pátio.
        ENTRADA = "entrada", "Entrega ao motorista"
        SAIDA = "saida", "Devolução pelo motorista"

    class Combustivel(models.TextChoices):
        CHEIO = "cheio", "Cheio"
        TRES_QUARTOS = "tres_quartos", "3/4"
        MEIO = "meio", "1/2"
        UM_QUARTO = "um_quarto", "1/4"
        RESERVA = "reserva", "Reserva"

    alocacao = models.ForeignKey(
        Alocacao, verbose_name="alocação", on_delete=models.PROTECT, related_name="vistorias"
    )
    # D10: durante uma troca temporária o carro vistoriado é o substituto. Vazio
    # (registros antigos) = carro da alocação; o save preenche.
    veiculo = models.ForeignKey(
        Veiculo,
        verbose_name="carro vistoriado",
        on_delete=models.PROTECT,
        related_name="vistorias",
        null=True,
        blank=True,
    )
    tipo = models.CharField("tipo", max_length=10, choices=Tipo.choices)
    data = models.DateField("data")
    km = models.PositiveIntegerField(
        "KM no painel", null=True, blank=True, validators=[VALIDADOR_KM]
    )
    combustivel = models.CharField(
        "combustível", max_length=15, choices=Combustivel.choices, blank=True
    )
    avarias = models.TextField(
        "marcas e avarias",
        blank=True,
        help_text="Riscos, amassados, trincas — onde e como estão",
    )
    notas = models.TextField("notas", blank=True)
    foto = models.FileField(
        "foto/PDF do checklist preenchido",
        upload_to="vistorias/",
        null=True,
        blank=True,
        validators=[FileExtensionValidator(EXTENSOES_VISTORIA), validar_tamanho_arquivo],
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = "vistoria"
        ordering = ["-data", "-pk"]

    def __str__(self):
        placa = (self.veiculo or self.alocacao.veiculo).placa
        return f"{self.get_tipo_display()} — {placa} em {self.data:%d/%m/%Y}"

    def save(self, *args, **kwargs):
        if self.veiculo_id is None and self.alocacao_id:
            self.veiculo_id = self.alocacao.veiculo_id
        km_antes = None
        if self.pk:
            km_antes = Vistoria.objects.filter(pk=self.pk).values_list("km", flat=True).first()
        with transaction.atomic():
            super().save(*args, **kwargs)
            veiculo = self.veiculo
            if self.km and self.km > veiculo.km_atual:
                veiculo.km_atual = self.km
                veiculo.save(update_fields=["km_atual"])
            elif km_antes and km_antes != self.km and km_antes == veiculo.km_atual:
                # D27: corrigir o KM errado da vistoria baixa o KM do carro de volta
                from apps.km.correcao import km_atual_recalculado

                veiculo.km_atual = km_atual_recalculado(veiculo)
                veiculo.save(update_fields=["km_atual"])

    def veiculos_possiveis(self):
        """Carro da alocação + substitutos que o motorista teve nela (D10)."""
        ids = [self.alocacao.veiculo_id]
        ids += list(self.alocacao.trocas.values_list("veiculo_substituto_id", flat=True))
        return Veiculo.objects.filter(pk__in=ids)

    def clean(self):
        if self.alocacao_id and self.veiculo_id:
            if not self.veiculos_possiveis().filter(pk=self.veiculo_id).exists():
                raise ValidationError(
                    {"veiculo": "Escolha o carro da locação ou o substituto que o motorista usou."}
                )
