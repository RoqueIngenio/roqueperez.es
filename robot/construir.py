"""Robot de la web roqueperez.es.

Lo ejecuta GitHub Actions antes de publicar (ver .github/workflows/publicar.yml).
Copia la web a _site/ y, encima, mete el contenido vivo:

  1. Textos de la web   ← Notion, "BBDD. Textos de la web"
  2. Últimas ediciones  ← Substack de Ingenio Lean
  5. Herramientas       ← Notion, "BBDD. Herramientas" (las que están en Publicado) + la carpeta
                          herramientas/<dirección>/ (herramienta.html, descargas/ y compartir.png)
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
import urllib.parse
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
DS_HERRAMIENTAS = "05a9d069-733a-4681-a6ae-7c109803420d"
CODIGO_HERRAMIENTAS = RAIZ / "herramientas"  # una carpeta por herramienta: herramienta.html, descargas/, vista-previa.jpg, compartir.png
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
        enlace = enlace_limpio((t.get("href") or "").strip())
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
                              "enlace": enlace_limpio(propiedad(f, "Enlace ").get("url")) or f"{SUBSTACK}/archive"})
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


def enlace_limpio(url):
    """Quita los añadidos de seguimiento de Substack (?r=…&utm_…) de un enlace."""
    return url.split("?")[0].split("#")[0] if url and "substack.com" in url else url


def edicion_relacionada(pagina):
    """Enlace y título de la edición relacionada, si ya está publicada (Listo) o programada en
    Substack con su enlace (Tramitado). Las que están en preparación no se enlazan."""
    p = propiedad(pagina, "Edición relacionada")
    for rel in p.get("relation", [])[:1]:
        edicion = notion(f"pages/{rel['id']}")
        if (propiedad(edicion, "Estado").get("status") or {}).get("name") not in ("Listo", "Tramitado"):
            continue
        enlace = propiedad(edicion, "Enlace ").get("url") or propiedad(edicion, "Enlace").get("url")
        titulo = valor_texto(edicion, "Tarea")
        if enlace:
            return enlace_limpio(enlace), titulo
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
        p_tema = propiedad(f, "Tema")  # admite selección única o múltiple
        temas = ([o["name"] for o in p_tema.get("multi_select") or []]
                 or ([p_tema["select"]["name"]] if p_tema.get("select") else []))
        tema = " · ".join(temas)
        resumen = valor_texto(f, "Resumen para Google").strip()
        enlace, titulo_edicion = edicion_relacionada(f)
        carpeta = SALIDA / "apuntes-de-planta" / slug
        cuerpo = bloques_a_html(hijos(f["id"]), carpeta / "img", slug)
        apuntes.append({"id": f["id"], "estado": (propiedad(f, "Estado").get("status") or {}).get("name"),
                        "titulo": titulo, "slug": slug, "fecha": fecha, "tema": tema, "temas": temas,
                        "resumen": resumen, "enlace": enlace, "titulo_edicion": titulo_edicion,
                        "cuerpo": cuerpo, "carpeta": carpeta})
    apuntes.sort(key=lambda a: a["fecha"], reverse=True)
    return apuntes


def pagina_apunte(a, plantilla_blog, apuntes):
    """Usa la cabecera y el pie de la página del blog para mantener el diseño."""
    cabeza = plantilla_blog[: plantilla_blog.index("<main>")]
    pie = plantilla_blog[plantilla_blog.index("</main>"):]
    titulo = html.escape(a["titulo"])
    descripcion = html.escape(a["resumen"] or a["titulo"])
    url = f"{DOMINIO}/apuntes-de-planta/{a['slug']}/"
    cabeza = re.sub(r"<title>.*?</title>", f"<title>{titulo} · Apuntes de planta · Roque Pérez</title>", cabeza)
    cabeza = re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{descripcion}">', cabeza)
    cabeza = re.sub(r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{url}">', cabeza)
    cabeza = cabeza.replace('<meta property="og:type" content="website">', '<meta property="og:type" content="article">')
    cabeza = re.sub(r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{titulo}">', cabeza)
    cabeza = re.sub(r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{descripcion}">', cabeza)
    cabeza = re.sub(r'<meta property="og:url" content="[^"]*">', f'<meta property="og:url" content="{url}">', cabeza)
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
        {sigue_leyendo(a, apuntes)}
      </div>
    </article>
  """
    return cabeza + cuerpo + pie


