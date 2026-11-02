#!/usr/bin/env python3
"""Radar — вікно налаштувань (GTK 3)."""

import datetime as dt
import json
import re
import string
import subprocess
import sys
import threading
from pathlib import Path

import gi
from gi.repository import GLib

# Ім'я вікна має збігатися з radar.desktop, інакше Cinnamon не дасть закріпити його на панелі.
# Задаємо до імпорту Gtk, бо Gtk запам'ятовує ім'я під час ініціалізації.
GLib.set_prgname("radar")
GLib.set_application_name("Radar")

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import radar  # noqa: E402


# Чорно-біле оформлення поверх теми Mint: світле або темне, як у системі
STYLE = string.Template("""
window, notebook, notebook > stack, scrolledwindow, treeview.view, textview, textview text, frame {
    background-color: $bg; color: $text;
}
notebook > header { background-color: $bg; border-color: $line; }
notebook > header tab { color: $muted; background: none; border-color: transparent; }
notebook > header tab:checked { color: $strong; box-shadow: inset 0 -2px $strong; }
treeview.view:selected, treeview.view:selected:focus { background-color: $strong; color: $on_strong; }
treeview.view header button { background: $field; color: $muted; border-color: $line; }
frame > border { border-color: $line; }
entry, spinbutton, spinbutton entry, spinbutton button {
    background: $field; color: $text; border-color: $border; caret-color: $strong;
}
button { background-image: none; background-color: $button; color: $text; border-color: $border; box-shadow: none; text-shadow: none; }
button:hover { background-color: $button_hover; }
button.suggested-action { background-color: $strong; color: $on_strong; border-color: $strong; }
button.suggested-action:hover { background-color: $strong_hover; }
button.suggested-action label { color: $on_strong; }
separator { background-color: $line; }
label { color: $text; }
check, radio {
    -gtk-icon-source: none; background-image: none; background-color: $bg;
    border: 1px solid $check_border; box-shadow: none; min-width: 14px; min-height: 14px; margin-right: 6px;
}
check { border-radius: 3px; }
radio { border-radius: 100%; }
check:checked {
    -gtk-icon-source: -gtk-icontheme("object-select-symbolic");
    background-color: $strong; border-color: $strong; color: $on_strong;
}
radio:checked {
    background-image: radial-gradient(circle, $on_strong 22%, $strong 32%);
    background-color: $strong; border-color: $strong;
}
check:hover, radio:hover { border-color: $check_hover; }
""")

DARK = dict(bg="#000000", text="#ededed", muted="#8c8c8c", line="#222222", field="#0c0c0c",
            border="#333333", button="#141414", button_hover="#232323", strong="#ffffff",
            on_strong="#000000", strong_hover="#d9d9d9", check_border="#7a7a7a", check_hover="#bdbdbd")
LIGHT = dict(bg="#ffffff", text="#0a0a0a", muted="#6b6b6b", line="#e2e2e2", field="#ffffff",
             border="#cfcfcf", button="#f4f4f4", button_hover="#e8e8e8", strong="#000000",
             on_strong="#ffffff", strong_hover="#2b2b2b", check_border="#8a8a8a", check_hover="#3a3a3a")


def system_prefers_dark():
    """Світла чи темна тема в налаштуваннях Mint (Параметри → Тема)."""
    source = Gio.SettingsSchemaSource.get_default()
    for schema in ("org.x.apps.portal", "org.gnome.desktop.interface"):
        if source and source.lookup(schema, True) and "color-scheme" in source.lookup(schema, True).list_keys():
            scheme = Gio.Settings.new(schema).get_string("color-scheme")
            if scheme == "prefer-dark":
                return True
            if scheme == "prefer-light":
                return False
    return "dark" in (Gtk.Settings.get_default().get_property("gtk-theme-name") or "").lower()


