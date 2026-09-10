#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply.py — применитель слоя ребрендинга RustDesk -> DugaDesk.

ЗАЧЕМ ЭТОТ СКРИПТ ВООБЩЕ СУЩЕСТВУЕТ
-----------------------------------
Форк живёт рядом с апстримом и регулярно с ним сливается. Если правки бренда
внести прямо в дерево, каждый merge превращается в разбор конфликтов, а часть
правок тихо теряется. Поэтому весь ребрендинг вынесен в каталог brand/ и
накатывается на дерево одной командой.

ПОЧЕМУ ЯКОРЯ, А НЕ НОМЕРА СТРОК
-------------------------------
Номера строк плавают между ревизиями. Реальный пример: в одной ревизии
libs/hbb_common/src/config.rs держит APP_NAME на строке 72, RENDEZVOUS_SERVERS
на 120 и RS_PUB_KEY на 121; в другой ревизии того же подмодуля — 72/117/118.
Патч по номерам строк в такой ситуации молча попадает не туда и ломает сборку
на середине (а сборка идёт около 1 ч 40 мин). Поэтому КАЖДАЯ правка задаётся
точной текстовой подстрокой и ожидаемым числом её вхождений.

РЕЖИМЫ
------
  --check   Ничего не пишет на диск. Прогоняет весь список правок в памяти и
            проверяет, что каждый якорь найден ровно ожидаемое число раз.
            Любое несовпадение -> ненулевой код возврата и сообщение вида
            "ЯКОРЬ НЕ НАЙДЕН в файле X". Это ловит разъезд с апстримом за
            30 секунд вместо полутора часов сборки.
  --apply   Применяет всё и делает самопроверку: повторно прогоняет тот же
            список по уже изменённому дереву; всё должно опознаться как
            "уже применено".

