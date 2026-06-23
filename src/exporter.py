"""Generación del documento Word con la transcripción."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.shared import Pt

from .idioma import ETIQUETA_IDIOMA, NOMBRES_IDIOMA, detectar_idioma
from .transcriber import ResultadoTranscripcion, Segmento

# Pausa entre segmentos (segundos) a partir de la cual se inicia un párrafo nuevo
PAUSA_NUEVO_PARRAFO = 2.0

# Pausa a partir de la cual se asume un cambio de interlocutor
PAUSA_CAMBIO_INTERLOCUTOR = 4.0

# Duración máxima de un párrafo: los discursos continuos sin pausas se trocean
# en párrafos manejables para la revisión (el corte se hace en fin de segmento)
DURACION_MAX_PARRAFO = 40.0


@dataclass
class _Parrafo:
    inicio: float
    pausa_previa: float
    texto: str


def _formato_tiempo(segundos: float) -> str:
    s = int(segundos)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _agrupar_parrafos(segmentos: list[Segmento]) -> list[_Parrafo]:
    """Agrupa los segmentos en párrafos.

    Se abre párrafo nuevo ante una pausa natural, cuando el párrafo se alarga
    demasiado o cuando el idioma del segmento cambia respecto al del párrafo
    en curso (para que el cambio de idioma quede en su propio párrafo y pueda
    marcarse).
    """
    parrafos: list[_Parrafo] = []
    textos: list[str] = []
    inicio = 0.0
    pausa_previa = 0.0
    fin_anterior: float | None = None
    idioma_parrafo: str | None = None

    for seg in segmentos:
        pausa = seg.inicio - fin_anterior if fin_anterior is not None else 0.0
        idioma_seg = detectar_idioma(seg.texto)
        demasiado_largo = (seg.inicio - inicio) > DURACION_MAX_PARRAFO
        cambia_idioma = (
            idioma_seg is not None
            and idioma_parrafo is not None
            and idioma_seg != idioma_parrafo
        )
        if textos and (pausa > PAUSA_NUEVO_PARRAFO or demasiado_largo or cambia_idioma):
            parrafos.append(_Parrafo(inicio, pausa_previa, " ".join(textos)))
            textos = []
            idioma_parrafo = None
        if not textos:
            inicio = seg.inicio
            pausa_previa = pausa
        textos.append(seg.texto)
        if idioma_seg is not None and idioma_parrafo is None:
            idioma_parrafo = idioma_seg
        fin_anterior = seg.fin

    if textos:
        parrafos.append(_Parrafo(inicio, pausa_previa, " ".join(textos)))
    return parrafos


def exportar_word(
    resultado: ResultadoTranscripcion,
    ruta_salida: Path,
    titulo: str,
    modelo: str,
) -> Path:
    """Genera el documento .docx y devuelve su ruta.

    El texto va en párrafos limpios, sin marcas de tiempo. Solo se inserta una
    marca [hh:mm:ss] cuando se detecta un posible cambio de interlocutor
    (pausa larga) o un cambio de idioma castellano/valenciano.
    """
    ruta_salida.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    doc.add_heading(titulo, level=0)

    if resultado.multilingue:
        idioma_inicial = NOMBRES_IDIOMA.get(resultado.idioma, resultado.idioma)
        idioma = (
            "Detección automática por segmento "
            f"(idioma inicial: {idioma_inicial})"
        )
    else:
        idioma = NOMBRES_IDIOMA.get(resultado.idioma, resultado.idioma)
    metadatos = doc.add_paragraph()
    metadatos.add_run(
        f"Fecha: {datetime.now():%d/%m/%Y %H:%M}\n"
        f"Idioma: {idioma} (confianza {resultado.probabilidad_idioma:.0%})\n"
        f"Duración del audio: {_formato_tiempo(resultado.duracion)}\n"
        f"Modelo Whisper: {modelo}"
    ).font.size = Pt(9)

    doc.add_heading("Transcripción", level=1)

    idioma_anterior: str | None = None
    for n, parrafo in enumerate(_agrupar_parrafos(resultado.segmentos)):
        idioma_parrafo = detectar_idioma(parrafo.texto)
        cambio_idioma = (
            idioma_parrafo is not None
            and idioma_anterior is not None
            and idioma_parrafo != idioma_anterior
        )
        cambio_interlocutor = parrafo.pausa_previa > PAUSA_CAMBIO_INTERLOCUTOR

        if n == 0 or cambio_idioma or cambio_interlocutor:
            etiqueta = ETIQUETA_IDIOMA.get(idioma_parrafo or idioma_anterior or "")
            marca = f"[{_formato_tiempo(parrafo.inicio)}]"
            if etiqueta:
                marca += f" ({etiqueta})"
            p = doc.add_paragraph()
            run = p.add_run(marca)
            run.bold = True
            run.font.size = Pt(9)

        doc.add_paragraph(parrafo.texto)

        if idioma_parrafo is not None:
            idioma_anterior = idioma_parrafo

    doc.save(str(ruta_salida))
    return ruta_salida
