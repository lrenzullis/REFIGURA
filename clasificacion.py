"""Lee un vídeo de SQLite; un JSON guarda el progreso y el dataset clasificado."""
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import date
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REASONING_EFFORT = "medium"
MAX_OUTPUT_TOKENS = 16384
TONOS = ["Positive", "Negative", "Neutral", "Mixed", None]
INTENCIONES = ["Praise", "Irony", "Hype", "Direct Critique", "Constructive Critique", "Nostalgia", "Humor"]
META = ["Politics", "Social Commentary", "Fan Culture", "Anti-critic Defense", "Pop References"]
ESQUEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "Tone": {"type": ["string", "null"], "enum": TONOS},
        "Intention": {"type": "array", "items": {"type": "string", "enum": INTENCIONES}},
        "Metaconversation": {"anyOf": [
            {"type": "array", "items": {"type": "string", "enum": ["None"]}, "minItems": 1, "maxItems": 1},
            {"type": "array", "items": {"type": "string", "enum": META}, "minItems": 1},
        ]},
        "Emotion": {"type": "array", "items": {"type": "string"}},
        "Confidence": {"type": "integer", "minimum": -1, "maximum": 100},
    },
    "required": ["Tone", "Intention", "Metaconversation", "Emotion", "Confidence"],
}


def _guardar(ruta, datos):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(ruta.suffix + ".tmp")
    with temporal.open("w", encoding="utf-8", newline="\n") as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=2, allow_nan=False)
        archivo.flush()
        os.fsync(archivo.fileno())
    os.replace(temporal, ruta)


def _sin_duplicados(pares):
    # Se usa también como parse_constant para rechazar NaN e Infinity.
    if isinstance(pares, str):
        raise ValueError("JSON con un número no válido.")
    resultado = {}
    for clave, valor in pares:
        if clave in resultado:
            raise ValueError("JSON con claves duplicadas.")
        resultado[clave] = valor
    return resultado


def _validar(datos, ids):
    if not isinstance(datos, dict) or set(datos) != {"results"}:
        raise ValueError("El lote debe devolver únicamente results.")
    resultados = datos["results"]
    if not isinstance(resultados, dict) or set(resultados) != set(ids):
        raise ValueError("Los IDs devueltos no coinciden con los del lote.")
    for analisis in resultados.values():
        if not isinstance(analisis, dict) or set(analisis) != set(ESQUEMA["required"]):
            raise ValueError("El análisis contiene campos ausentes o adicionales.")
        tono, confianza = analisis["Tone"], analisis["Confidence"]
        if tono not in TONOS or type(confianza) is not int or not -1 <= confianza <= 100:
            raise ValueError("Tone o Confidence no válidos.")
        if (tono is None) != (confianza == -1):
            raise ValueError("Tone=null requiere Confidence=-1, y viceversa.")
        for campo in ("Intention", "Metaconversation", "Emotion"):
            etiquetas = analisis[campo]
            if not isinstance(etiquetas, list) or any(not isinstance(e, str) for e in etiquetas):
                raise ValueError(f"{campo} debe ser una lista de textos.")
            if len(etiquetas) != len(set(etiquetas)):
                raise ValueError(f"{campo} contiene etiquetas repetidas.")
        intenciones, meta = analisis["Intention"], analisis["Metaconversation"]
        if any(e not in INTENCIONES for e in intenciones):
            raise ValueError("Etiqueta de Intention desconocida.")
        if "Direct Critique" in intenciones and "Constructive Critique" in intenciones:
            raise ValueError("Las dos etiquetas de crítica son excluyentes.")
        if not meta or any(e not in META + ["None"] for e in meta) or ("None" in meta and meta != ["None"]):
            raise ValueError("Metaconversation no válida; None debe aparecer sola.")


