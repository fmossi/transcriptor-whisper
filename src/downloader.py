"""Descarga de la pista de audio de un vídeo de YouTube mediante yt-dlp."""

from pathlib import Path

import yt_dlp


def es_url(entrada: str) -> bool:
    """Devuelve True si la entrada parece una URL en lugar de un fichero local."""
    return entrada.lower().startswith(("http://", "https://", "www."))


def descargar_audio(url: str, directorio_destino: Path) -> tuple[Path, str]:
    """Descarga la mejor pista de audio del vídeo.

    Devuelve la ruta del fichero descargado y el título del vídeo.
    """
    directorio_destino.mkdir(parents=True, exist_ok=True)
    opciones = {
        "format": "bestaudio/best",
        "outtmpl": str(directorio_destino / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opciones) as ydl:
        info = ydl.extract_info(url, download=True)
        ruta = Path(ydl.prepare_filename(info))
        titulo = info.get("title") or ruta.stem
    if not ruta.exists():
        raise FileNotFoundError(f"yt-dlp no generó el fichero esperado: {ruta}")
    return ruta, titulo
