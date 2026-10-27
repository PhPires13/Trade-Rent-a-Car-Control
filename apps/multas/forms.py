"""Formulários de multas — usados pelas telas e também pelo /admin.

Ficam aqui (e não em views.py) porque o admin precisa exatamente do mesmo
tratamento de credenciais: um form próprio no admin já devolveu a senha do
órgão em claro no HTML (revisão de segurança).
"""

from django import forms
from django.db.models import Q
from django.utils import timezone

from apps.campos import FormularioBR
from apps.pessoas.models import CondutorAutorizado

from .models import Multa, OrgaoAutuador


def condutores_do_cliente(cliente, atual=None):
    """Condutores que fazem sentido para a multa: os do cliente e os avulsos.

    O que já estava escolhido continua na lista, para a edição não perdê-lo.
    """
    filtro = Q(cliente__isnull=True)
    if cliente is not None:
        filtro |= Q(cliente=cliente)
    if atual:
        filtro |= Q(pk=atual)
    return CondutorAutorizado.objects.filter(filtro)


class MultaForm(FormularioBR, forms.ModelForm):
    class Meta:
        model = Multa
        fields = [
            "veiculo",
            "data_infracao",
            "cliente",
            "codigo",
            "ait",
            "num_processamento",
            "orgao",
            "descricao",
            "valor",
            "pontos",
            "tipo_condutor",
            "condutor_autorizado",
            "fici_prazo",
            "responsavel",
            "observacoes",
        ]
        widgets = {
            "data_infracao": forms.DateInput(attrs={"type": "date"}),
            "fici_prazo": forms.DateInput(attrs={"type": "date"}),
            "observacoes": forms.Textarea(attrs={"rows": 2}),
        }

    #: rótulos em texto comum, com a sigla entre parênteses (auditoria UX #10)
    ROTULOS = {
        "veiculo": "Carro (placa)",
        "data_infracao": "Data da infração",
        "cliente": "Cliente que paga (só se for outro)",
        "codigo": "Código da infração",
        "ait": "Número do auto de infração (AIT)",
        "num_processamento": "Número de processamento (se vier na carta)",
        "orgao": "Órgão que multou",
        "descricao": "O que aconteceu (descrição da infração)",
        "valor": "Valor da multa (R$)",
        "pontos": "Pontos na carteira",
        "tipo_condutor": "Quem estava dirigindo",
        "condutor_autorizado": "Nome de quem dirigia",
        "fici_prazo": "Prazo para indicar o motorista (FICI)",
        "responsavel": "Quem vai pagar a multa",
        "observacoes": "Observações",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for nome, rotulo in self.ROTULOS.items():
            if nome in self.fields:
                self.fields[nome].label = rotulo
        self.fields["cliente"].required = False
        self.fields[
            "cliente"
        ].help_text = "Deixe vazio: o sistema usa quem estava com o carro na data da infração."
        self.fields["fici_prazo"].help_text = (
            'Veja "Data limite para identificação do condutor" na carta. '
            "Passou o prazo, o órgão manda outra multa para a empresa (NIC)."
        )
        self.fields[
            "condutor_autorizado"
        ].help_text = "Cadastre a pessoa na ficha do cliente, em Condutores autorizados."
        # Alpine: o nome de quem dirigia só aparece quando é "outra pessoa"
        self.fields["tipo_condutor"].widget.attrs["x-model"] = "tipo"
        # "Empresa (absorve)" já escolhe Empresa em "Quem vai pagar" — a não ser que a
        # pessoa já tenha mexido nesse campo; voltando para cliente/outro, volta Cliente
        self.fields["tipo_condutor"].widget.attrs["x-on:change"] = (
            "if (!mexeuResp) resp = $event.target.value === 'empresa' ? 'empresa' "
            ": (resp === 'empresa' ? 'cliente' : resp)"
        )
        self.fields["responsavel"].widget.attrs["x-model"] = "resp"
        self.fields["responsavel"].widget.attrs["x-on:change"] = "mexeuResp = true"
        if not self.instance.pk and not self.is_bound:
            # antes de escolher o carro, só os avulsos — não os condutores de todos
            self.fields["condutor_autorizado"].queryset = condutores_do_cliente(None)

    def clean_valor(self):
        valor = self.cleaned_data.get("valor")
        if valor is not None and valor < 0:
            raise forms.ValidationError("O valor não pode ser negativo.")
        return valor

    def clean_data_infracao(self):
        # ano/mês trocado na digitação: a multa ia para a ND com data impossível e
        # "quem estava com o carro" apontava para ninguém (igual ao sinistro)
        dia = self.cleaned_data.get("data_infracao")
        if dia and dia > timezone.localdate() and "data_infracao" in self.changed_data:
            raise forms.ValidationError("A data da infração não pode ser no futuro.")
        return dia

    #: pontos que uma infração pode valer (CTB art. 259); 0 = sem pontos
    PONTOS_VALIDOS = (0, 3, 4, 5, 7)

    def clean_pontos(self):
        pontos = self.cleaned_data.get("pontos")
        if pontos is not None and pontos not in self.PONTOS_VALIDOS:
            raise forms.ValidationError(
                "Uma infração vale 3, 4, 5 ou 7 pontos (ou 0). Confira na notificação."
            )
        return pontos

    def clean_ait(self):
        return (self.cleaned_data.get("ait") or "").strip()

    def _conferir_ait_repetido(self, dados):
        """Mesmo AIT no mesmo órgão já cadastrado: seria cobrado duas vezes do cliente."""
        ait = dados.get("ait")
        if not ait or "ait" in self._errors:
            return
        repetida = (
            Multa.objects.filter(ait__iexact=ait, orgao=dados.get("orgao"))
            .exclude(pk=self.instance.pk)
            .select_related("veiculo")
            .first()
        )
        if repetida is not None:
            self.add_error(
                "ait",
                f"Esta multa já foi cadastrada: AIT {repetida.ait}, {repetida.veiculo.placa} "
                f"em {repetida.data_infracao:%d/%m/%Y}. Procure-a na lista de multas "
                "(filtro Concluídas ou pela placa) em vez de cadastrar de novo.",
            )

    def _novo_ou_mudou(self, *campos):
        return not self.instance.pk or bool(set(campos) & set(self.changed_data))

    def clean(self):
        dados = super().clean()
        self._conferir_ait_repetido(dados)
        infracao, prazo = dados.get("data_infracao"), dados.get("fici_prazo")
        if (
            infracao
            and prazo
            and prazo < infracao
            and self._novo_ou_mudou("data_infracao", "fici_prazo")
        ):
            # mês/ano trocado: a multa já nascia "Prazo vencido" oferecendo NIC à toa
            self.add_error(
                "fici_prazo",
                "O prazo para indicar não pode ser antes da infração — confira o mês e o ano.",
            )
        if (
            dados.get("tipo_condutor") == Multa.TipoCondutor.OUTRO
            and not dados.get("condutor_autorizado")
            and "condutor_autorizado" not in self._errors
            and self._novo_ou_mudou("tipo_condutor", "condutor_autorizado")
        ):
            # sem o nome não há quem indicar ao órgão
            self.add_error(
                "condutor_autorizado",
                "Escolha quem estava dirigindo. Se a pessoa não aparece, cadastre-a na ficha "
                "do cliente (Condutores autorizados) ou escolha “O próprio cliente”.",
            )
        self._conferir_vendedor(dados)
        if dados.get("tipo_condutor") != Multa.TipoCondutor.OUTRO:
            # campo escondido na tela não pode deixar um nome esquecido gravado
            dados["condutor_autorizado"] = None
        if (
            dados.get("tipo_condutor") == Multa.TipoCondutor.EMPRESA
            and dados.get("responsavel") == Multa.Responsavel.CLIENTE
        ):
            # "a empresa absorve" e "o cliente paga" ao mesmo tempo: a multa ia para a ND
            self.add_error(
                "responsavel",
                "Você marcou que a empresa absorve; escolha Empresa em Quem vai pagar.",
            )
        self._conferir_condutor_do_cliente(dados)
        return dados

    def _conferir_vendedor(self, dados):
        """ "Vendedor anterior" só vale para infração de antes da compra do carro (§4.9).

        Depois da compra, a multa ficava sem ser cobrada de ninguém.
        """
        veiculo, dia = dados.get("veiculo"), dados.get("data_infracao")
        if (
            dados.get("responsavel") == Multa.Responsavel.VENDEDOR
            and veiculo
            and dia
            and veiculo.data_aquisicao
            and dia >= veiculo.data_aquisicao
            and (not self.instance.pk or "responsavel" in self.changed_data)
        ):
            self.add_error(
                "responsavel",
                f"O carro foi comprado em {veiculo.data_aquisicao:%d/%m/%Y}, antes da "
                "infração: a multa não é do vendedor anterior. Escolha Cliente ou Empresa.",
            )

    def _conferir_condutor_do_cliente(self, dados):
        """Carro/data corrigidos mudam o cliente: o condutor do cliente antigo não serve.

        Sem isso a multa ficava com o cliente novo e o condutor autorizado do outro
        motorista (a ND sairia para um e o nome indicado seria de outro).
        """
        condutor = dados.get("condutor_autorizado")
        veiculo, dia = dados.get("veiculo"), dados.get("data_infracao")
        if not self.instance.pk and condutor and condutor.cliente_id and veiculo and dia:
            # cadastro: o condutor tem de ser do cliente escolhido (ou de quem estava
            # com o carro) — senão a ND sai para um e o nome indicado é de outro
            from apps.alocacoes.services import cliente_vigente

            dono = dados.get("cliente") or cliente_vigente(veiculo, dia)
            if dono is None or dono.pk != condutor.cliente_id:
                quem = f"com {dono.nome}" if dono else "sem cliente"
                self.add_error(
                    "condutor_autorizado",
                    f"{condutor} é condutor de outro cliente; nessa data o carro estava "
                    f"{quem}. Confira quem dirigia.",
                )
            return
        if not (self.instance.pk and condutor and condutor.cliente_id and veiculo and dia):
            return
        mudou = set(self.changed_data)
        if not {"veiculo", "data_infracao"} & mudou or "cliente" in mudou:
            return
        if self.instance.itens_nd.exists() or self.instance.multa_origem_nic_id:
            return  # nesses casos o cliente não é recalculado (ver save)
        from apps.alocacoes.services import cliente_vigente

        novo = cliente_vigente(veiculo, dia)
        if novo is not None and novo.pk != condutor.cliente_id:
            self.add_error(
                "condutor_autorizado",
                f"{condutor} é condutor de outro cliente; nessa data o carro estava com "
                f"{novo.nome}. Confira quem dirigia.",
            )

    def save(self, commit=True):
        multa = super().save(commit=False)
        if (
            self.instance.pk
            and {"veiculo", "data_infracao"} & set(self.changed_data)
            # na ND, cliente e responsável ficam como foram cobrados
            and not self.instance.itens_nd.exists()
            # a NIC guarda o motorista da multa original, não o do dia em que chegou
            and not self.instance.multa_origem_nic_id
        ):
            if "cliente" not in self.changed_data:
                # carro ou data corrigidos: o motorista é recalculado pela nova data
                # (senão a ND sairia para quem estava com o carro no dia antigo)
                multa.cliente = None
            if "responsavel" not in self.changed_data:
                aquisicao = multa.veiculo.data_aquisicao
                antes_da_compra = bool(aquisicao and multa.data_infracao < aquisicao)
                if antes_da_compra and multa.responsavel == Multa.Responsavel.CLIENTE:
                    multa.responsavel = Multa.Responsavel.VENDEDOR  # dono anterior (§4.9)
                elif not antes_da_compra and multa.responsavel == Multa.Responsavel.VENDEDOR:
                    multa.responsavel = Multa.Responsavel.CLIENTE
        if commit:
            multa.save()
            self._save_m2m()
        return multa


class MultaEdicaoForm(MultaForm):
    """Edição completa: os dados da notificação mais a situação da multa."""

    CAMPOS_SITUACAO = ["resultado", "fici_status", "fici_data_indicacao", "pagamento", "pago_por"]

    class Meta(MultaForm.Meta):
        fields = MultaForm.Meta.fields + [
            "resultado",
            "fici_status",
            "fici_data_indicacao",
            "pagamento",
            "pago_por",
        ]
        widgets = {
            **MultaForm.Meta.widgets,
            "fici_data_indicacao": forms.DateInput(attrs={"type": "date"}),
        }

    ROTULOS = {
        **MultaForm.ROTULOS,
        "resultado": "Resultado do recurso / situação",
        "fici_status": "Indicação do motorista (FICI)",
        "fici_data_indicacao": "Data em que indicou o motorista",
        "pagamento": "Pagamento ao órgão",
        "pago_por": "Quem pagou",
    }

    #: o que a ND congela: mudar aqui não muda a cobrança que o cliente já recebeu
    CAMPOS_CONGELADOS_PELA_ND = ("valor", "cliente", "responsavel")

    #: "Quem pagou" é escolha, não texto livre (D23): só "Cliente" tira a multa da
    #: ND, e um texto como "João (motorista)" deixava a multa ser cobrada em dobro.
    OPCOES_PAGO_POR = [
        ("", "Não informado"),
        ("Empresa", "Empresa"),
        (Multa.PAGO_POR_CLIENTE, "Cliente (o motorista pagou ao órgão)"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        campo = self.fields["pago_por"]
        opcoes = list(self.OPCOES_PAGO_POR)
        atual = (self.instance.pago_por or "").strip()
        conhecidos = {valor.lower(): valor for valor, _ in opcoes if valor}
        if atual.lower() in conhecidos:
            # "cliente" gravado à mão vira a opção certa
            self.initial["pago_por"] = conhecidos[atual.lower()]
        elif atual:
            # texto livre antigo continua na lista para a edição não apagá-lo
            opcoes.append((atual, f"{atual} (anotado antes)"))
        self.fields["pago_por"] = forms.ChoiceField(
            label=campo.label,
            choices=opcoes,
            required=False,
            help_text="Escolha “Cliente” só se o motorista pagou direto ao órgão — "
            "aí a multa sai da nota de débito.",
        )

    def clean(self):
        dados = super().clean()
        if dados.get("pagamento") != Multa.Pagamento.PAGO:
            dados["pago_por"] = ""  # voltou para "a pagar": não fica "Paga (Empresa)" guardado
        item = self.instance.pk and self.instance.itens_nd.select_related("nota_debito").first()
        if item:
            for campo in self.CAMPOS_CONGELADOS_PELA_ND:
                if campo in self.changed_data and campo in dados:
                    self.add_error(
                        campo,
                        f"Esta multa já está na ND {item.nota_debito.numero:03d}, cobrada do "
                        "cliente com o valor antigo. Ajuste ou cancele a nota de débito antes.",
                    )
        return dados


class OrgaoForm(FormularioBR, forms.ModelForm):
    """Cadastro do órgão com portal, credenciais e procedimento (docs.md §4.1).

    A senha nunca volta no HTML da edição (ficaria em claro no fonte e no
    cache do navegador — revisão de segurança); campo vazio mantém a senha atual.

    O login continua visível: é o identificador do cadastro no portal, não o
    segredo — quem opera precisa conferir com qual usuário está entrando, e a
    tela de órgãos também o mostra. O que não pode aparecer é a senha.
    """

    class Meta:
        model = OrgaoAutuador
        fields = [
            "nome",
            "esfera",
            "portal",
            "login",
            "senha",
            "email",
            "telefone",
            "procedimento",
            "endereco",
            "observacoes",
        ]
        widgets = {
            "login": forms.TextInput(),
            "senha": forms.PasswordInput(),
            "procedimento": forms.Textarea(attrs={"rows": 3}),
            "endereco": forms.Textarea(attrs={"rows": 3}),
            "observacoes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["senha"].required = False
        if self.instance.pk and self.instance.senha:
            self.fields["senha"].help_text = "Deixe em branco para manter a senha atual."

    def clean_senha(self):
        senha = self.cleaned_data.get("senha")
        if not senha and self.instance.pk:
            return self.instance.senha
        return senha
