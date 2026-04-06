from __future__ import annotations

try:
    import flet as ft  # type: ignore[import-not-found]
except ModuleNotFoundError:  # 変換ロジックの単体利用向け（UI起動時は必要）
    ft = None  # type: ignore[assignment]
import re, unicodedata
import json, time
from pathlib import Path

_ROMAN_RUN = re.compile(r"[A-Za-z']+|[Ａ-Ｚａ-ｚ＇]+")
_POS_COMMENT_SPLIT = re.compile(r"^(\S+)\s{2,}(.*)$")
# 長音記号「ー」(U+30FC) を読み列に許可
_DISALLOWED_READING_CHARS = re.compile(r"[^ぁ-ゖ゛゜ー0-9０-９A-Za-zＡ-Ｚａ-ｚ]+")

# 読み（A列）正規化: 濁点・半濁点（結合文字/分離記号）
_DAKUTEN_MARKS = frozenset(("\u3099", "\u309a", "\u309b", "\u309c"))
# カタカナ「ヴ」→ ひらがな「う」+ 分離濁点「゛」（ゔ に合成されない表記）
_U_VOICED = "\u3046\u309b"
_VU_KATAKANA = "\u30f4"

# 品詞ラベル（3列目）の完全一致置換辞書
_POS_LABEL_MAP_RAW: dict[str, str] = {
    "固有名詞": "固有一般",
    "姓": "固有人名",
    "地名その他": "固有地名",
    "さ変名詞": "名詞サ変",
    "ざ変名詞": "名詞ザ変",
    "か行五段": "カ行五段",
    "が行五段": "ガ行五段",
    "さ行五段": "サ行五段",
    "た行五段": "タ行五段",
    "な行五段": "ナ行五段",
    "は行五段": "ハ行五段",
    "ば行五段": "バ行五段",
    "ま行五段": "マ行五段",
    "ら行五段": "ラ行五段",
    "あわ行五段": "アワ行五段",
}

# 念のため、キーは実装前に NFKC 正規化して同一化
_POS_LABEL_MAP: dict[str, str] = {
    unicodedata.normalize("NFKC", k): v for k, v in _POS_LABEL_MAP_RAW.items()
}

_DEBUG_LOG_PATH = Path(__file__).resolve().parent / "debug-85397b.log"
_DEBUG_SESSION_ID = "85397b"
_DEBUG_RUN_ID = "pre-fix"


def _append_debug_ndjson(payload: dict) -> None:
    # NDJSON (one JSON object per line) を追記
    payload = dict(payload)
    payload.setdefault("timestamp", int(time.time() * 1000))
    with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _debug_log(
    hypothesisId: str,
    location: str,
    message: str,
    data: dict | None = None,
) -> None:
    # ルール: secrets/PIIは入れない。必要なら preview を短くする。
    _append_debug_ndjson(
        {
            "sessionId": _DEBUG_SESSION_ID,
            "runId": _DEBUG_RUN_ID,
            "hypothesisId": hypothesisId,
            "location": location,
            "message": message,
            "data": data or {},
        }
    )


def _reading_units(s: str) -> list[str]:
    """NFD クラスタごとに分割し、各要素を NFC の最短表現に戻す。"""
    s = unicodedata.normalize("NFC", s)
    s_nfd = unicodedata.normalize("NFD", s)
    parts: list[str] = []
    i, n = 0, len(s_nfd)
    while i < n:
        ch = s_nfd[i]
        if unicodedata.combining(ch):
            i += 1
            continue
        j = i + 1
        while j < n and unicodedata.combining(s_nfd[j]):
            j += 1
        parts.append(unicodedata.normalize("NFC", s_nfd[i:j]))
        i = j
    return parts


def _reading_unit_weight(unit: str) -> int:
    """仕様: 濁点・半濁点付きは 2 カウント、それ以外は 1。"""
    nfd = unicodedata.normalize("NFD", unit)
    return 2 if any(c in _DAKUTEN_MARKS for c in nfd) else 1


def _replace_vu_in_reading(s: str) -> str:
    """機能3: ヴ → う゛（結合濁点）。"""
    return s.replace(_VU_KATAKANA, _U_VOICED)


