"""Голосовое управление Инвокером в Dota 2.

Говорите название заклинания — программа нажимает нужные сферы и Invoke (R).
Заклинание оказывается в слоте D, применяете его сами. Ghost Walk применяется
сразу (после Invoke нажимается D).

Говорите номер башни (1–18) или место («трон тьмы») — камера переедет туда.

Программа слушает, только пока включён Caps Lock. Выключили — не слышит.
Клавиши нажимаются, только когда активно окно Dota 2.

При запуске программа спрашивает, на каких клавишах у вас сферы, Invoke и слот D,
и запоминает ответ в keys.json. Модель распознавания скачивается при первом запуске.
"""

import ctypes
import json
import msvcrt
from audio_buffer import AudioBuffer
import re
import sys
import time
import urllib.request
import winreg
import zipfile
from ctypes import wintypes
from pathlib import Path

import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

MODEL_DIR = Path(__file__).with_name("model")
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
KEYS_FILE = Path(__file__).with_name("keys.json")  # клавиши, выбранные при запуске
SAMPLE_RATE = 16000
KEY_DELAY = 0.025  # пауза между нажатиями, сек
MIC_NAME = None  # часть названия микрофона, например "AT2020USB+"; None — микрофон Windows по умолчанию

# Клавиши сфер, Invoke и слота D. Значения по умолчанию — стандартные бинды Dota,
# при запуске программа спрашивает ваши и запоминает их в keys.json.
# Число — скан-код физической клавиши (не зависит от раскладки, в русской нет буквы q),
# строка — кнопка мыши: "MOUSE3", "MOUSE4" или "MOUSE5".
KEYS = {"Q": 0x10, "W": 0x11, "E": 0x12, "INVOKE": 0x13, "CAST": 0x20}
KEY_ROLES = {
    "Q": "Quas (сфера льда)",
    "W": "Wex (сфера молнии)",
    "E": "Exort (сфера огня)",
    "INVOKE": "Invoke",
    "CAST": "Слот D (первое вызванное заклинание, им кастуется Ghost Walk)",
}

# Эти заклинания применяются сразу после Invoke нажатием CAST (у вас D, слот нового заклинания).
CAST_AFTER_INVOKE = {"Ghost Walk"}
CAST_DELAY = 0.15  # пауза между Invoke и D, чтобы заклинание успело встать в слот

# Заклинание -> (сферы, голосовые варианты). Варианты можно дописывать,
# но только словами из словаря модели (обычные русские слова).
SPELLS = {
    "Cold Snap":       ("QQQ", ["холод", "мороз", "колд", "заморозка", "фриз"]),
    "Ghost Walk":      ("QQW", ["призрак", "невидимость", "гост", "невидимка"]),
    "Ice Wall":        ("QQE", ["стена", "лёд", "айс", "стенка", "барьер"]),
    "EMP":             ("WWW", ["импульс", "мана", "эмп"]),
    "Tornado":         ("WWQ", ["торнадо", "смерч", "вихрь", "ураган", "циклон", "буря", "ветер",
                                "ветра"]),
    "Alacrity":        ("WWE", ["ускорение", "скорость", "бафф"]),
    "Sun Strike":      ("EEE", ["солнце", "луч", "сан страйк"]),
    "Forge Spirit":    ("EEQ", ["духи", "спирит", "демоны"]),
    "Chaos Meteor":    ("EEW", ["метеор", "метеорит", "комета", "метеоры",
                                "хаос метеор", "кинь метеор"]),
    "Deafening Blast": ("QWE", ["волна", "бласт"]),
}

ALIASES = {alias: name for name, (_, aliases) in SPELLS.items() for alias in aliases}

# --- Места на карте для камеры ---
# Координаты взяты из файлов карты Dota (maps/dota.vpk). Если после патча место
# съехало, наведите камеру, введите в консоли dota_camera_get_lookatpos и впишите
# новые числа сюда.

# (сторона, линия) -> позиции T1, T2, T3
TOWERS = {
    ("good", "top"): [(-6336, 1856), (-6501, -872), (-6592, -3408)],
    ("good", "mid"): [(-1544, -1408), (-3190, -2926), (-4640, -4144)],
    ("good", "bot"): [(4860, -6379), (-360, -6256), (-3952, -6112)],
    ("bad", "top"):  [(-5275, 6036), (-128, 6016), (3552, 5776)],
    ("bad", "mid"):  [(524, 652), (2496, 2112), (4272, 3759)],
    ("bad", "bot"):  [(6269, -2240), (6400, 384), (6336, 3032)],
}
BASE = {
    "good": {"fort": (-5920, -5352), "fountain": (-7456, -6938)},
    "bad":  {"fort": (5528, 5000), "fountain": (7408, 6848)},
}
ROSHAN = {"top": (-3194, 2395), "bot": (2860, -2765)}  # два логова, Рошан переходит днём/ночью