def ejecutar(args):

    aliases_path = Path(__file__).with_name("videos.json")
    if aliases_path.exists():
        aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
        args.video_id = aliases.get(args.video_id, args.video_id)

    
    entrada, salida = Path(args.entrada).resolve(), Path(args.salida).resolve()
    if entrada == salida:
        raise ValueError("La salida debe ser diferente de la base de datos.")
    video = args.video_id
    if not isinstance(video, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        raise ValueError("El valor indicado no es un video_id válido ni un alias definido en videos.json.")
    if type(args.lote) is not int or not 1 <= args.lote <= 100:
        raise ValueError("--lote debe estar entre 1 y 100.")
    if args.limite is not None and (type(args.limite) is not int or args.limite < 1):
        raise ValueError("--limite debe ser un entero mayor que 0.")
    if not args.obra.strip() or not args.modelo.strip():
        raise ValueError("Indica una obra y un modelo.")
    if args.estreno and date.fromisoformat(args.estreno).isoformat() != args.estreno:
        raise ValueError("--estreno debe usar YYYY-MM-DD.")
    for nombre, valor in (
        ("--desde-fecha", args.desde_fecha),
        ("--hasta-fecha", args.hasta_fecha),
    ):
        if valor and date.fromisoformat(valor).isoformat() != valor:
            raise ValueError(f"{nombre} debe usar YYYY-MM-DD.")

    if (
        args.desde_fecha
        and args.hasta_fecha
        and args.desde_fecha > args.hasta_fecha
    ):
        raise ValueError("--desde-fecha no puede ser posterior a --hasta-fecha.")
    
    lectura = {"object_pairs_hook": _sin_duplicados, "parse_constant": _sin_duplicados}
    # La lectura es consistente y sólo permite leer; una ruta errónea no crea otra base.
    with closing(sqlite3.connect(entrada.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("La base SQLite no tiene una versión de ingesta compatible.")
        fila = db.execute("""
            SELECT video_id, limite, solo_principales, created_at, revision, completo
            FROM ingestas WHERE video_id = ?
        """, (video,)).fetchone()
        if fila is None:
            raise ValueError("No hay una ingesta para ese vídeo en la base indicada.")
        origen = dict(fila)
        if origen.pop("completo") != 1:
            raise ValueError("La ingesta del vídeo debe estar completa antes de clasificar.")
        comentarios = [dict(fila) for fila in db.execute("""
            SELECT video_id, comment_id, parent_id, text, published_at, updated_at,
                like_count, reply_count
            FROM comentarios
            WHERE video_id = ?
                AND (? IS NULL OR substr(published_at, 1, 10) >= ?)
                AND (? IS NULL OR substr(published_at, 1, 10) <= ?)
            ORDER BY position
            LIMIT COALESCE(?, -1)
        """, (
            video,
            args.desde_fecha, args.desde_fecha,
            args.hasta_fecha, args.hasta_fecha,
            args.limite,
        ))]
    if not comentarios:
        raise ValueError("No hay comentarios para clasificar.")
    for comentario in comentarios:
        if (not isinstance(comentario, dict) or comentario.get("video_id") != video
                or not isinstance(comentario.get("comment_id"), str) or not comentario["comment_id"].strip()
                or not isinstance(comentario.get("text"), str) or not comentario["text"].strip()
                or "analysis" in comentario):
            raise ValueError("El corpus contiene un comentario inválido.")
    ids = [c["comment_id"] for c in comentarios]
    if len(ids) != len(set(ids)):
        raise ValueError("El corpus contiene IDs duplicados.")
    # El hash corresponde al vídeo seleccionado, no a los bytes de toda la base.
    contenido = json.dumps({"ingesta": origen, "comentarios": comentarios}, sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    plantilla = Path(__file__).with_name("prompt.txt").read_text(encoding="utf-8").strip()
    if not plantilla:
        raise ValueError("El archivo prompt.txt está vacío.")
    config = {
        "video_id": video, "obra": args.obra, "estreno": args.estreno,
        "modelo": args.modelo, "lote": args.lote, "limite": args.limite, "reasoning_effort": REASONING_EFFORT, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "entrada_sha256": hashlib.sha256(contenido).hexdigest(),
        "desde_fecha": args.desde_fecha,
        "hasta_fecha": args.hasta_fecha,
        "prompt_sha256": hashlib.sha256(plantilla.encode("utf-8")).hexdigest(),
        "esquema_sha256": hashlib.sha256(json.dumps(ESQUEMA, sort_keys=True).encode("utf-8")).hexdigest(),
    }
    estado = {"config": config, "completo": False, "resultados": []}
    if salida.exists():
        if not args.reanudar:
            raise ValueError("La salida ya existe. Usa --reanudar o elige otro archivo.")
        estado = json.loads(salida.read_text(encoding="utf-8"), **lectura)
        if not isinstance(estado, dict) or estado.get("config") != config:
            raise ValueError("Han cambiado los datos, el prompt o los parámetros; utiliza otra salida.")
        resultados = estado.get("resultados")
        if not isinstance(resultados, list) or len(resultados) > len(comentarios):
            raise ValueError("El archivo de resultados no es válido.")
        for original, guardado in zip(comentarios, resultados):
            if not isinstance(guardado, dict) or {k: v for k, v in guardado.items() if k != "analysis"} != original:
                raise ValueError("Los resultados guardados no corresponden al prefijo del corpus.")
            _validar({"results": {original["comment_id"]: guardado.get("analysis")}}, [original["comment_id"]])
        if type(estado.get("completo")) is not bool or estado["completo"] != (len(resultados) == len(comentarios)):
            raise ValueError("El estado de finalización del archivo no es coherente.")
        if len(resultados) < len(comentarios) and len(resultados) % args.lote:
            raise ValueError("Los resultados no terminan en un lote completo.")
        if estado["completo"]:
            print(f"Clasificación ya completa: {len(resultados)} comentarios en {salida}")
            return estado
    elif args.reanudar:
        raise ValueError("No existe el archivo que se quiere reanudar.")
    clave = os.getenv("OPENAI_API_KEY", "").strip()
    if not clave or "\n" in clave or "\r" in clave:
        raise ValueError("Falta una OPENAI_API_KEY válida en el entorno.")
    cabeceras = {"Authorization": f"Bearer {clave}", "Content-Type": "application/json"}
    for variable, cabecera in (("OPENAI_PROJECT_ID", "OpenAI-Project"), ("OPENAI_ORG_ID", "OpenAI-Organization")):
        if os.getenv(variable):
            cabeceras[cabecera] = os.environ[variable]
    if not salida.exists():
        _guardar(salida, estado)
    for inicio in range(len(estado["resultados"]), len(comentarios), args.lote):
        lote = comentarios[inicio:inicio + args.lote]
        esperados = [c["comment_id"] for c in lote]
        valores = {"movie": args.obra, "release_date": args.estreno or "unknown", "batch_size": str(len(lote))}
        prompt = re.sub(r"\{(movie|release_date|batch_size)\}", lambda m: valores[m.group(1)], plantilla)
        esquema = {
            "type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "object", "additionalProperties": False,
                "properties": {cid: {"$ref": "#/$defs/analysis"} for cid in esperados}, "required": esperados}},
            "$defs": {"analysis": ESQUEMA},
        }
        solicitud = {
            "model": args.modelo, "instructions": prompt,
            "input": json.dumps({"comments": [{k: c[k] for k in ("comment_id", "text", "published_at")
                                               if c.get(k) is not None} for c in lote]}, ensure_ascii=False),
            "reasoning": {"effort": config["reasoning_effort"]}, "max_output_tokens": config["max_output_tokens"],
            "store": False, "service_tier": "default",
            "text": {"format": {"type": "json_schema", "name": "audience_comments", "strict": True, "schema": esquema}},
        }
        peticion = Request("https://api.openai.com/v1/responses", headers=cabeceras,
                           data=json.dumps(solicitud, ensure_ascii=False).encode("utf-8"), method="POST")
        print(f"Clasificando {inicio + 1}–{inicio + len(lote)} de {len(comentarios)}...", flush=True)
        try:
            with urlopen(peticion, timeout=180) as respuesta:
                bruto = json.loads(respuesta.read(), **lectura)
        except HTTPError as error:
            raise RuntimeError(f"OpenAI HTTP {error.code}; el último lote confirmado se conserva.") from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise RuntimeError("Fallo de conexión con OpenAI; el último lote confirmado se conserva.") from None
        if not isinstance(bruto, dict) or bruto.get("status") != "completed" or bruto.get("error"):
            raise ValueError("OpenAI no completó el lote; no se ha guardado.")
        mensajes = bruto.get("output")
        if not isinstance(mensajes, list) or any(not isinstance(m, dict) for m in mensajes):
            raise ValueError("OpenAI devolvió una respuesta mal formada; lote no guardado.")
        partes = []
        for mensaje in mensajes:
            if mensaje.get("type") == "message":
                if mensaje.get("status") not in (None, "completed"):
                    raise ValueError("OpenAI devolvió un mensaje incompleto; lote no guardado.")
                contenido = mensaje.get("content")
                if not isinstance(contenido, list) or any(not isinstance(p, dict) for p in contenido):
                    raise ValueError("OpenAI devolvió contenido mal formado; lote no guardado.")
                partes.extend(contenido)
        if any(p.get("type") == "refusal" for p in partes):
            raise ValueError("OpenAI rechazó clasificar el lote; no se ha guardado.")
        if any(not isinstance(p.get("text"), str) for p in partes if p.get("type") == "output_text"):
            raise ValueError("OpenAI devolvió una salida no textual; lote no guardado.")
        texto = "".join(p["text"] for p in partes if p.get("type") == "output_text")
        datos = json.loads(texto, **lectura)
        _validar(datos, esperados)
        estado["resultados"].extend({**c, "analysis": datos["results"][c["comment_id"]]} for c in lote)
        estado["completo"] = len(estado["resultados"]) == len(comentarios)
        _guardar(salida, estado)
    print(f"Clasificación completa: {len(comentarios)} comentarios en {salida}")
    return estado
