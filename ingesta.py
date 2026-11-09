"""Ingesta de YouTube: páginas crudas, comentarios y progreso en SQLite."""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


def _abrir(ruta):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conexion = sqlite3.connect(ruta, timeout=30)
    conexion.row_factory = sqlite3.Row
    try:
        conexion.execute("PRAGMA foreign_keys = ON")
        with conexion:
            conexion.execute("BEGIN IMMEDIATE")
            version = conexion.execute("PRAGMA user_version").fetchone()[0]
            tablas = {r[0] for r in conexion.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if version == 0 and not tablas:
                sentencias = (
                    """CREATE TABLE ingestas (
                        video_id TEXT PRIMARY KEY, limite INTEGER CHECK(limite IS NULL OR limite > 0),
                        solo_principales INTEGER NOT NULL CHECK(solo_principales IN (0, 1)),
                        cursor_json TEXT NOT NULL, completo INTEGER NOT NULL CHECK(completo IN (0, 1)),
                        revision INTEGER NOT NULL CHECK(revision >= 0),
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                    """CREATE TABLE paginas_crudas (
                        id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES ingestas(video_id),
                        recurso TEXT NOT NULL, parent_id TEXT NOT NULL, page_token TEXT NOT NULL,
                        next_page_token TEXT, request_json TEXT NOT NULL,
                        raw_response BLOB NOT NULL, fetched_at TEXT NOT NULL,
                        UNIQUE(video_id, recurso, parent_id, page_token), UNIQUE(video_id, id))""",
                    """CREATE TABLE comentarios (
                        video_id TEXT NOT NULL, comment_id TEXT NOT NULL CHECK(length(comment_id) > 0),
                        parent_id TEXT, text TEXT NOT NULL CHECK(length(text) > 0),
                        published_at TEXT NOT NULL, updated_at TEXT,
                        like_count INTEGER NOT NULL CHECK(like_count >= 0),
                        reply_count INTEGER NOT NULL CHECK(reply_count >= 0),
                        position INTEGER NOT NULL CHECK(position >= 0), source_page_id INTEGER NOT NULL,
                        PRIMARY KEY(video_id, comment_id), UNIQUE(video_id, position),
                        FOREIGN KEY(video_id) REFERENCES ingestas(video_id),
                        FOREIGN KEY(video_id, parent_id) REFERENCES comentarios(video_id, comment_id),
                        FOREIGN KEY(video_id, source_page_id) REFERENCES paginas_crudas(video_id, id))""",
                    "CREATE INDEX comentarios_padres ON comentarios(video_id, parent_id, position)",
                )
                for sentencia in sentencias:
                    conexion.execute(sentencia)
                conexion.execute("PRAGMA user_version = 1")
            elif version != 1 or tablas != {"ingestas", "paginas_crudas", "comentarios"}:
                raise ValueError("La base de datos no corresponde a esta versión del proyecto; utiliza otra ruta.")
        if conexion.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
            raise RuntimeError("No se pudo activar el modo WAL de SQLite.")
        conexion.execute("PRAGMA synchronous = FULL")
        return conexion
    except BaseException:
        conexion.close()
        raise


def _pagina(recurso, parametros, clave):
    url = "https://www.googleapis.com/youtube/v3/" + recurso
    try:
        with urlopen(url + "?" + urlencode({**parametros, "key": clave}), timeout=45) as respuesta:
            bruto = respuesta.read()
            datos = json.loads(bruto)
    except HTTPError as error:
        raise RuntimeError(f"YouTube HTTP {error.code}. Comprueba cuota, vídeo y credenciales.") from None
    except (URLError, OSError, HTTPException):
        raise RuntimeError("Se interrumpió la conexión con YouTube; reanuda la ingesta.") from None
    except (ValueError, UnicodeError):
        raise ValueError("YouTube no devolvió un JSON válido; la página no se ha guardado.") from None
    if not isinstance(datos, dict) or not isinstance(datos.get("items"), list):
        raise ValueError("Respuesta de YouTube sin una lista items válida.")
    siguiente = datos.get("nextPageToken")
    if siguiente is not None and (not isinstance(siguiente, str) or not siguiente):
        raise ValueError("YouTube devolvió un cursor de página inválido.")
    return datos, bruto


def _normalizar(item, video_id, padre):
    try:
        respuestas = 0
        if padre is None:
            hilo = item["snippet"]
            if hilo["videoId"] != video_id:
                raise ValueError("El hilo recibido pertenece a otro vídeo.")
            respuestas, item = hilo["totalReplyCount"], hilo["topLevelComment"]
        sn, cid = item["snippet"], item["id"]
        texto = sn.get("textOriginal", sn.get("textDisplay"))
        if not isinstance(cid, str) or not cid or any(c.isspace() for c in cid):
            raise ValueError("Comentario sin identificador válido.")
        if not isinstance(texto, str) or not texto.strip():
            print("\nComentario sin texto válido:")
            print("comment_id:", cid)
            print("snippet completo:", sn)
            print(json.dumps(item, ensure_ascii=False, indent=2))
        if sn.get("parentId") != padre or cid == padre:
            raise ValueError("La respuesta recibida no corresponde al comentario padre.")
        publicado, actualizado = sn["publishedAt"], sn.get("updatedAt")
        if not isinstance(publicado, str):
            raise ValueError("Comentario sin fecha de publicación válida.")
        for fecha in (publicado, actualizado):
            if fecha is not None and (not isinstance(fecha, str) or
                    datetime.fromisoformat(fecha.replace("Z", "+00:00")).tzinfo is None):
                raise ValueError("Fecha sin formato o zona horaria válidos.")
        likes = sn.get("likeCount", 0)
        if any(type(n) is not int or n < 0 for n in (likes, respuestas)):
            raise ValueError("Comentario con contadores inválidos.")
        return {"video_id": video_id, "comment_id": cid, "parent_id": padre,
                "text": texto, "published_at": publicado, "updated_at": actualizado,
                "like_count": likes, "reply_count": respuestas}
    except (KeyError, TypeError, AttributeError):
        raise ValueError("YouTube devolvió un comentario con estructura incompleta.") from None


def _guardar_pagina(conexion, estado, cursor, parametros, pagina, bruto):
    video, padre = estado["video_id"], parametros.get("parentId")
    recurso = "commentThreads" if cursor["fase"] == "principales" else "comments"
    lote = [_normalizar(item, video, padre) for item in pagina["items"]]
    siguiente = pagina.get("nextPageToken")
    nuevo = dict(cursor)
    ahora = datetime.now(timezone.utc).isoformat()
    with conexion:
        conexion.execute("BEGIN IMMEDIATE")
        actual = conexion.execute("SELECT revision, completo FROM ingestas WHERE video_id=?", (video,)).fetchone()
        if actual is None or tuple(actual) != (estado["revision"], 0):
            raise RuntimeError("Otra ejecución ha cambiado esta ingesta; reanuda para leer su progreso.")
        if siguiente is not None and (siguiente == cursor["pagina"] or conexion.execute(
                "SELECT 1 FROM paginas_crudas WHERE video_id=? AND recurso=? AND parent_id=? AND page_token=?",
                (video, recurso, padre or "", siguiente)).fetchone()):
            raise ValueError("YouTube repitió un cursor; la página no se ha guardado.")
        registro = conexion.execute(
            """INSERT INTO paginas_crudas(video_id,recurso,parent_id,page_token,next_page_token,
               request_json,raw_response,fetched_at) VALUES(?,?,?,?,?,?,?,?)""",
            (video, recurso, padre or "", cursor["pagina"] or "", siguiente,
             json.dumps(parametros, ensure_ascii=False), bruto, ahora))
        pagina_id = registro.lastrowid
        cantidad = estado["cantidad"]
        for comentario in lote:
            existente = conexion.execute("SELECT parent_id FROM comentarios WHERE video_id=? AND comment_id=?",
                                         (video, comentario["comment_id"])).fetchone()
            if existente is not None:
                if existente[0] != comentario["parent_id"]:
                    raise ValueError("Un comentario recibido cambia de padre; la página no se ha guardado.")
                continue
            if estado["limite"] is not None and cantidad >= estado["limite"]:
                continue
            if not comentario["text"].strip():
                print(
                    f"[AVISO] Comentario {comentario['comment_id']} ignorado: texto vacío "
                    f"(publishedAt={comentario.get('publishedAt')})"
                )
                continue
            conexion.execute(
                """INSERT INTO comentarios(video_id,comment_id,parent_id,text,published_at,updated_at,
                   like_count,reply_count,position,source_page_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (*comentario.values(), cantidad, pagina_id))
            cantidad += 1
        nuevo["pagina"] = siguiente
        if siguiente is None:
            if nuevo["fase"] == "principales":
                nuevo["fase"] = "respuestas"
            else:
                nuevo["indice_padre"] += 1
        padres = 0 if estado["solo_principales"] else conexion.execute(
            "SELECT COUNT(*) FROM comentarios WHERE video_id=? AND parent_id IS NULL AND reply_count>0",
            (video,)).fetchone()[0]
        completo = (estado["limite"] is not None and cantidad >= estado["limite"]) or (
            nuevo["fase"] == "respuestas" and nuevo["indice_padre"] == padres)
        conexion.execute(
            "UPDATE ingestas SET cursor_json=?,completo=?,revision=revision+1,updated_at=? WHERE video_id=?",
            (json.dumps(nuevo), int(completo), ahora, video))
    return nuevo, completo, cantidad


def ejecutar(args):
    ruta = Path(args.salida)
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", args.video_id):
        raise ValueError("El identificador del vídeo debe tener 11 caracteres.")
    if args.limite is not None and (type(args.limite) is not int or args.limite <= 0):
        raise ValueError("El límite debe ser positivo; omítelo para descargar todos.")
    if args.reanudar and not ruta.exists():
        raise ValueError("No existe la base de datos que se quiere reanudar.")
    conexion = _abrir(ruta)
    try:
        registro = conexion.execute("SELECT * FROM ingestas WHERE video_id=?", (args.video_id,)).fetchone()
        if registro is not None:
            if not args.reanudar:
                raise ValueError("El vídeo ya tiene una ingesta. Usa --reanudar o elige otra base de datos.")
            if registro["limite"] != args.limite or registro["solo_principales"] != int(args.solo_principales):
                raise ValueError("La configuración no coincide con la ingesta guardada.")
        else:
            if args.reanudar:
                raise ValueError("No existe una ingesta de ese vídeo para reanudar.")
            if not os.environ.get("YOUTUBE_API_KEY", "").strip():
                raise ValueError("Falta la variable de entorno YOUTUBE_API_KEY.")
            ahora = datetime.now(timezone.utc).isoformat()
            cursor = {"fase": "principales", "pagina": None, "indice_padre": 0}
            with conexion:
                conexion.execute("BEGIN IMMEDIATE")
                conexion.execute("INSERT INTO ingestas VALUES(?,?,?,?,?,?,?,?)", (
                    args.video_id, args.limite, int(args.solo_principales), json.dumps(cursor), 0, 0, ahora, ahora))
            registro = conexion.execute("SELECT * FROM ingestas WHERE video_id=?", (args.video_id,)).fetchone()
        estado = dict(registro)
        cursor = json.loads(estado["cursor_json"])
        cantidad, maxima = conexion.execute(
            "SELECT COUNT(*),COALESCE(MAX(position),-1) FROM comentarios WHERE video_id=?", (args.video_id,)).fetchone()
        estado["cantidad"] = cantidad
        padres = 0 if args.solo_principales else conexion.execute(
            "SELECT COUNT(*) FROM comentarios WHERE video_id=? AND parent_id IS NULL AND reply_count>0",
            (args.video_id,)).fetchone()[0]
        if (not isinstance(cursor, dict) or set(cursor) != {"fase", "pagina", "indice_padre"} or
                cursor["fase"] not in ("principales", "respuestas") or
                type(cursor["indice_padre"]) is not int or not 0 <= cursor["indice_padre"] <= padres or
                cursor["fase"] == "principales" and cursor["indice_padre"] != 0 or
                cursor["pagina"] is not None and (not isinstance(cursor["pagina"], str) or not cursor["pagina"]) or
                not estado["completo"] and cursor["fase"] == "respuestas" and cursor["indice_padre"] == padres or
                maxima != cantidad - 1 or args.limite is not None and cantidad > args.limite):
            raise ValueError("El progreso guardado de la ingesta no es válido.")
        if estado["completo"]:
            print(f"Ingesta ya completa: {cantidad} comentarios en {ruta}")
            return {"video_id": args.video_id, "completo": True, "cantidad": cantidad}
        clave = os.environ.get("YOUTUBE_API_KEY", "").strip()
        if not clave:
            raise ValueError("Falta la variable de entorno YOUTUBE_API_KEY.")
        campos_api = "id,snippet(textOriginal,textDisplay,publishedAt,updatedAt,likeCount,parentId)"
        while not estado["completo"]:
            principal = cursor["fase"] == "principales"
            parametros = {"part": "snippet", "maxResults": 100, "textFormat": "plainText"}
            if principal:
                parametros.update(videoId=args.video_id, order="time",
                    fields=f"nextPageToken,items(id,snippet(videoId,totalReplyCount,topLevelComment({campos_api})))")
            else:
                padre = conexion.execute(
                    """SELECT comment_id FROM comentarios WHERE video_id=? AND parent_id IS NULL
                       AND reply_count>0 ORDER BY position LIMIT 1 OFFSET ?""",
                    (args.video_id, cursor["indice_padre"])).fetchone()[0]
                parametros.update(parentId=padre, fields=f"nextPageToken,items({campos_api})")
            if cursor["pagina"]:
                parametros["pageToken"] = cursor["pagina"]
            pagina, bruto = _pagina("commentThreads" if principal else "comments", parametros, clave)
            cursor, completo, cantidad = _guardar_pagina(conexion, estado, cursor, parametros, pagina, bruto)
            estado["revision"] += 1
            estado["completo"] = completo
            estado["cantidad"] = cantidad
            print(f"Guardados: {cantidad} comentarios{' (completo)' if completo else ''}")
        return {"video_id": args.video_id, "completo": True, "cantidad": cantidad}
    finally:
        conexion.close()
