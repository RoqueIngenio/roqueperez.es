"""Robot de la web roqueperez.es.

Lo ejecuta GitHub Actions antes de publicar (ver .github/workflows/publicar.yml).
Copia la web a _site/ y, encima, mete el contenido vivo:

  1. Textos de la web   ← Notion, "BBDD. Textos de la web"
  2. Últimas ediciones  ← Substack de Ingenio Lean
  3. Apuntes de planta  ← Notion, "BBDD. Apuntes de planta" (los que están en Aprobado o Publicado)

Si una fuente falla, la web se publica igualmente con lo que ya tenía y el
fallo queda anotado en _site/robot-estado.json.

Solo usa la biblioteca estándar de Python: no hay nada que instalar.
"""

import datetime as dt
import html
import json
import os
import re
import shutil
import sys
import traceback
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "_site"
DOMINIO = "https://roqueperez.es"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_VERSION = "2025-09-03"
DS_TEXTOS = "148e0594-5608-499f-bbf6-87f5bd99bae7"
DS_APUNTES = "3f22a7a0-ee64-8050-8e95-000b911a2f83"
DS_PUBLICACIONES = "19c2a7a0-ee64-81ce-843d-000b3b7817fa"
NAVEGADOR = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
SUBSTACK = "https://ingeniolean.substack.com"

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MESES_LARGOS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

estado = {"fecha": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


# ---------------------------------------------------------------- utilidades

def pedir(url, metodo="GET", datos=None, cabeceras=None):
    cuerpo = json.dumps(datos).encode() if datos is not None else None
    peticion = urllib.request.Request(url, data=cuerpo, method=metodo, headers=cabeceras or {})
    if cuerpo is not None:
        peticion.add_header("Content-Type", "application/json")
    if not peticion.has_header("User-agent"):
        peticion.add_header("User-Agent", NAVEGADOR)
    with urllib.request.urlopen(peticion, timeout=30) as r:
        return r.read()


def notion(ruta, metodo="GET", datos=None):
    if not NOTION_TOKEN:
        raise RuntimeError("Falta el secreto NOTION_TOKEN")
    cabeceras = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NOTION_VERSION}
    try:
        return json.loads(pedir(f"https://api.notion.com/v1/{ruta}", metodo, datos, cabeceras))
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Notion {e.code} en {ruta}: {detalle}") from None


def consultar(data_source, filtro=None):
    filas, cursor = [], None
    while True:
        datos = {"page_size": 100}
        if filtro:
            datos["filter"] = filtro
        if cursor:
            datos["start_cursor"] = cursor
        r = notion(f"data_sources/{data_source}/query", "POST", datos)
        filas += r["results"]
        if not r.get("has_more"):
            return filas
        cursor = r["next_cursor"]


def hijos(bloque):
    todos, cursor = [], None
    while True:
        ruta = f"blocks/{bloque}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        r = notion(ruta)
        todos += r["results"]
        if not r.get("has_more"):
            return todos
        cursor = r["next_cursor"]


def texto_plano(rich):
    return "".join(t.get("plain_text", "") for t in rich or [])


def a_marcado(rich):
    """Texto enriquecido de Notion → el formato de textos.js (**, ==, ~~)."""
    salida = ""
    for t in rich or []:
        s = t.get("plain_text", "")
        a = t.get("annotations", {})
        if not s.strip():
            salida += s
            continue
        # Las marcas van pegadas al texto, sin espacios por dentro
        inicio, nucleo, fin = re.match(r"^(\s*)(.*?)(\s*)$", s, re.S).groups()
        if a.get("bold") and "**" not in nucleo:
            nucleo = f"**{nucleo}**"
        if a.get("strikethrough") and "~~" not in nucleo:
            nucleo = f"~~{nucleo}~~"
        salida += inicio + nucleo + fin
    return salida


