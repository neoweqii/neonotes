import json
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    VERTICAL,
    WORD,
    BooleanVar,
    Frame,
    Label,
    Listbox,
    Menu,
    PanedWindow,
    Scrollbar,
    StringVar,
    Text,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
)
from tkinter import ttk
from tkinter.font import Font

try:
    from tkcalendar import Calendar
except Exception:
    Calendar = None

DB_FILE = "notes.db"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def now_str() -> str:
    return datetime.now().strftime(DATE_FMT)


@dataclass
class Note:
    id: int
    title: str
    content: str
    created_at: str
    updated_at: str
    note_date: str | None
    deadline: str | None
    archived: int
    tags: str


class NotesRepository:
    def __init__(self, db_path: str = DB_FILE):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    note_date TEXT,
                    deadline TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    tags TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self.conn.commit()

    def create_note(self, title: str = "Новая заметка") -> int:
        ts = now_str()
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                """
                INSERT INTO notes (title, content, created_at, updated_at)
                VALUES (?, '', ?, ?)
                """,
                (title, ts, ts),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def list_notes(self, include_archived: bool, search: str, sort_mode: str) -> list[Note]:
        query = "SELECT * FROM notes WHERE 1=1"
        params: list[str | int] = []
        if not include_archived:
            query += " AND archived = 0"
        if search:
            query += " AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like])

        order = {
            "updated_desc": "updated_at DESC",
            "created_desc": "created_at DESC",
            "deadline_asc": "CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline ASC",
            "title_asc": "title COLLATE NOCASE ASC",
        }[sort_mode]
        query += f" ORDER BY {order}"

        with closing(self.conn.cursor()) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [Note(**dict(r)) for r in rows]

    def get_note(self, note_id: int) -> Note | None:
        with closing(self.conn.cursor()) as cur:
            cur.execute("SELECT * FROM notes WHERE id=?", (note_id,))
            row = cur.fetchone()
        return Note(**dict(row)) if row else None

    def update_note(self, note_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = now_str()
        pairs = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [note_id]
        with closing(self.conn.cursor()) as cur:
            cur.execute(f"UPDATE notes SET {pairs} WHERE id=?", values)
            self.conn.commit()

    def delete_note(self, note_id: int) -> None:
        with closing(self.conn.cursor()) as cur:
            cur.execute("DELETE FROM notes WHERE id=?", (note_id,))
            self.conn.commit()

    def export_json(self, file_path: str) -> None:
        with closing(self.conn.cursor()) as cur:
            cur.execute("SELECT * FROM notes ORDER BY id ASC")
            rows = [dict(r) for r in cur.fetchall()]
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)

    def import_json(self, file_path: str) -> int:
        with open(file_path, "r", encoding="utf-8") as f:
            rows = json.load(f)
        count = 0
        with closing(self.conn.cursor()) as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO notes
                    (title, content, created_at, updated_at, note_date, deadline, archived, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r.get("title", "Импортированная заметка"),
                        r.get("content", ""),
                        r.get("created_at", now_str()),
                        r.get("updated_at", now_str()),
                        r.get("note_date"),
                        r.get("deadline"),
                        int(r.get("archived", 0)),
                        r.get("tags", ""),
                    ),
                )
                count += 1
            self.conn.commit()
        return count


class BackupManager:
    def __init__(self, repo: NotesRepository):
        self.repo = repo

    def local_backup(self, directory: str) -> str:
        Path(directory).mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = str(Path(directory) / f"notes_backup_{stamp}.json")
        self.repo.export_json(target)
        return target

    def google_drive_backup(self) -> None:
        raise NotImplementedError("Для Google Drive нужен OAuth-клиент и API интеграция")

    def dropbox_backup(self) -> None:
        raise NotImplementedError("Для Dropbox нужен OAuth-токен и API интеграция")


class NotesApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("NeoNotes — заметки и дедлайны")
        self.root.geometry("1250x760")

        self.repo = NotesRepository()
        self.backup = BackupManager(self.repo)

        self.search_var = StringVar()
        self.sort_var = StringVar(value="updated_desc")
        self.show_archived = BooleanVar(value=False)
        self.current_note_id: int | None = None

        self.base_font = Font(family="Arial", size=12)
        self.bold_font = Font(family="Arial", size=12, weight="bold")
        self.italic_font = Font(family="Arial", size=12, slant="italic")

        self._build_ui()
        self._load_notes()
        self._start_reminder_loop()

    def _build_ui(self) -> None:
        top = Frame(self.root)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Button(top, text="+ Новая", command=self.new_note).pack(side=LEFT, padx=2)
        ttk.Button(top, text="Удалить", command=self.delete_note).pack(side=LEFT, padx=2)
        ttk.Button(top, text="Архив/Разарх.", command=self.toggle_archive).pack(side=LEFT, padx=2)
        ttk.Button(top, text="Экспорт", command=self.export_notes).pack(side=LEFT, padx=2)
        ttk.Button(top, text="Импорт", command=self.import_notes).pack(side=LEFT, padx=2)
        ttk.Button(top, text="Календарь", command=self.open_calendar).pack(side=LEFT, padx=2)
        ttk.Button(top, text="Бэкап", command=self.make_backup).pack(side=LEFT, padx=2)

        ttk.Label(top, text="Поиск:").pack(side=LEFT, padx=(18, 4))
        search_entry = ttk.Entry(top, textvariable=self.search_var, width=24)
        search_entry.pack(side=LEFT)
        search_entry.bind("<KeyRelease>", lambda _e: self._load_notes())

        ttk.Label(top, text="Сортировка:").pack(side=LEFT, padx=(12, 4))
        sort_cb = ttk.Combobox(
            top,
            textvariable=self.sort_var,
            values=["updated_desc", "created_desc", "deadline_asc", "title_asc"],
            state="readonly",
            width=14,
        )
        sort_cb.pack(side=LEFT)
        sort_cb.bind("<<ComboboxSelected>>", lambda _e: self._load_notes())

        ttk.Checkbutton(
            top,
            text="Показывать архив",
            variable=self.show_archived,
            command=self._load_notes,
        ).pack(side=LEFT, padx=(12, 0))

        main = PanedWindow(self.root, sashrelief="ridge")
        main.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))

        left = Frame(main)
        right = Frame(main)
        main.add(left, minsize=350)
        main.add(right)

        self.listbox = Listbox(left)
        self.listbox.pack(side=LEFT, fill=BOTH, expand=True)
        scroll = Scrollbar(left, command=self.listbox.yview, orient=VERTICAL)
        scroll.pack(side=RIGHT, fill="y")
        self.listbox.config(yscrollcommand=scroll.set)
        self.listbox.bind("<<ListboxSelect>>", self.on_note_select)

        meta = Frame(right)
        meta.pack(fill="x", pady=(4, 8))

        self.title_var = StringVar()
        ttk.Label(meta, text="Название:").grid(row=0, column=0, sticky="w")
        self.title_entry = ttk.Entry(meta, textvariable=self.title_var)
        self.title_entry.grid(row=0, column=1, sticky="ew", padx=4)

        ttk.Label(meta, text="Дата заметки (YYYY-MM-DD):").grid(row=1, column=0, sticky="w")
        self.note_date_var = StringVar()
        self.note_date_entry = ttk.Entry(meta, textvariable=self.note_date_var)
        self.note_date_entry.grid(row=1, column=1, sticky="ew", padx=4)

        ttk.Label(meta, text="Дедлайн (YYYY-MM-DD HH:MM):").grid(row=2, column=0, sticky="w")
        self.deadline_var = StringVar()
        self.deadline_entry = ttk.Entry(meta, textvariable=self.deadline_var)
        self.deadline_entry.grid(row=2, column=1, sticky="ew", padx=4)

        ttk.Label(meta, text="Метки (через запятую):").grid(row=3, column=0, sticky="w")
        self.tags_var = StringVar()
        self.tags_entry = ttk.Entry(meta, textvariable=self.tags_var)
        self.tags_entry.grid(row=3, column=1, sticky="ew", padx=4)

        self.created_lbl = Label(meta, text="Создано: —")
        self.created_lbl.grid(row=4, column=0, sticky="w")
        self.updated_lbl = Label(meta, text="Изменено: —")
        self.updated_lbl.grid(row=4, column=1, sticky="w")

        meta.grid_columnconfigure(1, weight=1)

        toolbar = Frame(right)
        toolbar.pack(fill="x", pady=(0, 4))
        ttk.Button(toolbar, text="Жирный", command=self.make_bold).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="Курсив", command=self.make_italic).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="Обычный", command=self.make_normal).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="Сохранить", command=self.save_current).pack(side=LEFT, padx=10)
        ttk.Button(toolbar, text="Продлить дедлайн +1 день", command=self.extend_deadline).pack(side=LEFT)

        self.text = Text(right, wrap=WORD, font=self.base_font)
        self.text.pack(fill=BOTH, expand=True)

        self.text.tag_configure("bold", font=self.bold_font)
        self.text.tag_configure("italic", font=self.italic_font)

        menubar = Menu(self.root)
        backup_menu = Menu(menubar, tearoff=0)
        backup_menu.add_command(label="Локальный бэкап", command=self.make_backup)
        backup_menu.add_command(label="Google Drive (заглушка)", command=self.google_drive_backup)
        backup_menu.add_command(label="Dropbox (заглушка)", command=self.dropbox_backup)
        menubar.add_cascade(label="Синхронизация/Бэкап", menu=backup_menu)
        self.root.config(menu=menubar)

    def _load_notes(self) -> None:
        selected = self.current_note_id
        self.notes = self.repo.list_notes(
            include_archived=self.show_archived.get(),
            search=self.search_var.get().strip(),
            sort_mode=self.sort_var.get(),
        )
        self.listbox.delete(0, END)
        pick_idx = None
        for idx, n in enumerate(self.notes):
            prefix = "[ARCH] " if n.archived else ""
            suffix = f"  ⏰ {n.deadline}" if n.deadline else ""
            self.listbox.insert(END, f"{prefix}{n.title}{suffix}")
            if selected and n.id == selected:
                pick_idx = idx
        if pick_idx is not None:
            self.listbox.selection_set(pick_idx)
            self.listbox.event_generate("<<ListboxSelect>>")
        elif self.notes:
            self.listbox.selection_set(0)
            self.listbox.event_generate("<<ListboxSelect>>")
        else:
            self.current_note_id = None
            self._clear_editor()

    def _clear_editor(self) -> None:
        self.title_var.set("")
        self.note_date_var.set("")
        self.deadline_var.set("")
        self.tags_var.set("")
        self.text.delete("1.0", END)
        self.created_lbl.config(text="Создано: —")
        self.updated_lbl.config(text="Изменено: —")

    def new_note(self) -> None:
        note_id = self.repo.create_note()
        self.current_note_id = note_id
        self._load_notes()

    def _selected_note(self) -> Note | None:
        if self.current_note_id is None:
            return None
        return self.repo.get_note(self.current_note_id)

    def on_note_select(self, _event=None) -> None:
        if not self.listbox.curselection():
            return
        idx = self.listbox.curselection()[0]
        note = self.notes[idx]
        self.current_note_id = note.id
        self.title_var.set(note.title)
        self.note_date_var.set(note.note_date or "")
        self.deadline_var.set(note.deadline or "")
        self.tags_var.set(note.tags or "")
        self.text.delete("1.0", END)
        self.text.insert("1.0", note.content)
        self.created_lbl.config(text=f"Создано: {note.created_at}")
        self.updated_lbl.config(text=f"Изменено: {note.updated_at}")

    def save_current(self) -> None:
        note = self._selected_note()
        if not note:
            return
        self.repo.update_note(
            note.id,
            title=self.title_var.get().strip() or "Без названия",
            content=self.text.get("1.0", END).rstrip("\n"),
            note_date=self.note_date_var.get().strip() or None,
            deadline=self.deadline_var.get().strip() or None,
            tags=self.tags_var.get().strip(),
        )
        self._load_notes()

    def delete_note(self) -> None:
        note = self._selected_note()
        if not note:
            return
        if messagebox.askyesno("Удалить", "Удалить заметку безвозвратно?"):
            self.repo.delete_note(note.id)
            self.current_note_id = None
            self._load_notes()

    def toggle_archive(self) -> None:
        note = self._selected_note()
        if not note:
            return
        self.repo.update_note(note.id, archived=0 if note.archived else 1)
        self._load_notes()

    def extend_deadline(self) -> None:
        note = self._selected_note()
        if not note:
            return
        if not note.deadline:
            messagebox.showinfo("Дедлайн", "У этой заметки не установлен дедлайн")
            return
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M", DATE_FMT, "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(note.deadline, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            messagebox.showerror("Ошибка", "Не удалось распознать формат дедлайна")
            return
        new_deadline = (parsed + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        self.repo.update_note(note.id, deadline=new_deadline)
        self._load_notes()

    def export_notes(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Экспорт заметок",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
        )
        if not path:
            return
        self.repo.export_json(path)
        messagebox.showinfo("Экспорт", f"Экспортировано в {path}")

    def import_notes(self) -> None:
        path = filedialog.askopenfilename(title="Импорт заметок", filetypes=[("JSON files", "*.json")])
        if not path:
            return
        count = self.repo.import_json(path)
        self._load_notes()
        messagebox.showinfo("Импорт", f"Импортировано заметок: {count}")

    def make_backup(self) -> None:
        directory = filedialog.askdirectory(title="Выберите папку для бэкапа")
        if not directory:
            return
        path = self.backup.local_backup(directory)
        messagebox.showinfo("Бэкап", f"Создан бэкап:\n{path}")

    def google_drive_backup(self) -> None:
        try:
            self.backup.google_drive_backup()
        except NotImplementedError as e:
            messagebox.showinfo("Google Drive", str(e))

    def dropbox_backup(self) -> None:
        try:
            self.backup.dropbox_backup()
        except NotImplementedError as e:
            messagebox.showinfo("Dropbox", str(e))

    def make_bold(self) -> None:
        self._toggle_tag("bold")

    def make_italic(self) -> None:
        self._toggle_tag("italic")

    def make_normal(self) -> None:
        try:
            start = self.text.index("sel.first")
            end = self.text.index("sel.last")
        except Exception:
            return
        self.text.tag_remove("bold", start, end)
        self.text.tag_remove("italic", start, end)

    def _toggle_tag(self, tag: str) -> None:
        try:
            start = self.text.index("sel.first")
            end = self.text.index("sel.last")
        except Exception:
            return
        if tag in self.text.tag_names("sel.first"):
            self.text.tag_remove(tag, start, end)
        else:
            self.text.tag_add(tag, start, end)

    def open_calendar(self) -> None:
        notes = self.repo.list_notes(True, "", "deadline_asc")
        win = Toplevel(self.root)
        win.title("Дедлайны — календарь")
        win.state("zoomed") if os.name == "nt" else win.attributes("-zoomed", True)

        if Calendar is None:
            Label(
                win,
                text="tkcalendar не установлен. Установите: pip install tkcalendar",
                fg="red",
            ).pack(pady=10)
            lb = Listbox(win)
            lb.pack(fill=BOTH, expand=True, padx=10, pady=10)
            for n in notes:
                if n.deadline:
                    lb.insert(END, f"{n.deadline} — {n.title}")
            return

        cal = Calendar(win, selectmode="day", date_pattern="yyyy-mm-dd", locale="ru_RU")
        cal.pack(fill=BOTH, expand=True, padx=10, pady=10)
        map_by_date: dict[str, list[str]] = {}
        for n in notes:
            if not n.deadline:
                continue
            d = n.deadline[:10]
            map_by_date.setdefault(d, []).append(n.title)
            cal.calevent_create(datetime.strptime(d, "%Y-%m-%d"), n.title, "deadline")
        cal.tag_config("deadline", background="#f06292", foreground="white")

        info = Text(win, height=8)
        info.pack(fill="x", padx=10, pady=(0, 10))

        def show_day(_evt=None):
            d = cal.get_date()
            info.delete("1.0", END)
            titles = map_by_date.get(d, [])
            if not titles:
                info.insert("1.0", f"На {d} дедлайнов нет")
            else:
                info.insert("1.0", "\n".join(f"• {t}" for t in titles))

        cal.bind("<<CalendarSelected>>", show_day)
        show_day()

    def _start_reminder_loop(self) -> None:
        def worker():
            while True:
                now = datetime.now()
                soon = now + timedelta(minutes=10)
                notes = self.repo.list_notes(True, "", "deadline_asc")
                for n in notes:
                    if not n.deadline:
                        continue
                    try:
                        dl = datetime.strptime(n.deadline, "%Y-%m-%d %H:%M")
                    except ValueError:
                        continue
                    if now <= dl <= soon:
                        self.root.after(
                            0,
                            lambda title=n.title, deadline=n.deadline: messagebox.showwarning(
                                "Напоминание",
                                f"Скоро дедлайн: {title}\n{deadline}",
                            ),
                        )
                threading.Event().wait(60)

        t = threading.Thread(target=worker, daemon=True)
        t.start()


if __name__ == "__main__":
    root = Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    app = NotesApp(root)
    root.mainloop()