def tarjeta_apunte(a, prefijo=""):
    """Tarjeta sencilla de un apunte, para la portada y para el «Sigue leyendo»."""
    meta = " · ".join(x for x in [fecha_bonita(a["fecha"]), a["tema"]] if x)
    return (f'          <a class="apunte-tarjeta" href="{prefijo}{a["slug"]}/">\n'
            f'            <span class="etiqueta">{html.escape(meta)}</span>\n'
            f'            <h3>{html.escape(a["titulo"])}</h3>\n'
            + (f'            <p>{html.escape(a["resumen"])}</p>\n' if a["resumen"] else "")
            + '            <span class="apunte-leer">Leer el apunte →</span>\n'
            "          </a>")


def ultimos_apuntes(apuntes):
    """Los tres últimos apuntes, para la portada."""
    return ('        <div class="ultimos-apuntes aparece">\n'
            '          <div class="ultimos-titulo"><h3>Los últimos apuntes</h3>'
            '<a href="apuntes-de-planta/index.html">Ver todos →</a></div>\n'
            '          <div class="apuntes-rejilla apuntes-rejilla-3">\n'
            + "\n".join(tarjeta_apunte(a, "apuntes-de-planta/") for a in apuntes[:3])
            + "\n          </div>\n        </div>\n        ")


def sigue_leyendo(a, apuntes):
    """Dos apuntes más para el final de cada apunte: primero los del mismo tema, luego los más recientes."""
    otros = [o for o in apuntes if o["slug"] != a["slug"]]
    otros.sort(key=lambda o: (not set(o["temas"]) & set(a["temas"]),))  # orden estable: conserva «más recientes primero»
    if not otros:
        return ""
    return ('<aside class="apunte-mas">\n          <h2>Sigue leyendo</h2>\n'
            '          <div class="apuntes-rejilla">\n'
            + "\n".join(tarjeta_apunte(o, "../") for o in otros[:2])
            + "\n          </div>\n        </aside>")


def para_buscar(texto):
    """Minúsculas y sin tildes, para que el buscador encuentre «gestion» en «Gestión»."""
    sin_tildes = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn").lower()


def clave_tema(tema):
    """«Mejora continua» → «mejora-continua»: una palabra por tema, para el filtro del buscador."""
    return re.sub(r"\s+", "-", para_buscar(tema).strip())


