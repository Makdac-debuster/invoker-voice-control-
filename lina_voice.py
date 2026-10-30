"""Голосовой прокаст Лины в Dota 2.

Пока включён Caps Lock, скажите «бам» — Лина прокастует туда, куда указывает мышь:
Light Strike Array (W), Dragon Slave (Q), Laguna Blade (R).

Следующую клавишу раньше конца каста предыдущего жать нельзя — нажатие потеряется
или собьёт каст. А когда каст закончится, заранее не известно: если цель сбоку или
за спиной, Лина сначала разворачивается, если дальше дальности W — идёт к ней.
Поэтому скрипт слушает Game State Integration (официальная передача состояния
своего героя из Dota) и жмёт следующую клавишу, как только у предыдущего скилла
начался кулдаун. Если у Q или R кулдаун так и не начался — жмёт клавишу ещё раз.
Без данных из игры жмёт по таймеру: cast point из файлов игры + CAST_MARGIN.
Laguna Blade бьёт в юнита, так что под курсором должен быть враг.

Ввод, микрофон и модель распознавания общие с invoker_voice.py.
"""

import json
from audio_buffer import AudioBuffer
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

from invoker_voice import (CHUNK, MODEL_DIR, dota_is_active, ensure_model, find_dota_cfg, find_mic,
                           listening, tap, words_from_result)

# Прокаст по порядку: заклинание, скан-код физической клавиши (не зависит от раскладки),
# имя способности в Dota и её cast point — сколько секунд длится каст. Значения из файлов
# игры (scripts/npc/heroes/npc_dota_hero_lina.txt, октябрь 2026); после патча могут поменяться.
COMBO = [
    ("Light Strike Array", 0x11, "lina_light_strike_array", 0.45),  # W
    ("Dragon Slave",       0x10, "lina_dragon_slave",       0.35),  # Q
    ("Laguna Blade",       0x13, "lina_laguna_blade",       0.30),  # R
]
# True — в Dota включён quick cast (заклинание летит в курсор сразу по клавише).
# False — после каждой клавиши скрипт кликает левой кнопкой мыши, как при обычном касте.
QUICK_CAST = True
# Слова, по которым срабатывает прокаст. Только слова из словаря модели.
TRIGGERS = ["бам"]
# Срабатывать, если промежуточный результат столько секунд держится на «бам», не дожидаясь
# паузы после слова. Если прокаст срабатывает от шума или посторонней речи — увеличьте.
FAST_TRIGGER = 0.1

# --- Game State Integration: Dota сама присылает скрипту состояние способностей героя.
# Работает, если в параметрах запуска Dota в Steam есть -gamestateintegration. ---
GSI = True  # False — всегда жать по таймеру
GSI_PORT = 3017
GSI_CFG = "gamestate_integration_voice.cfg"
# Если у Q или R нет кулдауна через cast point + RETRY_AFTER после нажатия — нажать ещё раз.
RETRY_AFTER = 0.25
# Сколько ждать каста W: если цель дальше дальности W, Лина сначала к ней идёт
# (за 2 с не успевала дойти). Если за это время стана нет — Q и R не жмутся.
FIRST_CAST_TIMEOUT = 6.0
# Сколько всего ждать каста Q и R.
CAST_TIMEOUT = 2.0
# Без GSI следующая клавиша жмётся через cast point + CAST_MARGIN. Запас нужен, потому что
# сервер Dota считает время тиками по 1/30 с. Если следующий скилл теряется — увеличьте.
CAST_MARGIN = 0.03
# Сюда пишется, что присылала Dota во время каждого прокаста, — чтобы разобраться, если
# скилл нажался не вовремя.
LOG_FILE = Path(__file__).with_name("lina_log.txt")