def a_html(rich):
    """Texto enriquecido de Notion → HTML seguro (para los apuntes)."""
    salida = ""
    for t in rich or []:
        s = html.escape(t.get("plain_text", "")).replace("\n", "<br>")
        a = t.get("annotations", {})
        if a.get("code"):
            s = f"<code>{s}</code>"
        if a.get("bold"):
            s = f"<strong>{s}</strong>"
        if a.get("italic"):
            s = f"<em>{s}</em>"
        if a.get("strikethrough"):
            s = f"<s>{s}</s>"
        if a.get("underline"):
            s = f"<u>{s}</u>"
        enlace = (t.get("href") or "").strip()
        if enlace.startswith("http"):
            s = f'<a href="{html.escape(enlace)}" target="_blank" rel="noopener">{s}</a>'
        salida += s
    return salida


def propiedad(pagina, nombre):
    return pagina["properties"].get(nombre) or {}


def valor_texto(pagina, nombre):
    p = propiedad(pagina, nombre)
    return texto_plano(p.get(p.get("type"), [])) if p.get("type") in ("title", "rich_text") else ""


def valor_fecha(pagina, nombre):
    p = propiedad(pagina, nombre)
    if p.get("type") == "date" and p.get("date"):
        return p["date"]["start"][:10]
    if p.get("type") == "formula":
        f = p["formula"]
        if f.get("type") == "date" and f.get("date"):
            return f["date"]["start"][:10]
        if f.get("type") == "string" and f.get("string"):
            m = re.search(r"\d{4}-\d{2}-\d{2}", f["string"])
            if m:
                return m.group(0)
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", f["string"])
            if m:
                return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def a_slug(texto):
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:80] or "apunte"


def fecha_bonita(iso):
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {MESES_LARGOS[d.month - 1]} {d.year}"


def reemplazar_entre(html_texto, nombre, nuevo):
    inicio, fin = f"<!-- ROBOT:{nombre}:INICIO -->", f"<!-- ROBOT:{nombre}:FIN -->"
    i, j = html_texto.index(inicio) + len(inicio), html_texto.index(fin)
    return html_texto[:i] + "\n" + nuevo + html_texto[j:]


# ---------------------------------------------------------------- textos

def leer_textos_js():
    bruto = (SALIDA / "textos.js").read_text()
    bruto = bruto[bruto.index("`") + 1: bruto.rindex("`")]
    textos, clave = {}, None
    for linea in bruto.split("\n"):
        etiqueta = re.match(r"^\[([\w.\-]+)\]$", linea.strip())
        if etiqueta:
            clave = etiqueta.group(1)
            textos[clave] = []
        elif clave and not linea.strip().startswith("##"):
            textos[clave].append(linea)
    return {k: "\n".join(v).strip() for k, v in textos.items()}


def textos_desde_notion():
    textos = {}
    for fila in consultar(DS_TEXTOS):
        clave = valor_texto(fila, "Clave").strip()
        p = propiedad(fila, "Texto")
        if clave and p.get("type") == "rich_text":
            texto = a_marcado(p["rich_text"]).strip()
            if texto:
                textos[clave] = texto
    return textos


def escribir_textos_js(textos):
    cabecera = (SALIDA / "textos.js").read_text()
    cabecera = cabecera[: cabecera.index("window.TEXTOS_WEB")]
    cuerpo = "".join(f"\n[{k}]\n{v.replace('`', chr(39))}\n" for k, v in textos.items())
    (SALIDA / "textos.js").write_text(cabecera + "window.TEXTOS_WEB = `\n## Generado por el robot desde Notion. Edita los textos en Notion, no aquí.\n" + cuerpo + "`;\n")


def formatear(texto):
    texto = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", texto)
    texto = re.sub(r"==(.+?)==", r'<span class="subrayado">\1</span>', texto)
    return re.sub(r"~~(.+?)~~", r'<span class="tachado">\1</span>', texto)


