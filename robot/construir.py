"""Robot de la web roqueperez.es.

Lo ejecuta GitHub Actions antes de publicar (ver .github/workflows/publicar.yml).
Copia la web a _site/ y, encima, mete el contenido vivo:

  1. Textos de la web   ← Notion, "BBDD. Textos de la web"
  2. Últimas ediciones  ← Substack de Ingenio Lean
  4. Mi estantería      ← Notion, "BBDD. Mi estantería" (los libros marcados como Visible)
  3. Apuntes de planta  ← Notion, "BBDD. Apuntes de planta" (los que están en Aprobado o Publicado
                          y cuya "Fecha programada" ya ha llegado)

Antes de todo eso, pone al día la BBDD Publicaciones de Notion:
  - pasa a "Listo" lo que está en "Tramitado" con fecha de publicación de hoy o anterior (hora de España)
  - rellena el "Enlace" de las ediciones de la newsletter que lo tengan vacío, buscándolas en Substack por su número

Si una fuente falla, la web se publica igualmente con lo que ya tenía y el
fallo queda anotado en _site/robot-estado.json.

Solo usa la biblioteca estándar de Python: no hay nada que instalar.
"""

import datetime as dt
import hashlib
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
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "_site"
DOMINIO = "https://roqueperez.es"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_VERSION = "2025-09-03"
DS_TEXTOS = "148e0594-5608-499f-bbf6-87f5bd99bae7"
DS_APUNTES = "3f22a7a0-ee64-8050-8e95-000b911a2f83"
DS_PUBLICACIONES = "19c2a7a0-ee64-81ce-843d-000b3b7817fa"
DS_ESTANTERIA = "ee45a84e-3008-415a-b2a4-1899803e0a41"
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

def marcar_listos():
    """Tramitado → Listo en la BBDD Publicaciones si la fecha de publicación es hoy o ya pasó."""
    hoy = dt.datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()
    filas = consultar(DS_PUBLICACIONES, {"and": [
        {"property": "Estado", "status": {"equals": "Tramitado"}},
        {"property": "Fecha Publicación", "date": {"on_or_before": hoy}},
    ]})
    for f in filas:
        notion(f"pages/{f['id']}", "PATCH", {"properties": {"Estado": {"status": {"name": "Listo"}}}})
    return [valor_texto(f, "Tarea").strip() for f in filas]


def ediciones_desde_substack():
    datos = json.loads(pedir(f"{SUBSTACK}/api/v1/archive?sort=new&limit=50", cabeceras={"Accept": "application/json"}))
    numeradas = [p for p in datos if re.match(r"^\d+\.", p.get("title", ""))]
    if not numeradas:
        raise RuntimeError("Substack no devolvió ediciones")
    return [{"titulo": p["title"], "subtitulo": p.get("subtitle") or "", "fecha": p["post_date"][:10],
             "enlace": p["canonical_url"]} for p in numeradas]


def ediciones_desde_feed():
    """Plan B para leer Substack: su feed RSS."""
    canal = ET.fromstring(pedir(f"{SUBSTACK}/feed")).find("channel")
    ediciones = []
    for item in canal.findall("item"):
        titulo = (item.findtext("title") or "").strip()
        if re.match(r"^\d+\.", titulo):
            fecha = parsedate_to_datetime(item.findtext("pubDate")).astimezone(ZoneInfo("Europe/Madrid")).date().isoformat()
            ediciones.append({"titulo": titulo, "subtitulo": html.unescape(item.findtext("description") or "").strip(),
                              "fecha": fecha, "enlace": (item.findtext("link") or "").strip()})
    if not ediciones:
        raise RuntimeError("El feed de Substack no devolvió ediciones")
    return ediciones


def numero_edicion(titulo):
    m = re.match(r"^\s*(\d+)\.", titulo or "")
    return int(m.group(1)) if m else None


def rellenar_enlaces():
    """Pone el enlace de Substack en las ediciones de la newsletter ya publicadas que no lo tienen."""
    hoy = dt.datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()
    filas = consultar(DS_PUBLICACIONES, {"and": [
        {"property": "Plataforma", "select": {"equals": "Newsletter"}},
        {"property": "Enlace ", "url": {"is_empty": True}},
        {"property": "Fecha Publicación", "date": {"on_or_before": hoy}},
    ]})
    if not filas:
        return []
    publicadas = None
    for fuente in (ediciones_desde_substack, ediciones_desde_feed):
        try:
            publicadas = fuente()
            break
        except Exception as e:
            print(f"[rellenar_enlaces] {fuente.__name__}: {e}", file=sys.stderr)
    if publicadas is None:
        raise RuntimeError("Substack no responde (ni la API ni el feed)")
    por_numero = {numero_edicion(p["titulo"]): p["enlace"] for p in publicadas}
    rellenadas = []
    for f in filas:
        titulo = valor_texto(f, "Tarea").strip()
        enlace = por_numero.get(numero_edicion(titulo))
        if enlace:
            notion(f"pages/{f['id']}", "PATCH", {"properties": {"Enlace ": {"url": enlace}}})
            rellenadas.append(titulo)
    return rellenadas


