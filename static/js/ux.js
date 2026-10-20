// Ajustes de uso que valem para todas as telas (carregado no fim de base.html).
// Tudo aqui é idempotente: roda na página inteira e de novo em cada pedaço que o
// HTMX trocar (evento htmx:load), sem duplicar nada.
(function () {
  "use strict";

  const CONTROLES = "input:not([type=hidden]), select, textarea";
  // campos de digitar/escolher — botão de enviar não recebe rótulo
  const CAMPOS = "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), select, textarea";

  // Seletor de arquivo em português (o nativo do navegador sai "Choose File / No file chosen").
  function seletorDeArquivo(raiz) {
    raiz.querySelectorAll('input[type="file"]:not([data-ux-arquivo])').forEach(function (campo) {
      campo.dataset.uxArquivo = "1";
      campo.classList.add("sr-only");
      const caixa = document.createElement("span");
      caixa.className = "flex flex-wrap items-center gap-2";
      const botao = document.createElement("button");
      botao.type = "button";
      botao.className = "rounded border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-800 shadow-sm hover:bg-gray-50";
      botao.textContent = "📎 Escolher arquivo";
      const nome = document.createElement("span");
      nome.className = "text-sm text-gray-600";
      nome.textContent = "nenhum arquivo escolhido";
      botao.addEventListener("click", function () { campo.click(); });
      campo.addEventListener("change", function () {
        nome.textContent = campo.files.length ? campo.files[0].name : "nenhum arquivo escolhido";
        nome.className = campo.files.length ? "text-sm font-medium text-gray-800" : "text-sm text-gray-600";
      });
      campo.after(caixa);
      caixa.append(botao, nome);
    });
  }

  // Rótulo sem "for": liga ao campo logo depois dele (ou ao único campo do mesmo bloco),
  // para o toque no rótulo focar o campo e o leitor de tela anunciar o nome certo.
  let contador = 0;
  function ligarRotulos(raiz) {
    raiz.querySelectorAll("label:not([for])").forEach(function (rotulo) {
      if (rotulo.querySelector(CONTROLES)) return; // rótulo que envolve o campo já funciona
      let campo = rotulo.nextElementSibling;
      if (!campo || !campo.matches(CAMPOS)) {
        const doBloco = rotulo.parentElement ? rotulo.parentElement.querySelectorAll(CAMPOS) : [];
        campo = doBloco.length === 1 ? doBloco[0] : null;
      }
      if (!campo || campo.type === "file") return;
      if (!campo.id) campo.id = "campo-auto-" + (++contador);
      rotulo.htmlFor = campo.id;
    });
  }

  // Um padrão só para obrigatório/opcional: os obrigatórios ficam sem marca e os demais
  // ganham "(opcional)". Só nos campos de formulário Django (id "id_…"), que trazem
  // "required" certinho; campo escrito à mão pode pedir com data-opcional.
  function marcarOpcionais(raiz) {
    raiz.querySelectorAll('form[method="post" i]:not([data-sem-opcional]) label[for]').forEach(function (rotulo) {
      if (rotulo.dataset.uxOpcional) return;
      rotulo.dataset.uxOpcional = "1";
      // tira o "*" antigo de obrigatório
      const ultimo = rotulo.lastChild;
      if (ultimo && ultimo.nodeType === 3 && /\s*\*\s*$/.test(ultimo.nodeValue)) {
        ultimo.nodeValue = ultimo.nodeValue.replace(/\s*\*\s*$/, "");
      }
      const campo = document.getElementById(rotulo.htmlFor);
      if (!campo || !campo.matches(CONTROLES) || campo.required) return;
      if (campo.type === "checkbox" || campo.type === "radio" || campo.disabled || campo.readOnly) return;
      if (!campo.id.startsWith("id_") && !campo.hasAttribute("data-opcional")) return;
      if (/opcional/i.test(rotulo.textContent)) return;
      const marca = document.createElement("span");
      marca.className = "font-normal text-gray-600";
      marca.textContent = " (opcional)";
      rotulo.append(marca);
    });
  }

  // Dicas: o "title" só aparece com mouse parado em cima — no celular nunca aparece.
  // Em textos (cabeçalho de coluna, selo, rótulo…) ele vira um botão "?" que mostra a
  // explicação ao tocar ou clicar, e também pelo teclado (Enter/Espaço; Esc fecha).
  // Botões e links continuam com o title: a ação já tem nome no próprio texto.
  let balao = null;
  let botaoAberto = null;

  function fecharDica(devolverFoco) {
    if (!balao || !botaoAberto) return;
    balao.hidden = true;
    botaoAberto.setAttribute("aria-expanded", "false");
    if (devolverFoco) botaoAberto.focus();
    botaoAberto = null;
  }

  function abrirDica(botao) {
    if (!balao) {
      balao = document.createElement("div");
      balao.id = "dica-balao";
      balao.setAttribute("role", "tooltip");
      balao.className = "dica-balao";
      balao.hidden = true;
      document.body.append(balao);
    }
    fecharDica(false);
    balao.textContent = botao.dataset.dica;
    balao.hidden = false;
    botao.setAttribute("aria-expanded", "true");
    botaoAberto = botao;
    // posiciona embaixo do "?", sem sair da tela
    const r = botao.getBoundingClientRect();
    const largura = balao.offsetWidth;
    const esquerda = Math.max(8, Math.min(r.left + r.width / 2 - largura / 2, window.innerWidth - largura - 8));
    let topo = r.bottom + 8;
    if (topo + balao.offsetHeight > window.innerHeight - 8) topo = Math.max(8, r.top - balao.offsetHeight - 8);
    balao.style.left = esquerda + "px";
    balao.style.top = topo + "px";
  }

  function criarDicas(raiz) {
    const lista = raiz.matches && raiz.matches("[title]") ? [raiz] : [];
    raiz.querySelectorAll("[title]").forEach(function (el) { lista.push(el); });
    lista.forEach(function (el) {
      if (el.closest("a, button, select, input, textarea, summary, svg, [data-sem-dica]")) return;
      const texto = (el.getAttribute("title") || "").trim();
      if (!texto) return;
      el.removeAttribute("title");
      const botao = document.createElement("button");
      botao.type = "button";
      botao.className = "dica-botao";
      botao.textContent = "?";
      botao.dataset.dica = texto;
      botao.dataset.alvoLivre = "1"; // área de toque aumentada pelo CSS, sem esticar a linha
      botao.setAttribute("aria-label", "Explicação: " + texto);
      botao.setAttribute("aria-expanded", "false");
      botao.setAttribute("aria-controls", "dica-balao");
      // num bloco com título + valor, o "?" vai junto do título
      const alvo = el.tagName === "DIV" && el.firstElementChild ? el.firstElementChild : el;
      alvo.append(" ", botao);
    });
  }

  document.addEventListener("click", function (e) {
    const botao = e.target.closest(".dica-botao");
    if (botao) {
      e.preventDefault(); // dentro de <label> não pode mudar o foco nem marcar caixa
      if (botaoAberto === botao) fecharDica(false);
      else abrirDica(botao);
    } else if (botaoAberto && !(balao && balao.contains(e.target))) {
      fecharDica(false);
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && botaoAberto) fecharDica(true);
  });
  window.addEventListener("scroll", function () { fecharDica(false); }, { passive: true, capture: true });
  window.addEventListener("resize", function () { fecharDica(false); });

  // Envio de formulário: no toque em Salvar, o botão trava e mostra "Salvando…" — sem
  // isso a tela fica parada até a resposta e a pessoa toca de novo, duplicando o lançamento.
  // Só para POST; formulário que já se cuida (HTMX, @submit.prevent, confirm cancelado)
  // chega aqui com defaultPrevented e é ignorado. A validação do navegador barra o envio
  // antes do evento "submit", então nesse caso o botão nem chega a travar.
  function botoesDeEnvio(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      return (el.tagName === "BUTTON" && (el.type || "submit") === "submit") ||
        (el.tagName === "INPUT" && (el.type === "submit" || el.type === "image"));
    });
  }

  function travar(form, apertado) {
    botoesDeEnvio(form).forEach(function (botao) {
      if (botao.disabled) return; // a própria tela já travou (ex.: "Emitindo…" da ND)
      botao.dataset.uxTravado = "1";
      botao.disabled = true;
      botao.classList.add("cursor-wait", "opacity-60");
      if (botao !== apertado) return;
      const atual = (botao.tagName === "INPUT" ? botao.value : botao.textContent).trim();
      const novo = botao.dataset.enviando ||
        (/^entrar/i.test(atual) ? "Entrando…" : /^sair/i.test(atual) ? "Saindo…" : "Salvando…");
      if (botao.tagName === "INPUT") {
        botao.dataset.uxOriginal = botao.value;
        botao.value = novo;
      } else if (botao.textContent.trim().length > 2) { // botão só de ícone (✕) não troca o texto
        botao.dataset.uxOriginal = botao.innerHTML;
        botao.textContent = novo;
      }
    });
    form.setAttribute("aria-busy", "true");
  }

  function destravar(form) {
    delete form.dataset.uxEnviando;
    form.removeAttribute("aria-busy");
    botoesDeEnvio(form).forEach(function (botao) {
      if (!botao.dataset.uxTravado) return;
      delete botao.dataset.uxTravado;
      botao.disabled = false;
      botao.classList.remove("cursor-wait", "opacity-60");
      if (botao.dataset.uxOriginal !== undefined) {
        if (botao.tagName === "INPUT") botao.value = botao.dataset.uxOriginal;
        else botao.innerHTML = botao.dataset.uxOriginal;
        delete botao.dataset.uxOriginal;
      }
    });
  }

  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (e.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
    if (form.hasAttribute("data-sem-salvando") || form.target === "_blank") return;
    if (form.dataset.uxEnviando) { e.preventDefault(); return; } // segundo toque: ignora
    form.dataset.uxEnviando = "1";
    const apertado = e.submitter || botoesDeEnvio(form)[0];
    // trava no próximo instante: o navegador já leu o nome/valor do botão apertado
    setTimeout(function () { travar(form, apertado); }, 0);
    // resposta que não troca de página (ex.: download de arquivo) não deixa o botão preso.
    // Prazo longo: servidor acordando ou 3G do pátio passam fácil de 15s, e destravar
    // antes da resposta abriria a porta para o lançamento duplicado. Com foto (upload)
    // não destrava sozinho: o envio pode levar minutos e nenhum upload devolve arquivo.
    if ((form.enctype || "").toLowerCase() !== "multipart/form-data") {
      setTimeout(function () { destravar(form); }, 60000);
    }
  });

  // Voltar do navegador (página restaurada do cache) não pode mostrar o formulário travado.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-ux-enviando]").forEach(destravar);
  });

  function melhorar(raiz) {
    if (!raiz || !raiz.querySelectorAll) return;
    seletorDeArquivo(raiz);
    ligarRotulos(raiz);
    marcarOpcionais(raiz);
    criarDicas(raiz);
  }

  melhorar(document.body);
  document.addEventListener("htmx:load", function (e) { melhorar(e.target); });
})();