ИДЕМПОТЕНТНОСТЬ
---------------
Повторный --apply на уже обработанном дереве не ломает файлы: каждое правило
умеет отличить "якорь ещё не тронут" от "правка уже стоит" по маркеру.
"""

import argparse
import io
import os
import re
import shutil
import sys


# ---------------------------------------------------------------------------
# Кодировка вывода
# ---------------------------------------------------------------------------
# ЗАЧЕМ ЭТО ПЕРВЫМ ДЕЛОМ, ДО ЛЮБОЙ ПЕЧАТИ.
#
# Весь вывод скрипта — русский. На Linux и macOS stdout по умолчанию UTF-8, и
# всё печатается. На windows-раннере GitHub Actions Python берёт кодировку из
# кодовой страницы консоли — cp1252, в которой кириллицы нет вообще. Первая же
# строка заголовка валит процесс:
#
#   File ".../brand/apply.py", line ..., in run_pass
#       print(u"  %s" % title)
#   UnicodeEncodeError: 'charmap' codec can't encode characters ...
#
# Так упала сборка Windows в прогоне #63: шаг «Применить слой ребрендинга»
# умер за 1 секунду, а вместе с ним — обе Windows-сборки и час ожидания.
#
# Лечим перенастройкой самих потоков: errors="replace" гарантирует, что даже
# самый экзотический терминал не уронит процесс — в худшем случае отдельные
# символы станут "?", но скрипт доработает и вернёт честный код возврата.
# Молчать нельзя: без вывода не видно, какие якоря не нашлись.
#
# reconfigure() есть с Python 3.7 и только у текстовых потоков. Под
# перенаправлением в файл, в отладчике или в старом Python его может не быть —
# тогда просто работаем как раньше, поэтому try/except, а не проверка версии.
def _force_utf8_output():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # Поток не умеет reconfigure (обёртка, не-TextIO, старый Python).
            # Пробуем пересоздать обёртку поверх бинарного буфера.
            try:
                buffer = getattr(stream, "buffer", None)
                if buffer is not None:
                    setattr(sys, stream_name,
                            io.TextIOWrapper(buffer, encoding="utf-8",
                                             errors="replace", line_buffering=True))
            except Exception:
                # Совсем экзотика: оставляем как есть. Скрипт всё равно
                # отработает, а печать в худшем случае потеряет часть символов.
                pass


_force_utf8_output()

# ---------------------------------------------------------------------------
# Разбор brand.toml
# ---------------------------------------------------------------------------
# Пробуем штатный tomllib (Python 3.11+). Если его нет — используем свой
# минимальный разбор: brand.toml намеренно держим плоским (ключ = "значение"),
# чтобы слой ребрендинга работал даже на старом Python сборочного агента.
# Без этого fallback скрипт упал бы на машине с Python 3.8 и остановил релиз.
def load_brand(path):
    try:
        import tomllib  # noqa
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        return data.get("brand", data)
    except Exception:
        pass
    values = {}
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("["):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.split("#", 1)[0].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            values[key.strip()] = val
    return values


# ---------------------------------------------------------------------------
# Виртуальное дерево: правки сначала делаются в памяти, на диск попадают
# только в режиме --apply. Так --check проходит РОВНО тот же путь, что и
# --apply, включая цепочки правок (одно правило готовит текст для другого).
# Без этого --check давал бы ложные срабатывания на правилах второго уровня.
# ---------------------------------------------------------------------------
class Tree(object):
    def __init__(self, root):
        self.root = root
        self.texts = {}      # относительный путь -> содержимое (str)
        self.renames = []    # список (откуда, куда)
        self.copies = []     # список (абсолютный источник, относительный приёмник)
        self.deleted = set()

    def abs(self, rel):
        return os.path.join(self.root, rel)

    def exists(self, rel):
        if rel in self.texts:
            return True
        if rel in self.deleted:
            return False
        return os.path.exists(self.abs(rel))

    def read(self, rel):
        if rel in self.texts:
            return self.texts[rel]
        with io.open(self.abs(rel), encoding="utf-8") as fh:
            text = fh.read()
        self.texts[rel] = text
        return text

    def write(self, rel, text):
        self.texts[rel] = text

    def rename(self, src, dst):
        # Переносим и содержимое: дальнейшие правила работают уже по новому имени.
        text = None
        if self.exists(src):
            try:
                text = self.read(src)
            except Exception:
                text = None
        self.renames.append((src, dst))
        self.deleted.add(src)
        if src in self.texts:
            self.texts.pop(src)
        if text is not None:
            self.texts[dst] = text

    def copy_asset(self, src_abs, rel_dst):
        self.copies.append((src_abs, rel_dst))

    def flush(self):
        """Сбрасывает накопленные изменения на диск. Вызывается только в --apply."""
        # 1) сначала переименования файлов
        for src, dst in self.renames:
            src_abs, dst_abs = self.abs(src), self.abs(dst)
            if os.path.exists(src_abs):
                os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                shutil.move(src_abs, dst_abs)
        # 2) затем содержимое (новые имена уже на месте)
        for rel, text in self.texts.items():
            abs_path = self.abs(rel)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with io.open(abs_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        # 3) в последнюю очередь иконки
        for src_abs, rel_dst in self.copies:
            dst_abs = self.abs(rel_dst)
            os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
            shutil.copyfile(src_abs, dst_abs)


# ---------------------------------------------------------------------------
# Статусы правил
# ---------------------------------------------------------------------------
OK_PENDING = "OK"        # якорь найден ровно столько раз, сколько ждали
OK_APPLIED = "УЖЕ"       # правка уже стоит (идемпотентный повтор)
WARN = "ПРЕДУПР"         # не ошибка, но человек должен это увидеть (иконки)
FAIL = "ОШИБКА"          # якорь не найден -> дерево разъехалось с ожиданиями


class Rule(object):
    """Базовый класс правила. section/why нужны только для отчёта."""

    def __init__(self, path, why, section):
        self.path = path
        self.why = why
        self.section = section

    def label(self):
        return self.path

    def anchor(self):
        return ""

    def run(self, tree):
        raise NotImplementedError


class Sub(Rule):
    """Замена точной подстроки с обязательным числом вхождений.

    count   — сколько раз якорь обязан встретиться в исходном файле.
              Если встретился другое число раз, значит апстрим изменился и
              правку надо пересмотреть руками, а не применять вслепую.
    marker  — подстрока, доказывающая, что правка уже применена. По умолчанию
              это новая строка. Отдельный marker нужен там, где следующее
              правило переписывает часть уже вставленного текста (иначе
              повторный прогон посчитал бы файл разъехавшимся).
    """

    def __init__(self, path, old, new, count, why, section, marker=None,
                 marker_wins=False):
        Rule.__init__(self, path, why, section)
        self.old = old
        self.new = new
        self.count = count
        self.marker = marker if marker is not None else new
        # marker_wins=True нужен для ВСТАВОК: там якорь сохраняется в новом
        # тексте (мы дописываем строки рядом с ним), поэтому обычная проверка
        # "якорь исчез -> применено" не работает и повторный --apply вставил бы
        # блок второй раз. При marker_wins сначала смотрим на маркер результата.
        self.marker_wins = marker_wins

    def anchor(self):
        return self.old

    def run(self, tree):
        if not tree.exists(self.path):
            return FAIL, u"файла нет: %s" % self.path
        text = tree.read(self.path)
        if self.marker_wins and self.marker in text:
            return OK_APPLIED, u"вставка уже сделана"
        found = text.count(self.old)
        if found == self.count:
            tree.write(self.path, text.replace(self.old, self.new))
            return OK_PENDING, u"заменено %d" % found
        if found == 0 and self.marker in text:
            return OK_APPLIED, u"правка уже стоит"
        return FAIL, (u"ЯКОРЬ НЕ НАЙДЕН в файле %s: ожидалось %d вхождений, "
                      u"найдено %d" % (self.path, self.count, found))


class LineSub(Rule):
    """Замена внутри строк, содержащих маркер.

    Нужна там, где одинаковый текст надо поменять не везде, а только в части
    строк, и признак — содержимое самой строки. Пример: в flutter-build.yml
    номер версии в именах готовых артефактов (.exe/.msi/.dmg/.apk/...) должен
    стать продуктовым, а в именах промежуточных пакетов (.deb/.rpm/.zst),
    которые порождает build.py, обязан остаться прежним.

    count_lines фиксирован — если апстрим добавит ещё один артефакт, --check
    упадёт и заставит человека решить, к какой группе он относится.
    """

    def __init__(self, path, line_contains, old, new, count_lines, why, section,
                 marker=None, exclude_contains=(), require_any=()):
        Rule.__init__(self, path, why, section)
        self.line_contains = line_contains
        self.old = old
        self.new = new
        self.count_lines = count_lines
        self.marker = marker if marker is not None else new
        self.exclude_contains = tuple(exclude_contains)
        # require_any: строка обязана содержать хотя бы одну из этих подстрок.
        # Так отделяются готовые артефакты (по расширению файла) от
        # промежуточных пакетов, которые печёт build.py.
        self.require_any = tuple(require_any)

    def anchor(self):
        return u"строки с %s" % self.line_contains

    def _hit(self, line):
        if self.line_contains not in line or self.old not in line:
            return False
        for bad in self.exclude_contains:
            if bad in line:
                return False
        if self.require_any and not any(good in line for good in self.require_any):
            return False
        return True

    def run(self, tree):
        if not tree.exists(self.path):
            return FAIL, u"файла нет: %s" % self.path
        text = tree.read(self.path)
        lines = text.split(u"\n")
        hits = [i for i, l in enumerate(lines) if self._hit(l)]
        if len(hits) == self.count_lines:
            for i in hits:
                lines[i] = lines[i].replace(self.old, self.new)
            tree.write(self.path, u"\n".join(lines))
            return OK_PENDING, u"строк изменено %d" % len(hits)
        if not hits and self.marker in text:
            return OK_APPLIED, u"правка уже стоит"
        return FAIL, (u"ЯКОРЬ НЕ НАЙДЕН в файле %s: ожидалось %d подходящих "
                      u"строк, найдено %d" % (self.path, self.count_lines, len(hits)))


class WordSub(Rule):
    """Пакетная замена по всему файлу — для файлов, целиком принадлежащих бренду.

    Применяется к дистрибутивным файлам (.service, .desktop, DEBIAN/*, PKGBUILD,
    rpm*.spec, flatpak, appimage): там слово rustdesk встречается десятки раз и
    перечислять каждое вхождение якорем бессмысленно — файл целиком наш.
    protect — список подстрок; строка, содержащая любую из них, не трогается
    (так сохраняются ссылки на настоящий апстрим-репозиторий).
    """

    def __init__(self, path, pairs, why, section, protect=(), alt_paths=()):
        Rule.__init__(self, path, why, section)
        self.pairs = pairs
        self.protect = tuple(protect)
        self.alt_paths = tuple(alt_paths)

    def anchor(self):
        return u", ".join(a for a, _ in self.pairs)

    def _resolve(self, tree):
        for candidate in (self.path,) + self.alt_paths:
            if tree.exists(candidate):
                return candidate
        return None

    def run(self, tree):
        path = self._resolve(tree)
        if path is None:
            return FAIL, u"файла нет: %s" % self.path
        text = tree.read(path)
        # Считаем только по строкам, которые вообще подлежат правке: вхождения
        # внутри защищённых строк (ссылки на апстрим-репозиторий) остаются
        # навсегда, и если считать их тоже, повторный прогон каждый раз будет
        # думать, что файл ещё не обработан.
        editable = [l for l in text.split(u"\n")
                    if not any(bad in l for bad in self.protect)]
        total = sum(l.count(a) for l in editable for a, _ in self.pairs)
        if total == 0:
            if any(b in text for _, b in self.pairs):
                return OK_APPLIED, u"правка уже стоит"
            return FAIL, (u"ЯКОРЬ НЕ НАЙДЕН в файле %s: ни одного вхождения "
                          u"%s" % (path, self.anchor()))
        out = []
        for line in text.split(u"\n"):
            if any(bad in line for bad in self.protect):
                out.append(line)
                continue
            for old, new in self.pairs:
                line = line.replace(old, new)
            out.append(line)
        tree.write(path, u"\n".join(out))
        return OK_PENDING, u"заменено вхождений: %d" % total


class Rename(Rule):
    """Переименование файла дистрибутива.

    Имена файлов пакета — часть контракта с системой: бинарь с APP_NAME=DugaDesk
    выполняет `systemctl enable dugadesk`, ищет dugadesk.desktop и
    /etc/pam.d/dugadesk. Если пакет положит файлы под старыми именами, служба
    не будет управляться из интерфейса, а PAM тихо свалится на фолбэк.
    """

    def __init__(self, src, dst, why, section):
        Rule.__init__(self, src, why, section)
        self.src = src
        self.dst = dst

    def label(self):
        return u"%s -> %s" % (self.src, self.dst)

    def anchor(self):
        return u"файл %s" % self.src

    def run(self, tree):
        if tree.exists(self.src):
            tree.rename(self.src, self.dst)
            return OK_PENDING, u"переименован"
        if tree.exists(self.dst):
            return OK_APPLIED, u"уже переименован"
        return FAIL, u"ЯКОРЬ НЕ НАЙДЕН: нет ни %s, ни %s" % (self.src, self.dst)


class Asset(Rule):
    """Копирование иконки из brand/assets в дерево.

    Отсутствие файла — НЕ ошибка (иконки приносит владелец отдельно), но и не
    молчаливый пропуск: правило отдаёт ПРЕДУПРЕЖДЕНИЕ, чтобы факт "приложение
    осталось со старой иконкой" не выяснился уже после релиза.
    """

    def __init__(self, src_abs, src_name, dst, why, section):
        Rule.__init__(self, dst, why, section)
        self.src_abs = src_abs
        self.src_name = src_name
        self.dst = dst

    def label(self):
        return u"brand/assets/%s -> %s" % (self.src_name, self.dst)

    def anchor(self):
        return u"brand/assets/%s" % self.src_name

    def run(self, tree):
        if not os.path.exists(self.src_abs):
            return WARN, (u"НЕТ ФАЙЛА brand/assets/%s — иконка %s останется "
                          u"старой (RustDesk)" % (self.src_name, self.dst))
        tree.copy_asset(self.src_abs, self.dst)
        return OK_PENDING, u"скопирована"


class InsertStep(Rule):
    """Вставка шага в job GitHub Actions сразу после actions/checkout.

    ЗАЧЕМ. libs/hbb_common — git-подмодуль ЧУЖОГО репозитория
    (rustdesk/hbb_common). Правки его файлов физически невозможно закоммитить
    в наш репозиторий: git хранит для подмодуля только указатель на ревизию,
    а `git status` в основном дереве показывает лишь " M libs/hbb_common".
    В раннере после checkout подмодуль всегда приезжает с апстрим-пина, то
    есть APP_NAME="RustDesk", RENDEZVOUS_SERVERS=["rs-ny.rustdesk.com"] и
    чужой RS_PUB_KEY. Без применения слоя прямо в раннере релиз соберётся под
    именем RustDesk и уйдёт на чужой сервер — и выяснится это только на
    готовом релизе, через 1 ч 40 мин сборки.

    Альтернативу (форк hbb_common) отвергли: форк подмодуля пришлось бы
    держать в синхроне с пином апстрима, и любой рассинхрон молча вернул бы
    старые константы.

    Правило находит job по имени, внутри него — единственный блок checkout,
    и вставляет шаг сразу после него. Ни номеров строк, ни позиций: если
    апстрим переставит шаги местами, правило по-прежнему сработает, а если
    checkout в job'е исчезнет или задвоится — --check упадёт.
    """

    JOB_RE = re.compile(r"^  [A-Za-z0-9_.\-]+:\s*$")

    def __init__(self, path, job_name, step_lines, marker, why, section):
        Rule.__init__(self, path, why, section)
        self.job_name = job_name
        self.step_lines = list(step_lines)
        self.marker = marker

    def label(self):
        return u"%s :: job %s" % (self.path, self.job_name)

    def anchor(self):
        return u"job %s + actions/checkout" % self.job_name

    def run(self, tree):
        if not tree.exists(self.path):
            return FAIL, u"файла нет: %s" % self.path
        text = tree.read(self.path)
        lines = text.split(u"\n")

        # 1. Границы job'а: от строки "  <имя>:" до следующей строки того же
        #    уровня отступа (следующий job) или до конца файла.
        start = None
        head = u"  %s:" % self.job_name
        for i, line in enumerate(lines):
            if line.rstrip() == head:
                start = i
                break
        if start is None:
            return FAIL, (u"ЯКОРЬ НЕ НАЙДЕН в файле %s: нет job '%s' — его "
                          u"переименовали или удалили в апстриме"
                          % (self.path, self.job_name))
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if self.JOB_RE.match(lines[j]):
                end = j
                break
        body = lines[start:end]

        # 2. Шаг уже стоит? Тогда ничего не делаем (идемпотентность).
        if self.marker in u"\n".join(body):
            return OK_APPLIED, u"шаг уже вставлен"

        # 3. Ровно один checkout внутри job'а.
        hits = [k for k, l in enumerate(body) if "uses: actions/checkout@" in l]
        if len(hits) != 1:
            return FAIL, (u"ЯКОРЬ НЕ НАЙДЕН в файле %s: в job '%s' ожидался "
                          u"ровно один шаг actions/checkout, найдено %d"
                          % (self.path, self.job_name, len(hits)))
        k = hits[0]

        # 4. Конец блока checkout: первая пустая строка или начало следующего
        #    шага ("      - ..."). Вставляем сразу за ним.
        m = k + 1
        while m < len(body):
            stripped = body[m].strip()
            if stripped == u"" or stripped.startswith(u"- "):
                break
            m += 1
        if m < len(body) and body[m].strip() == u"":
            insert_at = m + 1                      # после пустой строки
            block = self.step_lines + [u""]
        else:
            insert_at = m                          # прямо перед следующим шагом
            block = self.step_lines + [u""]
        new_body = body[:insert_at] + block + body[insert_at:]

        tree.write(self.path, u"\n".join(lines[:start] + new_body + lines[end:]))
        return OK_PENDING, u"шаг вставлен после checkout"


# ===========================================================================
#                            С П И С О К   П Р А В О К
# ===========================================================================
# Каждая правка снабжена полем why: ЧТО СЛОМАЕТСЯ, если её не сделать.
# Список проверен по реальному дереву форка gdm0991/rustdesk (ветка master,
# подмодуль libs/hbb_common). Числа вхождений — фактические на момент сверки.
# ===========================================================================

def build_rules(b, assets_dir):
    APP = b["app_name"]                 # DugaDesk
    BIN = b["bin"]                      # dugadesk
    ORG = b["org"]                      # pw.duga
    SCHEME = b["scheme"]                # dugadesk
    RDV = b["rendezvous"]               # rustdesk.duga.pw
    KEY = b["pubkey"]
    PVER = b["product_version"]         # 1.0.0
    DOM = b["domain"]                   # duga.pw
    AID = b["android_app_id"]           # pw.duga.dugadesk
    MBID = b["macos_bundle_id"]
    FID = b["flatpak_id"]               # pw.duga.DugaDesk
    LAID = b["linux_app_id"]

    def asset(name):
        return os.path.join(assets_dir, name)

    R = []

    # -----------------------------------------------------------------
    # 1. ЯДРО: libs/hbb_common/src/config.rs
    # -----------------------------------------------------------------
    # Это и есть настоящий ребрендинг: всё остальное — обёртка вокруг него.
    CFG = "libs/hbb_common/src/config.rs"
    S = u"1. Ядро (hbb_common)"

    R.append(Sub(
        CFG,
        'pub static ref APP_NAME: RwLock<String> = RwLock::new("RustDesk".to_owned());',
        'pub static ref APP_NAME: RwLock<String> = RwLock::new("%s".to_owned());' % APP,
        1,
        u"APP_NAME определяет имя окна, имя службы Windows, имя каталога "
        u"конфигурации и включает признак is_custom_client(). Без этой правки "
        u"продукт остаётся RustDesk во всех системных местах, даже если "
        u"переименовать файлы.",
        S))

    R.append(Sub(
        CFG,
        'pub static ref ORG: RwLock<String> = RwLock::new("com.carriez".to_owned());',
        'pub static ref ORG: RwLock<String> = RwLock::new("%s".to_owned());' % ORG,
        1,
        u"ORG задаёт вендорный каталог настроек (%%APPDATA%%\\<ORG>\\<APP_NAME>) "
        u"и идентификаторы служб. Без правки настройки DugaDesk лягут в чужой "
        u"каталог com.carriez и будут перемешаны с RustDesk, если тот стоит рядом.",
        S))

    R.append(Sub(
        CFG,
        'pub const RENDEZVOUS_SERVERS: &[&str] = &["rs-ny.rustdesk.com"];',
        'pub const RENDEZVOUS_SERVERS: &[&str] = &["%s"];' % RDV,
        1,
        u"Дефолтный сервер сигнализации. Константа стоит ПОСЛЕДНЕЙ в приоритете "
        u"get_rendezvous_server() (EXE -> custom-rendezvous-server -> PROD -> "
        u"CONFIG2 -> константа), поэтому поле настроек у пользователя остаётся "
        u"рабочим и перекрывает вшитое значение. Без правки свежая установка "
        u"пойдёт на публичный сервер RustDesk.",
        S))

    R.append(Sub(
        CFG,
        'pub const RS_PUB_KEY: &str = "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw=";',
        'pub const RS_PUB_KEY: &str = "%s";' % KEY,
        1,
        u"Публичный ключ своего сервера. Без него клиент по умолчанию проверяет "
        u"ключ чужого сервера и защищённое соединение с нашим сервером не "
        u"установится (пользователю придётся вбивать ключ руками).",
        S))

    # -----------------------------------------------------------------
    # 2. WINDOWS
    # -----------------------------------------------------------------
    S = u"2. Windows"

    R.append(Sub("Cargo.toml", 'ProductName = "RustDesk"',
                 'ProductName = "%s"' % APP, 1,
                 u"Секция [package.metadata.winres]: имя продукта в свойствах "
                 u"exe. Без правки в 'Свойства файла' и в списке процессов "
                 u"видно RustDesk.", S))
    R.append(Sub("Cargo.toml", 'FileDescription = "RustDesk Remote Desktop"',
                 'FileDescription = "%s Remote Desktop"' % APP, 1,
                 u"Описание файла в ресурсах exe — то, что показывает "
                 u"Диспетчер задач в колонке 'Описание'.", S))
    R.append(Sub("Cargo.toml", 'OriginalFilename = "rustdesk.exe"',
                 'OriginalFilename = "%s.exe"' % APP, 1,
                 u"Оригинальное имя файла в ресурсах. Расхождение с реальным "
                 u"именем сбивает антивирусные эвристики и выглядит как подмена.", S))

    RC = "flutter/windows/runner/Runner.rc"
    R.append(Sub(RC, 'VALUE "FileDescription", "RustDesk Remote Desktop" "\\0"',
                 'VALUE "FileDescription", "%s Remote Desktop" "\\0"' % APP, 1,
                 u"То же самое, но для flutter-раннера (именно он становится "
                 u"итоговым exe). Без правки MSI ставит файл с описанием RustDesk.", S))
    R.append(Sub(RC, 'VALUE "InternalName", "rustdesk" "\\0"',
                 'VALUE "InternalName", "%s" "\\0"' % BIN, 1,
                 u"Внутреннее имя модуля в ресурсах exe.", S))
    R.append(Sub(RC, 'VALUE "OriginalFilename", "rustdesk.exe" "\\0"',
                 'VALUE "OriginalFilename", "%s.exe" "\\0"' % APP, 1,
                 u"Итоговый exe в CI переименовывается в %s.exe — ресурс "
                 u"обязан это отражать." % APP, S))
    R.append(Sub(RC, 'VALUE "ProductName", "RustDesk" "\\0"',
                 'VALUE "ProductName", "%s" "\\0"' % APP, 1,
                 u"Имя продукта в ресурсах exe.", S))

    R.append(Sub("flutter/windows/runner/main.cpp",
                 'std::wstring app_name = L"RustDesk";',
                 'std::wstring app_name = L"%s";' % APP, 1,
                 u"Запасное имя окна на случай, если get_rustdesk_app_name из "
                 u"librustdesk.dll не отдал имя. Без правки в редком сценарии "
                 u"(dll загрузилась, символ не найден) окно назовётся RustDesk.", S))

    # -----------------------------------------------------------------
    # 3. LINUX: имена файлов дистрибутива и пути установки
    # -----------------------------------------------------------------
    S = u"3. Linux (пакеты)"

    # Имя исполняемого файла flutter-сборки под Linux.
    # ЭТО КРИТИЧНО: deb кладёт содержимое bundle в /usr/share/dugadesk, а
    # postinst делает симлинк /usr/bin/dugadesk -> /usr/share/dugadesk/dugadesk.
    # Если BINARY_NAME остаётся "rustdesk", симлинк указывает в пустоту и
    # пакет ставится, но программа не запускается.
    R.append(Sub("flutter/linux/CMakeLists.txt", 'set(BINARY_NAME "rustdesk")',
                 'set(BINARY_NAME "%s")' % BIN, 1,
                 u"Имя исполняемого файла Linux-сборки. Без правки симлинк "
                 u"/usr/bin/%s из postinst указывает на несуществующий файл." % BIN, S))
    R.append(Sub("flutter/linux/CMakeLists.txt",
                 'set(APPLICATION_ID "com.carriez.flutter_hbb")',
                 'set(APPLICATION_ID "%s")' % LAID, 1,
                 u"GApplication id. По нему рабочий стол связывает окно с "
                 u".desktop-файлом (иконка в доке/переключателе задач).", S))

    R.append(Sub("src/ui.rs", '"/usr/share/rustdesk/libsciter-gtk.so"',
                 '"/usr/share/%s/libsciter-gtk.so"' % BIN, 1,
                 u"Захардкоженный путь к sciter. Сборку sciter мы не делаем, но "
                 u"оставлять путь к чужому каталогу нельзя: он попадёт в grep и "
                 u"будет вводить в заблуждение при следующем разборе.", S))

    # Переименование файлов дистрибутива.
    for src, dst, why in [
        ("res/rustdesk.service", "res/%s.service" % BIN,
         u"Бинарь с APP_NAME=%s выполняет `systemctl enable %s`. Файл службы "
         u"обязан называться так же, иначе служба не управляется из интерфейса." % (APP, BIN)),
        ("res/rustdesk.desktop", "res/%s.desktop" % BIN,
         u"Приложение ищет %s.desktop (автозапуск, ярлык). Старое имя = нет ярлыка." % BIN),
        ("res/rustdesk-link.desktop", "res/%s-link.desktop" % BIN,
         u"Обработчик схемы %s://. Старое имя = ссылки не открываются приложением." % SCHEME),
        ("res/pam.d/rustdesk.debian", "res/pam.d/%s.debian" % BIN,
         u"PAM ищет /etc/pam.d/%s. Если файла нет, PAM тихо падает на фолбэк "
         u"gdm — проверка пароля ведёт себя иначе, чем ожидается." % BIN),
        ("res/pam.d/rustdesk.suse", "res/pam.d/%s.suse" % BIN,
         u"То же самое для SUSE-веток."),
    ]:
        R.append(Rename(src, dst, why, S))

    # Содержимое дистрибутивных файлов — целиком наше, правим пакетно.
    PAIRS = [("rustdesk.com", DOM), ("com.carriez", ORG),
             ("RustDesk", APP), ("rustdesk", BIN), ("RUSTDESK", APP.upper())]
    PROTECT = ("github.com/rustdesk", "rustdesk-org", "ko-fi.com/rustdesk")

    for path, why in [
        ("res/%s.service" % BIN,
         u"Description/ExecStart/ExecStop/PIDFile: служба должна запускать "
         u"/usr/bin/%s и убивать свои же процессы. Со старыми именами служба "
         u"стартует чужой бинарь или не убивает свой." % BIN),
        ("res/%s.desktop" % BIN,
         u"Name/Exec/Icon/StartupWMClass. Без правки в меню видно 'RustDesk', "
         u"а окно не связывается с ярлыком (иконка в доке — дефолтная)."),
        ("res/%s-link.desktop" % BIN,
         u"MimeType=x-scheme-handler/%s. Без правки ссылки %s:// не "
         u"перехватываются, а перехватываются rustdesk://." % (SCHEME, SCHEME)),
        ("res/DEBIAN/postinst",
         u"Создаёт симлинк, ставит и включает службу. Со старыми путями "
         u"установка кладёт службу rustdesk, а бинарь ищет %s." % BIN),
        ("res/DEBIAN/preinst",
         u"Останавливает старую службу перед установкой."),
        ("res/DEBIAN/prerm",
         u"Останавливает и отключает службу при удалении. Без правки при "
         u"удалении пакета служба остаётся висеть."),
        ("res/DEBIAN/postrm",
         u"Чистит ~/.config при purge."),
        ("res/PKGBUILD",
         u"Arch-пакет: pkgname и все пути установки."),
        ("res/rpm-flutter.spec",
         u"Fedora/CentOS flutter-пакет: Name, пути, systemctl-вызовы в "
         u"%%post/%%preun. Это основной rpm, который собирает CI."),
        ("res/rpm-flutter-suse.spec",
         u"То же для SUSE."),
        ("res/rpm.spec",
         u"Sciter-вариант rpm. Не собираем, но имя пакета обязано быть нашим, "
         u"иначе случайная сборка положит в систему пакет rustdesk."),
        ("res/rpm-suse.spec",
         u"То же для SUSE."),
    ]:
        R.append(WordSub(path, PAIRS, why, S, protect=PROTECT))

    # -----------------------------------------------------------------
    # 4. LINUX: build.py (сборка deb/rpm/dmg)
    # -----------------------------------------------------------------
    S = u"4. build.py"
    BP = "build.py"

    # ВАЖНО, ЧТО НЕ ТРОГАЕМ в build.py:
    #   librustdesk.so / liblibrustdesk.dylib / librustdesk.dll — имя cdylib из
    #     Cargo.toml ([lib] name = "librustdesk"); переименование порвёт FFI;
    #   rustdesk-portable-packer.exe — имя крейта libs/portable;
    #   target/release/rustdesk — файл, который печёт cargo (package name);
    #   hbb_name = 'rustdesk' — та же причина.
    for old, new, cnt, why in [
        ('content = """Package: rustdesk', 'content = """Package: %s' % BIN, 1,
         u"Имя deb-пакета. Без правки apt считает, что установлен rustdesk, и "
         u"обновление поверх настоящего RustDesk снесёт его файлы."),
        ('Maintainer: rustdesk <info@rustdesk.com>',
         'Maintainer: %s <info@%s>' % (APP, DOM), 1,
         u"Сопровождающий пакета — в свойствах пакета не должно быть чужой почты."),
        ('Homepage: https://rustdesk.com', 'Homepage: https://%s' % DOM, 1,
         u"Домашняя страница пакета."),
        ('tmpdeb/usr/share/rustdesk', 'tmpdeb/usr/share/%s' % BIN, 17,
         u"Каталог установки. Бинарь с APP_NAME=%s ищет свои файлы в "
         u"/usr/share/%s; со старым путём не находит polkit-хелпер и systemd-юнит." % (APP, BIN)),
        ('tmpdeb/etc/rustdesk/', 'tmpdeb/etc/%s/' % BIN, 5,
         u"Каталог /etc/<app> с startwm.sh и xorg.conf для сессии входа."),
        ('tmpdeb/etc/X11/rustdesk/', 'tmpdeb/etc/X11/%s/' % BIN, 2,
         u"Конфиг X11 для headless-режима."),
        ('tmpdeb/usr/bin/rustdesk', 'tmpdeb/usr/bin/%s' % BIN, 4,
         u"Путь исполняемого файла внутри пакета."),
        ('../res/rustdesk.service', '../res/%s.service' % BIN, 2,
         u"Копирование юнита из res/ — файл там уже переименован."),
        ("cp res/rustdesk.service tmpdeb", "cp res/%s.service tmpdeb" % BIN, 1,
         u"То же в sciter-ветке (без ../)."),
        ('../res/rustdesk.desktop', '../res/%s.desktop' % BIN, 2,
         u"Копирование ярлыка."),
        ('../res/rustdesk-link.desktop', '../res/%s-link.desktop' % BIN, 2,
         u"Копирование обработчика схемы."),
        ('../res/pam.d/rustdesk.debian tmpdeb/etc/pam.d/rustdesk',
         '../res/pam.d/%s.debian tmpdeb/etc/pam.d/%s' % (BIN, BIN), 1,
         u"PAM-профиль обязан лечь как /etc/pam.d/%s." % BIN),
        ("cp pam.d/rustdesk.debian tmpdeb/etc/pam.d/rustdesk",
         "cp pam.d/%s.debian tmpdeb/etc/pam.d/%s" % (BIN, BIN), 1,
         u"То же в sciter-ветке."),
        ('apps/rustdesk.png', 'apps/%s.png' % BIN, 3,
         u"Имя файла иконки в hicolor. Оно обязано совпадать с Icon= в "
         u".desktop, иначе окно и меню остаются без иконки."),
        ('apps/rustdesk.svg', 'apps/%s.svg' % BIN, 3,
         u"То же для векторной иконки."),
        ('applications/rustdesk.desktop', 'applications/%s.desktop' % BIN, 3,
         u"Имя ярлыка в /usr/share/applications."),
        ('applications/rustdesk-link.desktop', 'applications/%s-link.desktop' % BIN, 3,
         u"Имя обработчика ссылок в /usr/share/applications."),
        ('dpkg-deb -b tmpdeb rustdesk.deb', 'dpkg-deb -b tmpdeb %s.deb' % BIN, 3,
         u"Имя собираемого deb. От него зависит имя, которое ищет CI."),
        ("os.rename('rustdesk.deb', '../rustdesk-%s.deb' % version)",
         "os.rename('%s.deb', '../%s-%%s.deb' %% version)" % (BIN, BIN), 2,
         u"Итоговое имя deb, которое подхватывает шаг CI 'Upload deb'."),
        ("os.rename('rustdesk.deb', 'rustdesk-%s.deb' % version)",
         "os.rename('%s.deb', '%s-%%s.deb' %% version)" % (BIN, BIN), 1,
         u"То же в sciter-ветке."),
        ("mv target/release/bundle/deb/rustdesk*.deb ./rustdesk.deb",
         "mv target/release/bundle/deb/%s*.deb ./%s.deb" % (BIN, BIN), 1,
         u"Sciter-ветка: cargo-bundle кладёт deb по имени пакета."),
        ("dpkg-deb -R rustdesk.deb tmpdeb", "dpkg-deb -R %s.deb tmpdeb" % BIN, 1,
         u"Распаковка собранного deb для доработки."),
        ('rustdesk-%s-0-x86_64.pkg.tar.zst', '%s-%%s-0-x86_64.pkg.tar.zst' % BIN, 1,
         u"Имя, которое печёт makepkg по pkgname из PKGBUILD."),
        ('rustdesk-%s-manjaro-arch.pkg.tar.zst', '%s-%%s-manjaro-arch.pkg.tar.zst' % BIN, 1,
         u"Итоговое имя arch-пакета."),
        ('rustdesk-%s-0.x86_64.rpm', '%s-%%s-0.x86_64.rpm' % BIN, 2,
         u"Имя, которое печёт rpmbuild по Name из spec."),
        ('./rustdesk-%s-fedora28-centos8.rpm', './%s-%%s-fedora28-centos8.rpm' % BIN, 1,
         u"Итоговое имя rpm для Fedora/CentOS."),
        ('./rustdesk-%s-suse.rpm', './%s-%%s-suse.rpm' % BIN, 1,
         u"Итоговое имя rpm для SUSE."),
        ('RustDesk.app', '%s.app' % APP, 13,
         u"Имя macOS-бандла. flutter build macos создаёт бандл по PRODUCT_NAME "
         u"из AppInfo.xcconfig; если здесь оставить RustDesk.app, create-dmg "
         u"не найдёт каталог и .dmg соберётся пустым."),
        ('RustDesk Installer', '%s Installer' % APP, 1,
         u"Заголовок тома в .dmg — первое, что видит пользователь при установке."),
        ('rustdesk.dmg', '%s.dmg' % BIN, 2,
         u"Промежуточное имя образа."),
        ("'rustdesk-%s.dmg' % version", "'%s-%%s.dmg' %% version" % BIN, 1,
         u"Итоговое имя образа (sciter-ветка)."),
        ('RustDesk %s.dmg', '%s %%s.dmg' % APP, 2,
         u"Имя образа, которое печёт create-dmg (sciter-ветка)."),
        ('rustdesk-{1}.dmg', '%s-{1}.dmg' % BIN, 3,
         u"Имя образа в шаблоне подписи/нотаризации."),
        ('rustdesk-{version}-win7-install.exe', '%s-{version}-win7-install.exe' % BIN, 2,
         u"Имя самораспаковывающегося установщика для Windows 7."),
        ('rustdesk_portable.exe', '%s_portable.exe' % BIN, 5,
         u"Промежуточное имя портативной сборки; меняется согласованно во всех "
         u"пяти местах, иначе следующий шаг не найдёт файл."),
        ('rustdesk-{version}-install.exe', '%s-{version}-install.exe' % BIN, 2,
         u"Итоговое имя портативного установщика."),
    ]:
        R.append(Sub(BP, old, new, cnt, why, S))

    # -----------------------------------------------------------------
    # 5. ANDROID
    # -----------------------------------------------------------------
    S = u"5. Android"
    AM = "flutter/android/app/src/main/AndroidManifest.xml"

    # НЕ ТРОГАЕМ в Android:
    #   package="com.carriez.flutter_hbb" в манифесте — он привязан к пакетам
    #     Kotlin-классов (MainActivity, MainService и др.);
    #   строки MethodChannel вида org.rustdesk.rustdesk/* в input_model.dart и
    #     relative_mouse_model.dart — они обязаны побайтово совпадать с
    #     Kotlin-стороной; переименование канала = приложение перестаёт
    #     передавать ввод, при этом компилируется без единой ошибки.
    R.append(Sub(AM, 'android:label="RustDesk"', 'android:label="%s"' % APP, 1,
                 u"Подпись приложения в списке приложений Android. Без правки "
                 u"на телефоне видно RustDesk.", S))
    R.append(Sub(AM, 'android:label="RustDesk Input"',
                 'android:label="%s Input"' % APP, 1,
                 u"Подпись службы ввода в системных настройках специальных "
                 u"возможностей — пользователь разрешает доступ именно ей.", S))
    R.append(Sub(AM, '<data android:scheme="rustdesk" />',
                 '<data android:scheme="%s" />' % SCHEME, 1,
                 u"URI-схема deep-link. Без правки ссылки %s:// не открывают "
                 u"приложение (а rustdesk:// — открывают, конфликтуя с настоящим "
                 u"RustDesk на том же телефоне)." % SCHEME, S))

    R.append(Sub("flutter/android/app/build.gradle",
                 'applicationId "com.carriez.flutter_hbb"',
                 'applicationId "%s"' % AID, 1,
                 u"applicationId — идентификатор пакета в системе и в магазине. "
                 u"Без правки apk конфликтует с настоящим RustDesk: одна из "
                 u"установок затирает другую.", S))

    STR = "flutter/android/app/src/main/res/values/strings.xml"
    R.append(Sub(STR, '<string name="app_name">RustDesk</string>',
                 '<string name="app_name">%s</string>' % APP, 1,
                 u"Имя под иконкой на рабочем столе телефона.", S))
    R.append(Sub(STR, 'when RustDesk screen sharing is established',
                 'when %s screen sharing is established' % APP, 1,
                 u"Текст запроса разрешения на спец.возможности — пользователь "
                 u"видит его в системном диалоге и должен узнать наш продукт.", S))

    # -----------------------------------------------------------------
    # 6. macOS
    # -----------------------------------------------------------------
    S = u"6. macOS"
    XC = "flutter/macos/Runner/Configs/AppInfo.xcconfig"
    R.append(Sub(XC, "PRODUCT_NAME = RustDesk", "PRODUCT_NAME = %s" % APP, 1,
                 u"Имя бандла: flutter build macos создаёт <PRODUCT_NAME>.app. "
                 u"Именно это имя ищут create-dmg и codesign в CI и build.py — "
                 u"без правки .dmg соберётся пустым.", S))
    R.append(Sub(XC, "PRODUCT_BUNDLE_IDENTIFIER = com.carriez.flutterHbb",
                 "PRODUCT_BUNDLE_IDENTIFIER = %s" % MBID, 1,
                 u"Bundle id: по нему macOS выдаёт разрешения (экран, "
                 u"специальные возможности). Совпадение с RustDesk означает, "
                 u"что две программы делят одну запись разрешений.", S))

    # -----------------------------------------------------------------
    # 7. FLUTTER / UI
    # -----------------------------------------------------------------
    S = u"7. Интерфейс (Dart)"

    R.append(Sub("flutter/lib/desktop/widgets/tabbar_widget.dart",
                 '                              "RustDesk",',
                 '                              "%s",' % APP, 1,
                 u"Литерал в заголовке вкладок главного окна — единственное "
                 u"место, где имя вписано текстом, а не берётся из APP_NAME.", S))

    # Надпись "Powered by RustDesk".
    # Как только APP_NAME != "RustDesk", включается is_custom_client(), и в
    # desktop_home_page.dart / mobile settings_page.dart показывается
    # loadPowered() со ссылкой на rustdesk.com. Штатный способ убрать её —
    # опция hide-powered-by-me из custom.txt, но custom.txt проверяется
    # подписью ЧУЖИМ ключом и нам недоступен. Поэтому правим Dart.
    R.append(Sub("flutter/lib/common.dart",
                 'if (bind.mainGetBuildinOption(key: "hide-powered-by-me") == \'Y\') {',
                 '// РЕБРЕНДИНГ %s: надпись "Powered by ..." скрыта безусловно.\n'
                 '  // Штатная опция hide-powered-by-me живёт в custom.txt, а он\n'
                 '  // проверяется подписью чужим ключом — поэтому условие заменено на true.\n'
                 '  if (true) {' % APP, 1,
                 u"Без правки в главном окне и в мобильных настройках висит "
                 u"кликабельная надпись со ссылкой на rustdesk.com.", S))

    # Ссылки на домен вендора.
    for path, old, new, cnt, why in [
        ("flutter/lib/common.dart",
         "launchUrl(Uri.parse('https://rustdesk.com'));",
         "launchUrl(Uri.parse('https://%s'));" % DOM, 1,
         u"Переход по надписи Powered by (на случай, если её вернут)."),
        ("flutter/lib/desktop/pages/desktop_home_page.dart",
         "'https://rustdesk.com/download'", "'https://%s/download'" % DOM, 1,
         u"Кнопка загрузки обновления в карточке статуса."),
        ("flutter/lib/desktop/pages/desktop_home_page.dart",
         "'https://rustdesk.com/docs/en/client/linux/#permissions-issue'",
         "'https://%s/docs/en/client/linux/#permissions-issue'" % DOM, 1,
         u"Ссылка справки о правах в Linux."),
        ("flutter/lib/desktop/pages/desktop_home_page.dart",
         "'https://rustdesk.com/docs/en/client/linux/#x11-required'",
         "'https://%s/docs/en/client/linux/#x11-required'" % DOM, 1,
         u"Ссылка справки о необходимости X11."),
        ("flutter/lib/desktop/pages/desktop_home_page.dart",
         "'https://rustdesk.com/docs/en/client/linux/#login-screen'",
         "'https://%s/docs/en/client/linux/#login-screen'" % DOM, 1,
         u"Ссылка справки об экране входа."),
        ("flutter/lib/desktop/pages/desktop_setting_page.dart",
         "launchUrlString('https://rustdesk.com/privacy.html');",
         "launchUrlString('https://%s/privacy.html');" % DOM, 1,
         u"Ссылка на политику конфиденциальности в настройках."),
        ("flutter/lib/desktop/pages/desktop_setting_page.dart",
         "launchUrlString('https://rustdesk.com');",
         "launchUrlString('https://%s');" % DOM, 1,
         u"Ссылка на сайт вендора в разделе 'О программе'."),
        ("flutter/lib/desktop/pages/install_page.dart",
         "'https://rustdesk.com/privacy.html'", "'https://%s/privacy.html'" % DOM, 2,
         u"Ссылка и всплывающая подсказка на экране установки — пользователь "
         u"обязан видеть наш домен, а не чужой."),
        ("flutter/lib/desktop/pages/connection_page.dart",
         'const url = "https://rustdesk.com/pricing";',
         'const url = "https://%s/pricing";' % DOM, 1,
         u"Ссылка на тарифы на странице подключения."),
    ]:
        R.append(Sub(path, old, new, cnt, why, S))

    # -----------------------------------------------------------------
    # 8. CI: .github/workflows/flutter-build.yml
    # -----------------------------------------------------------------
    S = u"8. CI (flutter-build.yml)"
    YML = ".github/workflows/flutter-build.yml"

    # 8.1 Отдельная переменная продуктовой версии.
    # Переменную VERSION НЕ трогаем: от неё зависят имена промежуточных
    # пакетов, которые печёт build.py (по версии из Cargo.toml), rpmbuild
    # (по Version из spec) и makepkg (по pkgver из PKGBUILD). Если поменять
    # VERSION, CI начнёт искать файлы, которых никто не создаёт.
    R.append(Sub(YML,
                 "  #signing keys env variable checks",
                 "  # РЕБРЕНДИНГ: продуктовая версия ТОЛЬКО для имён готовых\n"
                 "  # артефактов (exe/msi/dmg/apk/AppImage/flatpak/tar.gz).\n"
                 "  # Переменную VERSION выше не трогаем — по ней CI ищет\n"
                 "  # промежуточные deb/rpm/zst, которые печёт build.py.\n"
                 "  PROD_VERSION: \"%s\"\n"
                 "  PROD_NAME: \"%s\"\n"
                 "  #signing keys env variable checks" % (PVER, BIN), 1,
                 u"Без отдельной переменной пришлось бы менять VERSION, и CI "
                 u"стал бы искать несуществующие промежуточные файлы.", S,
                 marker="PROD_VERSION: \"%s\"" % PVER, marker_wins=True))

    # 8.2 Имя продукта в именах артефактов. Сначала меняем ТОЛЬКО имя,
    # версию оставляем — её разводим следующим правилом.
    R.append(Sub(YML, "rustdesk-${{ env.VERSION }}", "%s-${{ env.VERSION }}" % BIN, 37,
                 u"Имена всех артефактов релиза. Без правки релиз DugaDesk "
                 u"выкладывает файлы с именем rustdesk-*.",
                 S, marker="%s-${{ env." % BIN))

    # 8.3 Разводим версии: готовые артефакты получают продуктовую версию,
    # промежуточные пакеты (.deb/.rpm/.zst) остаются на версии сборки.
    # Признак готового артефакта — расширение файла в той же строке.
    READY_EXT = (".exe", ".msi", ".dmg", ".apk", ".AppImage", ".flatpak", ".tar.gz")
    R.append(LineSub(
        YML,
        "%s-${{ env.VERSION }}" % BIN,
        "${{ env.VERSION }}", "${{ env.PROD_VERSION }}", 26,
        u"Строки с готовыми артефактами (.exe/.msi/.dmg/.apk/.AppImage/"
        u".flatpak/.tar.gz) должны нести продуктовую версию %s. Промежуточные "
        u".deb/.rpm/.zst остаются на версии сборки, иначе шаги CI не найдут "
        u"файлы, созданные build.py/rpmbuild/makepkg." % PVER,
        S,
        marker="%s-${{ env.PROD_VERSION }}" % BIN,
        require_any=READY_EXT))
    # ВНИМАНИЕ: LineSub выше отбирает строки по расширению — задаём его через
    # отдельный фильтр ниже (см. параметр line_contains). Строка с dir_name
    # расширения не содержит, поэтому правится отдельным якорем и в паре со
    # строкой архива (иначе имя каталога и имя архива разъедутся).
    R.append(Sub(YML,
                 'dir_name="%s-${{ env.VERSION }}-${{ env.RELEASE_NAME }}"' % BIN,
                 'dir_name="%s-${{ env.PROD_VERSION }}-${{ env.RELEASE_NAME }}"' % BIN, 1,
                 u"Имя каталога web-сборки обязано совпасть с именем архива "
                 u"строкой ниже, иначе tar соберёт пустой архив.", S))

    # 8.4 Метки артефактов между job'ами (upload/download) и glob'ы публикации.
    for old, new, cnt, why in [
        ("rustdesk-unsigned-", "%s-unsigned-" % BIN, 7,
         u"Имена артефактов между job'ами. Пара upload/download обязана "
         u"совпадать, иначе job подписи не найдёт вход."),
        ("SignOutput/rustdesk-*", "SignOutput/%s-*" % BIN, 4,
         u"Маски публикации Windows-файлов. Со старой маской релиз выйдет "
         u"пустым: файлы уже называются %s-*." % BIN),
        ("rustdesk*??.deb", "%s*??.deb" % BIN, 2,
         u"Цикл переименования deb под архитектуру."),
        ("rustdesk*??.rpm", "%s*??.rpm" % BIN, 2,
         u"Цикл переноса rpm из ~/rpmbuild."),
        ("rustdesk-*.deb", "%s-*.deb" % BIN, 1,
         u"Маска публикации deb."),
        ("rustdesk-*.rpm", "%s-*.rpm" % BIN, 1,
         u"Маска публикации rpm."),
        ("RustDesk.app", "%s.app" % APP, 7,
         u"Имя macOS-бандла в create-dmg и codesign. Без правки dmg пустой, "
         u"а подпись падает на несуществующем пути."),
        ("appimage/rustdesk.deb", "appimage/%s.deb" % BIN, 1,
         u"Имя deb, который распаковывает рецепт AppImage (там же поправлено)."),
        ("flatpak/rustdesk.deb", "flatpak/%s.deb" % BIN, 1,
         u"Имя deb, который распаковывает манифест flatpak (там же поправлено)."),
        ("./build ./rustdesk.json", "./build ./%s.json" % BIN, 1,
         u"Манифест flatpak переименован — flatpak-builder должен звать новый. "
         u"ВАЖНО: строки '${{ github.workspace }}/rustdesk.json' в macOS-job "
         u"НЕ трогаем, это ключ нотаризации Apple, а не манифест."),
        ("com.rustdesk.RustDesk", FID, 1,
         u"Идентификатор приложения в flatpak build-bundle обязан совпасть с "
         u"id из манифеста, иначе бандл не собрать."),
    ]:
        R.append(Sub(YML, old, new, cnt, why, S))

    # 8.5 MSI: переименование exe и передача --app-name.
    # preprocess.py ищет в dist-каталоге ровно <app-name>.exe и ЗАПУСКАЕТ его
    # (--version). flutter собирает файл под именем rustdesk.exe (BINARY_NAME
    # в flutter/windows/CMakeLists.txt мы намеренно не трогаем, чтобы шаг
    # портативной сборки выше продолжал работать), поэтому переименовываем
    # ровно перед сборкой MSI. Без --app-name внутри MSI останутся служба
    # "RustDesk", ключи реестра .rustdesk и каталог установки RustDesk —
    # установленный продукт расколется надвое: ставится RustDesk, а бинарь
    # ищет службу DugaDesk.
    R.append(Sub(YML,
                 "          pushd ./res/msi\n"
                 "          python preprocess.py --arp -d ../../rustdesk",
                 "          pushd ./res/msi\n"
                 "          # РЕБРЕНДИНГ: preprocess.py ищет в dist-каталоге ровно <app-name>.exe\n"
                 "          # и запускает его (--version). flutter кладёт rustdesk.exe, поэтому\n"
                 "          # переименовываем прямо перед сборкой MSI.\n"
                 "          mv ../../rustdesk/rustdesk.exe ../../rustdesk/%s.exe\n"
                 "          # --app-name задаёт имя службы, ключи реестра и каталог установки\n"
                 "          # внутри MSI. Без него MSI поставит службу RustDesk.\n"
                 "          python preprocess.py --arp --app-name %s -d ../../rustdesk" % (APP, APP),
                 1,
                 u"Без переименования preprocess.py не найдёт %s.exe и MSI не "
                 u"соберётся. Без --app-name MSI поставит службу RustDesk, а "
                 u"бинарь будет искать службу %s." % (APP, APP), S,
                 # Маркер намеренно короткий: правило 14.6 дописывает в эту же
                 # строку --manufacturer, и маркер по полному тексту перестал бы
                 # находиться — повторный прогон счёл бы файл разъехавшимся.
                 marker="preprocess.py --arp --app-name %s" % APP))

    # 8.6 Отключаем сборки, которые владелец не выпускает: sciter (Windows и
    # Linux) и iOS. Собираем только flutter-варианты.
    # Побочный эффект: job publish_unsigned зависит от build-for-windows-sciter
    # и поэтому тоже будет пропущен — это ожидаемо, он публикует неподписанный
    # архив, который в релиз не входит.
    R.append(Sub(YML,
                 "  build-for-windows-sciter:\n    name:",
                 "  build-for-windows-sciter:\n"
                 "    # РЕБРЕНДИНГ: sciter-вариант не выпускаем, собираем только flutter.\n"
                 "    if: false\n    name:", 1,
                 u"Лишняя сборка тратит около часа времени раннера и кладёт в "
                 u"релиз файл, который никто не ставит.", S))
    R.append(Sub(YML,
                 "  build-rustdesk-ios:\n    if: ${{ inputs.upload-artifact }}",
                 "  build-rustdesk-ios:\n"
                 "    # РЕБРЕНДИНГ: iOS не выпускаем.\n    if: false", 1,
                 u"iOS-сборка требует своих сертификатов и не входит в поставку.", S))
    R.append(Sub(YML,
                 "  build-rustdesk-linux-sciter:\n    if: ${{ inputs.upload-artifact }}",
                 "  build-rustdesk-linux-sciter:\n"
                 "    # РЕБРЕНДИНГ: sciter-вариант не выпускаем.\n    if: false", 1,
                 u"То же для Linux-sciter.", S))

    # -----------------------------------------------------------------
    # 9. FLATPAK и APPIMAGE
    # -----------------------------------------------------------------
    S = u"9. Flatpak / AppImage"

    R.append(Rename("flatpak/rustdesk.json", "flatpak/%s.json" % BIN,
                    u"Манифест flatpak. Имя должно совпасть с тем, что зовёт CI.", S))
    R.append(Rename("flatpak/com.rustdesk.RustDesk.metainfo.xml",
                    "flatpak/%s.metainfo.xml" % FID,
                    u"Файл метаданных AppStream. Его имя обязано совпадать с id "
                    u"приложения (%s), иначе flatpak-builder ругается и бандл "
                    u"не собирается." % FID, S))

    FLATPAK_PAIRS = [("com.rustdesk.RustDesk", FID), ("com.rustdesk", ORG),
                     ("rustdesk.com", DOM), ("RustDesk", APP), ("rustdesk", BIN)]
    R.append(WordSub("flatpak/%s.json" % BIN, FLATPAK_PAIRS,
                     u"id, command, rename-desktop-file, rename-icon, имя "
                     u"распаковываемого deb и имя файла метаданных. Любое "
                     u"несовпадение — и flatpak собирается, но приложение "
                     u"запускается по несуществующему пути.", S,
                     protect=("github.com/", "flathub")))
    R.append(WordSub("flatpak/%s.metainfo.xml" % FID, FLATPAK_PAIRS,
                     u"id, launchable, имя и описание в магазине приложений. "
                     u"Ссылки на апстрим-репозиторий (github.com, ko-fi) "
                     u"намеренно оставлены как есть — это ссылки на проект, "
                     u"из которого сделан форк, а не на наш продукт.", S,
                     protect=("github.com/", "ko-fi.com/")))

    APPIMG_PAIRS = [("rustdesk.com", DOM), ("RustDesk", APP), ("rustdesk", BIN)]
    for arch in ("x86_64", "aarch64"):
        path = "appimage/AppImageBuilder-%s.yml" % arch
        R.append(WordSub(path, APPIMG_PAIRS,
                         u"app_info (id/name/icon) и, главное, exec: "
                         u"usr/share/%s/%s. Без правки AppImage запускает "
                         u"несуществующий путь и падает сразу после старта." % (BIN, BIN),
                         S, protect=("github.com/", "rustdesk-org")))
        R.append(Sub(path, "    version: 1.4.9", "    version: %s" % PVER, 1,
                     u"appimage-builder собирает имя файла из name+version. CI "
                     u"публикует по маске %s-%s-*.AppImage; при другой версии "
                     u"в рецепте маска не совпадёт и релиз выйдет без AppImage."
                     % (BIN, PVER), S))

    # -----------------------------------------------------------------
    # 10. ИКОНКИ
    # -----------------------------------------------------------------
    # Сами файлы иконок в слое НЕ лежат — их приносит владелец и кладёт в
    # brand/assets/. Отсутствие файла даёт ПРЕДУПРЕЖДЕНИЕ (а не тихий пропуск),
    # потому что молча собранная сборка со старой иконкой — худший исход:
    # выяснится это только у пользователя.
    S = u"10. Иконки"
    ICONS = [
        ("icon.ico", "res/icon.ico",
         u"Иконка Windows-бинаря (подставляется winres при сборке Cargo)."),
        ("icon.ico", "flutter/windows/runner/resources/app_icon.ico",
         u"Иконка окна flutter-раннера и ярлыка Windows."),
        ("tray-icon.ico", "res/tray-icon.ico",
         u"Иконка в системном трее Windows."),
        ("icon.png", "res/icon.png",
         u"Базовая растровая иконка, из неё res/gen_icon.sh печёт остальные размеры."),
        ("icon.png", "flutter/assets/icon.png",
         u"Иконка внутри интерфейса (loadIcon в common.dart)."),
        ("icon.svg", "flutter/assets/icon.svg",
         u"Векторный вариант той же иконки для интерфейса."),
        ("logo.png", "flutter/assets/logo.png",
         u"Логотип на стартовом экране."),
        ("mac-icon.png", "res/mac-icon.png",
         u"Исходник иконки macOS."),
        # Иконка в строке меню macOS. Читается ровно одна:
        # src/tray.rs:36 делает include_bytes!("../res/mac-tray-dark-x2.png")
        # с пометкой «use as template, so color is not important» — macOS в
        # шаблонном режиме берёт только альфу и перекрашивает силуэт под тему.
        # Поэтому нужен монохромный силуэт на прозрачном фоне; цветная иконка
        # там превратится в сплошной квадрат.
        ("mac-tray-dark-x2.png", "res/mac-tray-dark-x2.png",
         u"Иконка в строке меню macOS — единственная, которую читает код "
         u"(src/tray.rs, include_bytes!). Без замены в меню-баре висит "
         u"кольцо RustDesk всё время работы службы."),
        ("mac-tray-light-x2.png", "res/mac-tray-light-x2.png",
         u"Светлый вариант той же иконки. Лежит в дереве апстрима, но ни "
         u"одной строкой кода не читается — заменяем, чтобы в репозитории "
         u"не осталось чужой графики."),
        ("AppIcon.icns", "flutter/macos/Runner/AppIcon.icns",
         u"Иконка macOS-бандла. ВНИМАНИЕ: в этом дереве иконка лежит одним "
         u"файлом .icns, каталога Assets.xcassets/AppIcon.appiconset нет."),
        ("scalable.svg", "res/scalable.svg",
         u"Векторная иконка для hicolor (Linux)."),
        ("32x32.png", "res/32x32.png", u"Иконка 32x32 для Linux/AppImage."),
        ("64x64.png", "res/64x64.png", u"Иконка 64x64 для Linux/AppImage."),
        ("128x128.png", "res/128x128.png", u"Иконка 128x128 для Linux/AppImage."),
        ("128x128@2x.png", "res/128x128@2x.png",
         u"Иконка 256x256 — её build.py кладёт в hicolor как %s.png." % BIN),
    ]
    ANDROID_DPI = ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi")
    # ic_stat_logo — иконка в строке состояния Android. Её видно всё время,
    # пока работает служба. Android красит её сам (setSmallIcon, R.mipmap.
    # ic_stat_logo в MainService.kt), поэтому файл обязан быть монохромным
    # силуэтом на прозрачном фоне: цветная картинка станет белым пятном.
    for dpi in ANDROID_DPI:
        for name in ("ic_launcher.png", "ic_launcher_round.png",
                     "ic_launcher_foreground.png", "ic_stat_logo.png"):
            ICONS.append((
                "android/mipmap-%s/%s" % (dpi, name),
                "flutter/android/app/src/main/res/mipmap-%s/%s" % (dpi, name),
                u"Иконка Android (%s, %s)." % (dpi, name)))
    for src_name, dst, why in ICONS:
        R.append(Asset(asset(src_name), src_name, dst, why, S))


    # -----------------------------------------------------------------
    # 11. НАЙДЕНО ПРИ СВЕРКЕ С ДЕРЕВОМ (в исходном списке правок не было)
    # -----------------------------------------------------------------
    # Всё ниже обнаружено при проверке результата командой grep -ri rustdesk
    # по уже обработанному дереву. Каждая позиция — либо прямая поломка
    # пакета, либо имя, которое видит пользователь.
    S = u"11. Найдено при сверке"

    R.append(WordSub("res/pacman_install", PAIRS,
                     u"ПОЛОМКА Arch-пакета: скрипт копирует "
                     u"/usr/share/rustdesk/files/rustdesk.service и делает "
                     u"`systemctl enable rustdesk`. После переименования "
                     u"PKGBUILD и юнита этих файлов уже нет — установка "
                     u"проходит, служба не поднимается.", S, protect=PROTECT))

    R.append(WordSub("res/osx-dist.sh", PAIRS,
                     u"Ручной скрипт выпуска macOS вне CI: ссылается на "
                     u"RustDesk.app, которого после смены PRODUCT_NAME не "
                     u"существует. Скрипт молча соберёт пустой .dmg.", S,
                     protect=PROTECT))

    MYAPP = "flutter/linux/my_application.cc"
    R.append(Sub(MYAPP, 'gtk_icon_theme_load_icon(theme, "rustdesk"',
                 'gtk_icon_theme_load_icon(theme, "%s"' % BIN, 1,
                 u"Иконка окна на Linux ищется в системном кэше по имени. "
                 u"Пакет теперь кладёт %s.png — со старым именем окно "
                 u"остаётся с иконкой по умолчанию." % BIN, S))
    R.append(Sub(MYAPP, 'gtk_header_bar_set_title(header_bar, "rustdesk");',
                 'gtk_header_bar_set_title(header_bar, "%s");' % APP, 1,
                 u"Заголовок окна в GNOME. Без правки пользователь Linux видит "
                 u"'rustdesk' в шапке окна.", S))
    R.append(Sub(MYAPP, 'gtk_window_set_title(window, "rustdesk");',
                 'gtk_window_set_title(window, "%s");' % APP, 1,
                 u"Заголовок окна вне GNOME — то же самое.", S))
    # НЕ ТРОГАЕМ в my_application.cc и flutter_window.cpp строки каналов
    # org.rustdesk.rustdesk/* — они обязаны побайтово совпадать с Dart-стороной.

    PLIST = "flutter/macos/Runner/Info.plist"
    R.append(Sub(PLIST, "<string>com.carriez.rustdesk</string>",
                 "<string>%s</string>" % AID, 1,
                 u"CFBundleURLName — имя схемы в реестре macOS.", S))
    R.append(Sub(PLIST, "<string>rustdesk</string>", "<string>%s</string>" % SCHEME, 1,
                 u"CFBundleURLSchemes: без правки macOS отдаёт ссылки "
                 u"%s:// нашему приложению, а ссылки %s:// — нет." % ("rustdesk", SCHEME), S))

    R.append(Sub("flutter/macos/Runner/Base.lproj/MainMenu.xib",
                 'customModule="RustDesk"', 'customModule="%s"' % APP, 2,
                 u"Имя Swift-модуля в nib. Модуль называется по PRODUCT_NAME; "
                 u"после смены имени nib будет искать классы в несуществующем "
                 u"модуле RustDesk — приложение падает при старте окна.", S))
    R.append(Sub("flutter/macos/Runner.xcodeproj/project.pbxproj",
                 "RustDesk.app", "%s.app" % APP, 4,
                 u"Ссылка на продукт сборки в проекте Xcode.", S))
    R.append(Sub("flutter/macos/Runner.xcodeproj/xcshareddata/xcschemes/Runner.xcscheme",
                 '"RustDesk.app"', '"%s.app"' % APP, 4,
                 u"BuildableName в схеме сборки: при несовпадении xcodebuild "
                 u"не находит продукт.", S))

    KMAIN = "flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/MainService.kt"
    R.append(Sub(KMAIN, 'const val DEFAULT_NOTIFY_TITLE = "RustDesk"',
                 'const val DEFAULT_NOTIFY_TITLE = "%s"' % APP, 1,
                 u"Заголовок постоянного уведомления Android — самая заметная "
                 u"надпись во время сеанса.", S))
    R.append(Sub(KMAIN, 'val channelName = "RustDesk Service"',
                 'val channelName = "%s Service"' % APP, 1,
                 u"Имя канала уведомлений в системных настройках телефона.", S))
    R.append(Sub(KMAIN, 'description = "RustDesk Service Channel"',
                 'description = "%s Service Channel"' % APP, 1,
                 u"Описание канала уведомлений там же.", S))
    R.append(Sub("flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/BootReceiver.kt",
                 '"RustDesk is Open"', '"%s is Open"' % APP, 1,
                 u"Всплывающее сообщение при автозапуске после перезагрузки.", S))
    # НЕ ТРОГАЕМ в Kotlin: channelId = "RustDesk" и "RustDeskVD" — внутренние
    # идентификаторы; translate("Show RustDesk") — КЛЮЧ перевода, при замене
    # перевод не найдётся и в меню появится сырой английский ключ.

    # Мобильный интерфейс: те же ссылки на домен вендора, что и на десктопе.
    # В исходном списке правок были только desktop-страницы, но на телефоне
    # раздел "О программе" и кнопка загрузки ведут туда же.
    MSET = "flutter/lib/mobile/pages/settings_page.dart"
    R.append(Sub(MSET, "const url = 'https://rustdesk.com/';",
                 "const url = 'https://%s/';" % DOM, 2,
                 u"Ссылка на сайт вендора в разделе 'О программе' (две "
                 u"страницы настроек).", S))
    R.append(Sub(MSET, "Text('rustdesk.com',", "Text('%s'," % DOM, 2,
                 u"Видимая подпись ссылки — пользователь читает чужой домен.", S))
    R.append(Sub(MSET, "launchUrlString('https://rustdesk.com/privacy.html'),",
                 "launchUrlString('https://%s/privacy.html')," % DOM, 1,
                 u"Политика конфиденциальности в мобильных настройках.", S))
    R.append(Sub("flutter/lib/mobile/pages/connection_page.dart",
                 "final url = 'https://rustdesk.com/download';",
                 "final url = 'https://%s/download';" % DOM, 1,
                 u"Кнопка загрузки на мобильной странице подключения.", S))

    # Добито по результатам grep уже по обработанному дереву: три места в
    # build.py, которые не попали ни под один из якорей выше.
    R.append(Sub("build.py", 'f"../rustdesk-{version}.dmg"',
                 'f"../%s-{version}.dmg"' % BIN, 1,
                 u"Итоговое имя .dmg в flutter-ветке macOS. Без правки "
                 u"`build.py --flutter` на macOS выдаёт образ с именем "
                 u"rustdesk-<версия>.dmg, хотя внутри уже %s.app." % APP,
                 u"11. Найдено при сверке"))
    R.append(Sub("build.py", "cp res/rustdesk.desktop tmpdeb",
                 "cp res/%s.desktop tmpdeb" % BIN, 1,
                 u"Sciter-ветка Linux копирует res/rustdesk.desktop, которого "
                 u"после переименования не существует — сборка падает на "
                 u"отсутствующем файле.", u"11. Найдено при сверке"))
    R.append(Sub("build.py", "cp res/rustdesk-link.desktop tmpdeb",
                 "cp res/%s-link.desktop tmpdeb" % BIN, 1,
                 u"То же для обработчика ссылок.", u"11. Найдено при сверке"))

    # -----------------------------------------------------------------
    # 12. ПРИМЕНЕНИЕ СЛОЯ В РАННЕРЕ (обязательно из-за подмодуля)
    # -----------------------------------------------------------------
    # libs/hbb_common — подмодуль чужого репозитория. Его правки НЕЛЬЗЯ
    # закоммитить: в основном репозитории git хранит только указатель на
    # ревизию. Значит в раннере ядро всегда приезжает апстримовым, и слой
    # обязан применяться там повторно. Всё остальное дерево к этому моменту
    # уже применено и закоммичено — повторный прогон видит его как готовое
    # и трогает только подмодуль.
    S = u"12. Применение слоя в CI"

    STEP = [
        u"      - name: Применить слой ребрендинга DugaDesk",
        u"        # libs/hbb_common — git-подмодуль ЧУЖОГО репозитория",
        u"        # (rustdesk/hbb_common). Его правки невозможно закоммитить в наш",
        u"        # репозиторий: git хранит только указатель на ревизию, поэтому после",
        u"        # checkout подмодуль всегда приезжает с апстрим-пина — APP_NAME=\"RustDesk\",",
        u"        # сервер rs-ny.rustdesk.com и чужой публичный ключ.",
        u"        # Без этого шага релиз соберётся под именем RustDesk и уйдёт на чужой",
        u"        # сервер, а обнаружится это только на готовом релизе (1 ч 40 мин сборки).",
        u"        # Остальное дерево уже применено в коммите — прогон его не меняет.",
        u"        shell: bash",
        u"        env:",
        u"          # Второй пояс к перенастройке потоков внутри apply.py.",
        u"          # На windows-раннере stdout питона по умолчанию cp1252, и печать",
        u"          # русских заголовков валит шаг с UnicodeEncodeError за 1 секунду —",
        u"          # именно так упали обе Windows-сборки в прогоне #63.",
        u"          PYTHONUTF8: \"1\"",
        u"          PYTHONIOENCODING: \"utf-8\"",
        u"        run: |",
        u"          # На windows-раннере в git-bash есть python3 (этот же workflow уже",
        u"          # вызывает им res/job.py и libs/portable/generate.py), но берём",
        u"          # python как запасной вариант, чтобы шаг не зависел от образа.",
        u"          if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi",
        u"          \"$PY\" brand/apply.py --apply",
    ]
    MARKER = u"brand/apply.py --apply"

    # Job'ы, куда шаг вставляется. Список сверен с содержимым workflow:
    #   * генерация bridge и все сборки, компилирующие Rust (то есть
    #     подтягивающие hbb_common), плюс сборка apk, которой нужны наши
    #     Android-ресурсы;
    #   * sciter- и iOS-job'ы пропущены: они отключены (if: false);
    #   * build-appimage и build-flatpak пропущены сознательно — см. ниже.
    for job, why in [
        ("build-for-windows-flutter",
         u"Здесь идёт cargo build, то есть компилируется hbb_common из "
         u"подмодуля. Без шага exe соберётся с APP_NAME=RustDesk."),
        ("build-for-macOS",
         u"То же: cargo build под macOS."),
        ("build-rustdesk-android",
         u"То же: cargo ndk собирает librustdesk.so из подмодуля."),
        ("build-rustdesk-android-universal",
         u"Rust здесь не компилируется (библиотеки приходят артефактами), но "
         u"job собирает apk из ресурсов дерева. Шаг оставлен как страховка: "
         u"если слой забыли закоммитить, apk всё равно выйдет брендированным."),
        ("build-rustdesk-linux",
         u"cargo build внутри контейнера, дерево монтируется с хоста — шаг "
         u"выполняется на хосте до запуска контейнера."),
    ]:
        R.append(InsertStep(YML, job, STEP, MARKER, why, S))

    # bridge.yml вызывается из flutter-build.yml как job generate-bridge
    # (`uses: ./.github/workflows/bridge.yml`), поэтому вставлять шаг надо в
    # сам bridge.yml — в его job generate_bridge.
    R.append(InsertStep(".github/workflows/bridge.yml", "generate_bridge",
                        STEP, MARKER,
                        u"Генерация bridge разворачивает крейт через "
                        u"cargo-expand, то есть компилирует hbb_common из "
                        u"подмодуля. На содержимое generated_bridge.dart "
                        u"бренд не влияет, но собирается дерево целиком, и "
                        u"состояние подмодуля обязано быть одинаковым во всех "
                        u"job'ах — иначе кеш cargo будет собран с чужими "
                        u"константами.", S))

    # НЕ вставляем шаг:
    #   build-RustDeskTempTopMostWindow — вызывает
    #     third-party-RustDeskTempTopMostWindow.yml, который НЕ делает checkout
    #     нашего репозитория вообще: он клонирует чужой проект
    #     rustdesk-org/RustDeskTempTopMostWindow и собирает msbuild'ом.
    #     Каталога brand/ там просто нет, шаг упал бы на первой строке.
    #   build-appimage, build-flatpak — собирают из ГОТОВОГО .deb, скачанного
    #     артефактом у build-rustdesk-linux, плюс файлов основного репозитория
    #     (appimage/*.yml, flatpak/*.json, res/*.png). Rust там не собирается,
    #     подмодуль не участвует, а файлы основного репозитория уже применены
    #     в коммите. Лишний прогон ничего бы не изменил.

    # -----------------------------------------------------------------
    # 13. .gitignore: разрешить иконки бренда
    # -----------------------------------------------------------------
    # В корневом .gitignore стоят правила *png / *svg / *jpg (строки 17-19).
    # Из-за них `git add` МОЛЧА пропускает иконки: коммит уходит без них,
    # сборка выходит со старой иконкой RustDesk, и причину ищут долго —
    # ошибки git не показывает вообще. Файлы, которые уже в индексе (res/*.png,
    # android mipmap), от этого не страдают, а вот новые (flutter/assets/icon.png,
    # flutter/assets/logo.png) и весь brand/assets — страдают.
    # Добавляем точечные отрицания в конец файла: они действуют только на наши
    # пути и не трогают остальные правила апстрима.
    S = u"13. .gitignore"
    R.append(Sub(".gitignore",
                 "vcpkg_installed\nflutter/lib/generated_plugin_registrant.dart\n"
                 "libsciter.dylib\nflutter/web/",
                 "vcpkg_installed\nflutter/lib/generated_plugin_registrant.dart\n"
                 "libsciter.dylib\nflutter/web/\n"
                 "\n"
                 "# === Слой ребрендинга %s (brand/apply.py) ===\n"
                 "# Выше стоят правила *png / *svg / *jpg. Без отрицаний ниже\n"
                 "# `git add` молча пропустил бы иконки бренда, и сборка вышла бы\n"
                 "# со старой иконкой RustDesk — без единого сообщения об ошибке.\n"
                 "!brand/assets/**\n"
                 "!flutter/assets/icon.png\n"
                 "!flutter/assets/logo.png\n" % APP,
                 1,
                 u"Без этой правки иконки слоя не попадают в репозиторий: "
                 u"git add пропускает их молча, ошибки нет, а собранное "
                 u"приложение остаётся с иконкой RustDesk.", S,
                 marker="# === Слой ребрендинга %s (brand/apply.py) ===" % APP,
                 marker_wins=True))

    # -----------------------------------------------------------------
    # 14. ИСПРАВЛЕНИЯ ПО ИТОГАМ ПРИЁМКИ
    # -----------------------------------------------------------------
    S = u"14. Правки по приёмке"

    # --- 14.1 БЛОКЕР: глоб переименования dmg -------------------------
    # В macOS-job две ветки: неподписанная всегда делает
    # <bin>-<версия>-<arch>.dmg, подписанная (при наличии сертификата)
    # сначала стирает *.dmg и делает <bin>-<версия>.dmg БЕЗ архитектуры.
    # Шаг "Rename rustdesk" дописывает суффикс архитектуры, а публикация
    # ищет файл по маске с этим суффиксом. Обе маски остались на старом
    # имени: под `bash -e` несовпавший глоб отдаётся в mv литералом, mv
    # падает с "No such file or directory" и валит весь macOS-job при
    # upload-artifact=true — dmg не публикуется вовсе.
    # Шаг переименования НЕ убираем: без него подписанные dmg обеих
    # архитектур называются одинаково и второй job затирает первый в релизе.
    R.append(Sub(YML, "for name in rustdesk*??.dmg; do",
                 "for name in %s*??.dmg; do" % BIN, 1,
                 u"Глоб не совпадёт с dugadesk-*.dmg, mv получит литерал и "
                 u"уронит macOS-job целиком — .dmg в релиз не попадёт.", S))
    R.append(Sub(YML, "rustdesk*-${{ matrix.job.arch }}.dmg",
                 "%s*-${{ matrix.job.arch }}.dmg" % BIN, 1,
                 u"Маска публикации dmg. Со старой маской релиз выходит без "
                 u"образов macOS, хотя job зелёный.", S))

    # --- 14.2 БЛОКЕР: build-flatpak зависит от отключённого job --------
    # GitHub Actions: job со skipped-зависимостью тоже skipped, если в его
    # `if` нет always(). Мы отключили build-rustdesk-linux-sciter, значит
    # flatpak молча перестал бы собираться.
    R.append(Sub(YML,
                 "    needs:\n      - build-rustdesk-linux\n"
                 "      - build-rustdesk-linux-sciter\n",
                 "    needs:\n      - build-rustdesk-linux\n", 1,
                 u"Без правки build-flatpak пропускается вслед за отключённым "
                 u"sciter-job'ом и .flatpak в релизе не появляется.", S,
                 marker="      - build-rustdesk-linux\n    runs-on:"))

    # Элемент матрицы, собиравший flatpak из sciter-deb, которого больше нет.
    R.append(Sub(YML,
                 "          - {\n"
                 "              target: x86_64-unknown-linux-gnu,\n"
                 "              distro: ubuntu22.04,\n"
                 "              on: ubuntu-22.04,\n"
                 "              arch: x86_64,\n"
                 "              suffix: \"-sciter\",\n"
                 "            }\n",
                 "", 1,
                 u"Эта комбинация матрицы скачивает deb с суффиксом -sciter, "
                 u"которого никто не собирает: job упал бы на download-artifact.",
                 S, marker="suffix: \"\",\n            }\n          - {\n"
                           "              target: aarch64-unknown-linux-gnu,"))

    # --- 14.3 publish_unsigned ---------------------------------------
    # Отключаем целиком, а не чиним зависимость. Обоснование: job публикует
    # НЕподписанный tar.gz, который в поставку владельца не входит, и внутрь
    # он собирает windows-x86 — 32-битную sciter-сборку, которой у нас больше
    # нет. Даже без зависимости от sciter-job'а артефакт получился бы
    # заведомо неполным.
    # ВАЖНО: у job уже есть свой `if`, поэтому его именно ЗАМЕНЯЕМ. Вставка
    # второго ключа `if` дала бы дубликат ключа в YAML — GitHub Actions
    # отвергает такой workflow целиком, и не собралось бы вообще ничего.
    # Заодно убираем зависимость от отключённого sciter-job'а, чтобы в графе
    # не осталось ссылок на пропускаемые job'ы.
    R.append(Sub(YML,
                 "  publish_unsigned:\n"
                 "    needs:\n"
                 "      - build-for-macOS\n"
                 "      - build-for-windows-flutter\n"
                 "      - build-for-windows-sciter\n"
                 "    runs-on: ubuntu-latest\n"
                 "    if: ${{ inputs.upload-artifact }}\n",
                 "  publish_unsigned:\n"
                 "    # РЕБРЕНДИНГ: неподписанный сводный архив не выпускаем.\n"
                 "    # Он собирался в том числе из windows-x86 — 32-битной\n"
                 "    # sciter-сборки, которой у нас больше нет, поэтому архив\n"
                 "    # получился бы заведомо неполным. Отключаем job целиком,\n"
                 "    # а не чиним зависимость.\n"
                 "    needs:\n"
                 "      - build-for-macOS\n"
                 "      - build-for-windows-flutter\n"
                 "    runs-on: ubuntu-latest\n"
                 "    if: false\n", 1,
                 u"Иначе job молча пропускается по цепочке зависимостей от "
                 u"отключённого sciter-job'а: в релизе нет обещанного архива, "
                 u"а в логе — ни одной ошибки.", S,
                 marker="  publish_unsigned:\n    # РЕБРЕНДИНГ"))

    # --- 14.4 macOS bundle id в project.pbxproj ------------------------
    # PRODUCT_BUNDLE_IDENTIFIER, заданный на уровне таргета в pbxproj,
    # ПЕРЕКРЫВАЕТ значение из xcconfig. Доказательство прямо в апстриме:
    # xcconfig говорит com.carriez.flutterHbb, а собранный RustDesk.app имеет
    # id com.carriez.rustdesk.
    # ВНИМАНИЕ (исправление прежней неверной записи): имена LaunchDaemon и
    # LaunchAgent строятся НЕ из bundle id, а из ORG — см. get_full_name()
    # в src/common.rs (format!("{}.{}", ORG, APP_NAME)) и Config::path()
    # в hbb_common (ProjectDirs::from("", ORG, APP_NAME)). Bundle id влияет
    # на другое: запись разрешений macOS (экран, специальные возможности),
    # ключ AssociatedBundleIdentifiers в plist и подстановку
    # com.carriez.rustdesk в correct_app_name(). За имена службы и каталог
    # настроек отвечает правило 14.12 ниже.
    R.append(Sub("flutter/macos/Runner.xcodeproj/project.pbxproj",
                 "PRODUCT_BUNDLE_IDENTIFIER = com.carriez.rustdesk;",
                 "PRODUCT_BUNDLE_IDENTIFIER = %s;" % MBID, 3,
                 u"Правка одного xcconfig не действует: значение на уровне "
                 u"таргета сильнее. Без неё bundle id, LaunchDaemon и запись "
                 u"разрешений остаются общими с RustDesk.", S))

    # --- 14.5 Android: фон адаптивной иконки --------------------------
    # На Android 8+ лаунчер собирает иконку из пары background+foreground
    # (mipmap-anydpi-v26/ic_launcher.xml). Фон апстрима белый, а в нашем
    # foreground белые элементы — на белом фоне они исчезают, остаются только
    # оранжевые дуги. Красим фон в цвет бренда: правильнее, чем вшивать
    # плашку в foreground, потому что форму адаптивной иконки система
    # обрезает сама, и вшитая плашка получила бы срезанные углы.
    R.append(Sub("flutter/android/app/src/main/res/values/ic_launcher_background.xml",
                 '<color name="ic_launcher_background">#ffffff</color>',
                 '<color name="ic_launcher_background">#121212</color>', 1,
                 u"Без правки на Android 8+ от иконки видны только оранжевые "
                 u"дуги: белые буква и монитор сливаются с белым фоном.", S))

    # --- 14.6 Издатель (company) --------------------------------------
    # Параметр company из brand.toml раньше нигде не использовался, и
    # издателем во всех системных местах оставался Purslane Tech Pte. Ltd.
    COMPANY = b["company"]
    R.append(Sub(RC, 'VALUE "CompanyName", "Purslane Tech Pte. Ltd." "\\0"',
                 'VALUE "CompanyName", "%s" "\\0"' % COMPANY, 1,
                 u"Поле 'Организация' в свойствах exe. Без правки Windows "
                 u"показывает чужого издателя.", S))
    R.append(Sub(RC,
                 'VALUE "LegalCopyright", "Copyright \u00a9 2026 Purslane Tech Pte. Ltd. '
                 'All rights reserved." "\\0"',
                 'VALUE "LegalCopyright", "Copyright \u00a9 2026 %s. All rights '
                 'reserved." "\\0"' % COMPANY, 1,
                 u"Копирайт в ресурсах exe.", S))
    R.append(Sub("Cargo.toml",
                 'LegalCopyright = "Copyright \u00a9 2026 Purslane Tech Pte. Ltd. '
                 'All rights reserved."',
                 'LegalCopyright = "Copyright \u00a9 2026 %s. All rights reserved."' % COMPANY,
                 1, u"Копирайт, который winres вшивает в бинарь Cargo.", S))
    R.append(Sub(XC,
                 "PRODUCT_COPYRIGHT = Copyright \u00a9 2026 Purslane Tech Pte. Ltd. "
                 "All rights reserved.",
                 "PRODUCT_COPYRIGHT = Copyright \u00a9 2026 %s. All rights reserved." % COMPANY,
                 1, u"Строка копирайта в 'О программе' на macOS.", S))
    # MSI: издатель в списке 'Установка и удаление программ'.
    R.append(Sub(YML,
                 "          python preprocess.py --arp --app-name %s -d ../../rustdesk" % APP,
                 "          # --manufacturer задаёт издателя в 'Установка и удаление программ'\n"
                 "          # и поле Publisher в реестре; по умолчанию preprocess.py ставит Purslane.\n"
                 "          python preprocess.py --arp --app-name %s --manufacturer %s -d ../../rustdesk"
                 % (APP, COMPANY), 1,
                 u"Без --manufacturer в списке установленных программ издателем "
                 u"значится Purslane Tech Pte. Ltd.", S,
                 marker="--manufacturer %s" % COMPANY))

    # --- 14.7 Ресурсы самораспаковывающегося установщика ---------------
    # libs/portable — крейт rustdesk-portable-packer, из которого печётся
    # ИТОГОВЫЙ файл dugadesk-<версия>-<arch>.exe. Именно его пользователь
    # скачивает первым, и до этой правки в его свойствах стояли ProductName
    # "RustDesk", описание "RustDesk Remote Desktop" и копирайт Purslane.
    # Найдено при поиске остатков Purslane по дереву.
    PORT = "libs/portable/Cargo.toml"
    R.append(Sub(PORT,
                 'LegalCopyright = "Copyright \u00a9 2026 Purslane Tech Pte. Ltd. '
                 'All rights reserved."',
                 'LegalCopyright = "Copyright \u00a9 2026 %s. All rights reserved."' % COMPANY,
                 1, u"Копирайт в свойствах скачиваемого установщика.", S))
    R.append(Sub(PORT, 'ProductName = "RustDesk"', 'ProductName = "%s"' % APP, 1,
                 u"Имя продукта в свойствах установщика: пользователь видит "
                 u"RustDesk в диалоге контроля учётных записей Windows.", S))
    R.append(Sub(PORT, 'OriginalFilename = "rustdesk.exe"',
                 'OriginalFilename = "%s.exe"' % APP, 1,
                 u"Оригинальное имя файла в ресурсах установщика.", S))
    R.append(Sub(PORT, 'FileDescription = "RustDesk Remote Desktop"',
                 'FileDescription = "%s Remote Desktop"' % APP, 1,
                 u"Описание файла — колонка 'Описание' в Проводнике и "
                 u"Диспетчере задач.", S))

    # --- 14.8 Ночные сборки по расписанию отключены -------------------
    # Решение владельца. Причины:
    #   * поток чужих PR нулевой — дерево меняется по событию (merge апстрима
    #     или правка слоя), а не по расписанию, так что ловить регрессии
    #     каждую ночь нечего;
    #   * каждая ночь перезаписывала бы релиз с тегом nightly — для продукта,
    #     который отдают заказчику, это шум в списке релизов;
    #   * один прогон — 17 параллельных job'ов, из них два macOS, а macOS-минуты
    #     GitHub тарифицирует с множителем 10x; Windows — 2x;
    #   * после merge апстрима с разъездом якорей сборка падала бы в 00:00 без
    #     свидетелей и засоряла историю.
    # Блок schedule ЗАКОММЕНТИРОВАН, а не удалён: вернуть расписание после
    # первого зелёного релиза — снять решётки, ничего не вспоминая.
    # workflow_dispatch остаётся нетронутым: первый прогон запускается именно
    # им, в тег nightly. Ключ `on:` не остаётся пустым — workflow_dispatch под
    # ним сохранён, иначе GitHub отверг бы workflow целиком.
    R.append(Sub(".github/workflows/flutter-nightly.yml",
                 "on:\n"
                 "  schedule:\n"
                 "    # schedule build every night\n"
                 "    - cron: \"0 0 * * *\"\n"
                 "  workflow_dispatch:\n",
                 "on:\n"
                 "  # РЕБРЕНДИНГ: ночные сборки по расписанию отключены.\n"
                 "  # Поток чужих PR нулевой, дерево меняется по событию, а не по\n"
                 "  # расписанию; каждая ночь перезаписывала бы релиз nightly и жгла\n"
                 "  # квоту Actions (macOS тарифицируется 10x, Windows 2x), а падение\n"
                 "  # после merge апстрима случалось бы в 00:00 без свидетелей.\n"
                 "  # Чтобы вернуть расписание — снять решётки с трёх строк ниже.\n"
                 "  # schedule:\n"
                 "  #   # schedule build every night\n"
                 "  #   - cron: \"0 0 * * *\"\n"
                 "  workflow_dispatch:\n",
                 1,
                 u"Без правки полный набор из 17 job'ов запускается каждую ночь: "
                 u"жжёт минуты Actions (macOS 10x), перезаписывает релиз nightly "
                 u"и падает в 00:00 без свидетелей после merge апстрима. "
                 u"workflow_dispatch сохранён — ручной запуск в тег nightly "
                 u"остаётся рабочим.", S,
                 marker="# РЕБРЕНДИНГ: ночные сборки по расписанию отключены."))

    # --- 14.9 БЛОКЕР: переименование dmg только в подписанной ветви ----
    # В macOS-job две ветви создания образа:
    #   * "create unsigned dmg" — выполняется ВСЕГДА при UPLOAD_ARTIFACT и уже
    #     кладёт файл С архитектурой: <bin>-<версия>-<arch>.dmg;
    #   * "Codesign app and create signed dmg" — только при наличии секрета
    #     MACOS_P12_BASE64; она делает `rm -rf *.dmg` и создаёт файл БЕЗ
    #     архитектуры: <bin>-<версия>.dmg.
    # Шаг переименования дописывает суффикс архитектуры и до этой правки шёл
    # безусловно. У владельца сертификата Apple нет, значит работает первая
    # ветвь, и суффикс дописывался ВТОРОЙ раз:
    #     dugadesk-1.0.0-x86_64-x86_64.dmg
    # Проверено буквальным прогоном той же логики в bash для четырёх случаев
    # (подписанная/неподписанная x x86_64/aarch64).
    # Лечение: выполнять переименование только тогда, когда отработала
    # подписанная ветвь. Тогда обе ветви дают ровно <bin>-<версия>-<arch>.dmg.
    # Убрать шаг совсем нельзя: в подписанной ветви без него оба job'а
    # (x86_64 и aarch64) выложат файл с одинаковым именем и затрут друг друга.
    R.append(Sub(YML,
                 "      - name: Rename rustdesk\n"
                 "        if: env.UPLOAD_ARTIFACT == 'true'\n"
                 "        run: |\n"
                 "          for name in %s*??.dmg; do\n" % BIN,
                 "      - name: Rename dmg (только подписанная ветвь)\n"
                 "        # Неподписанная ветвь уже создаёт файл с суффиксом архитектуры,\n"
                 "        # и безусловное переименование давало dugadesk-1.0.0-x86_64-x86_64.dmg.\n"
                 "        # Подписанная ветвь создаёт файл БЕЗ архитектуры, поэтому суффикс\n"
                 "        # дописываем ровно здесь и только здесь.\n"
                 "        if: env.MACOS_P12_BASE64 != null && env.UPLOAD_ARTIFACT == 'true'\n"
                 "        run: |\n"
                 "          for name in %s*??.dmg; do\n" % BIN,
                 1,
                 u"Без правки .dmg выходит с удвоенным суффиксом архитектуры "
                 u"(dugadesk-1.0.0-x86_64-x86_64.dmg): ссылки на скачивание "
                 u"такие имена не принимают, кнопки macOS не работают.", S))

    # Косметика логов CI: второй шаг с тем же названием — в Linux-job.
    R.append(Sub(YML,
                 "      - name: Rename rustdesk\n        shell: bash\n",
                 "      - name: Rename deb\n        shell: bash\n", 1,
                 u"Название шага видно в логах Actions — чужое имя продукта "
                 u"там лишнее.", S))

    # --- 14.10 Копирайт в окне «О программе» ---------------------------
    # RustDesk распространяется под AGPL: удалять копирайт первоисточника
    # нельзя — это нарушение лицензии. Поэтому строку НЕ убираем, а дополняем
    # указанием, что это за продукт и на чём он основан. Год как и раньше
    # подставляется из DateTime.now(), вёрстка (Text внутри Column в Expanded)
    # не меняется — строка просто переносится.
    R.append(Sub("flutter/lib/desktop/pages/desktop_setting_page.dart",
                 "'Copyright \u00a9 ${DateTime.now().toString().substring(0, 4)} "
                 "Purslane Tech Pte. Ltd.\\n$license',",
                 "'%s \u2014 \u043d\u0430 \u043e\u0441\u043d\u043e\u0432\u0435 RustDesk. "
                 "Copyright \u00a9 ${DateTime.now().toString().substring(0, 4)} "
                 "Purslane Tech Pte. Ltd.\\n$license'," % APP,
                 1,
                 u"Пользователь в 'Настройки -> О программе' видел только чужой "
                 u"копирайт и ни слова о том, что это %s. Строка захардкожена, "
                 u"через translate() не проходит. Копирайт первоисточника "
                 u"сохранён: этого требует AGPL." % APP, S))

    # --- 14.11 Кодировка вывода в шаге CI ------------------------------
    # Шаг «Применить слой ребрендинга» вставляется правилом 12 и в уже
    # закоммиченном дереве стоит в старом виде — без env. Правило 12
    # идемпотентно и такой шаг не трогает, поэтому обновляем его отдельно.
    # На чистом дереве правило 12 вставит шаг сразу с env, и это правило
    # опознает результат по маркеру.
    STEP_ENV_OLD = (u"        shell: bash\n"
                    u"        run: |\n"
                    u"          # На windows-раннере в git-bash есть python3")
    STEP_ENV_NEW = (u"        shell: bash\n"
                    u"        env:\n"
                    u"          # Второй пояс к перенастройке потоков внутри apply.py.\n"
                    u"          # На windows-раннере stdout питона по умолчанию cp1252, и печать\n"
                    u"          # русских заголовков валит шаг с UnicodeEncodeError за 1 секунду —\n"
                    u"          # именно так упали обе Windows-сборки в прогоне #63.\n"
                    u"          PYTHONUTF8: \"1\"\n"
                    u"          PYTHONIOENCODING: \"utf-8\"\n"
                    u"        run: |\n"
                    u"          # На windows-раннере в git-bash есть python3")
    R.append(Sub(YML, STEP_ENV_OLD, STEP_ENV_NEW, 5,
                 u"Без PYTHONUTF8/PYTHONIOENCODING шаг зависит только от "
                 u"reconfigure() внутри скрипта. Два независимых пояса нужны "
                 u"потому, что цена ошибки — упавшая Windows-сборка через "
                 u"20 секунд и потерянный час.", S,
                 marker=u"PYTHONIOENCODING: \"utf-8\""))
    R.append(Sub(".github/workflows/bridge.yml", STEP_ENV_OLD, STEP_ENV_NEW, 1,
                 u"То же для генерации bridge.", S,
                 marker=u"PYTHONIOENCODING: \"utf-8\""))

    # --- 14.12 БЛОКЕР macOS: служба ставится под чужим именем ----------
    # Найдено приёмкой на живых артефактах сборки #64.
    #
    # Что происходило:
    #   * is_installed_daemon() (src/platform/macos.rs) ищет файл
    #     /Library/LaunchDaemons/{get_full_name()}_service.plist, а
    #     get_full_name() = "{ORG}.{APP_NAME}" -> pw.duga.DugaDesk_service.plist;
    #   * privileges_scripts/install.scpt перед запуском пропускается через
    #     correct_app_name(), который подменяет только "com.carriez.rustdesk"
    #     (полный bundle id), "rustdesk" и "RustDesk". Строка
    #     "com.carriez.RustDesk_service.plist" под первую замену не подходит
    #     (там RustDesk с заглавными), поэтому получалось
    #     com.carriez.DugaDesk_service.plist;
    #   * итог: служба ставится под одним именем, приложение ищет под другим.
    #     is_installed_daemon() возвращает false, uninstall_service() выходит
    #     по `return false` — служба не удаляется и не управляется из UI.
    #   * тем же промахом install.scpt копировал конфиг из
    #     ~/Library/Preferences/com.carriez.DugaDesk/, тогда как реальный
    #     каталог — ~/Library/Preferences/pw.duga.DugaDesk/ (Config::path ->
    #     ProjectDirs::from("", ORG, APP_NAME) + patch("Application Support"
    #     -> "Preferences")). ID и настройки не переносились в root-демон.
    #
    # ПОЧЕМУ ПРАВИМ correct_app_name, А НЕ САМИ .scpt/.plist:
    #   * correct_app_name вызывается РОВНО для privileges_scripts и больше
    #     нигде — проверено: 7 вызовов в macos.rs (install.scpt, uninstall.scpt,
    #     update.scpt, daemon.plist, agent.plist), других мест нет. Значит
    #     расширение подстановки ничего постороннего не заденет;
    #   * это один якорь вместо правок в пяти файлах, и любой новый скрипт,
    #     который апстрим добавит в privileges_scripts, будет покрыт сразу;
    #   * замена ставится ПОСЛЕ подстановки bundle id, поэтому строка
    #     AssociatedBundleIdentifiers = com.carriez.rustdesk по-прежнему
    #     превращается в bundle id. А если get_bundle_id() вернёт None
    #     (запуск не из бандла), цепочка com.carriez -> ORG, затем
    #     rustdesk -> dugadesk даст тот же pw.duga.dugadesk — то есть
    #     правка ещё и чинит апстримовский пробел в этой ветке.
    R.append(Sub("src/platform/macos.rs",
                 "    s = s.replace(\"rustdesk\", &crate::get_app_name().to_lowercase());\n"
                 "    s = s.replace(\"RustDesk\", &crate::get_app_name());\n",
                 "    // РЕБРЕНДИНГ %s: имена LaunchDaemon/LaunchAgent и каталог настроек\n"
                 "    // строятся из ORG (get_full_name() = \"{ORG}.{APP_NAME}\", Config::path()\n"
                 "    // = ProjectDirs::from(\"\", ORG, APP_NAME)), а не из bundle id.\n"
                 "    // Апстримовская подстановка меняла только \"RustDesk\", поэтому служба\n"
                 "    // ставилась как com.carriez.%s_service.plist, а is_installed_daemon()\n"
                 "    // искала %s.%s_service.plist — и не находила: uninstall_service()\n"
                 "    // выходил по `return false`, конфиг копировался из чужого каталога.\n"
                 "    let org = hbb_common::config::ORG.read().unwrap().clone();\n"
                 "    s = s.replace(\"com.carriez\", &org);\n"
                 "    s = s.replace(\"rustdesk\", &crate::get_app_name().to_lowercase());\n"
                 "    s = s.replace(\"RustDesk\", &crate::get_app_name());\n"
                 % (APP, APP, ORG, APP),
                 1,
                 u"Без правки служба macOS ставится как com.carriez.%s_service, "
                 u"а приложение ищет %s.%s_service: служба числится "
                 u"неустановленной, из интерфейса не удаляется, а ID и настройки "
                 u"не переносятся в root-демон (конфиг копируется из "
                 u"~/Library/Preferences/com.carriez.%s/ вместо "
                 u"~/Library/Preferences/%s.%s/)." % (APP, ORG, APP, APP, ORG, APP),
                 S, marker='s = s.replace("com.carriez", &org);',
                 # marker_wins обязателен: якорь (две строки replace) остаётся
                 # частью результата, поэтому обычная проверка «якорь исчез ->
                 # применено» не работает и повторный --apply вставил бы блок
                 # второй раз.
                 marker_wins=True))

    return R


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_root = os.path.dirname(here)   # brand/ лежит в корне дерева

    parser = argparse.ArgumentParser(
        description=u"Слой ребрендинга RustDesk -> DugaDesk")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true",
                       help=u"только проверить якоря, ничего не менять")
    group.add_argument("--apply", action="store_true",
                       help=u"применить правки и проверить результат")
    parser.add_argument("--root", default=default_root,
                        help=u"корень рабочего дерева (по умолчанию — каталог "
                             u"на уровень выше brand/)")
    parser.add_argument("--brand", default=os.path.join(here, "brand.toml"),
                        help=u"путь к brand.toml")
    parser.add_argument("--assets", default=os.path.join(here, "assets"),
                        help=u"каталог с иконками")
    parser.add_argument("--quiet", action="store_true",
                        help=u"печатать только проблемы и итог")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    brand = load_brand(args.brand)
    rules = build_rules(brand, os.path.abspath(args.assets))

    def run_pass(title):
        tree = Tree(root)
        rows = []
        section = None
        for rule in rules:
            try:
                status, message = rule.run(tree)
            except Exception as exc:                      # noqa
                status, message = FAIL, u"исключение: %s" % exc
            rows.append((rule, status, message))
        fails = [r for r in rows if r[1] == FAIL]
        warns = [r for r in rows if r[1] == WARN]
        if not args.quiet:
            print(u"")
            print(u"=" * 78)
            print(u"  %s" % title)
            print(u"  дерево: %s" % root)
            print(u"=" * 78)
            for rule, status, message in rows:
                if rule.section != section:
                    section = rule.section
                    print(u"\n--- %s" % section)
                print(u"  [%-7s] %-58s %s" % (status, rule.label()[:58], message))
        return tree, rows, fails, warns

    title = u"ПРОВЕРКА (--check): файлы не изменяются" if args.check \
        else u"ПРИМЕНЕНИЕ (--apply)"
    tree, rows, fails, warns = run_pass(title)

    print(u"")
    print(u"-" * 78)
    print(u"Всего правил: %d | ошибок: %d | предупреждений: %d"
          % (len(rows), len(fails), len(warns)))

    if warns:
        # Группируем по исходному файлу: одна иконка обычно раскладывается в
        # несколько мест, и 29 одинаковых строк только мешают читать вывод.
        missing = {}
        for rule, _, _m in warns:
            missing.setdefault(getattr(rule, "src_name", u"?"), []).append(rule.path)
        print(u"\nПРЕДУПРЕЖДЕНИЯ: нет файлов в brand/assets — эти иконки "
              u"останутся от RustDesk:")
        for name in sorted(missing):
            targets = missing[name]
            print(u"  - %s  (целей: %d, первая: %s)"
                  % (name, len(targets), targets[0]))
        print(u"  Иконки приносит владелец. После добавления не забыть "
              u"`git add -f` — в .gitignore репозитория есть *png/*svg/*jpg.")

    if fails:
        print(u"\nОШИБКИ — дерево разъехалось с ожиданиями слоя ребрендинга:")
        for rule, _, message in fails:
            print(u"  - [%s] %s" % (rule.section, message))
            print(u"      файл/цель : %s" % rule.label())
            print(u"      якорь     : %s" % rule.anchor()[:160])
            print(u"      зачем     : %s" % rule.why)
        print(u"\nЧто делать: открыть указанный файл, найти изменившийся текст, "
              u"поправить якорь в brand/apply.py и повторить проверку. "
              u"Применять правки вслепую нельзя — так ломается сборка на "
              u"середине, а это полтора часа.")
        return 1

    if args.check:
        print(u"\nВсе якоря на месте. Дерево готово к `--apply`.")
        return 0

    # --- применение ---
    tree.flush()
    print(u"\nПравки записаны на диск.")

    # --- самопроверка: второй проход по уже изменённому дереву ---
    _, rows2, fails2, _ = run_pass(u"САМОПРОВЕРКА после применения")
    # Копирование иконок исключаем: оно по природе повторяемо (кладём тот же
    # файл поверх того же файла), поэтому во втором проходе всегда числится
    # как выполненное заново и не является признаком неидемпотентности.
    not_applied = [r for r in rows2
                   if r[1] == OK_PENDING and not isinstance(r[0], Asset)]
    print(u"")
    print(u"-" * 78)
    if fails2:
        print(u"САМОПРОВЕРКА ПРОВАЛЕНА: %d правил не опознали свой результат."
              % len(fails2))
        for rule, _, message in fails2:
            print(u"  - %s" % message)
        return 2
    if not_applied:
        print(u"САМОПРОВЕРКА: %d правил повторно сработали как новые — это "
              u"значит, что правка не идемпотентна." % len(not_applied))
        for rule, _, _m in not_applied:
            print(u"  - %s" % rule.label())
        return 3
    print(u"САМОПРОВЕРКА пройдена: повторный прогон видит все правки как уже "
          u"применённые (слой идемпотентен).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