SIDE_NAMES = {"good": "света", "bad": "тьмы"}
LANE_NAMES = {"top": "топ", "mid": "мид", "bot": "бот"}

# Башни по номерам: 1–9 — свет, 10–18 — тьма. Внутри стороны топ, мид, бот,
# на каждой линии T1, T2, T3: 1 — T1 света топ, 4 — T1 света мид, 10 — T1 тьмы топ.
NUMBER_WORDS = ["один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять",
                "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
                "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать"]

# Фраза целиком -> (подпись, координаты)
PLACES = {}
for _n, _word in enumerate(NUMBER_WORDS):
    _side, _lane, _tier = ("good", "bad")[_n // 9], ("top", "mid", "bot")[_n % 9 // 3], _n % 3 + 1
    PLACES[_word] = (f"{_n + 1} — T{_tier} {SIDE_NAMES[_side]} {LANE_NAMES[_lane]}",
                     TOWERS[_side, _lane][_tier - 1])
for _side, _name in SIDE_NAMES.items():
    PLACES[f"трон {_name}"] = (f"Трон {_name}", BASE[_side]["fort"])
    PLACES[f"фонтан {_name}"] = (f"Фонтан {_name}", BASE[_side]["fountain"])
PLACES["логово верх"] = ("Логово Рошана (топ)", ROSHAN["top"])
PLACES["логово низ"] = ("Логово Рошана (бот)", ROSHAN["bot"])

# --- Ввод клавиш через WinAPI SendInput (скан-коды, их понимает Dota) ---

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
VK_CAPITAL = 0x14
INPUT_MOUSE = 0
# кнопка -> (флаг нажатия, флаг отпускания, mouseData)
MOUSE_BUTTONS = {
    "MOUSE1": (0x0002, 0x0004, 0),       # LEFTDOWN / LEFTUP (клик для каста без quick cast)
    "MOUSE3": (0x0020, 0x0040, 0),       # MIDDLEDOWN / MIDDLEUP
    "MOUSE4": (0x0080, 0x0100, 0x0001),  # XDOWN / XUP, XBUTTON1
    "MOUSE5": (0x0080, 0x0100, 0x0002),  # XBUTTON2
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_scan(scan: int, up: bool) -> None:
    # Скан-код вида 0xE0xx — расширенная клавиша (стрелки, правые Ctrl/Alt и т.п.)
    flags = (KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
             | (KEYEVENTF_EXTENDEDKEY if scan > 0xFF else 0))
    inp = INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, scan & 0xFF, flags, 0, 0)))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _send_mouse(button: str, up: bool) -> None:
    down_flag, up_flag, data = MOUSE_BUTTONS[button]
    flags = up_flag if up else down_flag
    inp = INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, data, flags, 0, 0)))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def tap(key: int | str) -> None:
    send = _send_mouse if isinstance(key, str) else _send_scan
    send(key, up=False)
    time.sleep(KEY_DELAY)
    send(key, up=True)
    time.sleep(KEY_DELAY)


def dota_is_active() -> bool:
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value.startswith("Dota 2")


def listening() -> bool:
    return bool(user32.GetKeyState(VK_CAPITAL) & 1)


def invoke(name: str) -> None:
    orbs, _ = SPELLS[name]
    for orb in orbs:
        tap(KEYS[orb])
    tap(KEYS["INVOKE"])
    if name in CAST_AFTER_INVOKE:
        time.sleep(CAST_DELAY)
        tap(KEYS["CAST"])


# --- Настройка клавиш при запуске: человек жмёт свои клавиши, скрипт запоминает скан-коды ---

VK_RETURN = 0x0D
VK_MOUSE = {0x04: "MOUSE3", 0x05: "MOUSE4", 0x06: "MOUSE5"}
# Левая/правая кнопки мыши (клики по окну) и общие Shift/Ctrl/Alt — у них есть
# отдельные левые и правые коды, ловим их. Ctrl не берём совсем: Ctrl+C — выход.
SKIP_VK = {0x01, 0x02, 0x10, 0x11, 0x12, 0xA2, 0xA3}
MAPVK_VK_TO_VSC_EX = 4
MAPVK_VSC_TO_VK_EX = 3


