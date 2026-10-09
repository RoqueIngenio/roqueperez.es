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