class GameState:
    """Последнее состояние способностей героя из GSI."""

    def __init__(self):
        self.changed = threading.Condition()
        self.abilities: dict[str, dict] = {}
        self.received = 0.0
        self.trace: list | None = None  # (время, состояние способностей прокаста) во время прокаста

    def update(self, data: dict) -> None:
        abilities = {a["name"]: a for a in data.get("abilities", {}).values()
                     if isinstance(a, dict) and "name" in a}
        with self.changed:
            self.abilities, self.received = abilities, time.perf_counter()
            if self.trace is not None:
                self.trace.append((self.received, {a: abilities.get(a) for _, _, a, _ in COMBO}))
            self.changed.notify_all()

    def ability(self, name: str) -> dict | None:
        """Состояние способности, если данные свежие (heartbeat в конфиге — раз в секунду)."""
        with self.changed:
            if time.perf_counter() - self.received > 2.0:
                return None
            return self.abilities.get(name)

    def wait_cooldown(self, name: str, deadline: float) -> bool:
        """Ждёт, пока у способности начнётся кулдаун, то есть каст состоялся."""
        with self.changed:
            while not self.abilities.get(name, {}).get("cooldown", 0):
                left = deadline - time.perf_counter()
                if left <= 0:
                    return False
                self.changed.wait(left)
            return True


STATE = GameState()


class _GsiHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            STATE.update(json.loads(body))
        except ValueError:
            pass
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


GSI_CFG_TEXT = f"""\
"Lina Voice"
{{
    "uri"       "http://127.0.0.1:{GSI_PORT}/"
    "timeout"   "1.0"
    "buffer"    "0.0"
    "throttle"  "0.0"
    "heartbeat" "1.0"
    "data"
    {{
        "provider"  "1"
        "abilities" "1"
    }}
}}
"""


def setup_gsi() -> None:
    """Кладёт конфиг GSI в папку Dota и запускает приёмник данных."""
    cfg_dir = find_dota_cfg()
    if cfg_dir is None:
        print("Папка cfg Dota не найдена — прокаст по таймеру. Впишите путь в DOTA_CFG_DIR "
              "в invoker_voice.py.")
        return
    cfg = cfg_dir / "gamestate_integration" / GSI_CFG
    if not cfg.exists() or cfg.read_text(encoding="utf-8") != GSI_CFG_TEXT:
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text(GSI_CFG_TEXT, encoding="utf-8")
        print(f"Записал {cfg}.\nDota читает его при запуске: перезапустите Dota, если она открыта.")
    try:
        server = HTTPServer(("127.0.0.1", GSI_PORT), _GsiHandler)
    except OSError as e:
        print(f"Порт {GSI_PORT} занят ({e}) — прокаст по таймеру. Поменяйте GSI_PORT.")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()


def press(key: int) -> None:
    tap(key)
    if not QUICK_CAST:
        tap("MOUSE1")


def combo_timed() -> str:
    next_press = time.perf_counter()
    for _, key, _, cast_point in COMBO:
        time.sleep(max(0.0, next_press - time.perf_counter()))
        # Каст начинается в момент нажатия: клавиши при quick cast, иначе клика мышью
        # (tap держит клавишу и ждёт после неё по KEY_DELAY = 0.025 с).
        cast_start = time.perf_counter() + (0.0 if QUICK_CAST else 0.05)
        press(key)
        next_press = cast_start + cast_point + CAST_MARGIN
    return " → ".join(name for name, *_ in COMBO) + " (по таймеру, данных из игры нет)"


def write_log(start: float, report: str, presses: list, trace: list) -> None:
    lines, last = [f"=== {time.strftime('%H:%M:%S')}  {report}"], None
    for t, state in trace:
        if state != last:  # heartbeat повторяет то же состояние — его не пишем
            lines.append(f"{t - start:6.3f}  " + json.dumps(state, ensure_ascii=False))
            last = state
    lines += [f"{t - start:6.3f}  нажата {name}" for t, name in presses]
    head, body = lines[0], sorted(lines[1:], key=lambda l: float(l.split()[0]))
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join([head] + body) + "\n\n")


def combo_gsi() -> str:
    with STATE.changed:
        STATE.trace = [(time.perf_counter(), {a: STATE.abilities.get(a) for _, _, a, _ in COMBO})]
    start = time.perf_counter()
    report, presses = [], []
    try:
        _combo_gsi(start, report, presses)
    finally:
        with STATE.changed:
            trace, STATE.trace = STATE.trace, None
        write_log(start, " → ".join(report), presses, trace)
    return " → ".join(report)