class Theme:
    """Застосовує світле чи темне оформлення і перемикає його, коли змінюється тема системи."""

    def __init__(self):
        self.provider = None
        self.watched = []
        source = Gio.SettingsSchemaSource.get_default()
        for schema in ("org.x.apps.portal", "org.gnome.desktop.interface"):
            if source and source.lookup(schema, True):
                settings = Gio.Settings.new(schema)
                settings.connect("changed", lambda *_args: self.apply())
                self.watched.append(settings)  # тримаємо посилання, інакше сигнал зникне
        Gtk.Settings.get_default().connect("notify::gtk-theme-name", lambda *_args: self.apply())
        self.apply()

    def apply(self):
        dark = system_prefers_dark()
        screen = Gdk.Screen.get_default()
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", dark)
        if self.provider:
            Gtk.StyleContext.remove_provider_for_screen(screen, self.provider)
        self.provider = Gtk.CssProvider()
        self.provider.load_from_data(STYLE.substitute(DARK if dark else LIGHT).encode())
        Gtk.StyleContext.add_provider_for_screen(screen, self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def in_background(work, done):
    """Виконує work() у фоновому потоці, а done(result, error) — у головному."""
    def target():
        try:
            result, error = work(), None
        except Exception as exc:  # показуємо помилку у вікні
            result, error = None, exc
        GLib.idle_add(done, result, error)
    threading.Thread(target=target, daemon=True).start()


def margin(widget, size=12):
    for side in ("top", "bottom", "start", "end"):
        getattr(widget, f"set_margin_{side}")(size)
    return widget


def message(parent, text, kind=Gtk.MessageType.INFO, secondary=None):
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=kind,
                               buttons=Gtk.ButtonsType.OK, text=text)
    if secondary:
        dialog.format_secondary_text(secondary)
    dialog.run()
    dialog.destroy()


def confirm(parent, text):
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION,
                               buttons=Gtk.ButtonsType.YES_NO, text=text)
    answer = dialog.run()
    dialog.destroy()
    return answer == Gtk.ResponseType.YES


class NewsPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        margin(self, 18)
        self.window = window

        buttons = Gtk.Box(spacing=12)
        self.refresh = Gtk.Button(label="Оновити новини зараз")
        self.refresh.get_style_context().add_class("suggested-action")
        self.refresh.connect("clicked", self.on_refresh)
        open_button = Gtk.Button(label="Відкрити список новин")
        open_button.connect("clicked", lambda _b: radar.open_report())
        buttons.pack_start(self.refresh, False, False, 0)
        buttons.pack_start(open_button, False, False, 0)
        self.spinner = Gtk.Spinner()
        buttons.pack_start(self.spinner, False, False, 0)
        self.message = Gtk.Label(xalign=0)
        buttons.pack_start(self.message, False, False, 0)
        self.pack_start(buttons, False, False, 0)

        self.status = Gtk.Label(xalign=0, selectable=True)
        self.status.set_line_wrap(True)
        self.pack_start(self.status, False, False, 0)
        self.update_status()

    def update_status(self):
        settings = radar.load_settings()
        lines = [f"<b>Розклад:</b> {radar.describe_schedule(settings)}"]
        if not settings["notifications"]:
            lines.append("<b>Сповіщення:</b> вимкнено")
        if settings["digest_enabled"]:
            lines.append(f"<b>Ранковий підсумок:</b> щодня о {settings['digest_time']}")
        nxt = radar.next_run_text()
        if nxt:
            lines.append(f"<b>Наступне оновлення:</b> {GLib.markup_escape_text(nxt)}")
        if radar.DB_FILE.exists():
            db = radar.open_db()
            row = db.execute("SELECT * FROM runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                when = radar.from_iso(row["finished"]).astimezone().strftime("%H:%M, %d.%m.%Y")
                lines.append(f"<b>Останнє оновлення:</b> {when} · {radar.publications(row['new_count'])}")
                errors = json.loads(row["errors"] or "{}")
                if errors:
                    lines.append("<b>Не вдалося отримати:</b> " + GLib.markup_escape_text(", ".join(errors)))
            db.close()
        self.status.set_markup("\n".join(lines))

    def on_refresh(self, _button):
        self.refresh.set_sensitive(False)
        self.spinner.start()
        self.message.set_text("Збираю новини…")
        in_background(lambda: radar.run(notify=False, verbose=False), self.on_done)

    def on_done(self, result, error):
        self.refresh.set_sensitive(True)
        self.spinner.stop()
        if error:
            self.message.set_text(f"Помилка: {error}")
        elif result is None:
            self.message.set_text("Radar саме оновлює новини за розкладом. Спробуйте за хвилину.")
        else:
            self.message.set_text("")  # підсумок оновлення показує рядок «Останнє оновлення»
        self.update_status()
        return False


class SourcesPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        margin(self, 18)
        self.window = window
        # увімкнено, назва, адреса, спосіб, усі новини
        self.store = Gtk.ListStore(bool, str, str, str, bool)
        self.loading = False
        self.store.connect("row-deleted", self.on_dragged)  # після перетягування рядка
        view = Gtk.TreeView(model=self.store)
        view.set_reorderable(True)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.on_toggle)
        view.append_column(Gtk.TreeViewColumn("Увімк.", toggle, active=0))
        name = Gtk.CellRendererText(editable=True)
        name.connect("edited", self.on_rename)
        column = Gtk.TreeViewColumn("Видання", name, text=1)
        column.set_resizable(True)
        view.append_column(column)
        url = Gtk.CellRendererText(ellipsize=3)
        column = Gtk.TreeViewColumn("Адреса", url, text=2)
        column.set_expand(True)
        view.append_column(column)
        view.append_column(Gtk.TreeViewColumn("Як читається", Gtk.CellRendererText(), text=3))
        everything = Gtk.CellRendererToggle()
        everything.connect("toggled", self.on_all_news)
        view.append_column(Gtk.TreeViewColumn("Усі новини", everything, active=4))
        self.view = view
        scroll = Gtk.ScrolledWindow()
        scroll.add(view)
        self.pack_start(scroll, True, True, 0)

        row = Gtk.Box(spacing=8)
        self.url_entry = Gtk.Entry(placeholder_text="https://сайт.ua/news")
        self.url_entry.set_hexpand(True)
        self.url_entry.connect("activate", self.on_add)
        self.name_entry = Gtk.Entry(placeholder_text="Назва (необов'язково)")
        self.add_button = Gtk.Button(label="Додати")
        self.add_button.connect("clicked", self.on_add)
        remove = Gtk.Button(label="Видалити вибране")
        remove.connect("clicked", self.on_remove)
        up = Gtk.Button(label="▲")
        up.set_tooltip_text("Перемістити вище")
        up.connect("clicked", self.on_move, -1)
        down = Gtk.Button(label="▼")
        down.set_tooltip_text("Перемістити нижче")
        down.connect("clicked", self.on_move, 1)
        self.spinner = Gtk.Spinner()
        for widget in (self.url_entry, self.name_entry, self.add_button, self.spinner, up, down, remove):
            row.pack_start(widget, widget is self.url_entry, True, 0)
        self.pack_start(row, False, False, 0)
        self.reload()

    def reload(self):
        self.loading = True
        self.store.clear()
        for s in radar.load_sources():
            kind = radar.source_kind(s)
            self.store.append([s.get("enabled", True), s["name"], s["url"], kind, bool(s.get("all_news"))])
        self.loading = False

    def save_order(self):
        by_name = {s["name"]: s for s in radar.load_sources()}
        names = [row[1] for row in self.store]
        if sorted(names) == sorted(by_name):
            radar.save_sources([by_name[n] for n in names])

    def on_dragged(self, _model, _path):
        if not self.loading:
            self.save_order()

    def on_move(self, _button, step):
        model, it = self.view.get_selection().get_selected()
        if not it:
            return
        i = model.get_path(it)[0]
        j = i + step
        if not 0 <= j < len(model):
            return
        other = model.get_iter(j)
        model.swap(it, other)
        self.save_order()
        self.view.scroll_to_cell(Gtk.TreePath(j), None, False, 0, 0)

    def on_toggle(self, _cell, path):
        sources = radar.load_sources()
        i = int(path)
        sources[i]["enabled"] = not sources[i].get("enabled", True)
        radar.save_sources(sources)
        self.store[path][0] = sources[i]["enabled"]

    def on_all_news(self, _cell, path):
        sources = radar.load_sources()
        i = int(path)
        sources[i]["all_news"] = not sources[i].get("all_news", False)
        radar.save_sources(sources)
        self.store[path][4] = sources[i]["all_news"]

    def on_rename(self, _cell, path, text):
        text = text.strip()
        if not text:
            return
        sources = radar.load_sources()
        old = sources[int(path)]["name"]
        sources[int(path)]["name"] = text
        radar.save_sources(sources)
        if radar.DB_FILE.exists():  # старі новини теж показуємо під новою назвою
            db = radar.open_db()
            db.execute("UPDATE items SET source = ? WHERE source = ?", (text, old))
            db.commit()
            db.close()
        self.store[path][1] = text

    def on_add(self, _widget):
        url = self.url_entry.get_text().strip()
        if not url:
            return
        name = self.name_entry.get_text().strip() or None
        self.add_button.set_sensitive(False)
        self.spinner.start()
        in_background(lambda: radar.add_source(url, name), self.on_added)

    def on_added(self, result, error):
        self.add_button.set_sensitive(True)
        self.spinner.stop()
        if error:
            message(self.window, "Не вдалося додати видання", Gtk.MessageType.WARNING,
                    radar.friendly_error(error).capitalize())
            return False
        source, items = result
        self.url_entry.set_text("")
        self.name_entry.set_text("")
        self.reload()
        how = radar.HOW[radar.source_kind(source)]
        examples = "\n".join(f"• {i['title']}" for i in items[:3])
        message(self.window, f"Додано «{source['name']}»",
                secondary=f"Новини читатимуться {how}. Зараз на сайті {len(items)} новин, наприклад:\n\n{examples}")
        return False

    def on_remove(self, _button):
        model, it = self.view.get_selection().get_selected()
        if not it:
            return
        name = model[it][1]
        if not confirm(self.window, f"Видалити видання «{name}»?"):
            return
        sources = [s for s in radar.load_sources() if s["name"] != name]
        radar.save_sources(sources)
        self.reload()


