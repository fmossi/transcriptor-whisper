"""Interfaz gráfica (Tkinter) para el transcriptor Whisper.

Reutiliza la misma lógica que la CLI (`src/downloader.py`, `src/transcriber.py`
y `src/exporter.py`). La transcripción se ejecuta en un hilo aparte para que la
ventana no se congele, y la comunicación hilo -> interfaz se hace mediante una
cola de eventos que el hilo principal consulta con `after`.
"""

import queue
import re
import tempfile
import threading
import time
from pathlib import Path
from tkinter import (
    BOTH,
    DISABLED,
    END,
    NORMAL,
    StringVar,
    Tk,
    filedialog,
    messagebox,
)
from tkinter import ttk

from src.downloader import descargar_audio, es_url
from src.exporter import exportar_word
from src.transcriber import MODELOS_VALIDOS, transcribir

DIRECTORIO_SALIDA = Path(__file__).resolve().parents[2] / "salida"

OPCIONES_IDIOMA = {
    "Automático (es/ca por segmento)": "auto",
    "Español": "es",
    "Catalán/Valenciano": "ca",
}


def _nombre_seguro(texto: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", texto).strip() or "transcripcion"


class AplicacionTranscriptor:
    """Ventana principal de la aplicación."""

    def __init__(self, root: Tk) -> None:
        self.root = root
        self.eventos: "queue.Queue[tuple]" = queue.Queue()
        self.hilo: threading.Thread | None = None

        root.title("Transcriptor Whisper")
        root.minsize(640, 520)

        self.var_entrada = StringVar()
        self.var_idioma = StringVar(value=next(iter(OPCIONES_IDIOMA)))
        self.var_modelo = StringVar(value="large-v3")
        self.var_salida = StringVar()
        self.var_estado = StringVar(value="Listo")

        self._construir_interfaz()
        self.root.after(100, self._procesar_eventos)

    def _construir_interfaz(self) -> None:
        marco = ttk.Frame(self.root, padding=16)
        marco.pack(fill=BOTH, expand=True)
        marco.columnconfigure(1, weight=1)

        fila = 0
        ttk.Label(marco, text="Audio/vídeo o URL de YouTube:").grid(
            row=fila, column=0, sticky="w", pady=(0, 4)
        )
        fila += 1
        ttk.Entry(marco, textvariable=self.var_entrada).grid(
            row=fila, column=0, columnspan=2, sticky="ew"
        )
        ttk.Button(marco, text="Examinar...", command=self._elegir_entrada).grid(
            row=fila, column=2, padx=(8, 0)
        )

        fila += 1
        ttk.Label(marco, text="Idioma:").grid(row=fila, column=0, sticky="w", pady=(12, 4))
        ttk.Label(marco, text="Modelo:").grid(row=fila, column=2, sticky="w", pady=(12, 4))

        fila += 1
        ttk.Combobox(
            marco,
            textvariable=self.var_idioma,
            values=list(OPCIONES_IDIOMA),
            state="readonly",
        ).grid(row=fila, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        ttk.Combobox(
            marco,
            textvariable=self.var_modelo,
            values=list(MODELOS_VALIDOS),
            state="readonly",
            width=12,
        ).grid(row=fila, column=2, sticky="ew")

        fila += 1
        ttk.Label(marco, text="Documento Word de salida:").grid(
            row=fila, column=0, sticky="w", pady=(12, 4)
        )
        fila += 1
        ttk.Entry(marco, textvariable=self.var_salida).grid(
            row=fila, column=0, columnspan=2, sticky="ew"
        )
        ttk.Button(marco, text="Guardar como...", command=self._elegir_salida).grid(
            row=fila, column=2, padx=(8, 0)
        )

        fila += 1
        self.boton_transcribir = ttk.Button(
            marco, text="Transcribir", command=self._iniciar_transcripcion
        )
        self.boton_transcribir.grid(row=fila, column=0, columnspan=3, sticky="ew", pady=16)

        fila += 1
        self.barra = ttk.Progressbar(marco, mode="determinate", maximum=100)
        self.barra.grid(row=fila, column=0, columnspan=3, sticky="ew")

        fila += 1
        ttk.Label(marco, textvariable=self.var_estado).grid(
            row=fila, column=0, columnspan=3, sticky="w", pady=(8, 4)
        )

        fila += 1
        marco.rowconfigure(fila, weight=1)
        self.registro = ScrolledLog(marco)
        self.registro.grid(row=fila, column=0, columnspan=3, sticky="nsew")

    # --- Acciones de la interfaz -------------------------------------------------

    def _elegir_entrada(self) -> None:
        ruta = filedialog.askopenfilename(
            title="Selecciona un fichero de audio o vídeo",
            filetypes=[
                ("Audio/vídeo", "*.mp3 *.wav *.m4a *.ogg *.flac *.mp4 *.mkv *.mov *.avi"),
                ("Todos los archivos", "*.*"),
            ],
        )
        if ruta:
            self.var_entrada.set(ruta)

    def _elegir_salida(self) -> None:
        ruta = filedialog.asksaveasfilename(
            title="Guardar transcripción como",
            defaultextension=".docx",
            filetypes=[("Documento Word", "*.docx")],
        )
        if ruta:
            self.var_salida.set(ruta)

    def _iniciar_transcripcion(self) -> None:
        if self.hilo and self.hilo.is_alive():
            return
        entrada = self.var_entrada.get().strip()
        if not entrada:
            messagebox.showwarning(
                "Falta la entrada", "Indica un fichero o una URL de YouTube."
            )
            return
        if not es_url(entrada) and not Path(entrada).exists():
            messagebox.showerror("Entrada no válida", f"No existe el fichero:\n{entrada}")
            return

        self.boton_transcribir.config(state=DISABLED)
        self.barra["value"] = 0
        self.registro.limpiar()

        idioma = OPCIONES_IDIOMA[self.var_idioma.get()]
        modelo = self.var_modelo.get()
        salida = self.var_salida.get().strip()

        self.hilo = threading.Thread(
            target=self._trabajo,
            args=(entrada, idioma, modelo, salida),
            daemon=True,
        )
        self.hilo.start()

    # --- Hilo de trabajo ---------------------------------------------------------

    def _trabajo(self, entrada: str, idioma_ui: str, modelo: str, salida: str) -> None:
        """Pipeline completo, ejecutado fuera del hilo de la interfaz."""
        try:
            with tempfile.TemporaryDirectory(prefix="whisper_") as tmp:
                if es_url(entrada):
                    self._evento("log", f"Descargando audio de: {entrada}")
                    ruta_audio, titulo = descargar_audio(entrada, Path(tmp))
                    self._evento("log", f"Audio descargado: {titulo}")
                else:
                    ruta_audio = Path(entrada)
                    titulo = ruta_audio.stem

                idioma = None if idioma_ui == "auto" else idioma_ui
                self._evento("log", f"Cargando modelo '{modelo}' (se descarga la primera vez)...")
                inicio = time.monotonic()

                def progreso(procesado: float, total: float) -> None:
                    pct = min(procesado / total * 100, 100) if total else 0
                    self._evento("progreso", pct)

                resultado = transcribir(
                    ruta_audio,
                    modelo,
                    idioma,
                    progreso,
                    lambda m: self._evento("log", m),
                )

                transcurrido = time.monotonic() - inicio
                if not resultado.segmentos:
                    self._evento("error", "No se detectó voz en el audio.")
                    return

                self._evento(
                    "log",
                    f"Transcripción completada en {transcurrido / 60:.1f} min "
                    f"(idioma: {resultado.idioma}, "
                    f"confianza {resultado.probabilidad_idioma:.0%}).",
                )

                if salida:
                    ruta_salida = Path(salida)
                else:
                    ruta_salida = DIRECTORIO_SALIDA / f"{_nombre_seguro(titulo)}.docx"
                if ruta_salida.suffix.lower() != ".docx":
                    ruta_salida = ruta_salida.with_suffix(".docx")

                exportar_word(resultado, ruta_salida, titulo, modelo)
                self._evento("done", str(ruta_salida.resolve()))
        except Exception as e:  # noqa: BLE001 - se muestra en la interfaz
            self._evento("error", str(e))

    def _evento(self, tipo: str, valor) -> None:
        self.eventos.put((tipo, valor))

    # --- Bucle de eventos en el hilo principal -----------------------------------

    def _procesar_eventos(self) -> None:
        try:
            while True:
                tipo, valor = self.eventos.get_nowait()
                if tipo == "progreso":
                    self.barra["value"] = valor
                    self.var_estado.set(f"Transcribiendo... {valor:.0f} %")
                elif tipo == "log":
                    self.registro.anadir(valor)
                elif tipo == "done":
                    self.barra["value"] = 100
                    self.var_estado.set("Completado")
                    self.registro.anadir(f"Documento generado: {valor}")
                    self.boton_transcribir.config(state=NORMAL)
                    messagebox.showinfo("Listo", f"Documento generado:\n{valor}")
                elif tipo == "error":
                    self.var_estado.set("Error")
                    self.registro.anadir(f"ERROR: {valor}")
                    self.boton_transcribir.config(state=NORMAL)
                    messagebox.showerror("Error", valor)
        except queue.Empty:
            pass
        self.root.after(100, self._procesar_eventos)


class ScrolledLog(ttk.Frame):
    """Área de texto de solo lectura con barra de desplazamiento."""

    def __init__(self, master) -> None:
        super().__init__(master)
        from tkinter import Text, Scrollbar

        self.texto = Text(self, height=10, wrap="word", state=DISABLED)
        barra = Scrollbar(self, command=self.texto.yview)
        self.texto.config(yscrollcommand=barra.set)
        self.texto.pack(side="left", fill=BOTH, expand=True)
        barra.pack(side="right", fill="y")

    def anadir(self, mensaje: str) -> None:
        self.texto.config(state=NORMAL)
        self.texto.insert(END, mensaje + "\n")
        self.texto.see(END)
        self.texto.config(state=DISABLED)

    def limpiar(self) -> None:
        self.texto.config(state=NORMAL)
        self.texto.delete("1.0", END)
        self.texto.config(state=DISABLED)


def iniciar() -> None:
    root = Tk()
    AplicacionTranscriptor(root)
    root.mainloop()


if __name__ == "__main__":
    iniciar()