def _is_allowed_reading_char(ch: str) -> bool:
    """機能2: ひらがな・長音・数字・英字のみ許可（半角・全角）。"""
    o = ord(ch)
    if 0x3041 <= o <= 0x3096:
        return True
    # 長音記号（カタカナ長音。読み列で「ー」を保持する）
    if ch == "\u30fc":
        return True
    # 分離濁点/半濁点（「う゛」の゛を残す）
    if ch == "\u309b" or ch == "\u309c":
        return True
    if "0" <= ch <= "9":
        return True
    if 0xFF10 <= o <= 0xFF19:
        return True
    if "A" <= ch <= "Z" or "a" <= ch <= "z":
        return True
    if 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:
        return True
    return False


def _filter_reading_column(s: str) -> str:
    """機能2: ひらがな・数字・英字以外を削除して左詰め。"""
    s = unicodedata.normalize("NFC", s)
    # 「、」「。」「．」「・」「？」「！」などの記号を除去
    return _DISALLOWED_READING_CHARS.sub("", s)


def _truncate_reading_at_count32(s: str) -> str:
    """機能1: 独自カウントで 32 以上なら 32 カウント目を @ にし、以降を削除。"""
    s = unicodedata.normalize("NFC", s)
    units = _reading_units(s)
    if not units:
        return s
    weights = [_reading_unit_weight(u) for u in units]
    if sum(weights) < 32:
        return s
    out: list[str] = []
    cum = 0
    for u, w in zip(units, weights):
        if cum >= 32:
            break
        if cum + w <= 31:
            out.append(u)
            cum += w
        else:
            # 32 カウント目がこのユニットにかかる → ユニット全体を @（仕様メモ）
            out.append("@")
            break
    return "".join(out)


def _normalize_reading_column_a(first: str) -> str:
    """読み列の 3→2→1（その後に英字→促音を適用）。"""
    s = unicodedata.normalize("NFKC", first)
    s = _replace_vu_in_reading(s)
    s = _filter_reading_column(s)
    s = _truncate_reading_at_count32(s)
    return s


# 対象子音（nは除外）
def _is_target_consonant(ch: str) -> bool:
    c = ch.lower()
    return ("a" <= c <= "z") and (c not in "aeiou" and c != "n")

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
    first = _normalize_reading_column_a(first)

    out, i = [], 0
    for m in _ROMAN_RUN.finditer(first):
        out.append(first[i:m.start()])
        run = first[m.start():m.end()]
        out.append(_convert_roman_run_to_atok(run))
        i = m.end()
    out.append(first[i:])
    return "".join(out) + "\t" + rest