def texto_a_html(texto, formato):
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    if formato == "parrafos":
        return "".join(f"<p>{formatear(p.strip())}</p>" for p in re.split(r"\n\s*\n", texto) if p.strip())
    if formato == "lista":
        items = []
        for l in lineas:
            corte = l.rfind(" · ")
            items.append(f"<li>{formatear(l[:corte])} <span>{l[corte + 1:]}</span></li>" if corte > 0 else f"<li>{formatear(l)}</li>")
        return "".join(items)
    if formato == "temas":
        return "".join(f'<span class="tema">{formatear(l)}</span>' for l in lineas)
    if formato == "libreta":
        return "".join(f"<p>{formatear(l)}</p>" for l in lineas)
    return formatear(" ".join(lineas))


def volcar_textos(html_texto, textos):
    """Escribe los textos dentro del HTML, para que Google los lea sin JavaScript."""
    for m in list(re.finditer(r'<(\w+)[^>]*\sdata-texto="([^"]+)"[^>]*>', html_texto))[::-1]:
        etiqueta, clave = m.group(1), m.group(2)
        if not textos.get(clave):
            continue
        formato = re.search(r'data-formato="([^"]+)"', m.group(0))
        prof, j = 1, m.end()
        patron = re.compile(r"<(/?)" + etiqueta + r"\b[^>]*>")
        while prof:
            c = patron.search(html_texto, j)
            prof += -1 if c.group(1) else 1
            j = c.end()
        cierre = j - len(f"</{etiqueta}>")
        html_texto = html_texto[: m.end()] + texto_a_html(textos[clave], formato.group(1) if formato else None) + html_texto[cierre:]
    return html_texto


# ---------------------------------------------------------------- ediciones

def ediciones_desde_substack():
    datos = json.loads(pedir(f"{SUBSTACK}/api/v1/archive?sort=new&limit=50", cabeceras={"Accept": "application/json"}))
    numeradas = [p for p in datos if re.match(r"^\d+\.", p.get("title", ""))]
    if not numeradas:
        raise RuntimeError("Substack no devolvió ediciones")
    return [{"titulo": p["title"], "subtitulo": p.get("subtitle") or "", "fecha": p["post_date"][:10],
             "enlace": p["canonical_url"]} for p in numeradas]


def ediciones_desde_notion():
    """Plan B: la BBDD Publicaciones de Ingenio Lean (filas de newsletter publicadas y con enlace)."""
    filas = consultar(DS_PUBLICACIONES, {"and": [
        {"property": "Plataforma", "select": {"equals": "Newsletter"}},
        {"property": "Estado", "status": {"equals": "Listo"}},
        {"property": "Enlace ", "url": {"is_not_empty": True}},
    ]})
    ediciones = []
    for f in filas:
        titulo = valor_texto(f, "Tarea").strip()
        fecha = valor_fecha(f, "Fecha Publicación")
        if re.match(r"^\d+\.", titulo) and fecha:
            ediciones.append({"titulo": titulo, "subtitulo": "", "fecha": fecha,
                              "enlace": propiedad(f, "Enlace ").get("url")})
    ediciones.sort(key=lambda e: e["fecha"], reverse=True)
    if not ediciones:
        raise RuntimeError("Notion no devolvió ediciones")
    return ediciones


def tarjetas_ediciones(ediciones):
    tarjetas = []
    for p in ediciones[:3]:
        num, titulo = re.match(r"^(\d+)\.\s*(.*)$", p["titulo"]).groups()
        subtitulo = p["subtitulo"].strip()
        if subtitulo and not subtitulo.endswith((".", "?", "!", "…")):
            subtitulo += "."
        d = dt.date.fromisoformat(p["fecha"])
        tarjetas.append(
            f'          <a class="edicion aparece" href="{html.escape(p["enlace"])}" target="_blank" rel="noopener">\n'
            f'            <span class="edicion-num">#{num}</span>\n'
            f"            <h4>{html.escape(titulo)}</h4>\n"
            + (f"            <p>{html.escape(subtitulo)}</p>\n" if subtitulo else "")
            + f'            <span class="edicion-fecha">{d.day} {MESES[d.month - 1]} {d.year}</span>\n'
            f"          </a>"
        )
    return '        <div class="ediciones">\n' + "\n".join(tarjetas) + "\n        </div>\n        "


