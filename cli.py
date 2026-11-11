"""Dos comandos: extraer comentarios a SQLite y clasificar un vídeo guardado."""
import argparse
import sqlite3
import sys
from pathlib import Path
from dotenv import load_dotenv

import ingesta
import clasificacion


def main():
    load_dotenv(
        Path(__file__).resolve().with_name(".env"),
        override=False,
        encoding="utf-8-sig",
    )

    parser = argparse.ArgumentParser(description="Pipeline mínimo AUDIOVISUALES")
    comandos = parser.add_subparsers(dest="comando", required=True)

    p = comandos.add_parser("ingesta", help="Descargar comentarios de un vídeo")
    p.add_argument("--video-id", required=True)
    p.add_argument("--limite", type=int, help="Máximo de comentarios; sin límite por defecto")
    p.add_argument("--solo-principales", action="store_true", help="Omitir las respuestas")
    p.add_argument("--db", "--salida", dest="salida", type=Path,
                   default=Path("datos/audiovisuales.sqlite"),
                   help="Base SQLite; por defecto datos/audiovisuales.sqlite")
    p.add_argument("--reanudar", action="store_true")

    p = comandos.add_parser("clasificacion", help="Clasificar comentarios ya descargados")
    p.add_argument("--db", "--entrada", dest="entrada", type=Path,
                   default=Path("datos/audiovisuales.sqlite"),
                   help="Base SQLite; por defecto datos/audiovisuales.sqlite")
    p.add_argument("--video-id", required=True, help="Vídeo guardado que se quiere clasificar")
    p.add_argument("--obra", required=True)
    p.add_argument("--estreno", help="Fecha YYYY-MM-DD, si se conoce")
    p.add_argument("--desde-fecha", help="Clasificar comentarios desde esta fecha inclusive (YYYY-MM-DD)")
    p.add_argument("--hasta-fecha", help="Clasificar comentarios hasta esta fecha inclusive (YYYY-MM-DD)")
    p.add_argument("--modelo", default="gpt-6-luna")
    p.add_argument("--lote", type=int, default=20)
    p.add_argument("--limite",  type=int, default=None, help="Máximo de comentarios que se clasificarán")
    p.add_argument("--salida", type=Path)
    p.add_argument("--reanudar", action="store_true")

    args = parser.parse_args()
    if args.comando == "clasificacion":
        args.salida = args.salida or args.entrada.parent / args.video_id / "clasificaciones.json"
    try:
        if args.comando == "ingesta":
            estado = ingesta.ejecutar(args)
            cantidad = estado["cantidad"]
        else:
            estado = clasificacion.ejecutar(args)
            cantidad = len(estado["resultados"])
        print(f"Finalizado: {cantidad} comentarios. Archivo: {args.salida}")
        return 0
    except KeyboardInterrupt:
        print("Interrumpido. Repite el comando con --reanudar para continuar.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