def _blank_pos_column_in_tsv(line: str) -> str:
    """TSVの品詞無し（省入力）出力へ整形。語句列は残し、品詞列のみ空にする。

    期待例（品詞無し）:
    - ① 読み\t語句\t品詞\tコメント -> 読み\t語句\t\tコメント
    - ② 読み\t品詞\tコメント -> 読み\t\t\tコメント（語句欠落時は空欄）
    - ③ 読み\t語句\t品詞  コメント -> 読み\t語句\t\tコメント
    """
    if "\t" not in line:
        return line
    parts = line.split("\t")
    if len(parts) < 3:
        return line
    # #region agent log (pos blank with unexpected column count)
    if len(parts) != 4:
        _debug_log(
            hypothesisId="H1_COLCOUNT",
            location="_blank_pos_column_in_tsv",
            message="blank_pos_called_with_non4_columns",
            data={
                "split_len": len(parts),
                "tabs": line.count("\t"),
                "parts_preview": parts[:5],
                "pos_before": parts[2] if len(parts) > 2 else None,
                "line_preview": line[:120],
            },
        )
    # #endregion
    # 入力は基本 4 列想定。語句列を落とさず、品詞列のみ空にする。
    if len(parts) == 4:
        # [0]=読み, [1]=語句, [2]=品詞, [3]=コメント
        return "\t".join([parts[0], parts[1], "", parts[3]])

    if len(parts) == 3:
        # ケースA: 3列目が「品詞(単語)+  コメント」で2+スペース区切りになっている
        #   例: 読み\t語句\t名詞  拠点/造語
        m = _POS_COMMENT_SPLIT.match(parts[2])
        if m:
            # 品詞は捨て、語句は残す（読み\t語句\t\tコメント）
            comment = m.group(2).strip()
            out = [parts[0], parts[1], "", comment]
            # #region agent log (pos blank normalize len=3 caseA)
            _debug_log(
                hypothesisId="H1_COLCOUNT_FIX",
                location="_blank_pos_column_in_tsv",
                message="blank_pos_norm_len3_caseA_split_pos_comment",
                data={"parts_preview": parts[:5], "comment": comment, "tabs_in": line.count("\t")},
            )
            # #endregion
            return "\t".join(out)

        # ケースB: 2列目が品詞で、3列目がコメント（語句列が欠落）
        #   例: 読み\t名詞\tコメント -> 読み\t\t\tコメント
        out = [parts[0], "", "", parts[2]]
        # #region agent log (pos blank normalize len=3 caseB)
        _debug_log(
            hypothesisId="H1_COLCOUNT_FIX",
            location="_blank_pos_column_in_tsv",
            message="blank_pos_norm_len3_caseB_pos_in_col2",
            data={"parts_preview": parts[:5], "tabs_in": line.count("\t")},
        )
        # #endregion
        return "\t".join(out)

    # 想定外: 5列以上。読み・語句（2列目）を残し、品詞を空、コメントは最後
    reading = parts[0]
    phrase = parts[1] if len(parts) > 1 else ""
    comment = parts[-1]
    return "\t".join([reading, phrase, "", comment])

def _convert_pos_column_exact(line: str) -> str:
    """TSVの3列目（品詞）を辞書で完全一致置換する。"""
    if "\t" not in line:
        return line
    parts = line.split("\t")
    if len(parts) < 3:
        return line

    pos = parts[2]
    pos_norm = unicodedata.normalize("NFKC", pos)
    mapped = _POS_LABEL_MAP.get(pos_norm)
    if mapped is None or mapped == pos:
        return line

    parts[2] = mapped
    return "\t".join(parts)


def _only_pos_column_changed(before: str, after: str) -> bool:
    """読み・語句・コメントは同一で、品詞列（0-based で index 2）だけが異なる行。"""
    if "\t" not in before or "\t" not in after:
        return False
    a = before.split("\t")
    b = after.split("\t")
    if len(a) != len(b) or len(a) < 3:
        return False
    if len(a) == 4:
        return a[0] == b[0] and a[1] == b[1] and a[3] == b[3] and a[2] != b[2]
    if len(a) == 3:
        return a[0] == b[0] and a[1] == b[1] and a[2] != b[2]
    return False


def convert_text(text: str, include_pos: bool = True):
    """戻り値: (出力テキスト, 変更行数, 差分テキスト)"""
    in_lines = text.splitlines()
    out_lines, diffs = [], []
    changed = 0
    for idx, ln in enumerate(in_lines, start=1):
        # #region agent log (input TSV split shape)
        if "\t" in ln:
            parts_len = len(ln.split("\t"))
            if parts_len != 4:
                _debug_log(
                    hypothesisId="H1_COLCOUNT",
                    location="convert_text",
                    message="input_tsv_split_len_not_4",
                    data={
                        "idx": idx,
                        "include_pos": include_pos,
                        "split_len": parts_len,
                        "tabs": ln.count("\t"),
                        "parts_preview": ln.split("\t")[:5],
                        "line_preview": ln[:120],
                    },
                )
        # #endregion
        new_ln = convert_first_field(ln) if "\t" in ln else ln
        # #region agent log (tab count stability after first-field conversion)
        if "\t" in ln and (len(ln.split("\t")) != 4):
            _debug_log(
                hypothesisId="H4_TAB_STABILITY",
                location="convert_text_after_convert_first_field",
                message="tab_count_change_after_first_field",
                data={
                    "idx": idx,
                    "tabs_in": ln.count("\t"),
                    "tabs_out": new_ln.count("\t"),
                    "out_preview": new_ln[:120],
                },
            )
        # #endregion
        if include_pos:
            new_ln = _convert_pos_column_exact(new_ln)
        else:
            new_ln = _blank_pos_column_in_tsv(new_ln)
        out_lines.append(new_ln)
        if new_ln != ln and not _only_pos_column_changed(ln, new_ln):
            changed += 1
            diffs.append(f"[{idx}行目] {ln}  ⇒  {new_ln}")
    return "\n".join(out_lines), changed, "\n".join(diffs)

