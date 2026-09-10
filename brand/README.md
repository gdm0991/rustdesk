# Слой ребрендинга DugaDesk

Каталог `brand/` превращает форк RustDesk в DugaDesk, **не размазывая правки по дереву**.
Само дерево остаётся максимально близким к апстриму, поэтому слияние с upstream
не превращается в разбор конфликтов.

> ## Что подтверждено сборкой, а что ещё нет
>
> Прогон #63 (коммит `baa5dbc`): **собрались** Android (3 APK, подписаны),
> Linux deb обеих архитектур, macOS arm64 — имена артефактов верные.
> **Windows упал** на обеих архитектурах: шаг применения слоя умер с
> `UnicodeEncodeError` — на windows-раннере stdout Python по умолчанию cp1252,
> а весь вывод скрипта русский. Исправлено (см. «Кодировка вывода» ниже),
> но **повторной сборки Windows ещё не было**.
>
> Не проверялись вживую до сих пор: MSI, macOS x86_64, AppImage, Flatpak, rpm.

```
brand/
  brand.toml   параметры продукта (имя, домен, ключ, версия) — единственный источник истины
  apply.py     применитель: 193 правила, всё по текстовым якорям
  assets/      иконки бренда
  .gitignore   отменяет корневые правила *png/*svg — иначе иконки молча не коммитятся
  README.md    этот файл
```

> **Каталог `brand/` обязан быть закоммичен в репозиторий.** Он нужен не только
> локально: workflow вызывает `brand/apply.py --apply` прямо в раннере (см. ниже,
> «Подмодуль hbb_common»). Нет каталога в коммите — шаг CI падает на первой строке.

---

## Как пользоваться

Каталог `brand/` кладётся в **корень рабочего дерева** форка (рядом с `Cargo.toml`).

```bash
# 1. Проверка: якоря на месте? Файлы НЕ меняются. 30 секунд.
python3 brand/apply.py --check

# 2. Применение + автоматическая самопроверка.
python3 brand/apply.py --apply
```

Коды возврата: `0` — всё хорошо, `1` — якорь не найден, `2` — провалена самопроверка,
`3` — правка оказалась не идемпотентной.

Если `brand/` лежит в другом месте:

```bash
python3 brand/apply.py --apply --root /путь/к/дереву --assets /путь/к/иконкам
```

---

## Порядок работы при обновлении с апстрима

| Шаг | Команда | Что означает |
|---|---|---|
| 1 | `git merge upstream/master` | подтянули апстрим |
| 2 | `python3 brand/apply.py --check` | **обязательно до сборки** |
| 3 | ошибок нет → `--apply` | ребрендинг накатан |
| 4 | ошибка «ЯКОРЬ НЕ НАЙДЕН» | открыть указанный файл, найти изменившийся текст, поправить якорь в `apply.py`, вернуться к шагу 2 |

**Зачем шаг 2.** Сборка идёт около 1 ч 40 мин. Разъезд с апстримом ловится за 30 секунд
проверкой якорей вместо полутора часов сборки, которая упадёт на середине — или, хуже,
соберётся с наполовину применённым брендом.

**Почему якоря, а не номера строк.** Номера плавают между ревизиями. В одной ревизии
`libs/hbb_common/src/config.rs` держит `APP_NAME` на строке 72, `RENDEZVOUS_SERVERS`
на 120 и `RS_PUB_KEY` на 121; в другой ревизии того же подмодуля — 72/117/118.
Патч по номерам строк в такой ситуации молча попадает не туда.

---

## Иконки

Иконки **лежат в слое** — 27 файлов в `brand/assets/`, `apply.py` раскладывает их
по дереву. Отсутствие любого файла даёт предупреждение, а не тихий пропуск.
Раскладка:

```
brand/assets/
  icon.ico            -> res/icon.ico, flutter/windows/runner/resources/app_icon.ico
  tray-icon.ico       -> res/tray-icon.ico
  icon.png            -> res/icon.png, flutter/assets/icon.png
  icon.svg            -> flutter/assets/icon.svg
  logo.png            -> flutter/assets/logo.png
  mac-icon.png        -> res/mac-icon.png
  AppIcon.icns        -> flutter/macos/Runner/AppIcon.icns
  scalable.svg        -> res/scalable.svg
  32x32.png 64x64.png 128x128.png 128x128@2x.png  -> res/
  android/mipmap-<dpi>/ic_launcher.png
  android/mipmap-<dpi>/ic_launcher_round.png
  android/mipmap-<dpi>/ic_launcher_foreground.png
      <dpi> = mdpi, hdpi, xhdpi, xxhdpi, xxxhdpi
```