class TopicsPage(Gtk.Box):
    # група, тема, ключі
    GROUP, NAME, KEYS = range(3)

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        margin(self, 18)
        self.window = window
        self.search = Gtk.SearchEntry(placeholder_text="Знайти тему або групу…")
        self.search.connect("search-changed", lambda _e: self.filter.refilter())
        self.pack_start(self.search, False, False, 0)

        self.store = Gtk.ListStore(str, str, str)
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self.visible)
        view = Gtk.TreeView(model=self.filter)
        for index, title in ((self.GROUP, "Група"), (self.NAME, "Тема"), (self.KEYS, "Ключові слова")):
            cell = Gtk.CellRendererText(editable=True)
            if index == self.GROUP:
                cell.set_property("foreground", "#8c8c8c")
            cell.connect("edited", self.on_edited, index)
            column = Gtk.TreeViewColumn(title, cell, text=index)
            column.set_resizable(True)
            if index == self.KEYS:
                column.set_expand(True)
            view.append_column(column)
        self.view = view
        scroll = Gtk.ScrolledWindow()
        scroll.add(view)
        self.pack_start(scroll, True, True, 0)

        row = Gtk.Box(spacing=8)
        self.new_group = Gtk.ComboBoxText.new_with_entry()
        self.new_group.get_child().set_placeholder_text("Група")
        self.new_name = Gtk.Entry(placeholder_text="Нова тема")
        self.new_keys = Gtk.Entry(placeholder_text="ключі через кому (необов'язково)")
        self.new_keys.set_hexpand(True)
        self.new_keys.connect("activate", self.on_add)
        add = Gtk.Button(label="Додати")
        add.connect("clicked", self.on_add)
        remove = Gtk.Button(label="Видалити вибрану")
        remove.connect("clicked", self.on_remove)
        as_text = Gtk.Button(label="Відкрити як текст")
        as_text.connect("clicked", lambda _b: subprocess.Popen(["xdg-open", str(radar.TOPICS_FILE)]))
        for widget in (self.new_group, self.new_name, self.new_keys, add, remove, as_text):
            row.pack_start(widget, widget is self.new_keys, True, 0)
        self.pack_start(row, False, False, 0)
        self.count = Gtk.Label(xalign=0)
        self.pack_start(self.count, False, False, 0)
        self.reload()

    def visible(self, model, it, _data):
        query = self.search.get_text().strip().casefold()
        text = " ".join(model[it][i] for i in (self.GROUP, self.NAME, self.KEYS))
        return not query or query in text.casefold()

    def groups(self):
        return list(dict.fromkeys(row[self.GROUP] for row in self.store))

    def reload(self):
        self.store.clear()
        self.header = []
        group, in_header = "Інше", True
        for line in radar.TOPICS_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                if in_header:
                    self.header.append(line)
                continue
            in_header = False
            if stripped.startswith("[") and stripped.endswith("]"):
                group = stripped[1:-1].strip() or "Інше"
                continue
            name, keys = radar.parse_topic_line(stripped)
            self.store.append([group, name, keys])
        self.refresh_groups()

    def refresh_groups(self):
        current = self.new_group.get_child().get_text()
        self.new_group.remove_all()
        for g in self.groups():
            self.new_group.append_text(g)
        self.new_group.get_child().set_text(current or "Інше")
        self.count.set_text(f"Тем: {len(self.store)} · груп: {len(self.groups())}")

    def save(self):
        lines = [l for l in self.header]
        while lines and not lines[-1].strip():
            lines.pop()
        for g in self.groups():
            lines.append(f"\n[{g}]")
            for group, name, keys in self.store:
                if group == g:
                    lines.append(f"{name}: {keys}" if keys else name)
        radar.TOPICS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.refresh_groups()

    def child_iter(self, path):
        return self.filter.convert_iter_to_child_iter(self.filter.get_iter(path))

    def on_edited(self, _cell, path, text, column):
        it = self.child_iter(path)
        text = text.strip()
        if column in (self.GROUP, self.NAME):
            text = text.replace(":", "").replace("[", "").replace("]", "").lstrip("!").strip()
            if not text:
                return
        if column == self.KEYS and text and not radar.parse_topics(f"x: {text}"):
            message(self.window, "Не вдалося розібрати ключові слова", Gtk.MessageType.WARNING)
            return
        self.store[it][column] = text
        self.save()

    def on_add(self, _widget):
        name = self.new_name.get_text().strip().replace(":", "").lstrip("!")
        keys = self.new_keys.get_text().strip()
        group = self.new_group.get_child().get_text().strip().strip("[]") or "Інше"
        if not name:
            self.new_name.grab_focus()
            return
        self.store.append([group, name, keys])
        self.save()
        self.new_name.set_text("")
        self.new_keys.set_text("")

    def on_remove(self, _button):
        model, it = self.view.get_selection().get_selected()
        if not it:
            return
        name = model[it][self.NAME]
        if confirm(self.window, f"Видалити тему «{name}»?"):
            self.store.remove(model.convert_iter_to_child_iter(it))
            self.save()


