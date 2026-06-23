"""Transcripción de audio con faster-whisper en CPU."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

from .idioma import ETIQUETA_IDIOMA, detectar_idioma

MODELOS_VALIDOS = ("tiny", "base", "small", "medium", "large-v3")

FRECUENCIA_MUESTREO = 16000

# Duración máxima (segundos) de una "isla" de idioma: un tramo cuyo texto salió
# en un idioma distinto al de los tramos que lo rodean. Tramos más largos se
# consideran un cambio de idioma deliberado del orador y no se tocan.
DURACION_MAX_ISLA = 120.0

# Opciones de calidad compartidas por la pasada principal y la de refinado.
_OPCIONES_CALIDAD = dict(
    # Sin condicionar al texto previo: evita que el idioma del orador
    # anterior "arrastre" al siguiente y reduce alucinaciones en audios largos.
    condition_on_previous_text=False,
    # Búsqueda más exhaustiva: más lento pero más fiel (prioridad: fidelidad).
    beam_size=10,
    best_of=10,
    # Timestamps por palabra + umbral de silencio: descarta texto alucinado
    # en pausas largas (aplausos, cambios de turno, silencios del pleno).
    word_timestamps=True,
    hallucination_silence_threshold=2.0,
    vad_filter=True,
)


@dataclass
class Segmento:
    inicio: float
    fin: float
    texto: str
    logprob: float = 0.0


@dataclass
class ResultadoTranscripcion:
    idioma: str
    probabilidad_idioma: float
    duracion: float
    multilingue: bool = False
    tramos_refinados: int = 0
    segmentos: list[Segmento] = field(default_factory=list)

    @property
    def texto_completo(self) -> str:
        return " ".join(s.texto for s in self.segmentos)


def transcribir(
    ruta_audio: Path,
    modelo: str = "large-v3",
    idioma: str | None = None,
    progreso: Callable[[float, float], None] | None = None,
    aviso: Callable[[str], None] | None = None,
) -> ResultadoTranscripcion:
    """Transcribe un fichero de audio o vídeo.

    - `idioma`: "es", "ca" o None para autodetección multilingüe: el idioma se
      vuelve a detectar en cada segmento, de modo que un audio que alterna
      castellano y valenciano se transcribe correctamente en ambos. En este
      modo se aplica además una segunda pasada que corrige tramos cortos cuyo
      idioma se detectó mal (ver `_refinar_islas`).
    - `progreso`: callback opcional (segundos_procesados, duracion_total).
    - `aviso`: callback opcional para mensajes informativos del refinado.
    """
    model = WhisperModel(modelo, device="cpu", compute_type="int8")

    multilingue = idioma is None
    segmentos_iter, info = model.transcribe(
        str(ruta_audio),
        language=idioma,
        # Re-detecta el idioma en cada ventana: clave cuando los oradores
        # alternan castellano y valenciano dentro de la misma sesión.
        multilingual=multilingue,
        **_OPCIONES_CALIDAD,
    )

    resultado = ResultadoTranscripcion(
        idioma=info.language,
        probabilidad_idioma=info.language_probability,
        duracion=info.duration,
        multilingue=multilingue,
    )

    for seg in segmentos_iter:
        resultado.segmentos.append(
            Segmento(
                inicio=seg.start,
                fin=seg.end,
                texto=seg.text.strip(),
                logprob=seg.avg_logprob,
            )
        )
        if progreso:
            progreso(seg.end, info.duration)

    if multilingue and resultado.segmentos:
        _refinar_islas(model, ruta_audio, resultado, aviso)

    return resultado


def _refinar_islas(
    model: WhisperModel,
    ruta_audio: Path,
    resultado: ResultadoTranscripcion,
    aviso: Callable[[str], None] | None = None,
) -> None:
    """Segunda pasada: corrige tramos cortos con el idioma mal detectado.

    Whisper detecta el idioma por ventanas de ~30 s y, al confundirse entre
    castellano y valenciano (muy parecidos), no transcribe literalmente sino
    que "traduce" al idioma equivocado. Aquí se buscan "islas" (tramos cortos
    en un idioma rodeados por otro), se re-transcriben forzando el idioma del
    contexto y se conserva la versión que mejor encaja con el audio (mayor
    log-probabilidad media). Los cambios de idioma reales se conservan, porque
    en ellos la versión forzada encaja peor que la original.
    """

    def avisar(mensaje: str) -> None:
        if aviso:
            aviso(mensaje)

    islas = _detectar_islas(resultado.segmentos)
    if not islas:
        return

    avisar(f"Revisando {len(islas)} tramo(s) con posible idioma mal detectado...")
    audio = decode_audio(str(ruta_audio), sampling_rate=FRECUENCIA_MUESTREO)

    reemplazos: dict[int, tuple[list[int], list[Segmento]]] = {}
    for indices, idioma_contexto in islas:
        originales = [resultado.segmentos[i] for i in indices]
        inicio, fin = originales[0].inicio, originales[-1].fin
        rango = f"{_formato_tiempo(inicio)}-{_formato_tiempo(fin)}"
        nuevos = _retranscribir(model, audio, inicio, fin, idioma_contexto)
        if nuevos and _logprob_media(nuevos) > _logprob_media(originales):
            reemplazos[indices[0]] = (indices, nuevos)
            resultado.tramos_refinados += 1
            avisar(
                f"  {rango}: corregido, el audio está en "
                f"{ETIQUETA_IDIOMA[idioma_contexto]}"
            )
        else:
            avisar(f"  {rango}: se mantiene (el cambio de idioma parece real)")

    if reemplazos:
        reconstruidos: list[Segmento] = []
        i = 0
        while i < len(resultado.segmentos):
            if i in reemplazos:
                indices, nuevos = reemplazos[i]
                reconstruidos.extend(nuevos)
                i = indices[-1] + 1
            else:
                reconstruidos.append(resultado.segmentos[i])
                i += 1
        resultado.segmentos = reconstruidos


def _detectar_islas(
    segmentos: list[Segmento],
) -> list[tuple[list[int], str]]:
    """Devuelve las islas como (índices de segmentos, idioma del contexto).

    Se agrupan los segmentos consecutivos por idioma detectado en su texto
    (los segmentos sin idioma claro se unen al grupo en curso) y se buscan
    grupos cortos cuyos vecinos por ambos lados están en el otro idioma.
    """
    grupos: list[list] = []  # [idioma | None, [índices]]
    for i, seg in enumerate(segmentos):
        idioma = detectar_idioma(seg.texto)
        if grupos and (idioma is None or grupos[-1][0] in (None, idioma)):
            grupos[-1][1].append(i)
            if grupos[-1][0] is None:
                grupos[-1][0] = idioma
        else:
            grupos.append([idioma, [i]])

    islas: list[tuple[list[int], str]] = []
    for j in range(1, len(grupos) - 1):
        idioma_isla, indices = grupos[j]
        vecino_previo = grupos[j - 1][0]
        vecino_siguiente = grupos[j + 1][0]
        duracion = segmentos[indices[-1]].fin - segmentos[indices[0]].inicio
        if (
            idioma_isla is not None
            and vecino_previo is not None
            and vecino_previo == vecino_siguiente
            and vecino_previo != idioma_isla
            and duracion <= DURACION_MAX_ISLA
        ):
            islas.append((indices, vecino_previo))
    return islas


def _retranscribir(
    model: WhisperModel,
    audio: np.ndarray,
    inicio: float,
    fin: float,
    idioma: str,
) -> list[Segmento]:
    """Re-transcribe el tramo [inicio, fin] del audio forzando `idioma`."""
    margen = 0.2
    a = max(0, int((inicio - margen) * FRECUENCIA_MUESTREO))
    b = min(audio.shape[0], int((fin + margen) * FRECUENCIA_MUESTREO))
    segmentos_iter, _ = model.transcribe(
        audio[a:b], language=idioma, **_OPCIONES_CALIDAD
    )
    desfase = a / FRECUENCIA_MUESTREO
    return [
        Segmento(
            inicio=seg.start + desfase,
            fin=seg.end + desfase,
            texto=seg.text.strip(),
            logprob=seg.avg_logprob,
        )
        for seg in segmentos_iter
    ]


def _logprob_media(segmentos: list[Segmento]) -> float:
    """Log-probabilidad media ponderada por la duración de cada segmento."""
    total = sum(s.fin - s.inicio for s in segmentos)
    if total <= 0:
        return float("-inf")
    return sum(s.logprob * (s.fin - s.inicio) for s in segmentos) / total


def _formato_tiempo(segundos: float) -> str:
    s = int(segundos)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
