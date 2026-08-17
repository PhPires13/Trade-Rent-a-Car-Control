from django.core.exceptions import ValidationError
from django.db import models
from simple_history.models import HistoricalRecords

from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente, CondutorAutorizado

from .fields import CampoCriptografado


class OrgaoAutuador(models.Model):
    """Órgão de trânsito com portal, credenciais e procedimento (docs.md §4.1)."""

    class Esfera(models.TextChoices):
        MUNICIPAL = "municipal", "Municipal"
        ESTADUAL = "estadual", "Estadual"
        FEDERAL = "federal", "Federal"

    nome = models.CharField("nome", max_length=60, unique=True)
    esfera = models.CharField("esfera", max_length=10, choices=Esfera.choices, blank=True)
    portal = models.URLField("portal / site", blank=True)
    login = CampoCriptografado("login/usuário", blank=True)
    senha = CampoCriptografado("senha", blank=True)
    email = models.EmailField("e-mail", blank=True)
    telefone = models.CharField("telefone", max_length=40, blank=True)
    procedimento = models.TextField(
        "procedimento / documentos exigidos",
        blank=True,
        help_text="Ex.: solicitar FICI e depois consultar; docs: CNH, contrato social...",
    )
    endereco = models.TextField("endereço (protocolo presencial/AR)", blank=True)
    observacoes = models.TextField("observações", blank=True)

    # Credenciais ficam fora do histórico: sem isso toda senha já usada — inclusive
    # a que foi trocada porque vazou — ficaria guardada e visível para sempre.
    history = HistoricalRecords(excluded_fields=["login", "senha"])

    class Meta:
        verbose_name = "órgão autuador"
        verbose_name_plural = "órgãos autuadores"
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class Multa(models.Model):
    """Multa/autuação com FICI, trâmite e repasse (docs.md §4.7)."""

    class Resultado(models.TextChoices):
        EM_ABERTO = "em_aberto", "Em aberto"
        ADVERTENCIA = "advertencia", "Convertida em advertência"
        PENALIDADE = "penalidade", "Penalidade confirmada"
        SUSPENSA = "suspensa", "Suspensa"
        DIVIDA_ATIVA = "divida_ativa", "Dívida ativa"
        NAO_EXIGIVEL = "nao_exigivel", "Não exigível"
        CANCELADA = "cancelada", "Cancelada"

    class TipoCondutor(models.TextChoices):
        CLIENTE = "cliente", "O próprio cliente"
        OUTRO = "outro", "Condutor autorizado / outra pessoa"
        EMPRESA = "empresa", "Empresa (absorve)"

    class Fici(models.TextChoices):
        PENDENTE = "pendente", "Pendente"
        INDICADO = "indicado", "Indicado"
        PRAZO_PERDIDO = "prazo_perdido", "Prazo perdido"
        NAO_SE_APLICA = "nao_se_aplica", "Não se aplica"

    class Pagamento(models.TextChoices):
        PENDENTE = "pendente", "Pendente"
        PAGO = "pago", "Pago"

    class Responsavel(models.TextChoices):
        CLIENTE = "cliente", "Cliente da alocação"
        CONDUTOR = "condutor", "Condutor identificado"
        EMPRESA = "empresa", "Empresa (absorve)"
        VENDEDOR = "vendedor", "Vendedor anterior (antes da aquisição)"

    # Resultados em que a multa deixa de ser devida — não há o que repassar.
    RESULTADOS_ANULADORES = (
        Resultado.ADVERTENCIA,
        Resultado.CANCELADA,
        Resultado.NAO_EXIGIVEL,
    )

    veiculo = models.ForeignKey(
        Veiculo, verbose_name="veículo", on_delete=models.PROTECT, related_name="multas"
    )
    cliente = models.ForeignKey(
        Cliente,
        verbose_name="cliente da alocação",
        on_delete=models.PROTECT,
        related_name="multas",
        null=True,
        blank=True,
        help_text="Preenchido automaticamente por quem estava com o carro na data",
    )
    data_infracao = models.DateField("data da infração")
    codigo = models.CharField("código da infração", max_length=20, blank=True)
    ait = models.CharField("AIT", max_length=30, blank=True)
    num_processamento = models.CharField("nº de processamento", max_length=30, blank=True)
    orgao = models.ForeignKey(
        OrgaoAutuador,
        verbose_name="órgão autuador",
        on_delete=models.PROTECT,
        related_name="multas",
        null=True,
        blank=True,
    )
    descricao = models.CharField("descrição da infração", max_length=200, blank=True)
    valor = models.DecimalField(
        "valor (R$)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Vazio quando convertida em advertência",
    )
    pontos = models.PositiveSmallIntegerField("pontos na CNH", null=True, blank=True)
    resultado = models.CharField(
        "resultado/situação",
        max_length=15,
        choices=Resultado.choices,
        default=Resultado.EM_ABERTO,
    )

    tipo_condutor = models.CharField(
        "condutor identificado",
        max_length=10,
        choices=TipoCondutor.choices,
        default=TipoCondutor.CLIENTE,
    )
    condutor_autorizado = models.ForeignKey(
        CondutorAutorizado,
        verbose_name="condutor (outra pessoa)",
        on_delete=models.PROTECT,
        related_name="multas",
        null=True,
        blank=True,
    )

    fici_status = models.CharField(
        "FICI", max_length=15, choices=Fici.choices, default=Fici.PENDENTE
    )
    fici_prazo = models.DateField("prazo limite do FICI", null=True, blank=True)
    fici_data_indicacao = models.DateField("data da indicação", null=True, blank=True)

    pagamento = models.CharField(
        "pagamento", max_length=10, choices=Pagamento.choices, default=Pagamento.PENDENTE
    )
    pago_por = models.CharField("pago por", max_length=60, blank=True)
    responsavel = models.CharField(
        "responsável pelo valor",
        max_length=10,
        choices=Responsavel.choices,
        default=Responsavel.CLIENTE,
    )
    multa_origem_nic = models.OneToOneField(
        "self",
        verbose_name="multa que originou (NIC)",
        on_delete=models.PROTECT,
        related_name="multa_nic",
        null=True,
        blank=True,
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "multa"
        ordering = ["-data_infracao"]

    def __str__(self):
        rotulo = self.descricao or self.codigo
        return f"{self.veiculo.placa} — {rotulo} em {self.data_infracao:%d/%m/%Y}"

    def save(self, *args, **kwargs):
        if self.cliente_id is None and self.veiculo_id and self.data_infracao:
            from apps.alocacoes.services import cliente_vigente

            self.cliente = cliente_vigente(self.veiculo, self.data_infracao)
        super().save(*args, **kwargs)

    def clean(self):
        """Não deixa anular o resultado de uma multa que já está sendo cobrada.

        A ND congela o valor numa cobrança própria: mudar o resultado aqui não
        desfaz nada, só escondia a multa da tela enquanto o cliente continuava
        sendo cobrado (revisão de segurança/financeiro).
        """
        super().clean()
        if not self.pk or self.resultado not in self.RESULTADOS_ANULADORES:
            return
        anterior = type(self).objects.filter(pk=self.pk).values_list("resultado", flat=True).first()
        if anterior in self.RESULTADOS_ANULADORES:
            return  # já estava assim; não trava a edição dos outros campos
        item = self.itens_nd.select_related("nota_debito").first()
        if item:
            raise ValidationError(
                {
                    "resultado": (
                        f"Esta multa está na ND {item.nota_debito.numero:03d}, que continua "
                        "sendo cobrada do cliente. Ajuste ou cancele a nota de débito antes "
                        "de registrar este resultado."
                    )
                }
            )

    @property
    def item_nd(self):
        return getattr(self, "_item_nd_cache", None) or self.itens_nd.first()

    @property
    def repasse(self):
        """A cobrar → Incluída em ND → Recebido; ou 'não se aplica' (docs.md §4.7).

        O vínculo com a ND é checado primeiro: multa já cobrada não pode sumir da
        tela porque o resultado mudou — a cobrança da ND continua de pé.
        """
        item = self.itens_nd.select_related("nota_debito").first()
        if item:
            cobranca = getattr(item.nota_debito, "cobranca", None)
            if cobranca and cobranca.status == "pago":
                return "Recebido"
            rotulo = f"Incluída na ND {item.nota_debito.numero:03d}"
            if self.resultado in self.RESULTADOS_ANULADORES:
                rotulo += " — revisar: resultado anulou a multa"
            return rotulo
        if self.responsavel in (self.Responsavel.EMPRESA, self.Responsavel.VENDEDOR):
            return "Não se aplica"
        if self.resultado in self.RESULTADOS_ANULADORES or not self.valor:
            return "Não se aplica"
        return "A cobrar"