def ediciones_desde_notion():
    """Plan C: la BBDD Publicaciones de Ingenio Lean (filas de newsletter en Listo).
    Si a una le falta el enlace, la tarjeta lleva al archivo de Substack."""
    filas = consultar(DS_PUBLICACIONES, {"and": [
        {"property": "Plataforma", "select": {"equals": "Newsletter"}},
        {"property": "Estado", "status": {"equals": "Listo"}},
    ]})
    ediciones = []
    for f in filas:
        titulo = valor_texto(f, "Tarea").strip()
        fecha = valor_fecha(f, "Fecha Publicación")
        if re.match(r"^\d+\.", titulo) and fecha:
            ediciones.append({"titulo": titulo, "subtitulo": "", "fecha": fecha,
                              "enlace": propiedad(f, "Enlace ").get("url") or f"{SUBSTACK}/archive"})
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


# ---------------------------------------------------------------- estantería

def libros_desde_notion():
    filas = consultar(DS_ESTANTERIA, {"property": "Visible", "checkbox": {"equals": True}})
    libros = [{"titulo": valor_texto(f, "Libro").strip(), "autor": valor_texto(f, "Autor").strip(),
               "categoria": (propiedad(f, "Categoría").get("select") or {}).get("name") or "",
               "frase": valor_texto(f, "Por qué lo recomiendo").strip(),
               "orden": propiedad(f, "Orden").get("number")} for f in filas]
    libros = [l for l in libros if l["titulo"]]
    libros.sort(key=lambda l: (l["orden"] is None, l["orden"] or 0, l["titulo"]))
    if not libros:
        raise RuntimeError("Notion no devolvió libros")
    return libros