Отдельно правится `flutter/android/app/src/main/res/values/ic_launcher_background.xml`:
фон адаптивной иконки Android перекрашен из белого `#ffffff` в `#121212`. Без этого
на Android 8+ белые элементы нашего foreground сливались бы с белым фоном и от иконки
остались бы только оранжевые дуги. Красим именно фон, а не вшиваем плашку в foreground:
форму адаптивной иконки система обрезает сама, и у вшитой плашки срезало бы углы.

### `.gitignore` и иконки — решено, `-f` больше не нужен

В корневом `.gitignore` репозитория стоят правила `*png`, `*svg`, `*jpg` (строки 17-19).
Обычный `git add` из-за них **молча пропускал** иконки: ни ошибки, ни предупреждения —
просто коммит без картинок и сборка со старой иконкой RustDesk.

Слой закрывает это двумя правками:

| Где | Что |
|---|---|
| `brand/.gitignore` (файл слоя) | `!*.png`, `!*.svg`, `!*.jpg`, `!*.ico`, `!*.icns` — правила подкаталога перекрывают корневые |
| корневой `.gitignore` (правка слоя, раздел 13) | блок `!brand/assets/**`, `!flutter/assets/icon.png`, `!flutter/assets/logo.png` |

Файлы `res/*.png`, `res/*.ico`, `flutter/android/.../mipmap-*/*.png` уже в индексе
апстрима, поэтому их изменения фиксируются обычным способом.

Проверить, что всё видно git:

```bash
git add -A --dry-run | grep -E 'brand/assets|flutter/assets/(icon|logo)\.png'
git check-ignore -v brand/assets/icon.png    # должно молчать
```

---

## Что слой меняет (14 разделов, 193 правила)

| Раздел | Файлы | Суть |
|---|---|---|
| 1. Ядро | `libs/hbb_common/src/config.rs` | `APP_NAME`, `ORG`, `RENDEZVOUS_SERVERS`, `RS_PUB_KEY` |
| 2. Windows | `Cargo.toml`, `Runner.rc`, `runner/main.cpp` | ресурсы exe, имя окна |
| 3. Linux | `res/*`, `flutter/linux/CMakeLists.txt`, `src/ui.rs` | имена файлов пакета, пути установки, имя бинаря |
| 4. build.py | `build.py` | пути и имена во всех сборочных ветках |
| 5. Android | манифест, `build.gradle`, `strings.xml` | label, схема, applicationId |
| 6. macOS | `AppInfo.xcconfig` | `PRODUCT_NAME`, bundle id |
| 7. Интерфейс | `flutter/lib/**` | заголовок вкладок, «Powered by», ссылки на домен |
| 8. CI | `.github/workflows/flutter-build.yml` | имена артефактов, MSI, отключение sciter/iOS |
| 9. Flatpak/AppImage | `flatpak/*`, `appimage/*` | id приложения, пути запуска |
| 10. Иконки | см. выше | копирование из `brand/assets` |
| 11. Найдено при сверке | `res/pacman_install`, `my_application.cc`, macOS-проект, Kotlin | то, что всплыло при проверке результата |
| 12. Применение в CI | `flutter-build.yml`, `bridge.yml` | шаг `brand/apply.py --apply` после checkout (из-за подмодуля) |
| 13. `.gitignore` | `.gitignore` | отрицания, без которых иконки молча не коммитятся |
| 14. Правки по приёмке | CI, pbxproj, `libs/portable/Cargo.toml`, Android, nightly, `desktop_setting_page.dart` | глобы и переименование dmg, отключение мёртвых job'ов, bundle id, издатель, фон иконки, отключение cron, копирайт в «О программе» |

Числа в этой таблице обязаны совпадать с выводом `python3 brand/apply.py --check`.
Если разошлись — правился `apply.py`, а README забыли.

### Сервер и ключ: вшиты, но заменяемы

Приоритет в `get_rendezvous_server()`:

```
EXE_RENDEZVOUS_SERVER -> опция custom-rendezvous-server -> PROD_RENDEZVOUS_SERVER
   -> CONFIG2 -> КОНСТАНТА (наша)
```

Константа стоит **последней**, поэтому поле настроек у пользователя продолжает работать
и перекрывает вшитый адрес. Это и требовалось: «вшить, но оставить возможность смены».

### Версии в CI

Введена переменная `PROD_VERSION` (продуктовая, `1.0.0`). Штатная `VERSION` (`1.4.9`)
**не меняется**: по ней CI ищет промежуточные `deb/rpm/zst`, имена которых порождают
`build.py` (версия из `Cargo.toml`), `rpmbuild` (`Version:` из spec) и `makepkg`
(`pkgver` из PKGBUILD).

