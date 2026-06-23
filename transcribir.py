"""Transcriptor Whisper local.

Transcribe un fichero de audio/vídeo o un enlace de YouTube a un documento Word.

Ejemplos:
    python transcribir.py "C:\\videos\\charla.mp4" --idioma es
    python transcribir.py "https://www.youtube.com/watch?v=XXXX" --idioma ca --modelo medium
    python transcribir.py entrada.mp3 --salida resultado.docx
"""

import argparse
import re
import sys
import tempfile
import time
from pathlib import Path

from src.downloader import descargar_audio, es_url
from src.exporter import exportar_word
from src.transcriber import MODELOS_VALIDOS, transcribir

DIRECTORIO_SALIDA = Path(__file__).parent / "salida"

# La consola de Windows suele usar cp1252; evita errores al imprimir títulos con
# caracteres fuera de ese juego (emojis, comillas tipográficas, etc.)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _nombre_seguro(texto: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", texto).strip() or "transcripcion"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe audio/vídeo o un enlace de YouTube a un documento Word.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("entrada", help="Fichero de audio/vídeo o URL de YouTube")
    parser.add_argument(
        "--idioma",
        choices=["es", "ca", "auto"],
        default="auto",
        help=(
            "Idioma de la transcripción. Con 'auto' se detecta el idioma en "
            "cada segmento, soportando audios que alternan castellano y valenciano"
        ),
    )
    parser.add_argument(
        "--modelo",
        choices=list(MODELOS_VALIDOS),
        default="large-v3",
        help="Modelo Whisper a usar (large-v3 = máxima fidelidad, lento en CPU)",
    )
    parser.add_argument(
        "--salida",
        type=Path,
        default=None,
        help="Ruta del .docx de salida (por defecto: salida\\<nombre>.docx)",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="whisper_") as tmp:
        # 1. Resolver la entrada
        if es_url(args.entrada):
            print(f"Descargando audio de: {args.entrada}")
            try:
                ruta_audio, titulo = descargar_audio(args.entrada, Path(tmp))
            except Exception as e:
                print(f"ERROR al descargar el vídeo: {e}", file=sys.stderr)
                return 1
            print(f"Audio descargado: {titulo}")
        else:
            ruta_audio = Path(args.entrada)
            if not ruta_audio.exists():
                print(f"ERROR: no existe el fichero '{ruta_audio}'", file=sys.stderr)
                return 1
            titulo = ruta_audio.stem

        # 2. Transcribir
        idioma = None if args.idioma == "auto" else args.idioma
        print(f"Cargando modelo '{args.modelo}' (se descarga la primera vez)...")
        inicio = time.monotonic()

        def progreso(procesado: float, total: float) -> None:
            pct = min(procesado / total * 100, 100) if total else 0
            print(f"\r  Transcribiendo... {pct:5.1f} %", end="", flush=True)

        progreso_cerrado = False

        def aviso(mensaje: str) -> None:
            # Cierra la línea de progreso ("\r ... %") antes del primer aviso
            nonlocal progreso_cerrado
            if not progreso_cerrado:
                print(flush=True)
                progreso_cerrado = True
            print(f"  {mensaje}", flush=True)

        try:
            resultado = transcribir(ruta_audio, args.modelo, idioma, progreso, aviso)
        except Exception as e:
            print(f"\nERROR durante la transcripción: {e}", file=sys.stderr)
            return 1

        transcurrido = time.monotonic() - inicio
        if resultado.multilingue:
            detalle_idioma = (
                f"multilingüe, idioma inicial: {resultado.idioma} "
                f"({resultado.probabilidad_idioma:.0%})"
            )
            if resultado.tramos_refinados:
                detalle_idioma += (
                    f", {resultado.tramos_refinados} tramo(s) de idioma corregidos"
                )
        else:
            detalle_idioma = (
                f"idioma: {resultado.idioma}, "
                f"confianza {resultado.probabilidad_idioma:.0%}"
            )
        print(
            f"\r  Transcripción completada en {transcurrido / 60:.1f} min "
            f"({detalle_idioma})"
        )

        if not resultado.segmentos:
            print("AVISO: no se detectó voz en el audio.", file=sys.stderr)
            return 1

        # 3. Exportar a Word
        ruta_salida = args.salida or DIRECTORIO_SALIDA / f"{_nombre_seguro(titulo)}.docx"
        if ruta_salida.suffix.lower() != ".docx":
            ruta_salida = ruta_salida.with_suffix(".docx")
        exportar_word(resultado, ruta_salida, titulo, args.modelo)
        print(f"Documento generado: {ruta_salida.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
