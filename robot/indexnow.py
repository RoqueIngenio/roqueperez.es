"""Avisa a Bing (y al resto de buscadores de IndexNow) de las páginas nuevas o cambiadas.

Se usa en dos pasos dentro de GitHub Actions:
  python3 robot/indexnow.py preparar   # tras generar la web y antes de publicarla: compara el sitemap nuevo con el publicado
  python3 robot/indexnow.py avisar     # tras publicar: envía la lista a IndexNow
  python3 robot/indexnow.py todo       # a mano: envía todas las páginas del sitemap (por ejemplo, la primera vez)
"""
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DOMINIO = "https://roqueperez.es"
HOST = "roqueperez.es"
CLAVE = next(p.stem for p in RAIZ.glob("*.txt") if len(p.stem) == 32 and p.read_text().strip() == p.stem)
PENDIENTES = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "indexnow.json"
FIJAS = {f"{DOMINIO}/", f"{DOMINIO}/apuntes-de-planta/", f"{DOMINIO}/herramientas/", f"{DOMINIO}/dinero/"}
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def leer_sitemap(xml):
    raiz = ET.fromstring(xml)
    return {u.findtext("s:loc", namespaces=NS): u.findtext("s:lastmod", namespaces=NS) for u in raiz.findall("s:url", NS)}


def preparar():
    nuevo = leer_sitemap((RAIZ / "_site" / "sitemap.xml").read_bytes())
    try:
        publicado = leer_sitemap(urllib.request.urlopen(f"{DOMINIO}/sitemap.xml", timeout=30).read())
    except Exception as e:
        print(f"No se pudo leer el sitemap publicado ({e}); no se avisa de nada")
        publicado = nuevo
    cambiadas = [u for u, f in nuevo.items() if u not in FIJAS and publicado.get(u) != f]
    if cambiadas:  # si hay algo nuevo, también han cambiado la portada y el listado de su sección
        secciones = {f"{DOMINIO}/" + u[len(DOMINIO) + 1:].split("/")[0] + "/" for u in cambiadas}
        cambiadas = [f"{DOMINIO}/"] + sorted(secciones) + cambiadas
    PENDIENTES.write_text(json.dumps(cambiadas))
    print(f"Páginas para IndexNow: {cambiadas or 'ninguna'}")


def enviar(urls):
    if not urls:
        print("IndexNow: nada nuevo que avisar")
        return
    datos = {"host": HOST, "key": CLAVE, "keyLocation": f"{DOMINIO}/{CLAVE}.txt", "urlList": urls}
    peticion = urllib.request.Request("https://api.indexnow.org/indexnow", data=json.dumps(datos).encode(),
                                      headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(peticion, timeout=30) as r:
        print(f"IndexNow respondió {r.status} para {len(urls)} páginas")


if __name__ == "__main__":
    orden = sys.argv[1] if len(sys.argv) > 1 else ""
    if orden == "preparar":
        preparar()
    elif orden == "avisar":
        enviar(json.loads(PENDIENTES.read_text()) if PENDIENTES.exists() else [])
    elif orden == "todo":
        enviar(list(leer_sitemap(urllib.request.urlopen(f"{DOMINIO}/sitemap.xml", timeout=30).read())))
    else:
        print(__doc__)