# ---------------------------------------------------------------- apuntes

def bloques_a_html(bloques, carpeta_img, prefijo_img):
    partes, lista_abierta = [], None
    n_img = 0

    def cerrar_lista():
        nonlocal lista_abierta
        if lista_abierta:
            partes.append(f"</{lista_abierta}>")
            lista_abierta = None

    for b in bloques:
        tipo = b["type"]
        datos = b.get(tipo, {})
        if tipo in ("bulleted_list_item", "numbered_list_item"):
            etiqueta = "ul" if tipo == "bulleted_list_item" else "ol"
            if lista_abierta != etiqueta:
                cerrar_lista()
                partes.append(f"<{etiqueta}>")
                lista_abierta = etiqueta
            partes.append(f"<li>{a_html(datos.get('rich_text'))}</li>")
            continue
        cerrar_lista()
        if tipo == "paragraph":
            contenido = a_html(datos.get("rich_text"))
            if contenido.strip():
                partes.append(f"<p>{contenido}</p>")
        elif tipo in ("heading_1", "heading_2"):
            partes.append(f"<h2>{a_html(datos.get('rich_text'))}</h2>")
        elif tipo == "heading_3":
            partes.append(f"<h3>{a_html(datos.get('rich_text'))}</h3>")
        elif tipo == "quote":
            partes.append(f"<blockquote>{a_html(datos.get('rich_text'))}</blockquote>")
        elif tipo == "callout":
            partes.append(f'<div class="nota-apunte">{a_html(datos.get("rich_text"))}</div>')
        elif tipo == "divider":
            partes.append("<hr>")
        elif tipo == "image":
            origen = datos.get("file", {}).get("url") or datos.get("external", {}).get("url")
            if origen:
                n_img += 1
                ext = re.search(r"\.(png|jpe?g|gif|webp)", origen.split("?")[0].lower())
                nombre = f"{prefijo_img}-{n_img}.{ext.group(1) if ext else 'jpg'}"
                carpeta_img.mkdir(parents=True, exist_ok=True)
                (carpeta_img / nombre).write_bytes(pedir(origen))
                pie = texto_plano(datos.get("caption"))
                partes.append(f'<figure><img src="img/{nombre}" alt="{html.escape(pie)}" loading="lazy">'
                              + (f"<figcaption>{html.escape(pie)}</figcaption>" if pie else "") + "</figure>")
    cerrar_lista()
    return "\n".join(partes)


def edicion_relacionada(pagina):
    p = propiedad(pagina, "Edición relacionada")
    for rel in p.get("relation", [])[:1]:
        edicion = notion(f"pages/{rel['id']}")
        enlace = propiedad(edicion, "Enlace ").get("url") or propiedad(edicion, "Enlace").get("url")
        titulo = valor_texto(edicion, "Tarea")
        if enlace:
            return enlace, titulo
    return None, None


def apuntes_desde_notion():
    hoy = dt.date.today().isoformat()
    filas = consultar(DS_APUNTES, {"or": [
        {"property": "Estado", "status": {"equals": "Aprobado"}},
        {"property": "Estado", "status": {"equals": "Publicado"}},
    ]})
    apuntes = []
    for f in filas:
        fecha = valor_fecha(f, "Fecha de publicación") or f["created_time"][:10]
        if fecha > hoy:
            continue  # programado para más adelante
        titulo = valor_texto(f, "Idea").strip()
        if not titulo:
            continue
        slug = a_slug(valor_texto(f, "Dirección web").strip() or titulo)
        tema = (propiedad(f, "Tema").get("select") or {}).get("name")
        resumen = valor_texto(f, "Resumen para Google").strip()
        enlace, titulo_edicion = edicion_relacionada(f)
        carpeta = SALIDA / "apuntes-de-planta" / slug
        cuerpo = bloques_a_html(hijos(f["id"]), carpeta / "img", slug)
        apuntes.append({"id": f["id"], "estado": (propiedad(f, "Estado").get("status") or {}).get("name"),
                        "titulo": titulo, "slug": slug, "fecha": fecha, "tema": tema,
                        "resumen": resumen, "enlace": enlace, "titulo_edicion": titulo_edicion,
                        "cuerpo": cuerpo, "carpeta": carpeta})
    apuntes.sort(key=lambda a: a["fecha"], reverse=True)
    return apuntes


