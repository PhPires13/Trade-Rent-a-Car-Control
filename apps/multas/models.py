from django.db import models
from simple_history.models import HistoricalRecords

from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente, CondutorAutorizado


class OrgaoAutuador(models.Model):
    """Órgão de trânsito emissor de multas (docs.md §4.1/§4.7).

    Guarda o portal e as credenciais de acesso — sensíveis, mascaradas na UI.
    """

    class Esfera(models.TextChoices):
        MUNICIPAL = "municipal", "Municipal"
        ESTADUAL = "estadual", "Estadual"
        FEDERAL = "federal", "Federal"

    nome = models.CharField("nome", max_length=80, unique=True)
    esfera = models.CharField("esfera", max_length=10, choices=Esfera.choices, blank=True)
    portal = models.CharField("portal/site", max_length=200, blank=True)
    login = models.CharField("login/usuário", max_length=100, blank=True)
    senha = models.CharField(
        "senha", max_length=200, blank=True, help_text="Sensível — mascarada na interface"
    )
    email = models.EmailField("e-mail de atendimento", blank=True)
    telefone = models.CharField("telefone", max_length=40, blank=True)
    procedimento = models.TextField(
        "procedimento e documentos exigidos",
        blank=True,
        help_text="Ex.: solicitar FICI; docs: doc. do veículo, CNH do motorista, termo de entrega",
    )
    endereco = models.CharField("endereço físico", max_length=200, blank=True)
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "órgão autuador"
        verbose_name_plural = "órgãos autuadores"
        ordering = ["nome"]

    def __str__(self):
        return self.nome

    @property
    def senha_mascarada(self):
        if not self.senha:
            return ""
        return "•" * len(self.senha)


class Multa(models.Model):
    """Infração vinculada a veículo e cliente (docs.md §4.7)."""

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
        OUTRA_PESSOA = "outra_pessoa", "Condutor autorizado / outra pessoa"
        EMPRESA = "empresa", "Empresa (dono absorve)"

    class FICI(models.TextChoices):
        PENDENTE = "pendente", "Pendente"
        INDICADO = "indicado", "Indicado"
        PRAZO_PERDIDO = "prazo_perdido", "Prazo perdido"

    class Pagamento(models.TextChoices):
        PENDENTE = "pendente", "Pendente"
        PAGO = "pago", "Pago"

    class Repasse(models.TextChoices):
        NAO_SE_APLICA = "nao_se_aplica", "Não se aplica"
        A_COBRAR = "a_cobrar", "A cobrar"
        INCLUIDA_ND = "incluida_nd", "Incluída em ND"
        RECEBIDO = "recebido", "Recebido"

    class Responsavel(models.TextChoices):
        CLIENTE = "cliente", "Cliente da alocação"
        CONDUTOR = "condutor", "Condutor identificado"
        EMPRESA = "empresa", "Empresa (absorve)"
        VENDEDOR = "vendedor", "Vendedor anterior"

    veiculo = models.ForeignKey(
        Veiculo, verbose_name="veículo", on_delete=models.PROTECT, related_name="multas"
    )
    cliente_alocacao = models.ForeignKey(
        Cliente,
        verbose_name="cliente da alocação",
        on_delete=models.PROTECT,
        related_name="multas",
        null=True,
        blank=True,
        help_text="Quem estava com o carro na data (preenchido automaticamente)",
    )
    data = models.DateField("data da infração")
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
    local = models.CharField("local", max_length=120, blank=True)
    valor = models.DecimalField("valor (R$)", max_digits=8, decimal_places=2, null=True, blank=True)
    pontos = models.PositiveSmallIntegerField("pontos na CNH", null=True, blank=True)
    resultado = models.CharField(
        "resultado/situação", max_length=15, choices=Resultado.choices, default=Resultado.EM_ABERTO
    )

    tipo_condutor = models.CharField(
        "tipo de condutor",
        max_length=15,
        choices=TipoCondutor.choices,
        default=TipoCondutor.CLIENTE,
    )
    condutor_autorizado = models.ForeignKey(
        CondutorAutorizado,
        verbose_name="condutor identificado",
        on_delete=models.PROTECT,
        related_name="multas",
        null=True,
        blank=True,
    )
    fici_status = models.CharField(
        "indicação (FICI)", max_length=15, choices=FICI.choices, default=FICI.PENDENTE
    )
    fici_prazo = models.DateField("prazo limite de indicação", null=True, blank=True)

    pagamento = models.CharField(
        "pagamento", max_length=10, choices=Pagamento.choices, default=Pagamento.PENDENTE
    )
    pago_por = models.CharField("pago por", max_length=80, blank=True)
    repasse = models.CharField(
        "repasse ao cliente", max_length=15, choices=Repasse.choices, default=Repasse.A_COBRAR
    )
    responsavel = models.CharField(
        "responsável pelo valor",
        max_length=10,
        choices=Responsavel.choices,
        default=Responsavel.CLIENTE,
    )
    multa_origem = models.ForeignKey(
        "self",
        verbose_name="multa original (para NIC)",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="multas_nic",
        help_text="Preenchido quando esta é uma multa por Não Indicação de Condutor",
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "multa"
        ordering = ["-data"]

    def __str__(self):
        return f"{self.veiculo.placa} — {self.descricao or self.codigo} ({self.data:%d/%m/%Y})"

    @property
    def eh_nic(self):
        return self.multa_origem_id is not None
