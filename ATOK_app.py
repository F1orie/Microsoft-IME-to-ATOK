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
# 読み（A列）から削除する記号（ユーザー要件）:
# 「」「、」「。」「，」「．」「・」およびスペース（全角/半角）
_READING_STRIP_CHARS = re.compile(r"[「」、。，．・,.\u3000 ]+")

# 読み（A列）正規化: 濁点・半濁点（結合文字/分離記号）
_DAKUTEN_MARKS = frozenset(("\u3099", "\u309a", "\u309b", "\u309c"))
# カタカナ「ヴ」→ ひらがな「う」+ 分離濁点「゛」（ゔ に合成されない表記）
_U_VOICED = "\u3046\u309b"
_VU_KATAKANA = "\u30f4"
_VU_HIRAGANA_COMPOSED = "\u3094"
_READING_TRUNCATION_MARK = "＠"
_OUTPUT_ENCODING = "cp932"
_OUTPUT_FIELD_LABELS = ("A列(読み)", "B列(語句)", "C列(品詞)", "D列(コメント)")

# 品詞ラベル（3列目）の完全一致置換辞書
_POS_LABEL_MAP_RAW: dict[str, str] = {
    "名詞": "名詞",
    "副詞的名詞": "名詞",
    "姓": "固有人姓",
    "名": "固有人名",
    "人名": "固有人他",
    "地名その他": "固有地名",
    "固有名詞": "固有一般",
    "さ変名詞": "名詞サ変",
    "ざ変名詞": "名詞ザ変",
    "形動名詞": "名詞形動",
    "さ変形動名詞": "名サ形動",
    "さ変軽動名詞": "名サ形動",
    "その他自立語": "独立語",
    "慣用句": "独立語",
    "固有接頭語": "接頭語",
    "姓名接頭語": "接頭語",
    "地名接頭語": "接頭語",
    "固有接尾語": "接尾語",
    "姓名接尾語": "接尾語",
    "地名接尾語": "接尾語",
    "か行五段": "カ行五段",
    "が行五段": "ガ行五段",
    "さ行五段": "サ行五段",
    "た行五段": "タ行五段",
    "な行五段": "ナ行五段",
    "は行五段": "ハ行五段",
    "ば行五段": "バ行五段",
    "ま行五段": "マ行五段",
    "ら行五段": "ラ行五段",
    "あわ行五段": "ワ行五段",
    "短縮よみ": "短縮読み",
    "姓接尾語": "接尾語",
    "名接尾語": "接尾語",
    "姓接尾語": "接尾語",
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
    """機能3: ヴ/ゔ → う゛（分離濁点）。"""
    return s.replace(_VU_KATAKANA, _U_VOICED).replace(
        _VU_HIRAGANA_COMPOSED, _U_VOICED
    )


def _make_reading_cp932_safe(s: str) -> str:
    """読み列の保存時に cp932 で扱えない合成済み「ゔ」を分離表記に戻す。"""
    return s.replace(_VU_HIRAGANA_COMPOSED, _U_VOICED)


def _canonicalize_vu_for_diff(s: str) -> str:
    """差分除外用: ヴ/ゔ を う゛ に寄せて同一視する。"""
    return s.replace(_VU_KATAKANA, _U_VOICED).replace(_VU_HIRAGANA_COMPOSED, _U_VOICED)


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
    """読み（A列）から指定の記号・スペースのみ削除して左詰め。"""
    s = unicodedata.normalize("NFC", s)
    return _READING_STRIP_CHARS.sub("", s)


def _truncate_reading_at_count32(s: str) -> str:
    """機能1: 独自カウントで 32 以上なら 32 カウント目を ＠ にし、以降を削除。"""
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
            # 32 カウント目がこのユニットにかかる → ユニット全体を ＠（仕様メモ）
            out.append(_READING_TRUNCATION_MARK)
            break
    return "".join(out)


def _normalize_reading_column_a(first: str) -> str:
    """読み列の 3→2→1（その後に英字→促音を適用）。"""
    s = unicodedata.normalize("NFKC", first)
    s = _replace_vu_in_reading(s)
    s = _filter_reading_column(s)
    s = _truncate_reading_at_count32(s)
    s = _make_reading_cp932_safe(s)
    return s


def _spreadsheet_column_name(index: int) -> str:
    """0-based の列番号を A, B, ..., AA のような表記にする。"""
    index += 1
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _output_field_label(index: int) -> str:
    if index < len(_OUTPUT_FIELD_LABELS):
        return _OUTPUT_FIELD_LABELS[index]
    return f"{_spreadsheet_column_name(index)}列"


def find_output_encoding_errors(
    text: str, sample_limit: int = 20
) -> tuple[int, set[int], list[str]]:
    """出力を cp932 保存できない文字の件数、対象行、表示用の対象一覧を返す。"""
    total = 0
    error_line_numbers: set[int] = set()
    samples: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        fields = line.split("\t")
        for field_index, field in enumerate(fields):
            field_label = _output_field_label(field_index)
            for ch in field:
                try:
                    ch.encode(_OUTPUT_ENCODING)
                except UnicodeEncodeError:
                    total += 1
                    error_line_numbers.add(line_no)
                    if len(samples) < sample_limit:
                        samples.append(
                            f"{line_no}行目 {field_label}: 「{ch}」(U+{ord(ch):04X})"
                        )
    return total, error_line_numbers, samples


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


def _build_fullwidth_ascii_fold_table() -> dict[int, str]:
    """差分表示用: U+FF01〜FF5E（全角英数・記号）と U+3000（和字間スペース）のみ半角へ寄せる。

    文字列全体に NFKC をかけない（半角カナ→全角カナなどは区別を潰さない）。
    """
    t: dict[int, str] = {0x3000: " "}
    for o in range(0xFF01, 0xFF5F):
        t[o] = chr(o - 0xFEE0)
    return t


_FULLWIDTH_ASCII_FOLD_TABLE = _build_fullwidth_ascii_fold_table()


def _fold_fullwidth_ascii_for_diff(s: str) -> str:
    return "".join(_FULLWIDTH_ASCII_FOLD_TABLE.get(ord(ch), ch) for ch in s)

def _pos_label_normalized_value(pos: str) -> str | None:
    """品詞ラベル正規化（完全一致置換）の結果を返す。変化がなければ None。"""
    pos_norm = unicodedata.normalize("NFKC", pos)
    mapped = _POS_LABEL_MAP.get(pos_norm)
    if mapped is None or mapped == pos:
        return None
    return mapped


def _canonicalize_reading_decimal_digits_for_diff(s: str) -> str:
    """差分除外用: 1 文字が NFKC でちょうど 1 桁の ASCII 数字になる場合のみ半角数字へ寄せる。

    全角数字（U+FF10 など）、丸数字（U+2460 系）など、読み列で現れうる「数字の形状差」を同一視する。
    英字の全角→半角（NFKC が 1 文字の英字）はここでは触らない（従来の ASCII フォールド側で扱う）。
    """
    out: list[str] = []
    for ch in s:
        nk = unicodedata.normalize("NFKC", ch)
        if len(nk) == 1 and "0" <= nk <= "9":
            out.append(nk)
        else:
            out.append(ch)
    return "".join(out)


def _with_first_field_digit_canonicalized(line: str) -> str:
    """TSV なら 1 列目だけ _canonicalize_reading_decimal_digits_for_diff を適用。"""
    if "\t" not in line:
        return _canonicalize_reading_decimal_digits_for_diff(line)
    a0, rest = line.split("\t", 1)
    return _canonicalize_reading_decimal_digits_for_diff(a0) + "\t" + rest

def _canonicalize_reading_column_a_for_diff(a0: str) -> str:
    """差分除外用: A列を「指定記号削除 → 数字形状差の正規化 → 全角ASCIIの折りたたみ」で正規化。"""
    a0 = _filter_reading_column(a0)
    a0 = _canonicalize_vu_for_diff(a0)
    a0 = _canonicalize_reading_decimal_digits_for_diff(a0)
    return _fold_fullwidth_ascii_for_diff(a0)

def _reading_a_fold_equivalent_for_diff(a0: str, b0: str) -> bool:
    """読み（A列）の差が全角ASCII/スペース/数字形状差の範囲で同一なら True。"""
    if a0 == b0:
        return False
    return _canonicalize_reading_column_a_for_diff(a0) == _canonicalize_reading_column_a_for_diff(b0)

def _only_reading_a_width_or_digit_change_and_pos_label_normalization_only(
    before: str, after: str
) -> bool:
    """差分除外用（辞書モード想定）:

    - A列の差が「全角/半角（ASCII範囲）・和字間スペース・数字形状差」のみ
    - それ以外の差が「品詞ラベルの完全一致置換（正規化）」のみ

    例: 読みが「０ｃｍ… → 0cm…」かつ 品詞が「固有名詞 → 固有一般」のようなケースを差分から除外する。
    """
    if "\t" not in before or "\t" not in after:
        return False
    a = before.split("\t")
    b = after.split("\t")
    if len(a) != len(b) or len(a) < 3:
        return False

    # 読み（A列）は折りたたみ同一、ただし完全一致は除外条件にしない
    if not _reading_a_fold_equivalent_for_diff(a[0], b[0]):
        return False

    # 語句列は同一であること（列構造が崩れるケースは対象外）
    if len(a) >= 2 and a[1] != b[1]:
        return False

    # コメント列（4列TSV）の一致も要求
    if len(a) == 4 and a[3] != b[3]:
        return False

    # 品詞列だけが「正規化置換」による差であること
    normalized = _pos_label_normalized_value(a[2])
    if normalized is None:
        return False
    return normalized == b[2]


def _only_reading_column_a_width_fold_change(before: str, after: str) -> bool:
    """A列（1列目）だけが変わり、かつ折りたたみ比較で同一なら真。

    タブ以降（語句・品詞・コメント等）が変換前後で完全一致しているときに限り、
    A列の差が全角英数・記号・和字間スペース⇄半角のみである場合に差分から除外する。
    """
    if "\t" not in before or "\t" not in after:
        return False
    a0, arest = before.split("\t", 1)
    b0, brest = after.split("\t", 1)
    if arest != brest:
        return False
    if a0 == b0:
        return False
    # 指定記号削除 + 数字形状差 + 全角ASCII折りたたみ まで同一なら差分にしない
    return _canonicalize_reading_column_a_for_diff(a0) == _canonicalize_reading_column_a_for_diff(b0)


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

        # 差分比較の基準行:
        # 省入力（品詞空欄化）では、入力側も同じく品詞空欄化したものを基準にする。
        # これにより「品詞を空にしただけ」の差分を表示しない（他の差分条件は従来通り）。
        base_ln = ln
        if not include_pos:
            base_ln = _blank_pos_column_in_tsv(ln) if "\t" in ln else ln

        if new_ln != base_ln and not _only_pos_column_changed(base_ln, new_ln):
            if _only_reading_column_a_width_fold_change(base_ln, new_ln):
                continue
            if _only_reading_a_width_or_digit_change_and_pos_label_normalization_only(base_ln, new_ln):
                continue
            if _fold_fullwidth_ascii_for_diff(base_ln) == _fold_fullwidth_ascii_for_diff(new_ln):
                continue
            # 数字: 全角・丸数字等 → 半角 1 桁（NFKC）だけの差は差分に出さない（A 列のみ変化のケースも含む）
            if _fold_fullwidth_ascii_for_diff(
                _with_first_field_digit_canonicalized(base_ln)
            ) == _fold_fullwidth_ascii_for_diff(_with_first_field_digit_canonicalized(new_ln)):
                continue
            changed += 1
            diffs.append(f"[{idx}行目] {base_ln}  ⇒  {new_ln}")
    return "\n".join(out_lines), changed, "\n".join(diffs)

# UI
def main(page: ft.Page):
    page.title = "Meriem ver.2.0.2"
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
        save_text = output_text
        error_count, error_line_numbers, error_samples = find_output_encoding_errors(output_text)
        if error_count:
            hidden_count = error_count - len(error_samples)
            removed_line_count = len(error_line_numbers)
            details = [
                "保存できない文字があります（ANSI/cp932非対応）。",
                f"対象の {removed_line_count} 行を除外して保存します。",
                "",
                *error_samples,
            ]
            if hidden_count > 0:
                details.append(f"...ほか {hidden_count} 件")
            diff_tf.value = "\n".join(details)
            save_text = "\n".join(
                line
                for line_no, line in enumerate(output_text.splitlines(), start=1)
                if line_no not in error_line_numbers
            )
            if not save_text:
                set_status(
                    "保存失敗: 保存可能な行がありません。詳細は差分欄を確認してください。",
                    ok=False,
                )
                return
            try:
                save_text.encode(_OUTPUT_ENCODING)
            except UnicodeEncodeError as e:
                set_status(f"保存失敗: {e}", ok=False)
                return
            page.update()
        try:
            Path(to_path).write_text(save_text, encoding=_OUTPUT_ENCODING, newline="\r\n")
            if error_count:
                set_status(
                    f"保存しました: {to_path}（保存不可文字を含む {len(error_line_numbers)} 行を除外）",
                    ok=True,
                )
            else:
                set_status(f"保存しました: {to_path}", ok=True)
        except Exception as e:
            set_status(
                f"保存失敗: {e}",
                ok=False,
            )

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