# UI
def main(page: ft.Page):
    page.title = "Meriem ver.1.2"
    page.window_min_width = 980
    page.window_min_height = 700
    page.theme_mode = ft.ThemeMode.LIGHT

    current_file: Path | None = None
    input_text = ""
    output_text = ""
    diff_text = ""

    stat = ft.Text("準備OK", size=12, color=ft.Colors.GREY_700)
    changed_txt = ft.Text("変更行: 0 / 総行数: 0", size=12)
    include_pos = True  # True: 辞書（品詞あり）, False: 省入力（品詞列のみ空欄）

    mono = ft.TextStyle(font_family="Consolas", size=13)
    input_tf = ft.TextField(label="入力（原文）", multiline=True, read_only=True,
                            text_style=mono, min_lines=16, expand=True)
    output_tf = ft.TextField(label="出力（変換後）", multiline=True, read_only=True,
                             text_style=mono, min_lines=16, expand=True)
    diff_tf = ft.TextField(label="差分", multiline=True, read_only=True,
                           text_style=mono, min_lines=8, expand=True)

    def refresh_output():
        """input_text を include_pos に応じて再変換し、UIへ反映する。"""
        nonlocal output_text, diff_text
        if not input_text:
            output_text, diff_text = "", ""
            output_tf.value = ""
            diff_tf.value = ""
            changed_txt.value = "変更行: 0 / 総行数: 0"
            return

        output_text, changed, diff_text = convert_text(input_text, include_pos=include_pos)
        output_tf.value = output_text
        diff_tf.value = diff_text
        changed_txt.value = f"変更行: {changed} / 総行数: {len(input_text.splitlines())}"

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
            set_status(f"読み込み失敗:フォーマットや文字コードを確認してください", ok=False)
            return
        input_text = txt
        input_tf.value = input_text
        refresh_output()
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
        base = current_file.stem if current_file else "変換"
        suffix = "" if include_pos else "(省入力)"
        default_name = f"【ATOK】{base}{suffix}.txt"
        save_picker.save_file(file_name=default_name, allowed_extensions=["txt"])

    def on_save_result(e: ft.FilePickerResultEvent):
        if e.path:
            save_output(Path(e.path))

    save_picker.on_result = on_save_result

    def on_pos_yes_click(e):
        nonlocal include_pos
        include_pos = True
        pos_yes_btn.disabled = True
        pos_no_btn.disabled = False
        refresh_output()
        page.update()

    def on_pos_no_click(e):
        nonlocal include_pos
        include_pos = False
        pos_yes_btn.disabled = False
        pos_no_btn.disabled = True
        refresh_output()
        page.update()

    pos_yes_btn = ft.ElevatedButton("辞書", disabled=True, on_click=on_pos_yes_click)
    pos_no_btn = ft.OutlinedButton("省入力", disabled=False, on_click=on_pos_no_click)
    pos_toggle = ft.Row(controls=[pos_yes_btn, pos_no_btn], spacing=10)

    top_bar = ft.Row(
        controls=[
            ft.ElevatedButton("ファイルを選択", icon=ft.Icons.FOLDER_OPEN, on_click=on_open_click),
            pos_toggle,
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

    # AppBar は page.appbar へ
    page.appbar = ft.AppBar(title=ft.Text("Microsoft IME→ATOK変換器"))

    page.add(
        ft.Column(
            controls=[
                ft.Container(content=top_bar, padding=10),
                ft.Container(content=panes, padding=10, expand=True),
                ft.Container(content=stat, padding=10),
            ],
            expand=True,
        )
    )

if __name__ == "__main__":
    if ft is None:
        raise SystemExit("flet が見つかりません。venv を有効化してから起動してください。")
    ft.app(target=main, view=ft.AppView.FLET_APP)