class SchedulePage(Gtk.Box):
    def __init__(self, window, news_page):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        margin(self, 18)
        self.window = window
        self.news_page = news_page
        settings = radar.load_settings()

        self.interval = Gtk.RadioButton.new_with_label(None, "Оновлювати кожні")
        self.minutes = Gtk.SpinButton.new_with_range(5, 1440, 5)
        self.minutes.set_value(settings["interval_minutes"])
        row = Gtk.Box(spacing=8)
        row.pack_start(self.interval, False, False, 0)
        row.pack_start(self.minutes, False, False, 0)
        row.pack_start(Gtk.Label(label="хвилин"), False, False, 0)
        self.pack_start(row, False, False, 0)

        self.at_times = Gtk.RadioButton.new_with_label_from_widget(self.interval, "Щодня о")
        self.times = Gtk.Entry(text=", ".join(settings["times"]), placeholder_text="08:00, 13:00, 19:00")
        row = Gtk.Box(spacing=8)
        row.pack_start(self.at_times, False, False, 0)
        row.pack_start(self.times, True, True, 0)
        self.pack_start(row, False, False, 0)

        self.off = Gtk.RadioButton.new_with_label_from_widget(self.interval, "Лише вручну (вимкнути розклад)")
        self.pack_start(self.off, False, False, 0)
        {"interval": self.interval, "times": self.at_times, "off": self.off}[settings["schedule_mode"]].set_active(True)

        self.pack_start(Gtk.Separator(), False, False, 6)

        self.days = Gtk.SpinButton.new_with_range(1, 30, 1)
        self.days.set_value(settings["report_days"])
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Показувати на сторінці новини за останні"), False, False, 0)
        row.pack_start(self.days, False, False, 0)
        row.pack_start(Gtk.Label(label="дн."), False, False, 0)
        self.pack_start(row, False, False, 0)

        self.summary = Gtk.CheckButton(label="Шукати ключові слова також у першому абзаці новини")
        self.summary.set_active(settings["match_summary"])
        self.pack_start(self.summary, False, False, 0)

        self.pack_start(Gtk.Separator(), False, False, 6)

        self.notifications = Gtk.CheckButton(label="Сповіщення на робочому столі")
        self.notifications.set_active(settings["notifications"])
        self.notifications.connect("toggled", self.on_notifications_toggled)
        self.pack_start(self.notifications, False, False, 0)

        self.digest = Gtk.CheckButton(label="Ранковий підсумок — одне сповіщення з головним за ніч, щодня о")
        self.digest.set_active(settings["digest_enabled"])
        self.digest_time = Gtk.Entry(text=settings["digest_time"], width_chars=6, max_length=5)
        row = Gtk.Box(spacing=8)
        row.pack_start(self.digest, False, False, 0)
        row.pack_start(self.digest_time, False, False, 0)
        self.pack_start(row, False, False, 0)
        self.digest_row = row
        self.on_notifications_toggled(self.notifications)

        apply = Gtk.Button(label="Зберегти")
        apply.get_style_context().add_class("suggested-action")
        apply.set_halign(Gtk.Align.START)
        apply.connect("clicked", self.on_apply)
        self.pack_start(apply, False, False, 6)
        self.result = Gtk.Label(xalign=0)
        self.pack_start(self.result, False, False, 0)

    def on_notifications_toggled(self, button):
        # Без сповіщень ранковий підсумок не діє
        self.digest_row.set_sensitive(button.get_active())

    def on_apply(self, _button):
        settings = radar.load_settings()
        times = re.findall(r"\d{1,2}:\d\d", self.times.get_text())
        if self.at_times.get_active():
            valid = [t for t in times if int(t.split(":")[0]) < 24 and int(t.split(":")[1]) < 60]
            if not valid:
                message(self.window, "Вкажіть час у форматі 08:00, 13:00", Gtk.MessageType.WARNING)
                return
            settings["schedule_mode"] = "times"
            settings["times"] = sorted(f"{int(t.split(':')[0]):02d}:{t.split(':')[1]}" for t in valid)
        elif self.off.get_active():
            settings["schedule_mode"] = "off"
        else:
            settings["schedule_mode"] = "interval"
        settings["interval_minutes"] = int(self.minutes.get_value())
        settings["report_days"] = int(self.days.get_value())
        settings["match_summary"] = self.summary.get_active()
        settings["notifications"] = self.notifications.get_active()
        digest_time = re.fullmatch(r"\s*(\d{1,2}):(\d\d)\s*", self.digest_time.get_text())
        if self.digest.get_active() and not (digest_time and int(digest_time[1]) < 24 and int(digest_time[2]) < 60):
            message(self.window, "Вкажіть час підсумку у форматі 09:00", Gtk.MessageType.WARNING)
            return
        if digest_time:
            settings["digest_time"] = f"{int(digest_time[1]):02d}:{digest_time[2]}"
        settings["digest_enabled"] = self.digest.get_active()
        radar.save_settings(settings)
        radar.apply_schedule(settings)
        self.times.set_text(", ".join(settings["times"]))
        self.result.set_text(f"Збережено: {radar.describe_schedule(settings)} · "
                             f"{dt.datetime.now():%H:%M:%S}")
        self.news_page.update_status()


class RadarWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Radar")
        self.set_default_size(980, 720)
        if radar.ICON_FILE.exists():
            self.set_icon_from_file(str(radar.ICON_FILE))
        notebook = Gtk.Notebook()
        news = NewsPage(self)
        notebook.append_page(news, Gtk.Label(label="Новини"))
        notebook.append_page(SourcesPage(self), Gtk.Label(label="Видання"))
        notebook.append_page(TopicsPage(self), Gtk.Label(label="Теми"))
        notebook.append_page(SchedulePage(self, news), Gtk.Label(label="Розклад і параметри"))
        self.add(notebook)


def main():
    radar.ensure_config()
    theme = Theme()  # noqa: F841 — живе, поки відкрите вікно
    window = RadarWindow()
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
