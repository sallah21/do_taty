#!/usr/bin/env python3
"""
KSeF Converter — Graphical Interface
=====================================
A simple tkinter GUI so non-technical users can convert invoices
by clicking buttons instead of using the command line.

Requirements: Python 3.9+ (tkinter is included with standard Python on Windows).
"""

import logging
import sys
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate the converter module next to this script
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import invoice_converter as converter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = SCRIPT_DIR / "ksef_config.json"
DEFAULT_INPUT_DIR = SCRIPT_DIR / "proper_basic_invoices"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output_ksef"


def find_initial_dir(candidate: Path) -> str:
    """Return candidate as initial directory for file dialogs, falling back to script dir."""
    return str(candidate) if candidate.is_dir() else str(SCRIPT_DIR)


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class KSeFApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KSeF Konwerter Faktur")
        self.geometry("720x560")
        self.minsize(600, 420)
        self.configure(bg="#f5f5f5")

        # State
        self.input_path = tk.StringVar(value="")
        self.output_path = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.config_path = tk.StringVar(
            value=str(DEFAULT_CONFIG_PATH) if DEFAULT_CONFIG_PATH.exists() else ""
        )
        self.mode = tk.StringVar(value="batch")  # "batch" or "single"

        self._log_generation = 0
        self._busy = False

        self._build_ui()
        self._set_defaults()
        self._setup_logging()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI construction ---------------------------------------------------

    def _build_ui(self):
        # --- Mode selector ---
        mode_frame = tk.LabelFrame(
            self, text="Tryb konwersji", padx=10, pady=5, bg="#f5f5f5"
        )
        mode_frame.pack(fill="x", padx=12, pady=(10, 4))

        tk.Radiobutton(
            mode_frame, text="Cały folder (batch)", variable=self.mode,
            value="batch", bg="#f5f5f5", command=self._on_mode_change,
        ).pack(side="left", padx=(0, 16))
        tk.Radiobutton(
            mode_frame, text="Pojedynczy plik", variable=self.mode,
            value="single", bg="#f5f5f5", command=self._on_mode_change,
        ).pack(side="left")

        # --- Paths ---
        paths_frame = tk.Frame(self, bg="#f5f5f5")
        paths_frame.pack(fill="x", padx=12, pady=4)

        # Input
        tk.Label(paths_frame, text="Wejście (XML):", bg="#f5f5f5", anchor="w").grid(
            row=0, column=0, sticky="w", pady=2
        )
        tk.Entry(paths_frame, textvariable=self.input_path, width=58).grid(
            row=0, column=1, padx=4, pady=2
        )
        self.btn_input = tk.Button(
            paths_frame, text="Wybierz…", width=10, command=self._pick_input
        )
        self.btn_input.grid(row=0, column=2, pady=2)

        # Output
        tk.Label(paths_frame, text="Wyjście:", bg="#f5f5f5", anchor="w").grid(
            row=1, column=0, sticky="w", pady=2
        )
        tk.Entry(paths_frame, textvariable=self.output_path, width=58).grid(
            row=1, column=1, padx=4, pady=2
        )
        tk.Button(
            paths_frame, text="Wybierz…", width=10, command=self._pick_output
        ).grid(row=1, column=2, pady=2)

        # Config
        tk.Label(paths_frame, text="Konfiguracja:", bg="#f5f5f5", anchor="w").grid(
            row=2, column=0, sticky="w", pady=2
        )
        tk.Entry(paths_frame, textvariable=self.config_path, width=58).grid(
            row=2, column=1, padx=4, pady=2
        )
        tk.Button(
            paths_frame, text="Wybierz…", width=10, command=self._pick_config
        ).grid(row=2, column=2, pady=2)

        # --- Action buttons ---
        btn_frame = tk.Frame(self, bg="#f5f5f5")
        btn_frame.pack(fill="x", padx=12, pady=8)

        self.btn_convert = tk.Button(
            btn_frame, text="  Konwertuj  ", font=("Segoe UI", 11, "bold"),
            bg="#1a73e8", fg="white", activebackground="#1558b0",
            activeforeground="white", relief="flat", padx=16, pady=4,
            command=self._start_conversion,
        )
        self.btn_convert.pack(side="left")

        self.btn_validate = tk.Button(
            btn_frame, text="Waliduj wyjście", padx=10,
            command=self._start_validation,
        )
        self.btn_validate.pack(side="left", padx=(12, 0))

        self.btn_init_cfg = tk.Button(
            btn_frame, text="Generuj konfigurację", padx=10,
            command=self._generate_config,
        )
        self.btn_init_cfg.pack(side="right")

        # --- Log area ---
        log_label = tk.Label(
            self, text="Logi:", bg="#f5f5f5", anchor="w"
        )
        log_label.pack(fill="x", padx=12)

        self.log = scrolledtext.ScrolledText(
            self, height=16, state="disabled", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
        )
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        # Tag colours for log
        self.log.tag_config("ok", foreground="#4ec9b0")
        self.log.tag_config("warn", foreground="#dcdcaa")
        self.log.tag_config("err", foreground="#f44747")
        self.log.tag_config("info", foreground="#9cdcfe")

    # ---- Defaults ----------------------------------------------------------

    def _set_defaults(self):
        if DEFAULT_INPUT_DIR.is_dir():
            self.input_path.set(str(DEFAULT_INPUT_DIR))

    def _on_mode_change(self):
        """Reset input/output fields when mode changes to avoid type mismatches."""
        self.input_path.set("")
        if self.mode.get() == "batch":
            self.output_path.set(str(DEFAULT_OUTPUT_DIR))
        else:
            self.output_path.set("")

    def _setup_logging(self):
        """Route converter module log messages into the GUI log widget."""
        self._gui_handler = _GUILogHandler(self)
        self._gui_handler.setFormatter(logging.Formatter("%(message)s"))
        self._gui_handler.setLevel(logging.DEBUG)
        self._conv_logger = logging.getLogger(converter.logger.name)
        self._conv_logger.addHandler(self._gui_handler)

    def _on_close(self):
        """Clean up logging handler before destroying the window."""
        self._conv_logger.removeHandler(self._gui_handler)
        self.destroy()

    # ---- Button state management -------------------------------------------

    def _disable_all_buttons(self):
        self._busy = True
        for btn in (self.btn_convert, self.btn_validate, self.btn_init_cfg):
            btn.configure(state="disabled")

    def _enable_all_buttons(self):
        self._busy = False
        self.btn_convert.configure(state="normal", text="  Konwertuj  ")
        self.btn_validate.configure(state="normal", text="Waliduj wyjście")
        self.btn_init_cfg.configure(state="normal")

    # ---- File pickers ------------------------------------------------------

    def _pick_input(self):
        if self.mode.get() == "batch":
            path = filedialog.askdirectory(
                title="Wybierz folder z fakturami XML",
                initialdir=find_initial_dir(DEFAULT_INPUT_DIR),
            )
        else:
            path = filedialog.askopenfilename(
                title="Wybierz plik XML faktury",
                initialdir=find_initial_dir(DEFAULT_INPUT_DIR),
                filetypes=[("Pliki XML", "*.xml"), ("Wszystkie", "*.*")],
            )
        if path:
            self.input_path.set(path)

    def _pick_output(self):
        if self.mode.get() == "batch":
            path = filedialog.askdirectory(
                title="Wybierz folder wyjściowy",
                initialdir=find_initial_dir(DEFAULT_OUTPUT_DIR),
            )
        else:
            path = filedialog.asksaveasfilename(
                title="Zapisz plik KSeF XML jako…",
                initialdir=find_initial_dir(DEFAULT_OUTPUT_DIR),
                defaultextension=".xml",
                filetypes=[("Pliki XML", "*.xml")],
            )
        if path:
            self.output_path.set(path)

    def _pick_config(self):
        path = filedialog.askopenfilename(
            title="Wybierz plik konfiguracyjny JSON",
            initialdir=str(SCRIPT_DIR),
            filetypes=[("JSON", "*.json"), ("Wszystkie", "*.*")],
        )
        if path:
            self.config_path.set(path)

    # ---- Logging -----------------------------------------------------------

    def _safe_after(self, callback):
        """Schedule callback on main thread, silently ignoring if app is destroyed."""
        try:
            self.after(0, callback)
        except tk.TclError:
            pass

    def _log(self, text, tag="info"):
        """Thread-safe logging — schedules widget update on the main thread."""
        gen = self._log_generation
        def _append():
            if self._log_generation != gen:
                return
            try:
                self.log.configure(state="normal")
                self.log.insert("end", text + "\n", tag)
                self.log.see("end")
                self.log.configure(state="disabled")
            except tk.TclError:
                pass
        self._safe_after(_append)

    def _log_clear(self):
        """Clear the log widget. Must be called from the main thread."""
        self._log_generation += 1
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---- Conversion --------------------------------------------------------

    def _start_conversion(self):
        if self._busy:
            return
        inp = self.input_path.get().strip()
        out = self.output_path.get().strip()
        mode = self.mode.get()
        cfg_path = self.config_path.get().strip()
        if not inp:
            messagebox.showwarning("Brak wejścia", "Wybierz plik lub folder wejściowy.")
            return
        if not out:
            messagebox.showwarning("Brak wyjścia", "Wybierz plik lub folder wyjściowy.")
            return
        if mode == "batch" and not Path(inp).is_dir():
            messagebox.showwarning("Nieprawidłowe wejście", "W trybie batch wybierz folder, nie plik.")
            return
        if mode == "single" and not Path(inp).is_file():
            messagebox.showwarning("Nieprawidłowe wejście", "W trybie pojedynczego pliku wybierz plik XML.")
            return
        if mode == "single":
            out_parent = Path(out).parent
            out_parent.mkdir(parents=True, exist_ok=True)

        self._disable_all_buttons()
        self.btn_convert.configure(text="Konwertowanie…")
        self._log_clear()
        threading.Thread(target=self._run_conversion, args=(inp, out, mode, cfg_path), daemon=True).start()

    def _run_conversion(self, inp, out, mode, cfg_path):
        try:
            config = converter.load_config(cfg_path if cfg_path else None)
            self._log(f"Konfiguracja: {cfg_path or '(domyślna)'}", "info")

            if mode == "batch":
                self._log(f"Tryb batch: {inp} -> {out}", "info")
                self._log("-" * 50, "info")
                results = converter.batch_convert(inp, out, config)
                self._log("-" * 50, "info")
                ok = results["success"]
                fail = results["failed"]
                if fail == 0:
                    self._log(
                        f"Zakończono: {ok} faktur(y) przekonwertowanych pomyślnie.",
                        "ok",
                    )
                else:
                    self._log(
                        f"Zakończono: {ok} OK, {fail} błędów.", "err"
                    )
                    for err in results["errors"]:
                        self._log(f"  {err['file']}: {err['error']}", "err")
            else:
                self._log(f"Konwersja: {inp} -> {out}", "info")
                converter.convert_to_ksef(inp, out, config)
                self._log(f"Sukces: {out}", "ok")

        except Exception as e:
            self._log(f"BŁĄD: {e}", "err")
        finally:
            self._safe_after(self._enable_all_buttons)

    # ---- Validation --------------------------------------------------------

    def _start_validation(self):
        if self._busy:
            return
        out = self.output_path.get().strip()
        if not out:
            messagebox.showwarning("Brak ścieżki", "Podaj folder/plik wyjściowy do walidacji.")
            return
        out_path = Path(out)
        if not out_path.exists():
            messagebox.showwarning("Ścieżka nie istnieje", f"Ścieżka nie istnieje:\n{out}")
            return

        self._disable_all_buttons()
        self.btn_validate.configure(text="Walidowanie…")
        self._log_clear()
        threading.Thread(target=self._run_validation, args=(out,), daemon=True).start()

    def _run_validation(self, out):
        try:
            out_path = Path(out)

            if out_path.is_dir():
                xml_files = sorted(out_path.glob("*.xml"))
                if not xml_files:
                    self._log("Brak plików XML w folderze wyjściowym.", "warn")
                    return
                all_ok = True
                has_warnings = False
                for xf in xml_files:
                    errors, warnings = converter.validate_ksef_xml(str(xf))
                    if errors:
                        all_ok = False
                        self._log(f"BŁĄD  {xf.name}:", "err")
                        for e in errors:
                            self._log(f"  {e}", "err")
                    elif warnings:
                        self._log(f"UWAGA {xf.name}:", "warn")
                    else:
                        self._log(f"OK    {xf.name}", "ok")
                    if warnings:
                        has_warnings = True
                    for w in warnings:
                        self._log(f"  {w}", "warn")
                if all_ok and not has_warnings:
                    self._log("Wszystkie pliki przeszły walidację.", "ok")
                elif all_ok and has_warnings:
                    self._log("Walidacja zakończona — brak błędów, ale wykryto ostrzeżenia.", "warn")
            elif out_path.is_file():
                errors, warnings = converter.validate_ksef_xml(str(out_path))
                for e in errors:
                    self._log(f"BŁĄD: {e}", "err")
                for w in warnings:
                    self._log(f"UWAGA: {w}", "warn")
                if not errors and not warnings:
                    self._log("Walidacja OK — brak problemów.", "ok")
            else:
                self._log(f"Ścieżka nie istnieje: {out}", "err")
        except Exception as e:
            self._log(f"BŁĄD: {e}", "err")
        finally:
            self._safe_after(self._enable_all_buttons)

    # ---- Config generation -------------------------------------------------

    def _generate_config(self):
        path = filedialog.asksaveasfilename(
            title="Zapisz nową konfigurację jako…",
            initialdir=str(SCRIPT_DIR),
            initialfile="ksef_config.json",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            self._log_clear()
            converter.generate_sample_config(path)
            self.config_path.set(path)
            self._log(f"Konfiguracja zapisana: {path}", "ok")
        except Exception as e:
            self._log(f"Błąd generowania konfiguracji: {e}", "err")


# ---------------------------------------------------------------------------
# Logging bridge: converter module → GUI log widget
# ---------------------------------------------------------------------------

class _GUILogHandler(logging.Handler):
    """Routes Python logging records into the tkinter log widget (thread-safe)."""

    def __init__(self, app: KSeFApp):
        super().__init__()
        self.app = app

    def emit(self, record):
        msg = self.format(record)
        if record.levelno >= logging.ERROR:
            tag = "err"
        elif record.levelno >= logging.WARNING:
            tag = "warn"
        else:
            tag = "info"
        self.app._log(msg, tag)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = KSeFApp()
    app.mainloop()
