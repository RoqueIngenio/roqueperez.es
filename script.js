// Textos editables: se leen de textos.js y se colocan en su sitio.
// Si textos.js falta o tiene un error, se quedan los textos que ya trae la página.
function formatear(texto) {
  return texto
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/==(.+?)==/g, '<span class="subrayado">$1</span>')
    .replace(/~~(.+?)~~/g, '<span class="tachado">$1</span>');
}

function leerTextos(bruto) {
  const textos = {};
  let clave = null;
  bruto.split("\n").forEach((linea) => {
    const etiqueta = linea.trim().match(/^\[([\w.\-]+)\]$/);
    if (etiqueta) {
      clave = etiqueta[1];
      textos[clave] = [];
    } else if (clave && !linea.trim().startsWith("##")) {
      textos[clave].push(linea);
    }
  });
  Object.keys(textos).forEach((k) => (textos[k] = textos[k].join("\n").trim()));
  return textos;
}

function colocarTextos() {
  if (typeof window.TEXTOS_WEB !== "string") return;
  const textos = leerTextos(window.TEXTOS_WEB);
  document.querySelectorAll("[data-texto]").forEach((el) => {
    const texto = textos[el.dataset.texto];
    if (!texto) return;
    const lineas = texto.split("\n").map((l) => l.trim()).filter(Boolean);
    switch (el.dataset.formato) {
      case "parrafos":
        el.innerHTML = texto.split(/\n\s*\n/).map((p) => `<p>${formatear(p.trim())}</p>`).join("");
        break;
      case "lista":
        el.innerHTML = lineas.map((l) => {
          const corte = l.lastIndexOf(" · ");
          return corte > 0
            ? `<li>${formatear(l.slice(0, corte))} <span>${l.slice(corte + 1)}</span></li>`
            : `<li>${formatear(l)}</li>`;
        }).join("");
        break;
      case "temas":
        el.innerHTML = lineas.map((l) => `<span class="tema">${formatear(l)}</span>`).join("");
        break;
      case "libreta":
        el.innerHTML = lineas.map((l) => `<p>${formatear(l)}</p>`).join("");
        break;
      default:
        el.innerHTML = formatear(lineas.join(" "));
    }
  });
}

try {
  colocarTextos();
} catch (error) {
  console.warn("No se pudieron cargar los textos de textos.js:", error);
}

// Menú del móvil: abrir y cerrar
const botonMenu = document.querySelector(".boton-menu");
const menu = document.querySelector(".menu");

if (botonMenu && menu) {
  botonMenu.addEventListener("click", () => {
    const abierto = menu.classList.toggle("abierto");
    botonMenu.classList.toggle("abierto", abierto);
    botonMenu.setAttribute("aria-expanded", abierto);
  });
  menu.querySelectorAll("a").forEach((enlace) =>
    enlace.addEventListener("click", () => {
      menu.classList.remove("abierto");
      botonMenu.classList.remove("abierto");
      botonMenu.setAttribute("aria-expanded", false);
    })
  );
}

// Animación suave: los bloques aparecen al bajar con el scroll
const bloques = document.querySelectorAll(".aparece");

if ("IntersectionObserver" in window) {
  const observador = new IntersectionObserver(
    (entradas) => {
      entradas.forEach((entrada) => {
        if (entrada.isIntersecting) {
          entrada.target.classList.add("visible");
          observador.unobserve(entrada.target);
        }
      });
    },
    { threshold: 0.12 }
  );
  bloques.forEach((bloque) => observador.observe(bloque));
} else {
  bloques.forEach((bloque) => bloque.classList.add("visible"));
}

// Buscador de apuntes: filtra las tarjetas por palabras y por tema, sin recargar la página.
// La selección se guarda en la dirección (?q=...&tema=...) para poder compartirla.
const buscador = document.querySelector("[data-buscador]");

if (buscador) {
  const campo = buscador.querySelector("input");
  const filtros = buscador.querySelectorAll(".filtro");
  const cuenta = buscador.querySelector(".buscador-cuenta");
  const tarjetas = document.querySelectorAll(".apunte-tarjeta");
  const vacio = document.querySelector(".buscador-vacio");
  const sinTildes = (t) => t.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  let tema = "";

  const filtrar = () => {
    const palabras = sinTildes(campo.value).split(/\s+/).filter(Boolean);
    let visibles = 0;
    tarjetas.forEach((tarjeta) => {
      const coincide =
        (!tema || tarjeta.dataset.tema.split(" ").includes(tema)) &&
        palabras.every((p) => tarjeta.dataset.buscar.includes(p));
      tarjeta.hidden = !coincide;
      if (coincide) visibles++;
    });
    filtros.forEach((f) => {
      const activo = f.dataset.tema === tema;
      f.classList.toggle("activo", activo);
      f.setAttribute("aria-pressed", activo);
    });
    const unidad = buscador.dataset.unidad || "apunte";
    cuenta.textContent = visibles === 1 ? `1 ${unidad}` : `${visibles} ${unidad}s`;
    vacio.hidden = visibles > 0;
    const parametros = new URLSearchParams();
    if (campo.value.trim()) parametros.set("q", campo.value.trim());
    if (tema) parametros.set("tema", tema);
    const consulta = parametros.toString();
    try {
      history.replaceState(null, "", consulta ? `?${consulta}` : location.pathname);
    } catch (error) { /* abierta con doble clic: la dirección no se puede cambiar */ }
  };

  const inicial = new URLSearchParams(location.search);
  campo.value = inicial.get("q") || "";
  const temaInicial = sinTildes(inicial.get("tema") || "");
  if ([...filtros].some((f) => f.dataset.tema === temaInicial)) tema = temaInicial;

  campo.addEventListener("input", filtrar);
  filtros.forEach((f) => f.addEventListener("click", () => { tema = f.dataset.tema; filtrar(); }));
  vacio.querySelector("[data-limpiar]").addEventListener("click", () => {
    campo.value = "";
    tema = "";
    filtrar();
    campo.focus();
  });
  filtrar();
}

// Herramientas: botón «Copiar enlace» de la ficha
document.querySelectorAll("[data-copiar-enlace]").forEach((boton) => {
  boton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(boton.dataset.copiarEnlace);
      boton.textContent = "¡Enlace copiado!";
    } catch (error) {
      prompt("Copia el enlace:", boton.dataset.copiarEnlace);
    }
  });
});

// Substack no deja mostrar su formulario en una web abierta como archivo (doble clic).
// En ese caso se cambia por un botón que lleva a la página de suscripción.
// Una vez publicada la web, se ve el formulario normal.
if (location.protocol === "file:") {
  const formulario = document.querySelector(".suscripcion iframe");
  if (formulario) {
    const aviso = document.createElement("div");
    aviso.className = "suscripcion-local";
    aviso.innerHTML =
      '<a class="boton boton-principal" href="https://ingeniolean.substack.com/subscribe" target="_blank" rel="noopener">Suscribirme en Substack →</a>' +
      "<small>Vista local: el formulario aparecerá aquí cuando la web esté publicada.</small>";
    formulario.replaceWith(aviso);
  }
}
