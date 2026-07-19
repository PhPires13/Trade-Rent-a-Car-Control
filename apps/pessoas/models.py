from django.db import models
from simple_history.models import HistoricalRecords


class Cliente(models.Model):
    """Cliente/motorista locatário (docs.md §4.1)."""

    class Status(models.TextChoices):
        ATIVO = "ativo", "Ativo"
        INADIMPLENTE = "inadimplente", "Inadimplente"
        INATIVO = "inativo", "Inativo"

    class DiaSemana(models.IntegerChoices):
        SEGUNDA = 0, "Segunda-feira"
        TERCA = 1, "Terça-feira"
        QUARTA = 2, "Quarta-feira"
        QUINTA = 3, "Quinta-feira"
        SEXTA = 4, "Sexta-feira"
        SABADO = 5, "Sábado"
        DOMINGO = 6, "Domingo"

    nome = models.CharField("nome completo", max_length=120)
    cpf_cnpj = models.CharField("CPF/CNPJ", max_length=18, unique=True)
    telefone = models.CharField("telefone/WhatsApp", max_length=20, blank=True)
    email = models.EmailField("e-mail", blank=True)
    endereco = models.TextField("endereço", blank=True)

    cnh_numero = models.CharField("CNH — número", max_length=20, blank=True)
    cnh_categoria = models.CharField("CNH — categoria", max_length=5, blank=True)
    cnh_validade = models.DateField("CNH — validade", null=True, blank=True)

    dia_vencimento = models.IntegerField(
        "dia de vencimento semanal",
        choices=DiaSemana.choices,
        null=True,
        blank=True,
        help_text="Padrão: dia da semana em que pegou o carro",
    )
    caucao_referencia = models.DecimalField(
        "caução acordada (R$)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Referência — a caução é opcional",
    )
    status = models.CharField("status", max_length=15, choices=Status.choices, default=Status.ATIVO)
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "cliente"
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class CondutorAutorizado(models.Model):
    """Pessoa não-cliente que pode ser indicada como condutora em multas (docs.md §4.1)."""

    nome = models.CharField("nome", max_length=120)
    cpf = models.CharField("CPF", max_length=14, blank=True)
    cnh_numero = models.CharField("CNH — número", max_length=20, blank=True)
    contato = models.CharField("contato", max_length=80, blank=True)
    cliente = models.ForeignKey(
        Cliente,
        verbose_name="cliente relacionado",
        on_delete=models.PROTECT,
        related_name="condutores_autorizados",
        null=True,
        blank=True,
    )
    observacoes = models.TextField("observações", blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "condutor autorizado"
        verbose_name_plural = "condutores autorizados"
        ordering = ["nome"]

    def __str__(self):
        return self.nome