def estanteria(libros):
    tarjetas = []
    for l in libros:
        tarjetas.append(
            f'          <article class="libro libro-{a_slug(l["categoria"]) or "otros"}">\n'
            + (f'            <span class="libro-categoria">{html.escape(l["categoria"])}</span>\n' if l["categoria"] else "")
            + f'            <h4>{html.escape(l["titulo"])}</h4>\n'
            + (f'            <span class="libro-autor">{html.escape(l["autor"])}</span>\n' if l["autor"] else "")
            + (f'            <p>{html.escape(l["frase"])}</p>\n' if l["frase"] else "")
            + "          </article>"
        )
    return '        <div class="estanteria aparece">\n' + "\n".join(tarjetas) + "\n        </div>\n        "


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
    hoy = dt.datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()
    filas = consultar(DS_APUNTES, {"or": [
        {"property": "Estado", "status": {"equals": "Aprobado"}},
        {"property": "Estado", "status": {"equals": "Publicado"}},
    ]})
    apuntes = []
    for f in filas:
        programada = (valor_fecha(f, "Fecha programada") or valor_fecha(f, "Fecha de publicación")
                      or f["created_time"][:10])
        if programada > hoy:
            continue  # programado para más adelante
        # La fecha que se ve en la web es la del día en que se publicó de verdad
        fecha = valor_fecha(f, "Publicado el") or hoy
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
          <a class="boton boton-secundario" href="{SUBSTACK}" target="_blank" rel="noopener">Suscribirme gratis →</a>
        </aside>
      </div>
    </article>
  """
    return cabeza + cuerpo + pie


def para_buscar(texto):
    """Minúsculas y sin tildes, para que el buscador encuentre «gestion» en «Gestión»."""
    sin_tildes = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn").lower()


def listado_apuntes(apuntes):
    tarjetas, temas = [], {}
    for a in apuntes:
        meta = " · ".join(x for x in [fecha_bonita(a["fecha"]), a["tema"]] if x)
        if a["tema"]:
            temas[a["tema"]] = temas.get(a["tema"], 0) + 1
        subtitulos = " ".join(re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h[23]>(.*?)</h[23]>", a["cuerpo"], re.S))
        buscar = para_buscar(html.unescape(" ".join([a["titulo"], a["resumen"], a["tema"] or "", subtitulos])))
        tarjetas.append(
            f'          <a class="apunte-tarjeta" href="{a["slug"]}/" data-tema="{html.escape(para_buscar(a["tema"]))}"'
            f' data-buscar="{html.escape(buscar)}">\n'
            f'            <span class="etiqueta">{html.escape(meta)}</span>\n'
            f'            <h2>{html.escape(a["titulo"])}</h2>\n'
            + (f'            <p>{html.escape(a["resumen"])}</p>\n' if a["resumen"] else "")
            + '            <span class="apunte-leer">Leer el apunte →</span>\n'
            "          </a>"
        )
    filtros = [f'<button type="button" class="filtro activo" data-tema="" aria-pressed="true">Todos <span>{len(apuntes)}</span></button>']
    filtros += [f'<button type="button" class="filtro" data-tema="{html.escape(para_buscar(t))}" aria-pressed="false">'
                f'{html.escape(t)} <span>{n}</span></button>' for t, n in sorted(temas.items())]
    buscador = (
        '        <div class="buscador" data-buscador>\n'
        '          <label class="buscador-campo">\n'
        '            <span class="etiqueta">Buscar en la libreta</span>\n'
        '            <input type="search" placeholder="5S, reuniones, pedidos…" autocomplete="off" spellcheck="false">\n'
        '          </label>\n'
        '          <div class="buscador-temas" role="group" aria-label="Filtrar por tema">\n            '
        + "\n            ".join(filtros) + "\n"
        '          </div>\n'
        '          <p class="buscador-cuenta" aria-live="polite"></p>\n'
        '        </div>\n'
    )
    vacio = (
        '        <div class="buscador-vacio" hidden>\n'
        '          <span class="sello">Sin apuntes</span>\n'
        '          <p>No tengo nada apuntado sobre eso todavía. Prueba con otra palabra o con otro tema.</p>\n'
        '          <button type="button" class="boton boton-secundario" data-limpiar>Ver todos los apuntes</button>\n'
        '        </div>\n'
    )
    return ('    <section class="blog-listado">\n      <div class="contenedor">\n' + buscador
            + '        <div class="apuntes-rejilla">\n' + "\n".join(tarjetas) + "\n        </div>\n" + vacio
            + "      </div>\n    </section>\n    ")


def marcar_publicados(apuntes):
    for a in apuntes:
        if a["estado"] == "Aprobado":
            notion(f"pages/{a['id']}", "PATCH", {"properties": {
                "Estado": {"status": {"name": "Publicado"}},
                "Publicado el": {"date": {"start": a["fecha"]}},
            }})


def escribir_sitemap(apuntes):
    hoy = dt.date.today().isoformat()
    urls = [(f"{DOMINIO}/", hoy), (f"{DOMINIO}/apuntes-de-planta/", hoy)]
    urls += [(f"{DOMINIO}/apuntes-de-planta/{a['slug']}/", a["fecha"]) for a in apuntes]
    filas = "".join(f"  <url>\n    <loc>{u}</loc>\n    <lastmod>{f}</lastmod>\n  </url>\n" for u, f in urls)
    (SALIDA / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                       '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + filas + "</urlset>\n")


def versionar_archivos():
    """Añade ?v=… a estilos.css, script.js y textos.js en todas las páginas, para que
    el navegador no use una copia antigua cuando cambian."""
    version = {n: hashlib.md5((SALIDA / n).read_bytes()).hexdigest()[:8]
               for n in ("estilos.css", "script.js", "textos.js")}
    for pagina in SALIDA.rglob("*.html"):
        texto = pagina.read_text()
        nuevo = re.sub(r'(estilos\.css|script\.js|textos\.js)"', lambda m: f'{m.group(1)}?v={version[m.group(1)]}"', texto)
        if nuevo != texto:
            pagina.write_text(nuevo)


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

    # 0. Poner al día la BBDD Publicaciones (solo en GitHub)
    if os.environ.get("MARCAR_PUBLICADOS") == "si":
        estado["marcados_listo"] = paso("marcar_listos", marcar_listos) or []
        estado["enlaces_rellenados"] = paso("rellenar_enlaces", rellenar_enlaces) or []

    # 2. Ediciones
    ediciones = paso("ediciones_substack", ediciones_desde_substack)
    if not ediciones:
        ediciones = paso("ediciones_feed", ediciones_desde_feed)
    if not ediciones:
        ediciones = paso("ediciones_notion", ediciones_desde_notion)
    if ediciones:
        total = max(numero_edicion(e["titulo"]) for e in ediciones)  # el feed solo trae las 20 últimas
        textos["portada.nota-ediciones"] = f"**{total}** ediciones de Ingenio Lean"
        portada = reemplazar_entre(portada, "EDICIONES", tarjetas_ediciones(ediciones))
        estado["ediciones"] = total

    # 4. Estantería
    libros = paso("estanteria_notion", libros_desde_notion)
    if libros:
        portada = reemplazar_entre(portada, "ESTANTERIA", estanteria(libros))
        estado["libros"] = len(libros)

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

    versionar_archivos()
    (SALIDA / "robot-estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=2))
    print(json.dumps(estado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
