import flet as ft
import re, unicodedata, os
from pathlib import Path

_ROMAN_RUN = re.compile(r"[A-Za-z']+|[Ａ-Ｚａ-ｚ＇]+")

# 対象子音（nは除外）
def _is_target_consonant(ch: str) -> bool:
    c = ch.lower()
    return ("a" <= c <= "z") and (c not in "aeiouv" and c != "n")

def _is_fullwidth_run(s: str) -> bool:
    has_fw = any("Ａ" <= ch <= "Ｚ" or "ａ" <= ch <= "ｚ" for ch in s)
    has_hw = any("A" <= ch <= "Z" or "a" <= ch <= "z" for ch in s)
    return has_fw and not has_hw

def _to_fullwidth_letters(s: str) -> str:
    out = []
    for ch in s:
        if "a" <= ch <= "z":
            out.append(chr(ord(ch) + (ord("ａ") - ord("a"))))
        elif "A" <= ch <= "Z":
            out.append(chr(ord(ch) + (ord("Ａ") - ord("A"))))
        else:
            out.append(ch)
    return "".join(out)

def _convert_roman_run_to_atok(run: str) -> str:
    was_full = _is_fullwidth_run(run)
    s = unicodedata.normalize("NFKC", run) 

    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if _is_target_consonant(ch):
            j = i + 1
            while j < n and s[j].lower() == ch.lower():
                j += 1
            L = j - i
            if L >= 2:
                last_char = s[j - 1]
                out.append("っ" * (L - 1) + last_char)
                i = j
                continue
            else:
                out.append(ch)
                i += 1
                continue
        else:
            out.append(ch)
            i += 1

    result = "".join(out)
    if was_full:
        result = _to_fullwidth_letters(result)
    return result

def convert_first_field(line: str) -> str:
    """TSVの1列目だけ変換"""
    if "\t" not in line:
        return line
    first, rest = line.split("\t", 1)

    out, i = [], 0
    for m in _ROMAN_RUN.finditer(first):
        out.append(first[i:m.start()])
        run = first[m.start():m.end()]
        out.append(_convert_roman_run_to_atok(run))
        i = m.end()
    out.append(first[i:])
    return "".join(out) + "\t" + rest

def convert_text(text: str):
    """全文変換。戻り値: (出力テキスト, 変更行数, 差分テキスト)"""
    in_lines = text.splitlines()
    out_lines, diffs = [], []
    changed = 0
    for idx, ln in enumerate(in_lines, start=1):
        new_ln = convert_first_field(ln) if "\t" in ln else ln
        out_lines.append(new_ln)
        if new_ln != ln:
            changed += 1
            diffs.append(f"[{idx}] {ln}  ⇒  {new_ln}")
    return "\n".join(out_lines), changed, "\n".join(diffs)

# UI
def main(page: ft.Page):
    page.title = "Meriem"
    page.window_min_width = 980
    page.window_min_height = 700
    page.theme_mode = ft.ThemeMode.LIGHT

    current_file: Path | None = None
    input_text = ""
    output_text = ""
    diff_text = ""

    stat = ft.Text("準備OK", size=12, color=ft.Colors.GREY_700)
    changed_txt = ft.Text("変更行: 0 / 総行数: 0", size=12)

    mono = ft.TextStyle(font_family="Consolas", size=13)
    input_tf = ft.TextField(label="入力（原文）", multiline=True, read_only=True,
                            text_style=mono, min_lines=16, expand=True)
    output_tf = ft.TextField(label="出力（変換後）", multiline=True, read_only=True,
                             text_style=mono, min_lines=16, expand=True)
    diff_tf = ft.TextField(label="差分", multiline=True, read_only=True,
                           text_style=mono, min_lines=8, expand=True)

    open_picker = ft.FilePicker()
    save_picker = ft.FilePicker()
    page.overlay.extend([open_picker, save_picker])

    def set_status(msg: str, ok=True):
        stat.value = msg
        stat.color = ft.Colors.GREEN_700 if ok else ft.Colors.RED_700
        page.update()

    def load_file(path: Path):
        nonlocal input_text, output_text, diff_text, current_file
        try:
            txt = Path(path).read_text(encoding="cp932")
        except Exception as e:
            set_status(f"読み込み失敗: {e}", ok=False)
            return
        input_text = txt
        output_text, changed, diff_text = convert_text(txt)
        input_tf.value = input_text
        output_tf.value = output_text
        diff_tf.value = diff_text
        changed_txt.value = f"変更行: {changed} / 総行数: {len(input_text.splitlines())}"
        current_file = Path(path)
        set_status(f"読み込み完了: {current_file.name}（ANSI）")

    def save_output(to_path: Path | None):
        if not output_text:
            set_status("出力がありません。先にファイルを開いてください。", ok=False)
            return
        try:
            Path(to_path).write_text(output_text, encoding="cp932", newline="\r\n")
            set_status(f"保存しました: {to_path}", ok=True)
        except Exception as e:
            set_status(f"保存失敗: {e}", ok=False)

    def on_open_click(e):
        open_picker.pick_files(
            allow_multiple=False,
            allowed_extensions=["txt", "tsv"],
            dialog_title="TXT/TSVを選択",
        )

    def on_open_result(e: ft.FilePickerResultEvent):
        if e.files and e.files[0].path:
            load_file(Path(e.files[0].path))

    open_picker.on_result = on_open_result

    def on_copy_output(e):
        if not output_text:
            set_status("出力がありません。", ok=False)
            return
        page.set_clipboard(output_text)
        set_status("出力をコピーしました。")

    def on_save_click(e):
        if not output_text:
            set_status("出力がありません。", ok=False)
            return
        default_name = (
            f"{current_file.stem}_ATOK.txt" if current_file else "converted_ATOK変換.txt"
        )
        save_picker.save_file(file_name=default_name, allowed_extensions=["txt"])

    def on_save_result(e: ft.FilePickerResultEvent):
        if e.path:
            save_output(Path(e.path))

    save_picker.on_result = on_save_result

    top_bar = ft.Row(
        controls=[
            ft.ElevatedButton("ファイルを選択", icon=ft.Icons.FOLDER_OPEN, on_click=on_open_click),
            ft.OutlinedButton("出力をコピー", icon=ft.Icons.CONTENT_COPY, on_click=on_copy_output),
            ft.FilledTonalButton("出力を保存", icon=ft.Icons.SAVE, on_click=on_save_click),
            ft.Container(expand=True),
            changed_txt,
        ],
        alignment=ft.MainAxisAlignment.START,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    two_pane = ft.Row(controls=[input_tf, output_tf], expand=True, spacing=10)
    single_tab_content = ft.Column(controls=[two_pane, diff_tf], expand=True, spacing=10)

    panes = ft.Tabs(
        selected_index=0,
        expand=True,
        tabs=[ft.Tab(text="プレビュー", content=single_tab_content)],
    )

    page.add(
        ft.AppBar(title=ft.Text("Microsoft IME→ATOK変換器")),
        ft.Container(content=top_bar, padding=10),
        ft.Container(content=panes, padding=10, expand=True),
        ft.Container(content=stat, padding=10),
    )

if __name__ == "__main__":
    ft.app(target=main)