def _pressed(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def read_key() -> int | str | None:
    """Ждёт нажатия и возвращает клавишу (скан-код или кнопку мыши). Enter — None."""
    while any(_pressed(vk) for vk in range(1, 256)):  # пока не отпущено прошлое нажатие
        time.sleep(0.01)
    while True:
        for vk in range(1, 256):
            if vk in SKIP_VK or not _pressed(vk):
                continue
            if vk == VK_RETURN:
                return None
            if vk in VK_MOUSE:
                return VK_MOUSE[vk]
            scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX)
            if scan:
                return scan
        time.sleep(0.01)


def key_name(key: int | str) -> str:
    if isinstance(key, str):
        return key
    vk = user32.MapVirtualKeyW(key, MAPVK_VSC_TO_VK_EX)
    if 0x30 <= vk <= 0x5A:  # цифры и латинские буквы — как на клавише, без раскладки
        return chr(vk)
    buf = ctypes.create_unicode_buffer(64)
    lparam = (key & 0xFF) << 16 | (1 << 24 if key > 0xFF else 0)
    return buf.value if user32.GetKeyNameTextW(lparam, buf, 64) else hex(key)


def _flush_console_input() -> None:
    # Нажатия во время настройки иначе останутся в консоли и съедят pause в start.bat
    while msvcrt.kbhit():
        msvcrt.getwch()


def setup_keys() -> None:
    """Спрашивает клавиши сфер, Invoke и слота D; прошлый выбор берёт из keys.json."""
    try:
        saved = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
        KEYS.update({role: key for role, key in saved.items() if role in KEYS})
    except (OSError, ValueError):
        saved = None
    if saved:
        print("Клавиши: " + ", ".join(f"{label.split(' (')[0]} — {key_name(KEYS[role])}"
                                      for role, label in KEY_ROLES.items()))
        print("Enter — оставить, любая другая клавиша — настроить заново.")
        if read_key() is None:
            _flush_console_input()
            return
    print("Нажимайте клавиши так, как они стоят у вас в Dota. Enter — оставить как есть.")
    for role, label in KEY_ROLES.items():
        print(f"  {label} [{key_name(KEYS[role])}]: ", end="", flush=True)
        key = read_key()
        if key is not None:
            KEYS[role] = key
        print(key_name(KEYS[role]))
    _flush_console_input()
    KEYS_FILE.write_text(json.dumps(KEYS, indent=2), encoding="utf-8")
    print(f"Сохранил в {KEYS_FILE.name}.\n")


# --- Камера: скрипт пишет команду в cfg-файл и жмёт клавишу, на которую в Dota
# забинден exec этого файла. Бинд скрипт сам дописывает в autoexec.cfg. ---

CAMERA_KEY = (0x37, "KP_MULTIPLY")  # скан-код и имя в Dota: «*» на цифровом блоке
CAMERA_CFG = "voice_camera"
DOTA_CFG_DIR = None  # None — найти через Steam; иначе путь к ...\dota 2 beta\game\dota\cfg


def find_dota_cfg() -> Path | None:
    if DOTA_CFG_DIR:
        return Path(DOTA_CFG_DIR)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return None
    libraries = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        paths = re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore"))
        libraries += [Path(p.replace("\\\\", "\\")) for p in paths]
    for library in libraries:
        cfg = library / "steamapps" / "common" / "dota 2 beta" / "game" / "dota" / "cfg"
        if cfg.is_dir():
            return cfg
    return None


def setup_camera() -> Path | None:
    """Добавляет бинд камеры в autoexec.cfg. Возвращает папку cfg или None."""
    cfg_dir = find_dota_cfg()
    if cfg_dir is None:
        print("Папка cfg Dota не найдена — камера голосом не работает. Впишите путь в DOTA_CFG_DIR.")
        return None
    autoexec = cfg_dir / "autoexec.cfg"
    bind = f'bind "{CAMERA_KEY[1]}" "exec {CAMERA_CFG}"'
    text = autoexec.read_text(encoding="utf-8", errors="ignore") if autoexec.exists() else ""
    if bind not in text:
        if text and not text.endswith("\n"):
            text += "\n"
        autoexec.write_text(text + bind + "\n", encoding="utf-8")
        print(f"Добавил в {autoexec} бинд камеры. Если Dota уже запущена,\n"
              "введите в её консоли: exec autoexec")
    return cfg_dir


def move_camera(cfg_dir: Path, pos: tuple[int, int]) -> None:
    (cfg_dir / f"{CAMERA_CFG}.cfg").write_text(f"dota_camera_set_lookatpos {pos[0]} {pos[1]}\n",
                                               encoding="utf-8")
    tap(CAMERA_KEY[0])