| Файл | Имя | Примечание |
|---|---|---|
| `.exe` (портативный установщик) | `dugadesk-1.0.0-<arch>.exe` | |
| `.msi` | `dugadesk-1.0.0-<arch>.msi` | издатель `Duga`, служба `DugaDesk` |
| `.dmg` | `dugadesk-1.0.0-<arch>.dmg` | суффикс архитектуры дописывает шаг «Rename rustdesk» |
| `.apk` | `dugadesk-1.0.0-<arch>.apk` | |
| `.AppImage` | `dugadesk-1.0.0-<arch>.AppImage` | версия берётся из рецепта `appimage/*.yml` |
| `.flatpak` | `dugadesk-1.0.0-<arch>.flatpak` | |
| `.deb` / `.rpm` / `.zst` | `dugadesk-1.4.9-*` | версия сборки: имена печёт `build.py` / `rpmbuild` / `makepkg` |

Сводный `*-unsigned.tar.gz` **не выпускается**: job `publish_unsigned` отключён — он
собирал архив в том числе из `windows-x86`, 32-битной sciter-сборки, которой больше нет.

---

## Как запускается сборка

**Ночные сборки по расписанию отключены** — слой комментирует блок `schedule:`
в `.github/workflows/flutter-nightly.yml`. Активного `cron` в репозитории не осталось
ни одного.

| Способ | Что делает |
|---|---|
| Actions → «Flutter Nightly Build» → Run workflow | ручной прогон, публикует в тег `nightly` — **основной способ, работает** |
| `git tag v1.0.0 && git push --tags` | `flutter-tag.yml` собирает и публикует релиз по тегу |

Почему отключили расписание: поток чужих PR нулевой — дерево меняется по событию
(merge апстрима или правка слоя), а не по расписанию; каждая ночь перезаписывала бы
релиз `nightly`; один прогон это 17 параллельных job'ов, из них два macOS, а
macOS-минуты GitHub тарифицирует с множителем 10× (Windows — 2×); падение после
merge апстрима случалось бы в 00:00 без свидетелей.

Вернуть расписание — снять решётки с трёх строк в `flutter-nightly.yml`
(блок закомментирован, а не удалён, именно для этого).

---

## Подмодуль `hbb_common` — почему слой применяется ещё и в CI

`libs/hbb_common` — **git-подмодуль чужого репозитория** `rustdesk/hbb_common`.
Правки его файлов физически невозможно закоммитить в наш репозиторий: git хранит
для подмодуля только указатель на ревизию, а `git status` в основном дереве
показывает лишь ` M libs/hbb_common`. В раннере после `actions/checkout` подмодуль
всегда приезжает с апстрим-пина — то есть `APP_NAME="RustDesk"`,
`RENDEZVOUS_SERVERS=["rs-ny.rustdesk.com"]` и чужой `RS_PUB_KEY`.

Поэтому слой накатывается **ещё раз, прямо в раннере**. В workflow вставлен шаг:

```yaml
      - name: Применить слой ребрендинга DugaDesk
        shell: bash
        run: |
          if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
          "$PY" brand/apply.py --apply
```

Шаг стоит сразу после `actions/checkout` в этих job'ах:

| Workflow | Job | Почему |
|---|---|---|
| `bridge.yml` | `generate_bridge` | `cargo expand` разворачивает крейт вместе с hbb_common |
| `flutter-build.yml` | `build-for-windows-flutter` | `cargo build` |
| `flutter-build.yml` | `build-for-macOS` | `cargo build` |
| `flutter-build.yml` | `build-rustdesk-android` | `cargo ndk` |
| `flutter-build.yml` | `build-rustdesk-android-universal` | apk из ресурсов дерева (страховка) |
| `flutter-build.yml` | `build-rustdesk-linux` | `cargo build` в контейнере, дерево с хоста |

Куда шаг **не** вставлен и почему:

* `build-RustDeskTempTopMostWindow` — вызывает `third-party-RustDeskTempTopMostWindow.yml`,
  который вообще не делает checkout нашего репозитория: он клонирует чужой проект
  `rustdesk-org/RustDeskTempTopMostWindow`. Каталога `brand/` там нет.
* `build-appimage`, `build-flatpak` — собирают из **готового `.deb`**, скачанного
  артефактом у `build-rustdesk-linux`, плюс файлов основного репозитория
  (`appimage/*.yml`, `flatpak/*.json`, `res/*.png`), которые уже применены в коммите.
  Rust там не компилируется, подмодуль не участвует.
* `build-for-windows-sciter`, `build-rustdesk-ios`, `build-rustdesk-linux-sciter` —
  отключены (`if: false`).

Шаг **не глушит код возврата**: `shell: bash` в GitHub Actions работает с
`set -eo pipefail`, поэтому непройденная проверка якорей валит сборку сразу,
а не через полтора часа на готовом релизе.