def listado_apuntes(apuntes):
    tarjetas, temas = [], {}
    for a in apuntes:
        meta = " · ".join(x for x in [fecha_bonita(a["fecha"]), a["tema"]] if x)
        for t in a["temas"]:
            temas[t] = temas.get(t, 0) + 1
        subtitulos = " ".join(re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h[23]>(.*?)</h[23]>", a["cuerpo"], re.S))
        buscar = para_buscar(html.unescape(" ".join([a["titulo"], a["resumen"], a["tema"] or "", subtitulos])))
        tarjetas.append(
            f'          <a class="apunte-tarjeta" href="{a["slug"]}/" data-tema="{html.escape(" ".join(clave_tema(t) for t in a["temas"]))}"'
            f' data-buscar="{html.escape(buscar)}">\n'
            f'            <span class="etiqueta">{html.escape(meta)}</span>\n'
            f'            <h2>{html.escape(a["titulo"])}</h2>\n'
            + (f'            <p>{html.escape(a["resumen"])}</p>\n' if a["resumen"] else "")
            + '            <span class="apunte-leer">Leer el apunte →</span>\n'
            "          </a>"
        )
    filtros = [f'<button type="button" class="filtro activo" data-tema="" aria-pressed="true">Todos <span>{len(apuntes)}</span></button>']
    filtros += [f'<button type="button" class="filtro" data-tema="{html.escape(clave_tema(t))}" aria-pressed="false">'
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


def escribir_sitemap(apuntes, herramientas):
    hoy = dt.date.today().isoformat()
    urls = [(f"{DOMINIO}/", hoy), (f"{DOMINIO}/apuntes-de-planta/", hoy), (f"{DOMINIO}/herramientas/", hoy)]
    urls += [(f"{DOMINIO}/apuntes-de-planta/{a['slug']}/", a["fecha"]) for a in apuntes]
    urls += [(f"{DOMINIO}/herramientas/{h['slug']}/", h["fecha"]) for h in herramientas]
    filas = "".join(f"  <url>\n    <loc>{u}</loc>\n    <lastmod>{f}</lastmod>\n  </url>\n" for u, f in urls)
    (SALIDA / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                       '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + filas + "</urlset>\n")


def pagina_404(plantilla_blog):
    """Página de error con el diseño de la web. GitHub Pages la sirve en cualquier dirección que no exista."""
    cabeza = plantilla_blog[: plantilla_blog.index("<main>")]
    pie = plantilla_blog[plantilla_blog.index("</main>"):]
    cabeza = re.sub(r"<title>.*?</title>", "<title>Página no encontrada · Roque Pérez Calvo</title>", cabeza)
    cabeza = re.sub(r'\s*<link rel="canonical" href="[^"]*">', '\n  <meta name="robots" content="noindex">', cabeza)
    cabeza = re.sub(r'\s*<meta (property="og:|name="twitter:)[^>]*>', "", cabeza)
    cabeza = cabeza.replace('href="../', 'href="/').replace('src="../', 'src="/').replace('href="index.html"', 'href="/apuntes-de-planta/"')
    cabeza = cabeza.replace('href="/index.html', 'href="/')
    pie = pie.replace('src="../', 'src="/').replace('href="../', 'href="/').replace('href="/index.html', 'href="/')
    cuerpo = """<main>
    <section class="blog-vacio">
      <div class="contenedor">
        <span class="sello">Error 404</span>
        <h1 style="font-size: clamp(2.2rem, 6vw, 3.6rem); font-weight: 800; margin-bottom: 18px;">Esta página no está en la libreta</h1>
        <p>Puede que la dirección esté mal escrita o que haya movido la página de sitio. Desde aquí puedes volver a lo importante.</p>
        <div class="botones" style="justify-content: center;">
          <a href="/" class="boton boton-principal">Ir al inicio →</a>
          <a href="/apuntes-de-planta/" class="boton boton-secundario">Ver los apuntes</a>
        </div>
      </div>
    </section>
  """
    return cabeza + cuerpo + pie


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


def enlaces_limpios():
    """Cambia los enlaces internos a «…/index.html» por su dirección limpia («…/»), que es
    la canónica. Así Google no encuentra dos direcciones para la misma página. En la
    carpeta local se mantienen con index.html para poder abrirlas con doble clic."""
    patron = re.compile(r'href="((?:\.\./)*(?:[\w-]+/)*)index\.html(#[^"]*)?"')
    for pagina in SALIDA.rglob("*.html"):
        texto = pagina.read_text()
        nuevo = patron.sub(lambda m: f'href="{m.group(1) or "./"}{m.group(2) or ""}"', texto)
        if nuevo != texto:
            pagina.write_text(nuevo)


# ---------------------------------------------------------------- herramientas

TIPOS_DESCARGA = ("PDF", "Plantilla", "Programa")


def tamano_bonito(n):
    return f"{n / 1024 / 1024:.1f} MB".replace(".", ",") if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


def herramientas_desde_notion():
    filas = consultar(DS_HERRAMIENTAS, {"property": "Estado", "select": {"equals": "Publicado"}})
    herramientas = []
    for f in filas:
        nombre = valor_texto(f, "Nombre").strip()
        if not nombre:
            continue
        tipo = (propiedad(f, "Tipo").get("select") or {}).get("name") or "Calculadora"
        slug = a_slug(valor_texto(f, "Dirección web").strip() or nombre)
        enlace, titulo_edicion = edicion_relacionada(f)
        archivos = []
        for a in propiedad(f, "Archivo").get("files") or []:
            url = (a.get("file") or a.get("external") or {}).get("url")
            if url:
                archivos.append({"nombre": a.get("name") or "archivo", "url": url})
        herramientas.append({
            "id": f["id"], "nombre": nombre, "tipo": tipo, "slug": slug,
            "descripcion": valor_texto(f, "Descripción").strip(),
            "destacada": bool(propiedad(f, "Destacada").get("checkbox")),
            "orden": propiedad(f, "Orden").get("number"),
            "fecha": valor_fecha(f, "Última revisión") or f["last_edited_time"][:10],
            "enlace": enlace, "titulo_edicion": titulo_edicion, "archivos": archivos,
        })
    herramientas.sort(key=lambda h: (h["orden"] is None, h["orden"] or 0, h["nombre"]))
    return herramientas


def botones_descarga(descargas, slug, principal=True):
    """Un botón por archivo: el PDF primero y en amarillo (si principal), el resto con contorno."""
    formatos = {"PDF": "PDF", "PPTX": "PowerPoint editable", "PPT": "PowerPoint editable", "XLSX": "Excel editable",
                "XLS": "Excel editable", "DOCX": "Word editable", "DOC": "Word editable", "ZIP": "ZIP"}
    botones = []
    for i, (nombre, tam) in enumerate(sorted(descargas, key=lambda d: (not d[0].lower().endswith(".pdf"), d[0]))):
        ext = nombre.rsplit(".", 1)[-1].upper() if "." in nombre else ""
        clase = "boton-principal" if principal and i == 0 else "boton-secundario"
        botones.append(f'<a class="boton {clase}" href="{html.escape(nombre)}" download '
                       f'data-goatcounter-click="descarga-{slug}-{ext.lower() or "archivo"}">'
                       f'Descargar {formatos.get(ext, ext)} · {tamano_bonito(tam)} ↓</a>')
    return botones


def contenido_desde_notion(pagina_id):
    """Lee las preguntas de un test escritas en el cuerpo de su página de Notion:
    cada título (H2) es un área; una cita debajo, la acción recomendada de esa área;
    cada elemento de lista numerada, una pregunta, con sus 4 respuestas como viñetas dentro,
    de menos a más dependencia. Lo demás (avisos, párrafos) se ignora."""
    areas = []
    for b in hijos(pagina_id):
        tipo = b["type"]
        texto = texto_plano(b.get(tipo, {}).get("rich_text")).strip()
        if tipo in ("heading_1", "heading_2", "heading_3") and texto:
            areas.append({"nombre": texto, "accion": "", "preguntas": []})
        elif tipo == "quote" and areas:
            areas[-1]["accion"] = texto
        elif tipo == "numbered_list_item" and areas and texto:
            opciones = [texto_plano(h.get(h["type"], {}).get("rich_text")).strip()
                        for h in (hijos(b["id"]) if b.get("has_children") else [])]
            opciones = [o for o in opciones if o]
            if len(opciones) == 4:
                areas[-1]["preguntas"].append({"texto": texto, "opciones": opciones})
    areas = [a for a in areas if a["preguntas"]]
    return areas or None


def preparar_herramienta(h):
    """Deja en _site/herramientas/<dirección>/ lo que necesita la ficha: el código de la calculadora
    o los archivos para descargar. Devuelve el HTML que va en el cuerpo de la ficha."""
    carpeta = SALIDA / "herramientas" / h["slug"]
    carpeta.mkdir(parents=True, exist_ok=True)
    origen = CODIGO_HERRAMIENTAS / h["slug"]
    h["imagen"] = None
    if (origen / "compartir.png").exists():
        shutil.copy(origen / "compartir.png", carpeta / "compartir.png")
        h["imagen"] = f"{DOMINIO}/herramientas/{h['slug']}/compartir.png"
    descargas = []
    if (origen / "descargas").is_dir():
        for f in sorted((origen / "descargas").iterdir()):
            if f.is_file() and not f.name.startswith("."):
                shutil.copy(f, carpeta / f.name)
                descargas.append((f.name, f.stat().st_size))
    if h["tipo"] not in TIPOS_DESCARGA:
        codigo = origen / "herramienta.html"
        if not codigo.exists():
            raise RuntimeError(f"falta {codigo.relative_to(RAIZ)}")
        cuerpo = codigo.read_text()
        if "/*CONTENIDO_NOTION*/null" in cuerpo:  # textos editables desde la página de Notion (p. ej. un test)
            contenido = contenido_desde_notion(h["id"])
            if contenido:
                datos = json.dumps(contenido, ensure_ascii=False).replace("</", "<\\/")
                cuerpo = cuerpo.replace("/*CONTENIDO_NOTION*/null", datos, 1)
            else:
                print(f"Aviso: {h['slug']}: la página de Notion no tiene preguntas válidas; se usan las de reserva")
        if descargas:  # una calculadora con plantilla para trabajar sin conexión (p. ej. un Excel)
            extra = ('<section class="descarga descarga-extra">\n  <h2>¿Prefieres hacerlo en tu Excel?</h2>\n'
                     '  <p>Descarga la plantilla, pega tus datos y se calcula sola, sin conexión.</p>\n'
                     f'  <div class="botones">{"".join(botones_descarga(descargas, h["slug"], principal=False))}</div>\n</section>\n')
            marca = "<!-- NOTAS"
            cuerpo = cuerpo.replace(marca, extra + marca, 1) if marca in cuerpo else cuerpo + extra
        return cuerpo
    # Descargables: primero los de la carpeta descargas/ del repositorio; si no hay, los de Notion
    if not descargas:
        for a in h["archivos"]:
            nombre = re.sub(r"[^\w.\-]+", "-", unicodedata.normalize("NFKD", a["nombre"]).encode("ascii", "ignore").decode()).strip("-").lower() or "archivo"
            datos = urllib.request.urlopen(urllib.request.Request(a["url"], headers={"User-Agent": NAVEGADOR}), timeout=60).read()
            (carpeta / nombre).write_bytes(datos)
            descargas.append((nombre, len(datos)))
    if not descargas:
        raise RuntimeError("no tiene archivos: ni en descargas/ ni en la propiedad Archivo de Notion")
    botones = botones_descarga(descargas, h["slug"])
    vista = ""
    imagen_vista = next((origen / n for n in ("vista-previa.jpg", "vista-previa.png") if (origen / n).exists()), None)
    if imagen_vista:  # imagen de cómo es el documento, antes de descargarlo
        shutil.copy(imagen_vista, carpeta / imagen_vista.name)
        vista = (f'<figure class="descarga-vista"><img src="{imagen_vista.name}" alt="Vista previa: {html.escape(h["nombre"])}" '
                 f'loading="lazy" width="1400"></figure>\n')
    return (vista + '<div class="descarga">\n  <div class="botones">' + "".join(botones) + "</div>\n"
            '  <p class="descarga-nota">Gratis y sin registro. Si te resulta útil, compártelo con quien le pueda servir.</p>\n</div>')


def pagina_herramienta(h, cuerpo_herramienta, plantilla):
    """Ficha de una herramienta con la cabecera y el pie de la biblioteca."""
    cabeza = plantilla[: plantilla.index("<main>")]
    pie = plantilla[plantilla.index("</main>"):]
    nombre = html.escape(h["nombre"])
    descripcion = html.escape(h["descripcion"] or h["nombre"])
    url = f"{DOMINIO}/herramientas/{h['slug']}/"
    cabeza = re.sub(r"<title>.*?</title>", f"<title>{nombre} · Herramientas · Roque Pérez</title>", cabeza)
    cabeza = re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{descripcion}">', cabeza)
    cabeza = re.sub(r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{url}">', cabeza)
    cabeza = re.sub(r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{nombre}">', cabeza)
    cabeza = re.sub(r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{descripcion}">', cabeza)
    cabeza = re.sub(r'<meta property="og:url" content="[^"]*">', f'<meta property="og:url" content="{url}">', cabeza)
    if h["imagen"]:
        cabeza = re.sub(r'<meta property="og:image" content="[^"]*">', f'<meta property="og:image" content="{h["imagen"]}">', cabeza)
    cabeza = cabeza.replace('href="../', 'href="../../').replace('src="../', 'src="../../')
    cabeza = cabeza.replace('<a href="index.html">Herramientas</a>', '<a href="../index.html">Herramientas</a>')
    pie = pie.replace('src="../', 'src="../../')
    datos_google = json.dumps({
        "@context": "https://schema.org",
        "@type": "WebApplication" if h["tipo"] not in TIPOS_DESCARGA else "CreativeWork",
        "name": h["nombre"], "description": h["descripcion"], "url": url, "inLanguage": "es",
        "isAccessibleForFree": True, "dateModified": h["fecha"],
        "author": {"@type": "Person", "name": "Roque Pérez Calvo", "url": f"{DOMINIO}/"},
    }, ensure_ascii=False)
    cabeza = cabeza.replace("</head>", f'  <script type="application/ld+json">{datos_google}</script>\n</head>')

    num = numero_edicion(h["titulo_edicion"] or "")
    if h["enlace"]:
        texto_edicion = (f'En <strong>{html.escape(h["titulo_edicion"] or "esta edición")}</strong> lo cuento a fondo. '
                         'Y cada dos lunes, a las 7:00, un problema real de planta, su causa raíz y una herramienta para aplicarla ese mismo lunes.')
        boton_edicion = f'<a class="boton boton-secundario" href="{html.escape(h["enlace"])}" target="_blank" rel="noopener">Leer la edición{f" #{num}" if num else ""}</a>'
    else:
        texto_edicion = "Cada dos lunes, a las 7:00, un problema real de planta, su causa raíz y una herramienta para aplicarla ese mismo lunes."
        boton_edicion = ""
    notas = ""
    if "<!-- NOTAS" in cuerpo_herramienta:  # notas de cálculo de la herramienta: van al final de la página
        cuerpo_herramienta, notas = cuerpo_herramienta.split("<!-- NOTAS", 1)
        notas = notas.split("-->", 1)[1].strip()
        notas = f'<p class="herramienta-notas">{notas}</p>' if notas else ""
    q = urllib.parse.quote
    compartir = (f'<div class="compartir-redes">\n'
                 f'          <span>Compártela:</span>\n'
                 f'          <a href="https://www.linkedin.com/sharing/share-offsite/?url={q(url, safe="")}" target="_blank" rel="noopener" data-goatcounter-click="compartir-linkedin-{h["slug"]}">LinkedIn</a>\n'
                 f'          <a href="https://wa.me/?text={q(h["nombre"] + " " + url, safe="")}" target="_blank" rel="noopener" data-goatcounter-click="compartir-whatsapp-{h["slug"]}">WhatsApp</a>\n'
                 f'          <button type="button" data-copiar-enlace="{url}">Copiar enlace</button>\n'
                 f'        </div>')
    cuerpo = f"""<main>
    <section class="herramienta-intro">
      <div class="contenedor">
        <a class="apunte-volver" href="../index.html">← Todas las herramientas</a>
        <p class="etiqueta">Herramientas para tu fábrica · {html.escape(h["tipo"])}</p>
        <h1>{nombre}</h1>
        {f'<p class="entradilla">{descripcion}</p>' if h["descripcion"] else ""}
      </div>
    </section>
    <section class="herramienta-cuerpo">
      <div class="contenedor">
{cuerpo_herramienta}
      </div>
    </section>
    <section class="herramienta-cierre">
      <div class="contenedor">
        <div class="herramienta-cierre-texto">
          <span class="etiqueta">Ingenio Lean{f" · #{num}" if num else ""}</span>
          <h2>¿Y ahora qué hago con esto?</h2>
          <p>{texto_edicion}</p>
        </div>
        <div class="botones">
          {boton_edicion}
          <a class="boton boton-principal" href="{SUBSTACK}" target="_blank" rel="noopener">Suscríbete a Ingenio Lean →</a>
        </div>
        {compartir}
        {notas}
      </div>
    </section>
  """
    return cabeza + cuerpo + pie


def listado_herramientas(herramientas):
    tarjetas, tipos = [], {}
    for h in herramientas:
        tipos[h["tipo"]] = tipos.get(h["tipo"], 0) + 1
        accion = "Descargar gratis →" if h["tipo"] in TIPOS_DESCARGA else "Hacer el test →" if h["tipo"] == "Test" else "Usar gratis →"
        buscar = para_buscar(" ".join([h["nombre"], h["descripcion"], h["tipo"]]))
        tarjetas.append(
            f'          <a class="apunte-tarjeta herramienta-tarjeta" href="{h["slug"]}/" data-tema="{clave_tema(h["tipo"])}" data-buscar="{html.escape(buscar)}">\n'
            f'            <span class="etiqueta">{html.escape(h["tipo"])}</span>\n'
            f'            <h2>{html.escape(h["nombre"])}</h2>\n'
            + (f'            <p>{html.escape(h["descripcion"])}</p>\n' if h["descripcion"] else "")
            + f'            <span class="apunte-leer">{accion}</span>\n'
            "          </a>"
        )
    filtros = ""
    if len(tipos) > 1:
        botones = [f'<button type="button" class="filtro activo" data-tema="" aria-pressed="true">Todas <span>{len(herramientas)}</span></button>']
        botones += [f'<button type="button" class="filtro" data-tema="{clave_tema(t)}" aria-pressed="false">{html.escape(t)} <span>{n}</span></button>'
                    for t, n in sorted(tipos.items())]
        filtros = ('          <div class="buscador-temas" role="group" aria-label="Filtrar por tipo">\n            '
                   + "\n            ".join(botones) + "\n          </div>\n")
    buscador = (
        '        <div class="buscador" data-buscador data-unidad="herramienta">\n'
        '          <label class="buscador-campo">\n'
        '            <span class="etiqueta">Buscar una herramienta</span>\n'
        '            <input type="search" placeholder="reuniones, costes, plantilla…" autocomplete="off" spellcheck="false">\n'
        '          </label>\n' + filtros +
        '          <p class="buscador-cuenta" aria-live="polite"></p>\n'
        '        </div>\n'
    )
    vacio = (
        '        <div class="buscador-vacio" hidden>\n'
        '          <span class="sello">Sin resultados</span>\n'
        '          <p>Todavía no tengo ninguna herramienta sobre eso. Prueba con otra palabra.</p>\n'
        '          <button type="button" class="boton boton-secundario" data-limpiar>Ver todas las herramientas</button>\n'
        '        </div>\n'
    )
    return ('    <section class="blog-listado">\n      <div class="contenedor">\n' + buscador
            + '        <div class="apuntes-rejilla">\n' + "\n".join(tarjetas) + "\n        </div>\n" + vacio
            + "      </div>\n    </section>\n    ")


def herramientas_portada(herramientas):
    """Bloque de herramientas destacadas en la sección de Ingenio Lean de la portada."""
    destacadas = [h for h in herramientas if h["destacada"]][:3]
    if not destacadas:
        return "        "
    tarjetas = "\n".join(
        f'          <a class="edicion herramienta-portada aparece" href="herramientas/{h["slug"]}/">\n'
        f'            <span class="herramienta-portada-tipo">{html.escape(h["tipo"])}</span>\n'
        f'            <h4>{html.escape(h["nombre"])}</h4>\n'
        + (f'            <p>{html.escape(h["descripcion"])}</p>\n' if h["descripcion"] else "")
        + f'            <span class="edicion-fecha">{"Descargar →" if h["tipo"] in TIPOS_DESCARGA else "Hacer el test →" if h["tipo"] == "Test" else "Usar gratis →"}</span>\n'
        "          </a>" for h in destacadas)
    return ('        <div class="ediciones-titulo aparece herramientas-portada-titulo">\n'
            '          <h3 data-texto="lean.herramientas-titulo">Herramientas para tu fábrica</h3>\n'
            '          <a href="herramientas/index.html" data-texto="lean.herramientas-enlace">Ver todas →</a>\n'
            '        </div>\n'
            '        <div class="ediciones">\n' + tarjetas + "\n        </div>\n        ")


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
    # Las carpetas de cada herramienta solo se publican si están en Publicado: las monta el robot más abajo
    for carpeta in (SALIDA / "herramientas").iterdir():
        if carpeta.is_dir():
            shutil.rmtree(carpeta)

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
        textos["portada.nota-ediciones"] = f"**{total} ediciones** de Ingenio Lean"
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
        (a["carpeta"] / "index.html").write_text(volcar_textos(pagina_apunte(a, blog, apuntes), textos))
    if apuntes:
        blog = reemplazar_entre(blog, "APUNTES", listado_apuntes(apuntes))
        portada = reemplazar_entre(portada, "ULTIMOS", ultimos_apuntes(apuntes))
    estado["apuntes"] = [a["slug"] for a in apuntes]

    # 5. Herramientas
    plantilla_herr = (SALIDA / "herramientas" / "index.html").read_text()
    herramientas, publicadas = paso("herramientas_notion", herramientas_desde_notion) or [], []
    for h in herramientas:
        try:
            cuerpo_h = preparar_herramienta(h)
        except Exception as e:  # una herramienta rota no tumba las demás
            estado.setdefault("herramientas_errores", {})[h["slug"]] = str(e)
            shutil.rmtree(SALIDA / "herramientas" / h["slug"], ignore_errors=True)
            continue
        (SALIDA / "herramientas" / h["slug"] / "index.html").write_text(
            volcar_textos(pagina_herramienta(h, cuerpo_h, plantilla_herr), textos))
        publicadas.append(h)
    if publicadas:
        plantilla_herr = reemplazar_entre(plantilla_herr, "HERRAMIENTAS", listado_herramientas(publicadas))
    portada = reemplazar_entre(portada, "HERRAMIENTAS-PORTADA", herramientas_portada(publicadas))
    estado["herramientas"] = [h["slug"] for h in publicadas]

    escribir_textos_js(textos)
    (SALIDA / "index.html").write_text(volcar_textos(portada, textos))
    (SALIDA / "apuntes-de-planta" / "index.html").write_text(volcar_textos(blog, textos))
    (SALIDA / "herramientas" / "index.html").write_text(volcar_textos(plantilla_herr, textos))
    escribir_sitemap(apuntes, publicadas)
    (SALIDA / "404.html").write_text(volcar_textos(pagina_404(blog), textos))

    if apuntes and os.environ.get("MARCAR_PUBLICADOS") == "si":
        paso("marcar_publicados", lambda: marcar_publicados(apuntes))

    enlaces_limpios()
    versionar_archivos()
    (SALIDA / "robot-estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=2))
    print(json.dumps(estado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
