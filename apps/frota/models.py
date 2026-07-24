from django.db import models
from simple_history.models import HistoricalRecords


class Categoria(models.Model):
    """Categoria de veículo — define a faixa de valor semanal (docs.md §4.1)."""

    nome = models.CharField("nome", max_length=50, unique=True)
    valor_semanal_referencia = models.DecimalField(
        "valor semanal de referência (R$)", max_digits=8, decimal_places=2
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "categoria"
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class Fornecedor(models.Model):
    """Oficina/prestador de serviço (docs.md §4.1). Ex.: By Car, Pedrinho Baterias."""

    nome = models.CharField("nome / razão social", max_length=100, unique=True)
    cnpj = models.CharField("CNPJ", max_length=18, blank=True)
    contato = models.CharField("contato (telefone/e-mail)", max_length=100, blank=True)
    tipo_servico = models.CharField(
        "tipo de serviço",
        max_length=100,
        blank=True,
        help_text="Ex.: mecânica, funilaria, bateria, rastreador",
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "fornecedor"
        verbose_name_plural = "fornecedores"
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class Veiculo(models.Model):
    """Veículo da frota (docs.md §4.1)."""

    class Uso(models.TextChoices):
        LOCACAO = "locacao", "Locação"
        FORA_LOCACAO = "fora_locacao", "Fora de locação"

    class Status(models.TextChoices):
        DISPONIVEL = "disponivel", "Disponível"
        ALOCADO = "alocado", "Alocado"
        EM_MANUTENCAO = "em_manutencao", "Em manutenção"
        INATIVO = "inativo", "Inativo"
        VENDIDO = "vendido", "Vendido"

    class ChaveReserva(models.TextChoices):
        SIM = "sim", "Sim"
        NAO = "nao", "Não"
        DUVIDA = "duvida", "Dúvida"

    placa = models.CharField("placa", max_length=8, unique=True)
    renavam = models.CharField("renavam", max_length=20, blank=True)
    chassi = models.CharField("chassi", max_length=30, blank=True)
    marca_modelo = models.CharField("marca/modelo", max_length=80)
    ano = models.CharField("ano", max_length=10, blank=True, help_text='Ex.: "20/21"')
    categoria = models.ForeignKey(
        Categoria,
        verbose_name="categoria",
        on_delete=models.PROTECT,
        related_name="veiculos",
        null=True,
        blank=True,
    )
    uso = models.CharField("uso", max_length=20, choices=Uso.choices, default=Uso.LOCACAO)
    status = models.CharField(
        "status", max_length=20, choices=Status.choices, default=Status.DISPONIVEL
    )

    data_aquisicao = models.DateField("data de aquisição", null=True, blank=True)
    valor_compra = models.DecimalField(
        "valor de compra (R$)", max_digits=10, decimal_places=2, null=True, blank=True
    )
    km_compra = models.PositiveIntegerField("KM na compra", null=True, blank=True)
    km_atual = models.PositiveIntegerField("KM atual", default=0)
    valor_venda_estimado = models.DecimalField(
        "valor estimado de venda (R$)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="FIPE/mercado, atualizado manualmente — usado na desmobilização",
    )

    chave_reserva = models.CharField(
        "chave reserva", max_length=10, choices=ChaveReserva.choices, default=ChaveReserva.DUVIDA
    )

    rastreador_fornecedor = models.CharField("rastreador — fornecedor", max_length=80, blank=True)
    rastreador_vigencia_fim = models.DateField(
        "rastreador — fim da vigência", null=True, blank=True
    )
    bateria_data_troca = models.DateField("bateria — data da troca", null=True, blank=True)
    bateria_fornecedor = models.CharField("bateria — fornecedor", max_length=80, blank=True)
    bateria_garantia_fim = models.DateField("bateria — fim da garantia", null=True, blank=True)

    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "veículo"
        ordering = ["placa"]

    def __str__(self):
        return f"{self.marca_modelo} — {self.placa}"

    def save(self, *args, **kwargs):
        self.placa = self.placa.upper().replace("-", "").replace(" ", "")
        super().save(*args, **kwargs)