Альтернативу — форк `hbb_common` — отвергли: форк подмодуля пришлось бы держать
в синхроне с пином апстрима, и любой рассинхрон молча вернул бы старые константы.

---

## Копирайт и AGPL

RustDesk выпущен под AGPL, поэтому копирайт первоисточника **не удаляется**.
Строка в «Настройки → О программе» (`desktop_setting_page.dart`) дополнена, а не заменена:

```
DugaDesk — на основе RustDesk. Copyright © 2026 Purslane Tech Pte. Ltd.
```

Эта строка захардкожена и через `translate()` не проходит — потому её и правит слой.
Остальные надписи с именем продукта подставляются автоматически: `translate_locale`
в `src/lang.rs` при `APP_NAME != "RustDesk"` сама заменяет «RustDesk» на имя приложения
(исключения — ключи `powered_by_me` и `upgrade_rustdesk_server_pro*`). Поэтому ключи
переводов вроде `About RustDesk` слой не трогает: на экране появится «About DugaDesk».

---

## Подпись Android APK

Без секретов workflow идёт по ветке «Publish unsigned apk package», а неподписанный APK
Android не установит. Подпись выполняет действие
`r0adkll/sign-android-release@349ebdef` **после** сборки, поэтому `build.gradle` менять
не требуется.

| Секрет репозитория | Вход действия | Что это |
|---|---|---|
| `ANDROID_SIGNING_KEY` | `signingKeyBase64` | keystore целиком, в base64 |
| `ANDROID_ALIAS` | `alias` | алиас ключа внутри keystore |
| `ANDROID_KEY_STORE_PASSWORD` | `keyStorePassword` | пароль хранилища |
| `ANDROID_KEY_PASSWORD` | `keyPassword` | пароль ключа (необязательный вход действия) |

`ANDROID_SIGNING_KEY` дополнительно объявлен в `env:` workflow — от него зависят условия
`if:` у шагов подписи, загрузки и публикации. Нет секрета — все три пропускаются.

---

## Кодировка вывода (почему упала Windows в прогоне #63)

Весь вывод `apply.py` русский. На Linux и macOS stdout по умолчанию UTF-8; на
windows-раннере GitHub Actions Python берёт кодировку из кодовой страницы консоли —
`cp1252`, где кириллицы нет. Первая же строка заголовка валила процесс:

```
File "D:\a\rustdesk\rustdesk\brand\apply.py", line 1685, in run_pass
    print(u"  %s" % title)
UnicodeEncodeError: 'charmap' codec can't encode characters in position 2-11
```

Шаг умирал за секунду, унося обе Windows-сборки. Закрыто двумя независимыми поясами:

| Пояс | Где | Что делает |
|---|---|---|
| `_force_utf8_output()` | первым делом в `apply.py`, до любой печати | `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`, при неудаче — обёртка поверх `.buffer`, при неудаче — работаем как есть |
| `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` | `env:` шага «Применить слой ребрендинга» во всех шести job'ах | задают UTF-8 ещё до старта интерпретатора |

`errors="replace"` выбран сознательно: даже на самом экзотическом терминале скрипт
доработает и вернёт честный код возврата. Молчать нельзя — без вывода не видно,
какие якоря не нашлись.

---

## Что слой намеренно НЕ трогает

| Что | Почему |
|---|---|
| `librustdesk` / `liblibrustdesk` / `rustdesk_core_main` | имена символов FFI и cdylib; переименование рвёт связку Rust ↔ Flutter |
| `org.rustdesk.rustdesk/*` в Dart и Kotlin/Swift/C++ | имена MethodChannel; обязаны совпадать побайтово с обеих сторон. Переименуешь — приложение компилируется, но перестаёт передавать ввод |
| `package="com.carriez.flutter_hbb"` в AndroidManifest | привязан к пакетам Kotlin-классов (не путать с `applicationId`, его меняем) |
| `rustdesk-portable-packer`, `target/release/rustdesk` | имена крейта и продукта cargo |
| `translate("Show RustDesk")` в Kotlin | это **ключ перевода**; замена → перевод не найдётся и в меню появится сырой ключ |
| `res/msi/Package/Language/*.wxl`, `CustomActions` | `preprocess.py --app-name` подставляет имя сам |
| ссылки на `github.com/rustdesk`, `ko-fi.com/rustdesk` | это ссылки на проект-первоисточник, а не на наш продукт |
| `src/lang/*.rs` | файлы переводов апстрима; трогать их — конфликт при каждом merge |

---

## Изменение параметров

Поменять домен, ключ или версию — править **только** `brand.toml` и снова прогнать
`--apply`. Руками в дереве не править: следующий merge затрёт правку молча.
