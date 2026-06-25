# -*- coding: utf-8 -*-
"""
Поиск документов по адресу в библиотеке адресов.

Программа ищет документы, относящиеся к одному адресу, сразу в нескольких
корневых папках с разной структурой:

  Структура A (папки-буквы):
      корень / А / <улица> / <дом> / <квартира> / документы...
  Структура B (улицы сразу):
      корень / <улица> / <дом> / <квартира> / документы...

Поиск ведётся по улице (часть названия), и при желании уточняется
номером дома и номером квартиры. Программа сама понимает обе структуры.

Запуск:  python address_search.py
Зависимостей нет — только стандартная библиотека Python (Tkinter).
"""

import os
import sys
import json
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "Поиск документов по адресу"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".address_search_config.json")


def _resource_dir():
    """Папка с ресурсами рядом с программой (учитывает сборку PyInstaller)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# Папкой-буквой считаем каталог, чьё имя — одна буква (А, Б, ... возможно с точкой).
def is_letter_folder(name):
    core = name.strip().strip(".").strip()
    return len(core) == 1 and core.isalpha()


def normalize(text):
    """Нормализация для сравнения: регистр не важен, ё=е, лишние пробелы убраны."""
    return (text or "").strip().lower().replace("ё", "е")


# Префиксы, которые отбрасываем у названий улиц при сравнении.
STREET_PREFIXES = (
    "улица", "ул.", "ул ", "проспект", "пр-кт", "пр.", "пр ",
    "переулок", "пер.", "пер ", "бульвар", "б-р", "проезд",
    "шоссе", "ш.", "набережная", "наб.", "площадь", "пл.",
)


def street_core(name):
    n = normalize(name)
    for p in STREET_PREFIXES:
        if n.startswith(p):
            n = n[len(p):].strip()
            break
        # префикс может стоять после названия: "Ленина ул."
        if n.endswith(p.strip()):
            n = n[: -len(p.strip())].strip()
            break
    return n.strip(" .,")


def street_matches(folder_name, query):
    if not query:
        return True
    q = street_core(query)
    f = street_core(folder_name)
    return q in f


# Префиксы у номеров домов / квартир.
NUM_PREFIXES = ("дом", "д.", "д ", "квартира", "кв.", "кв ", "кв", "№", "no", "n")

# Латинские буквы, похожие на кириллические (чтобы "13A" совпадало с "13А").
LOOKALIKE = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к",
    "m": "м", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
})

# Разделители внутри номера, которые при сравнении игнорируем.
NUM_SEPARATORS = ("/", "\\", "-", "_", " ", ".", "№")


def num_core(name):
    """Привести номер дома/квартиры к виду, устойчивому к мелким различиям.

    "13/Г", "13-Г", "13 Г", "д. 13Г" -> "13г";  латинские двойники -> кириллица.
    """
    n = normalize(name).translate(LOOKALIKE).replace(" ", "")
    for p in NUM_PREFIXES:
        p2 = p.replace(" ", "")
        if n.startswith(p2):
            n = n[len(p2):]
            break
    for sep in NUM_SEPARATORS:
        n = n.replace(sep, "")
    return n.strip(" .,")


def num_matches(folder_name, query):
    """Совпадение номера дома/квартиры. Пусто — совпадает с любым."""
    if not query:
        return True
    return num_core(folder_name) == num_core(query)


def list_subdirs(path):
    try:
        with os.scandir(path) as it:
            return [e for e in it if e.is_dir()]
    except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
        return []


def find_street_dirs(root, street_query):
    """Найти каталоги улиц в корне, учитывая обе структуры (с буквами и без)."""
    results = []
    for entry in list_subdirs(root):
        if is_letter_folder(entry.name):
            # Папка-буква: улицы лежат внутри неё.
            for sub in list_subdirs(entry.path):
                if street_matches(sub.name, street_query):
                    results.append(sub.path)
        else:
            # Улица идёт сразу.
            if street_matches(entry.name, street_query):
                results.append(entry.path)
    return results


def narrow_by_num(dirs, query):
    """Сузить список каталогов по номеру дома или квартиры."""
    if not query:
        return dirs
    narrowed = []
    for d in dirs:
        for sub in list_subdirs(d):
            if num_matches(sub.name, query):
                narrowed.append(sub.path)
    return narrowed


def collect_files(directory):
    """Все файлы внутри каталога (рекурсивно)."""
    files = []
    for dirpath, _dirnames, filenames in os.walk(directory):
        for fn in filenames:
            files.append(os.path.join(dirpath, fn))
    return files


def search(roots, street, house, apartment):
    """Главная функция поиска. Возвращает список (полный_путь, корень)."""
    found = []
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        target_dirs = find_street_dirs(root, street)
        target_dirs = narrow_by_num(target_dirs, house)
        target_dirs = narrow_by_num(target_dirs, apartment)
        for d in target_dirs:
            for f in collect_files(d):
                if f not in seen:
                    seen.add(f)
                    found.append((f, root))
    return found


def _external_env():
    """Окружение для запуска внешних программ.

    Если приложение собрано PyInstaller (--onefile), оно подменяет
    LD_LIBRARY_PATH своими библиотеками. Системный «открывальщик» и
    файловый менеджер не должны их наследовать — иначе они падают и
    файлы/папки не открываются. PyInstaller сохраняет оригинал в *_ORIG.
    """
    env = dict(os.environ)
    for var in ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH"):
        orig = env.get(var + "_ORIG")
        if orig is not None:
            env[var] = orig
        else:
            env.pop(var, None)
    return env


def open_path(path):
    """Открыть файл или папку средствами ОС (Windows / macOS / Linux/Astra)."""
    if sys.platform.startswith("win"):
        try:
            os.startfile(path)  # noqa
        except Exception as e:  # noqa
            messagebox.showerror("Ошибка", "Не удалось открыть:\n%s\n\n%s" % (path, e))
        return

    if sys.platform == "darwin":
        openers = [["open", path]]
    else:
        # Несколько вариантов: подойдёт первый установленный в системе.
        # fly-fm — файловый менеджер окружения Fly в Astra Linux.
        openers = [
            ["xdg-open", path],
            ["gio", "open", path],
            ["exo-open", path],
            ["kde-open5", path],
            ["kde-open", path],
            ["fly-fm", path],
            ["nautilus", path],
            ["pcmanfm", path],
            ["thunar", path],
            ["dolphin", path],
        ]

    env = _external_env()
    last_err = None
    for cmd in openers:
        try:
            subprocess.Popen(cmd, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError as e:
            last_err = e  # этой программы нет — пробуем следующую
        except Exception as e:  # noqa
            last_err = e
    messagebox.showerror(
        "Ошибка",
        "Не удалось открыть:\n%s\n\n"
        "Не найдена программа для открытия. Установите xdg-utils:\n"
        "sudo apt install xdg-utils\n\n%s" % (path, last_err),
    )


# Типы файлов, которые умеет печатать встроенный SumatraPDF.
SUMATRA_EXT = {".pdf", ".xps", ".djvu", ".cbz", ".cbr",
               ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp"}


def print_path(path):
    """Отправить один файл на принтер по умолчанию.

    Возвращает (True, "") при успехе или (False, "причина") при ошибке.
    Windows — через зарегистрированное приложение (глагол "print").
    Linux/Astra и macOS — через CUPS (команда lp).
    """
    if sys.platform.startswith("win"):
        ext = os.path.splitext(path)[1].lower()
        # Для PDF и изображений печатаем через встроенный SumatraPDF —
        # не зависит от того, какой просмотрщик стоит в системе.
        sumatra = os.path.join(_resource_dir(), "SumatraPDF.exe")
        if os.path.exists(sumatra) and ext in SUMATRA_EXT:
            try:
                res = subprocess.run(
                    [sumatra, "-print-to-default", "-silent", path])
                if res.returncode == 0:
                    return True, ""
            except Exception:  # noqa
                pass  # не вышло — пробуем системную печать ниже
        # Прочие типы (Word, txt и т.п.) — системная печать.
        try:
            os.startfile(path, "print")  # noqa
            return True, ""
        except Exception as e:  # noqa
            return False, ("нет приложения, умеющего печатать «%s».\n"
                           "Откройте файл и нажмите Ctrl+P, либо установите "
                           "программу-просмотрщик с поддержкой печати.\n%s"
                           % (ext or "файл", e))

    # Linux / macOS: печать через CUPS.
    env = _external_env()
    for cmd in (["lp", path], ["lpr", path]):
        try:
            res = subprocess.run(cmd, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            if res.returncode == 0:
                return True, ""
            err = (res.stderr or b"").decode("utf-8", "replace").strip()
            # Команда есть, но печать не удалась (нет принтера и т.п.).
            return False, err or ("код возврата %d" % res.returncode)
        except FileNotFoundError:
            continue  # нет этой команды — пробуем следующую
        except Exception as e:  # noqa
            return False, str(e)
    return False, ("не найдена система печати (lp/lpr).\n"
                   "Установите клиент CUPS:  sudo apt install cups-client")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return {"roots": []}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("900x600")
        self.minsize(700, 480)

        self.cfg = load_config()
        self.result_paths = {}      # item_id -> полный путь
        self.queue = queue.Queue()  # сообщения из рабочего потока

        self._build_ui()
        self._refresh_folders_list()
        self.after(100, self._poll_queue)

    # ---------- интерфейс ----------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        # Блок папок
        folders = ttk.LabelFrame(self, text="Папки с адресами (можно сетевые)")
        folders.pack(fill="x", **pad)

        self.folders_list = tk.Listbox(folders, height=3)
        self.folders_list.pack(side="left", fill="both", expand=True, padx=6, pady=6)

        fbtns = ttk.Frame(folders)
        fbtns.pack(side="left", fill="y", padx=6, pady=6)
        ttk.Button(fbtns, text="Добавить…", command=self._add_folder).pack(fill="x", pady=2)
        ttk.Button(fbtns, text="Удалить", command=self._remove_folder).pack(fill="x", pady=2)

        # Блок поиска
        srch = ttk.LabelFrame(self, text="Адрес для поиска")
        srch.pack(fill="x", **pad)

        ttk.Label(srch, text="Улица:").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        self.e_street = ttk.Entry(srch, width=40)
        self.e_street.grid(row=0, column=1, sticky="we", padx=6, pady=6)

        ttk.Label(srch, text="Дом:").grid(row=0, column=2, sticky="e", padx=6, pady=6)
        self.e_house = ttk.Entry(srch, width=10)
        self.e_house.grid(row=0, column=3, sticky="we", padx=6, pady=6)

        ttk.Label(srch, text="Квартира:").grid(row=0, column=4, sticky="e", padx=6, pady=6)
        self.e_apt = ttk.Entry(srch, width=10)
        self.e_apt.grid(row=0, column=5, sticky="we", padx=6, pady=6)

        self.btn_search = ttk.Button(srch, text="Искать", command=self._on_search)
        self.btn_search.grid(row=0, column=6, padx=8, pady=6)
        srch.columnconfigure(1, weight=1)

        self.e_street.bind("<Return>", lambda _e: self._on_search())
        self.e_house.bind("<Return>", lambda _e: self._on_search())
        self.e_apt.bind("<Return>", lambda _e: self._on_search())

        # Результаты
        res = ttk.LabelFrame(self, text="Найденные документы")
        res.pack(fill="both", expand=True, **pad)

        cols = ("doc", "path", "root")
        self.tree = ttk.Treeview(res, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("doc", text="Документ")
        self.tree.heading("path", text="Адрес / расположение")
        self.tree.heading("root", text="Папка-источник")
        self.tree.column("doc", width=240)
        self.tree.column("path", width=420)
        self.tree.column("root", width=160)

        vsb = ttk.Scrollbar(res, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="left", fill="y", pady=6)
        self.tree.bind("<Double-1>", lambda _e: self._open_selected_file())

        # Нижняя панель
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        ttk.Button(bottom, text="Открыть файл", command=self._open_selected_file).pack(side="left")
        ttk.Button(bottom, text="Открыть папку с файлом", command=self._open_selected_folder).pack(side="left", padx=6)
        ttk.Button(bottom, text="Печать", command=self._print_selected).pack(side="left")
        self.status = ttk.Label(bottom, text="Готово")
        self.status.pack(side="right")

    # ---------- папки ----------
    def _refresh_folders_list(self):
        self.folders_list.delete(0, tk.END)
        for r in self.cfg.get("roots", []):
            self.folders_list.insert(tk.END, r)

    def _add_folder(self):
        d = filedialog.askdirectory(title="Выберите папку с адресами")
        if d:
            roots = self.cfg.setdefault("roots", [])
            if d not in roots:
                roots.append(d)
                save_config(self.cfg)
                self._refresh_folders_list()

    def _remove_folder(self):
        sel = self.folders_list.curselection()
        if not sel:
            return
        roots = self.cfg.setdefault("roots", [])
        del roots[sel[0]]
        save_config(self.cfg)
        self._refresh_folders_list()

    # ---------- поиск ----------
    def _on_search(self):
        roots = list(self.cfg.get("roots", []))
        if not roots:
            messagebox.showinfo("Нет папок", "Сначала добавьте хотя бы одну папку с адресами.")
            return
        street = self.e_street.get().strip()
        house = self.e_house.get().strip()
        apt = self.e_apt.get().strip()
        if not street and not house and not apt:
            messagebox.showinfo("Пустой запрос", "Введите хотя бы улицу.")
            return

        self.tree.delete(*self.tree.get_children())
        self.result_paths.clear()
        self.btn_search.config(state="disabled")
        self.status.config(text="Идёт поиск…")

        t = threading.Thread(
            target=self._search_worker, args=(roots, street, house, apt), daemon=True
        )
        t.start()

    def _search_worker(self, roots, street, house, apt):
        try:
            results = search(roots, street, house, apt)
            self.queue.put(("results", results))
        except Exception as e:  # noqa
            self.queue.put(("error", str(e)))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "results":
                    self._show_results(payload)
                elif kind == "error":
                    self.btn_search.config(state="normal")
                    self.status.config(text="Ошибка")
                    messagebox.showerror("Ошибка поиска", payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _show_results(self, results):
        self.btn_search.config(state="normal")
        for full_path, root in results:
            rel = os.path.relpath(os.path.dirname(full_path), root)
            item = self.tree.insert(
                "", tk.END,
                values=(os.path.basename(full_path), rel, os.path.basename(root.rstrip("/\\")) or root),
            )
            self.result_paths[item] = full_path
        self.status.config(text="Найдено документов: %d" % len(results))
        if not results:
            messagebox.showinfo("Ничего не найдено",
                                "По указанному адресу документы не найдены.\n"
                                "Попробуйте ввести часть названия улицы.")

    # ---------- открытие ----------
    def _selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.result_paths.get(sel[0])

    def _open_selected_file(self):
        p = self._selected_path()
        if p:
            open_path(p)

    def _open_selected_folder(self):
        p = self._selected_path()
        if p:
            open_path(os.path.dirname(p))

    # ---------- печать ----------
    def _print_selected(self):
        sel = self.tree.selection()
        # Если ничего не выделено — предлагаем напечатать все найденные.
        if sel:
            paths = [self.result_paths[i] for i in sel if i in self.result_paths]
        else:
            paths = list(self.result_paths.values())
            if not paths:
                messagebox.showinfo("Печать", "Сначала найдите документы.")
                return
            if not messagebox.askyesno(
                "Печать",
                "Ничего не выделено. Напечатать ВСЕ найденные документы (%d шт.)?"
                % len(paths)):
                return

        if not paths:
            return
        if len(paths) > 1 and not messagebox.askyesno(
                "Печать", "Отправить на печать %d документ(ов)?" % len(paths)):
            return

        ok, failed = 0, []
        for p in paths:
            success, err = print_path(p)
            if success:
                ok += 1
            else:
                failed.append((p, err))

        self.status.config(text="Отправлено на печать: %d" % ok)
        if failed:
            details = "\n".join("• %s\n   %s" % (os.path.basename(p), e)
                                for p, e in failed[:10])
            messagebox.showwarning(
                "Печать",
                "Напечатано: %d. Не удалось: %d.\n\n%s" % (ok, len(failed), details))
        else:
            messagebox.showinfo("Печать", "Отправлено на печать: %d документ(ов)." % ok)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