def _combo_gsi(start: float, report: list, presses: list) -> None:
    for i, (name, key, ability, cast_point) in enumerate(COMBO):
        if not (STATE.ability(ability) or {}).get("can_cast"):
            report.append(f"{name} недоступен (кулдаун или мана)")
            continue
        pressed = time.perf_counter()
        press(key)
        presses.append((pressed, name))
        if i == 0:
            # Первый скилл может долго не кастоваться: Лина разворачивается или идёт к цели.
            # Второе нажатие тут сбило бы каст, поэтому просто ждём.
            timeout = FIRST_CAST_TIMEOUT
            cast = STATE.wait_cooldown(ability, pressed + timeout)
            retried = False
        else:
            timeout = CAST_TIMEOUT
            # Дальше Лина уже смотрит на цель, и задержки быть не может: нет кулдауна —
            # значит, нажатие потерялось.
            cast = STATE.wait_cooldown(ability, pressed + cast_point + RETRY_AFTER)
            retried = not cast
            if retried:
                presses.append((time.perf_counter(), name + " (повтор)"))
                press(key)
                cast = STATE.wait_cooldown(ability, pressed + CAST_TIMEOUT)
        if not cast:
            report.append(f"{name} не скастовался за {timeout:.0f} с — прокаст прерван")
            break
        report.append(f"{name} {pressed - start:.2f}–{time.perf_counter() - start:.2f}"
                      + (" (нажат дважды)" if retried else ""))


_combo_running = threading.Lock()


def _run_combo() -> None:
    try:
        ready = GSI and all(STATE.ability(ability) for _, _, ability, _ in COMBO)
        print("→ " + (combo_gsi() if ready else combo_timed()))
    finally:
        _combo_running.release()


def fire() -> None:
    if not dota_is_active():
        print("[не в Dota] бам")
        return
    # Прокаст идёт в своём потоке: пока Лина идёт к цели, микрофон продолжает слушать,
    # и «бам», сказанный в это время, не сработает с опозданием, когда прокаст закончится.
    if not _combo_running.acquire(blocking=False):
        print("[прокаст ещё идёт] бам")
        return
    threading.Thread(target=_run_combo, daemon=True).start()


class Listener:
    """Как в invoker_voice.py: срабатывает по устойчивому промежуточному результату
    или по итоговому, но не больше одного прокаста на фразу."""

    def __init__(self, recognizer: KaldiRecognizer):
        self.recognizer = recognizer
        self.partial, self.stable, self.fired = "", 0.0, False

    def feed(self, chunk: bytes) -> None:
        if self.recognizer.AcceptWaveform(chunk):
            self._final(self.recognizer.Result())
            return
        current = json.loads(self.recognizer.PartialResult()).get("partial", "")
        if current != self.partial:
            self.partial, self.stable = current, 0.0
            return
        self.stable += CHUNK
        if not self.fired and self.stable >= FAST_TRIGGER and current in TRIGGERS:
            fire()
            self.fired = True

    def reset(self) -> None:
        self.recognizer.Reset()
        self.partial, self.stable, self.fired = "", 0.0, False

    def finish(self) -> None:
        self._final(self.recognizer.FinalResult())

    def _final(self, result_json: str) -> None:
        fired, self.fired, self.partial = self.fired, False, ""
        self.stable = 0.0
        words = words_from_result(result_json)
        if fired:
            return
        if words and all(w in TRIGGERS for w in words):
            fire()
        elif words:
            print(f"(не принято: {' '.join(words)})")


def main() -> None:
    ensure_model()
    SetLogLevel(-1)
    if GSI:
        setup_gsi()
    mic = find_mic()
    rate = int(mic["default_samplerate"])
    grammar = json.dumps(TRIGGERS + ["[unk]"], ensure_ascii=False)
    recognizer = KaldiRecognizer(Model(str(MODEL_DIR)), rate, grammar)
    recognizer.SetWords(True)
    audio = AudioBuffer(listening)

    print(f"Микрофон: {mic['name']} ({rate} Гц)")
    print(f"Слушаю, пока включён Caps Lock. «{TRIGGERS[0]}» — прокаст W → Q → R в курсор. "
          "Ctrl+C — выход.")
    print(f"Quick cast: {'включён' if QUICK_CAST else 'выключен (скрипт кликает мышью)'}")
    if GSI:
        print("Данные из игры: нужен параметр запуска Dota -gamestateintegration. Если после «бам»\n"
              "написано «по таймеру» — данных нет: проверьте параметр и перезапустите Dota.")

    with sd.RawInputStream(device=mic["index"], samplerate=rate, blocksize=int(rate * CHUNK),
                           dtype="int16", channels=1, callback=audio.callback):
        listener = Listener(recognizer)
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