def pagina_apunte(a, plantilla_blog):
    """Usa la cabecera y el pie de la página del blog para mantener el diseño."""
    cabeza = plantilla_blog[: plantilla_blog.index("<main>")]
    pie = plantilla_blog[plantilla_blog.index("</main>"):]
    titulo = html.escape(a["titulo"])
    descripcion = html.escape(a["resumen"] or a["titulo"])
    url = f"{DOMINIO}/apuntes-de-planta/{a['slug']}/"
    cabeza = re.sub(r"<title>.*?</title>", f"<title>{titulo} · Apuntes de planta · Roque Pérez</title>", cabeza)
    cabeza = re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{descripcion}">', cabeza)
    cabeza = re.sub(r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{url}">', cabeza)
    cabeza = cabeza.replace('href="../', 'href="../../').replace('src="../', 'src="../../')
    pie = pie.replace('src="../', 'src="../../')
    cabeza = cabeza.replace('<a href="index.html">Apuntes de planta</a>', '<a href="../index.html">Apuntes de planta</a>')
    datos_google = json.dumps({
        "@context": "https://schema.org", "@type": "BlogPosting", "headline": a["titulo"],
        "description": a["resumen"], "datePublished": a["fecha"], "url": url, "inLanguage": "es",
        "author": {"@type": "Person", "name": "Roque Pérez Calvo", "url": f"{DOMINIO}/"},
    }, ensure_ascii=False)
    cabeza = cabeza.replace("</head>", f'  <script type="application/ld+json">{datos_google}</script>\n</head>')
    edicion = ""
    if a["enlace"]:
        nombre = html.escape(a["titulo_edicion"] or "la edición de Ingenio Lean")
        edicion = (f'<aside class="apunte-edicion"><span class="etiqueta">Para ir más a fondo</span>'
                   f'<p>Lo cuento con calma en <strong>{nombre}</strong>, una de las ediciones de Ingenio Lean.</p>'
                   f'<a class="boton boton-principal" href="{html.escape(a["enlace"])}" target="_blank" rel="noopener">Leer la edición completa →</a></aside>')
    meta = " · ".join(x for x in [fecha_bonita(a["fecha"]), a["tema"]] if x)
    cuerpo = f"""<main>
    <article class="apunte">
      <div class="contenedor apunte-contenedor">
        <a class="apunte-volver" href="../index.html">← Todos los apuntes</a>
        <p class="etiqueta">{html.escape(meta)}</p>
        <h1>{titulo}</h1>
        {f'<p class="entradilla">{descripcion}</p>' if a["resumen"] else ""}
        <div class="apunte-cuerpo">
{a["cuerpo"]}
        </div>
        {edicion}
        <aside class="apunte-suscripcion">
          <h2>¿Te ha servido?</h2>
          <p>Cada dos lunes envío una edición de Ingenio Lean con un problema real de planta, su causa raíz y una herramienta para aplicar.</p>
          <a class="boton boton-secundario" href="{SUBSTACK}/subscribe" target="_blank" rel="noopener">Suscribirme gratis →</a>
        </aside>
      </div>
    </article>
  """
    return cabeza + cuerpo + pie


def listado_apuntes(apuntes):
    tarjetas = []
    for a in apuntes:
        meta = " · ".join(x for x in [fecha_bonita(a["fecha"]), a["tema"]] if x)
        tarjetas.append(
            f'        <a class="apunte-tarjeta" href="{a["slug"]}/">\n'
            f'          <span class="etiqueta">{html.escape(meta)}</span>\n'
            f'          <h2>{html.escape(a["titulo"])}</h2>\n'
            + (f'          <p>{html.escape(a["resumen"])}</p>\n' if a["resumen"] else "")
            + '          <span class="apunte-leer">Leer el apunte →</span>\n'
            "        </a>"
        )
    return ('    <section class="blog-listado">\n      <div class="contenedor">\n'
            + "\n".join(tarjetas) + "\n      </div>\n    </section>\n    ")


def marcar_publicados(apuntes):
    for a in apuntes:
        if a["estado"] == "Aprobado":
            notion(f"pages/{a['id']}", "PATCH", {"properties": {"Estado": {"status": {"name": "Publicado"}}}})


def escribir_sitemap(apuntes):
    hoy = dt.date.today().isoformat()
    urls = [(f"{DOMINIO}/", hoy), (f"{DOMINIO}/apuntes-de-planta/", hoy)]
    urls += [(f"{DOMINIO}/apuntes-de-planta/{a['slug']}/", a["fecha"]) for a in apuntes]
    filas = "".join(f"  <url>\n    <loc>{u}</loc>\n    <lastmod>{f}</lastmod>\n  </url>\n" for u, f in urls)
    (SALIDA / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                       '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + filas + "</urlset>\n")


# ---------------------------------------------------------------- principal

def paso(nombre, funcion):
    try:
        resultado = funcion()
        estado[nombre] = "ok"
        return resultado
    except Exception as e:  # el fallo de una fuente no tumba la web
        estado[nombre] = f"error: {e}"
        print(f"[{nombre}] {e}", file=sys.stderr)
        traceback.print_exc()
        return None


def main():
    if SALIDA.exists():
        shutil.rmtree(SALIDA)
    shutil.copytree(RAIZ, SALIDA, ignore=shutil.ignore_patterns(".*", "_site", "robot"))

    portada = (SALIDA / "index.html").read_text()
    blog = (SALIDA / "apuntes-de-planta" / "index.html").read_text()

    # 1. Textos
    textos = leer_textos_js()
    de_notion = paso("textos_notion", textos_desde_notion)
    if de_notion:
        textos.update(de_notion)
        estado["textos"] = len(de_notion)

    # 2. Ediciones
    ediciones = paso("ediciones_substack", ediciones_desde_substack)
    if not ediciones:
        ediciones = paso("ediciones_notion", ediciones_desde_notion)
    if ediciones:
        textos["portada.nota-ediciones"] = f"**{len(ediciones)}** ediciones de Ingenio Lean"
        portada = reemplazar_entre(portada, "EDICIONES", tarjetas_ediciones(ediciones))
        estado["ediciones"] = len(ediciones)

    # 3. Apuntes
    apuntes = paso("apuntes_notion", apuntes_desde_notion) or []
    for a in apuntes:
        a["carpeta"].mkdir(parents=True, exist_ok=True)
        (a["carpeta"] / "index.html").write_text(volcar_textos(pagina_apunte(a, blog), textos))
    if apuntes:
        blog = reemplazar_entre(blog, "APUNTES", listado_apuntes(apuntes))
    estado["apuntes"] = [a["slug"] for a in apuntes]

    escribir_textos_js(textos)
    (SALIDA / "index.html").write_text(volcar_textos(portada, textos))
    (SALIDA / "apuntes-de-planta" / "index.html").write_text(volcar_textos(blog, textos))
    escribir_sitemap(apuntes)

    if apuntes and os.environ.get("MARCAR_PUBLICADOS") == "si":
        paso("marcar_publicados", lambda: marcar_publicados(apuntes))

    (SALIDA / "robot-estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=2))
    print(json.dumps(estado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