# --- Распознавание речи ---

MIN_CONFIDENCE = 0.7  # ниже — скорее всего обычная речь, подогнанная под словарь
CHUNK = 0.05  # сек звука за раз
# Быстрое срабатывание: если промежуточный результат — готовая команда и не меняется
# столько секунд, выполняем сразу, не дожидаясь паузы после фразы (Vosk ждёт ~0.5–1 с).
# Если «пятнадцать» иногда срабатывает как «пять», увеличьте. None — выключить.
FAST_TRIGGER = 0.15
CAMERA_TRIGGER = 0.25


def find_mic() -> dict:
    if MIC_NAME is None:
        return sd.query_devices(kind="input")
    for dev in sd.query_devices():
        if dev["max_input_channels"] > 0 and MIC_NAME.lower() in dev["name"].lower():
            return dev  # первое совпадение — устройство MME, оно самое совместимое
    names = sorted({d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0})
    print(f"Микрофон «{MIC_NAME}» не найден, беру микрофон Windows по умолчанию. Есть такие:\n  "
          + "\n  ".join(names))
    return sd.query_devices(kind="input")


def make_recognizer(model: Model, sample_rate: int = SAMPLE_RATE) -> KaldiRecognizer:
    grammar = json.dumps(list(ALIASES) + list(PLACES) + ["[unk]"], ensure_ascii=False)
    recognizer = KaldiRecognizer(model, sample_rate, grammar)
    recognizer.SetWords(True)
    return recognizer


def words_from_result(result_json: str) -> list[str] | None:
    """Слова итогового результата Vosk, если вся фраза распознана уверенно.

    С ограниченным словарём Vosk подгоняет под него любую речь, поэтому каждое
    слово должно пройти MIN_CONFIDENCE. Сверяем и поле text: в нём остаются [unk]
    от посторонних слов, так что «нет маны» не превратится в «мана».
    """
    result = json.loads(result_json)
    words = result.get("result", [])
    if not words or any(w["conf"] < MIN_CONFIDENCE for w in words):
        return None
    phrase = [w["word"] for w in words]
    if result.get("text", "").strip() != " ".join(phrase):
        return None
    return phrase


def execute(phrase: str, cfg_dir: Path | None) -> bool:
    """Выполняет команду. False — фраза не команда."""
    spell, place = ALIASES.get(phrase), PLACES.get(phrase)
    if not (spell or place):
        return False
    label = spell or f"камера: {place[0]}"
    if not dota_is_active():
        print(f"[не в Dota] {label}")
    elif spell:
        invoke(spell)
        cast = " + D" if spell in CAST_AFTER_INVOKE else ""
        print(f"→ {spell} ({SPELLS[spell][0]} + Invoke{cast})")
    elif cfg_dir is None:
        print(f"[камера не настроена] {place[0]}")
    else:
        move_camera(cfg_dir, place[1])
        print(f"→ {label}")
    return True


def split_commands(words: list[str]) -> list[str] | None:
    """Делит фразу на команды подряд («пять гост» -> «пять», «гост»).

    None, если хоть одно слово не входит в команду.
    """
    commands, i = [], 0
    while i < len(words):
        for n in (3, 2, 1):  # сначала длинные: «хаос метеор» раньше «метеор»
            phrase = " ".join(words[i:i + n])
            if len(words) - i >= n and (phrase in ALIASES or phrase in PLACES):
                commands.append(phrase)
                i += n
                break
        else:
            return None
    return commands


