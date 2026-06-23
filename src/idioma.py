"""Detección heurística castellano/valenciano sobre texto ya transcrito."""

import re

NOMBRES_IDIOMA = {"es": "Español", "ca": "Catalán/Valenciano"}
ETIQUETA_IDIOMA = {"es": "castellano", "ca": "valenciano"}

# Palabras frecuentes y exclusivas de cada idioma, para detectar en qué idioma
# está escrito un texto (el castellano y el valenciano comparten mucho
# léxico, por eso solo se usan palabras inequívocas de cada uno).
_PALABRAS_ES = frozenset(
    "y con para pero los e como qué muy también hay señor señora gracias "
    "ayuntamiento pues esto eso nosotros ustedes porque hacer decir bueno "
    "buenos días tardes ahora aquí desde donde tiene tienen ser está están "
    "todos todas mucho año años".split()
)
_PALABRAS_CA = frozenset(
    "i amb és però com què molt també senyor senyora gràcies ajuntament "
    "doncs açò això aixina nosaltres vostès perquè fer dir bé bon dies "
    "vesprada ara ací des d'on té tenen ésser està estan tots totes per "
    "any anys aquesta aquest hi ho us em dels als pel".split()
)


def detectar_idioma(texto: str) -> str | None:
    """Clasifica un texto como "es" o "ca" contando palabras inequívocas.

    Devuelve None si no hay evidencia suficiente.
    """
    palabras = re.findall(r"[a-záéíóúàèòïüçñ']+", texto.lower())
    puntos_es = sum(1 for p in palabras if p in _PALABRAS_ES)
    # Los apóstrofos (l', d', s'...) son un rasgo muy distintivo del valenciano
    puntos_ca = sum(1 for p in palabras if p in _PALABRAS_CA or "'" in p)
    if puntos_es == puntos_ca:
        return None
    return "es" if puntos_es > puntos_ca else "ca"