class Listener:
    """Кормит Vosk звуком и выполняет распознанные команды.

    Команда срабатывает либо по итоговому результату (после паузы в речи), либо
    раньше — по промежуточному, если он FAST_TRIGGER секунд держится на готовой
    команде. Сработавшие так слова запоминаются в done: итоговый результат той же
    фразы их повторит, и выполнять нужно только то, что сказано после них.
    """

    def __init__(self, recognizer: KaldiRecognizer, cfg_dir: Path | None):
        self.recognizer, self.cfg_dir = recognizer, cfg_dir
        self.partial, self.stable, self.done = "", 0.0, []

    def feed(self, chunk: bytes) -> None:
        if self.recognizer.AcceptWaveform(chunk):
            self._final(self.recognizer.Result())
            return
        if FAST_TRIGGER is None:
            return
        current = json.loads(self.recognizer.PartialResult()).get("partial", "")
        if current != self.partial:
            self.partial, self.stable = current, 0.0
            return
        self.stable += CHUNK
        words = current.split()
        rest = " ".join(words[len(self.done):])
        commands = split_commands(rest.split()) if rest else None
        threshold = max(FAST_TRIGGER, CAMERA_TRIGGER) if commands and any(c in PLACES for c in commands) else FAST_TRIGGER
        if (self.stable >= threshold and words[:len(self.done)] == self.done
                and commands):
            for command in commands:
                execute(command, self.cfg_dir)
            self.done = words

    def reset(self) -> None:
        self.recognizer.Reset()
        self.partial, self.stable, self.done = "", 0.0, []

    def finish(self) -> None:
        """Дораспознать начатую фразу (выключили Caps)."""
        self._final(self.recognizer.FinalResult())

    def _final(self, result_json: str) -> None:
        done, self.done, self.partial = self.done, [], ""
        self.stable = 0.0
        words = words_from_result(result_json)
        if done:
            # Начало фразы уже сработало быстро. Если Vosk в итоге услышал его иначе —
            # не выполняем ничего второй раз (у «тринадцать» хвост слышится как «десять»).
            if not words or words[:len(done)] != done:
                return
            words = words[len(done):]
            if not words:
                return
        commands = split_commands(words) if words else None
        if commands:
            for command in commands:
                execute(command, self.cfg_dir)
            return
        if done:
            return
        heard = [f"{w['word']} {w['conf']:.2f}" for w in json.loads(result_json).get("result", [])]
        if heard:
            print(f"(не принято: {', '.join(heard)})")


def ensure_model() -> None:
    """Скачивает модель распознавания при первом запуске (в репозитории её нет)."""
    if MODEL_DIR.exists():
        return
    print("Скачиваю модель распознавания речи (45 МБ, только при первом запуске)...")
    archive = MODEL_DIR.with_suffix(".zip")
    try:
        urllib.request.urlretrieve(MODEL_URL, archive)
        with zipfile.ZipFile(archive) as z:
            top = z.namelist()[0].split("/")[0]  # vosk-model-small-ru-0.22
            z.extractall(MODEL_DIR.parent)
        (MODEL_DIR.parent / top).rename(MODEL_DIR)
        # Паузы конца фразы короче, чем в модели (0.5 / 1.0 / 2.0 с), — команды срабатывают быстрее
        conf = MODEL_DIR / "conf" / "model.conf"
        lines = [l for l in conf.read_text().splitlines() if not l.startswith("--endpoint.rule")]
        lines += [f"--endpoint.{rule}.min-trailing-silence={sec}"
                  for rule, sec in (("rule2", 0.15), ("rule3", 0.25), ("rule4", 0.5))]
        conf.write_text("\n".join(lines) + "\n")
    except (OSError, zipfile.BadZipFile) as e:
        sys.exit(f"Не удалось скачать модель ({e}).\nСкачайте вручную {MODEL_URL}\n"
                 f"и распакуйте так, чтобы получилась папка {MODEL_DIR}")
    finally:
        archive.unlink(missing_ok=True)


def main() -> None:
    ensure_model()
    setup_keys()
    SetLogLevel(-1)
    cfg_dir = setup_camera()
    mic = find_mic()
    # Пишем с родной частотой микрофона, Vosk сам пересэмплирует.
    rate = int(mic["default_samplerate"])
    recognizer = make_recognizer(Model(str(MODEL_DIR)), rate)
    audio = AudioBuffer(listening)

    print(f"Микрофон: {mic['name']} ({rate} Гц)")
    print("Слушаю, пока включён Caps Lock. Ctrl+C — выход.")
    for name, (orbs, aliases) in SPELLS.items():
        print(f"  {name:<16} {orbs}  ← {', '.join(aliases)}")
    print("  Камера:")
    for i in range(0, len(NUMBER_WORDS), 3):
        print("    " + "   ".join(PLACES[w][0] for w in NUMBER_WORDS[i:i + 3]))
    print("    «трон света/тьмы», «фонтан света/тьмы», «логово верх» / «логово низ»")

    with sd.RawInputStream(device=mic["index"], samplerate=rate, blocksize=int(rate * CHUNK),
                           dtype="int16", channels=1, callback=audio.callback):
        listener = Listener(recognizer, cfg_dir)
        was_listening = False
        while True:
            chunk, reset = audio.get()
            if reset:
                listener.reset()
            if chunk is None:
                if was_listening:
                    print("[Caps выкл / сброс звука] не слушаю")
                was_listening = False
                continue
            if not was_listening:
                print("[Caps вкл] слушаю")
                was_listening = True
            listener.feed(chunk)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
