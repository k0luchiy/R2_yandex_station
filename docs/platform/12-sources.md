# Источники

Все источники проверены **1 октября 2026 года**. Дата обращения одна для всех, поэтому нигде
не повторяется.

## Как читать этот список

| Уровень | Что это |
|---|---|
| 🟢 **первичный** | официальная документация Яндекса или юридический документ |
| 🔵 **код** | открытая реализация, прочитанная по исходникам |
| 🟡 **практика** | блог, форум,issue — наблюдение разработчика, не гарантия платформы |
| 🔴 **не подтверждено** | утверждение есть, но первоисточник не найден; **не использовать как основание** |

## Способ чтения документации

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-01 | `https://yandex.ru/dev/dialogs/alice/doc/sitemap.xml` | 🟢 | Полный перечень страниц документации — **85 URL**. Рабочая точка входа: не перечислять страницы вручную, а читать карту |
| S-02 | `https://yandex.ru/dev/dialogs/alice/doc/ru/<страница>.md` | 🟢 | Исходный Markdown любой страницы. Основной приём всего исследования |

⚠️ Рекомендуемый индекс `https://yandex.ru/dev/dialogs/alice/doc/ru/llms.txt` **отдаёт 404**,
хотя упоминается в шапке каждой страницы. Не тратьте на него время.

⚠️ Включаемые блоки (`{% include %}`) в `.md` **не раскрываются** — цитаты про таймаут и про
`Content-Type` брались из отрендеренного HTML.

## Протокол

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-03 | [`/doc/ru/request`](https://yandex.ru/dev/dialogs/alice/doc/ru/request.md) | 🟢 | Формат запроса: `version`, `meta` (`locale`, `timezone`, `client_id`, `interfaces`), `session`, `request` |
| S-04 | [`/doc/ru/request-simpleutterance`](https://yandex.ru/dev/dialogs/alice/doc/ru/request-simpleutterance.md) | 🟢 | Определения `command` и `original_utterance`, правила нормализации, типы запроса, `ping`, поведение при первом контакте |
| S-05 | [`/doc/ru/request-buttonpressed`](https://yandex.ru/dev/dialogs/alice/doc/ru/request-buttonpressed.md) | 🟢 | Условия формирования `ButtonPressed` — правило `hide`/`payload` |
| S-06 | [`/doc/ru/request-show-pull`](https://yandex.ru/dev/dialogs/alice/doc/ru/request-show-pull.md) | 🟢 | Утреннее шоу, `show_type: MORNING` |
| S-07 | [`/doc/ru/response`](https://yandex.ru/dev/dialogs/alice/doc/ru/response.md) | 🟢 | Формат ответа: `text`, `tts`, `card`, `buttons`, `end_session`, `directives`, `show_item_meta`, `analytics`, стейты. Фраза о том, что на устройствах только с голосом читается `text` |
| S-08 | [`/doc/ru/wait-response`](https://yandex.ru/dev/dialogs/alice/doc/ru/wait-response.md) | 🟢 | **Таймаут 4,5 секунды** и перечень приёмов уложиться |
| S-09 | [`/doc/ru/warehouse/concepts`](https://yandex.ru/dev/dialogs/alice/doc/ru/warehouse/concepts.md) | 🟢 | Определение времени ответа и его слагаемых; **лимит всего ответа 5000 символов**; лимиты OAuth-токенов; `expires_in`; сессия на поверхностях без экрана; 5 секунд на OAuth-колбэк |
| S-10 | [`/doc/ru/archive/protocol-surface`](https://yandex.ru/dev/dialogs/alice/doc/ru/archive/protocol-surface.md) | 🟢 | **Архивная версия протокола** — основа для таблицы переименований |
| S-11 | [`/doc/ru/protocol`](https://yandex.ru/dev/dialogs/alice/doc/ru/protocol.md) | 🟢 | Интеграция по HTTPS |
| S-12 | [`/doc/ru/troubleshooting`](https://yandex.ru/dev/dialogs/alice/doc/ru/troubleshooting.md) | 🟢 | «Почему навык не отвечает» — превышение 4,5 с |

## Ответная поверхность

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-13 | [`/doc/ru/buttons`](https://yandex.ru/dev/dialogs/alice/doc/ru/buttons.md) | 🟢 | Кнопки: лимиты, `hide`, «подсказки после основного ответа», кнопки только для устройств с экраном, `footer` игнорируется для `ImageGallery` |
| S-14 | [`/doc/ru/response-card-bigimage`](https://yandex.ru/dev/dialogs/alice/doc/ru/response-card-bigimage.md) | 🟢 | Схема `BigImage` |
| S-15 | [`/doc/ru/response-card-itemslist`](https://yandex.ru/dev/dialogs/alice/doc/ru/response-card-itemslist.md) | 🟢 | Схема `ItemsList`, 1–5 элементов |
| S-16 | [`/doc/ru/response-card-imagegallery`](https://yandex.ru/dev/dialogs/alice/doc/ru/response-card-imagegallery.md) | 🟢 | Схема `ImageGallery`. ⚠️ Говорит «1–10», тогда как `response.md` говорит «1–7» |
| S-17 | [`/doc/ru/interface`](https://yandex.ru/dev/dialogs/alice/doc/ru/interface.md) | 🟢 | **Ключевая страница для Станции.** Типы интерфейса GUI/VUI/CUI, «устройства, которые поддерживают только голосовое управление (например, Яндекс Станция), не смогут отобразить графические элементы», размеры картинок, **не более 6 кнопок в бабле**, «баблы и подсказки» |
| S-18 | [`/doc/ru/resource-upload`](https://yandex.ru/dev/dialogs/alice/doc/ru/resource-upload.md) | 🟢 | Загрузка изображений: 1 КБ – 1 МБ, форматы, квота 100 МБ, формат `image_id`, 429 |
| S-19 | [`/doc/ru/resource-sounds-upload`](https://yandex.ru/dev/dialogs/alice/doc/ru/resource-sounds-upload.md) | 🟢 | Загрузка звука: до 120 с, до 5 МБ, MP3/WAV/OGG → Opus, квота 1 ГБ |
| S-20 | [`/doc/ru/sounds`](https://yandex.ru/dev/dialogs/alice/doc/ru/sounds.md) | 🟢 | Библиотека звуков |
| S-21 | [`/doc/ru/speech-tuning`](https://yandex.ru/dev/dialogs/alice/doc/ru/speech-tuning.md) | 🟢 | Разметка речи: `+`, `sil <[…]>`, `word <[…]>` |
| S-22 | [`/doc/ru/response-start-account-linking`](https://yandex.ru/dev/dialogs/alice/doc/ru/response-start-account-linking.md) | 🟢 | Единственная документированная директива |

## NLU

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-23 | [`/doc/ru/nlu`](https://yandex.ru/dev/dialogs/alice/doc/ru/nlu.md) | 🟢 | `nlu.tokens`, `nlu.entities`, `nlu.intents` как карта, встроенные интенты `YANDEX.CONFIRM/REJECT/HELP/REPEAT`, грамматика. ⚠️ Содержит пример, где `command` сохраняет запятую |
| S-24 | [`/doc/ru/naming-entities`](https://yandex.ru/dev/dialogs/alice/doc/ru/naming-entities.md) | 🟢 | Именованные сущности: `YANDEX.DATETIME`, `FIO`, `GEO`, `NUMBER` и форматы значений |
| S-25 | [`/doc/ru/word-processing`](https://yandex.ru/dev/dialogs/alice/doc/ru/word-processing.md) | 🟢 | Обработка реплик в консоли, работа с логами |
| S-26 | [`/doc/ru/write-scenario`](https://yandex.ru/dev/dialogs/alice/doc/ru/write-scenario.md) | 🟢 | Начало сценария |

## Станция и активация

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-27 | [`/doc/ru/surfaces`](https://yandex.ru/dev/dialogs/alice/doc/ru/surfaces.md) | 🟢 | **Стация в списке поверхностей без экрана** вместе с Навигатором и Авто |
| S-28 | [`/doc/ru/access`](https://yandex.ru/dev/dialogs/alice/doc/ru/access.md) | 🟢 | Приватный навык виден только владельцу; настройки доступа могут ограничивать круг устройств |
| S-29 | [`/doc/ru/activation`](https://yandex.ru/dev/dialogs/alice/doc/ru/activation.md) | 🟢 | Фразы запуска и выхода; «в `command` попадает весь текст, кроме активационной фразы»; «публиковать навык необязательно» для приватного |
| S-30 | [`/doc/ru/requirements#key-phrase`](https://yandex.ru/dev/dialogs/alice/doc/ru/requirements.md) | 🟢 | Правила имени и активационных имён, **правило фонетического сходства**, рекомендация избегать омографий |
| S-31 | [`/doc/ru/publish-settings`](https://yandex.ru/dev/dialogs/alice/doc/ru/publish-settings.md) | 🟢 | **HTTPS обязателен**, fullchain-сертификат, автоодерация приватных, **«Можно задать три дополнительных активационных имени»**, «Нужно устройство с экраном», примеры запросов |
| S-32 | [`/doc/ru/test`](https://yandex.ru/dev/dialogs/alice/doc/ru/test.md) | 🟢 | Тестер консоли, переключатель «Нет экрана — имитирует работу устройства без экрана, например Станции Мини» |
| S-33 | `https://alice.yandex.ru/support/ru/station/meet/` | 🟢 | Характеристики моделей станций. **Пользовательская документация**, не документация для разработчиков |
| S-34 | `https://dialogs.yandex.ru/store/skills/` | 🟢 | Каталог: примеры активационных команд у реальных навыков, блок «Работает с устройствами» |
| S-35 | Внутренний JSON каталога (в странице магазина) | 🔴 | Перечень поверхностей: `desktop`, `mobile`, `auto`, `navigator`, `station`, `maps`, `watch`. **Не документирован**, извлечён из страницы |

## Консоль, модерация, публикация

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-36 | [`https://yandex.ru/legal/dialogues_requirements`](https://yandex.ru/legal/dialogues_requirements) | 🟢 | **Юридически обязательные требования.** §3.6 — 21 запрещённый пункт; §3.10 — политика конфиденциальности; §3.11 — медизделия; §3.14 — **приватный режим максимум 5 лиц**; §4–§7 — правила названия, активационных имён, описания, иконки, включая «схоже до степени смешения… при звуковом воспроизведении»; §8.3 — уникальность контента и запрет рекламы; §9.1.1 — 3 секунды для приложений YaOS |
| S-37 | [`/doc/ru/requirements`](https://yandex.ru/dev/dialogs/alice/doc/ru/requirements.md) | 🟢 | «На каждые 10 обращений допускается лишь 1 ошибка сервера»; периодические проверки и отключение не отвечающего навыка; «Все навыки — и публичные, и приватные — перед публикацией проходят модерацию» |
| S-38 | [`/doc/ru/publication`](https://yandex.ru/dev/dialogs/alice/doc/ru/publication.md) | 🟢 | Схема lifecycle, сроки появления в каталоге, правило «модерация черновика не влияет на каталог» |
| S-39 | [`/doc/ru/moderation`](https://yandex.ru/dev/dialogs/alice/doc/ru/moderation.md) | 🟢 | Что проверяют, автоодерация приватных, сроки, тема формы обратной связи |
| S-40 | [`/doc/ru/checklist`](https://yandex.ru/dev/dialogs/alice/doc/ru/checklist.md) | 🟢 | Чек-лист перед отправкой |
| S-41 | [`/doc/ru/monitoring`](https://yandex.ru/dev/dialogs/alice/doc/ru/monitoring.md) | 🟢 | Вкладка «Мониторинг»: запросы, ошибки, тайминги. Только для опубликованных навыков |
| S-42 | [`/doc/ru/skill-create-console`](https://yandex.ru/dev/dialogs/alice/doc/ru/skill-create-console.md) | 🟢 | Создание навыка в консоли |
| S-43 | [`https://github.com/trudenboy/ya-dialogs-api`](https://github.com/trudenboy/ya-dialogs-api) `RESEARCH.md`, SHA `f699c4970d97366fc1b0279a93605c8a8a95f910` | 🔵 | Реверс-инжиниринг внутреннего API консоли: статусы черновика, эндпоинты интентов, боковая панель, аудит-лог операций. **Неофициально** — пригодилось, чтобы понять, что интенты живут отдельным API |
| S-44 | [`https://seonews.ru`](https://www.seonews.ru/events/yandeks-otklyuchil-navyki-v-chate-s-alisoy-ai-i-mobilnykh-prilozheniyakh/) | 🟡 | **Август 2026:** Яндекс отключил запуск развлекательных и обучающих навыков в чате Алиса AI и в мобильных приложениях. Станции не затронуты |
| S-45 | [`https://habr.com/ru/articles/434194`](https://habr.com/ru/articles/434194/) | 🟡 | Разработчик о консоли, приватном навыке, тестировании; отказ по слишком общему названию |
| S-46 | [`https://habr.com/ru/companies/just_ai/articles/504496`](https://habr.com/ru/companies/just_ai/articles/504496/) | 🟡 | Повторная модерация уже опубликованного навыка отклонена из-за названия; помогло письмо в поддержку |

## Нормализация и распознавание

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-47 | [`https://forpes.ru/post/85991`](https://forpes.ru/post/85991) и зеркало [`habr.com/ru/articles/471388`](https://habr.com/ru/articles/471388) | 🟡 | **Полевое наблюдение 14.10.2019.** Недокументированное поведение `command`: срезает «Алиса», **исправляет опечатки**, приводит телефоны к `(916)123-45-67`, склеивает соседние цифры. Приводит старую всплывающую подсказку по `command` |
| S-48 | [`https://github.com/mahenzon/aioalice`](https://github.com/mahenzon/aioalice) | 🔵 | Первый коммит (июль 2018) уже содержит `command` + `original_utterance`, поля `utterance` нет |
| S-49 | [`https://github.com/AlekSi/alice`](https://github.com/AlekSi/alice) `request.go` | 🔵 | Go-структура: `Meta` содержит `Locale`, `Timezone`, `ClientID`, `Interfaces` — и **ни** `country`, **ни** `device` |
| S-50 | [`https://github.com/vitalets/alice-types`](https://github.com/vitalets/alice-types) | 🔵 | TypeScript-типы, зеркалящие документацию 1:1 |
| S-51 | [`https://github.com/trudenboy/ma-provider-yandex-alice`](https://github.com/trudenboy/ma-provider-yandex-alice) `docs/VOICE_COMMANDS.md`, `docs/VOICE_UX_RESEARCH.md` | 🔵 | Независимое описание `command`; защитный срез «Алиса»; `original_utterance` содержит имя навыка |
| S-52 | [`https://github.com/borzunov/alice_scripts`](https://github.com/borzunov/alice_scripts) | 🔵 | `request.command`: «свойство, содержащее значение поля command, из которого убраны завершающие точки»; показывает, что «подсказки» = `buttons` с `hide: true` |
| S-53 | [`https://github.com/AlexxIT/YandexDialogs`](https://github.com/AlexxIT/YandexDialogs) | 🔵 | Приватный навык создаётся и публикуется автоматически; «интенты можно настраивать только после публикации» |
| S-54 | [`https://developers.sber.ru/docs/ru/va/about/migration/alice-skills`](https://developers.sber.ru/docs/ru/va/about/migration/alice-skills) | 🔵 | Независимое подтверждение существования `request.command` и `request.original_utterance` |
| S-55 | [`https://yandex.cloud/ru-kz/docs/speechkit/stt/normalization`](https://yandex.cloud/ru-kz/docs/speechkit/stt/normalization) | 🟢 | Нормализация в SpeechKit STT: шесть уровней, маскирование нецензурной лексики, предупреждение о том, что правила меняются |
| S-56 | [`https://yandex.cloud/ru-kz/docs/speechkit/quickstart`](https://yandex.cloud/ru-kz/docs/speechkit/quickstart) | 🟢 | Параметры автоматического распознавания: нормализация текста, фильтрация обсценной лексики, литературный текст |
| S-57 | [`https://habr.com/ru/companies/yandex/articles/350968`](https://habr.com/ru/companies/yandex/articles/350968/) | 🟡 | Официальный блог Яндекса (13.03.2018): Алиса понимает **морфологические формы** одного активационного имени; «включи» и другие сигнальные слова |
| S-58 | [`https://privet-alice.ru`](https://privet-alice.ru/alisa-yandex/yandeks-dialogi) | 🟡 | Практика: активационное имя 2–4 слова; объяснение, почему запрещены созвучные имена — «чтобы при голосовом вводе Алиса не перепутала текст и не открыла не то приложение» |
| S-59 | [`https://github.com/fletcherist/yandex-dialogs-sdk`](https://github.com/fletcherist/yandex-dialogs-sdk) | 🔵 | `ctx.message` ← `request.command`, `ctx.originalUtterance` ← `request.original_utterance` |

## Хостинг и туннели

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-60 | [`https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/`](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) | 🟢 | «The hostname changes each time you create a Quick Tunnel»; лимит 200 одновременных запросов; нет SSE; нет гарантий доступности |
| S-61 | [`https://developers.cloudflare.com/tunnel/`](https://developers.cloudflare.com/tunnel/) | 🟢 | Именованный тоннель: стабильный адрес на своём домене, бесплатно |
| S-62 | [`https://ngrok.com/blog/free-static-domains-ngrok-users`](https://ngrok.com/blog/free-static-domains-ngrok-users) | 🟢 | С августа 2023 у каждого аккаунта ngrok есть бесплатный статический dev-домен |
| S-63 | [`https://ngrok.com/docs/domains`](https://ngrok.com/docs/domains) и [`free-plan-limits`](https://github.com/ngrok/ngrok-docs/blob/main/pricing-limits/free-plan-limits.mdx) | 🟢 | Бесплатный план: домен, лимиты, «endpoints have no timeout»; случайные домены — только платные; предупреждение не показывается при программном доступе |
| S-64 | [`https://tailscale.com/docs/features/tailscale-funnel`](https://tailscale.com/docs/features/tailscale-funnel) | 🟢 | Стабильный `*.ts.net`, бесплатно; порты 443/8443/10000; только DNS-имена внутри tailnet |
| S-65 | [`https://theboroer.github.io/localtunnel-www/`](https://github.com/localtunnel/localtunnel) | 🟢 | «You may not actually receive this name depending on availability» |
| S-66 | [`https://localhost.run/docs/faq`](https://localhost.run/docs/faq) и [`custom-domains`](https://localhost.run/docs/custom-domains) | 🟢 | Адрес меняется при каждом подключении; стабильное имя платное |
| S-67 | [`https://yandex.cloud/ru/docs/functions/`](https://yandex.cloud/ru/docs/functions/) | 🟢 | Лимиты Cloud Functions; **навыки Алисы на Cloud Functions бесплатны и не тарифицируются** |
| S-68 | [`https://yandex.cloud/ru/docs/api-gateway/`](https://yandex.cloud/ru/docs/api-gateway/) | 🟢 | API Gateway: домен `*.apigw.yandexcloud.net`, HTTPS, таймауты |
| S-69 | [`https://github.com/volodarskij/alice-spotify-bridge`](https://github.com/volodarskij/alice-spotify-bridge) `docs/ALICE_SKILL_GUIDE.md` | 🟡 | Практика: нестандартный порт `:8889`, отвечать 200 при ошибках, `max-time 1.5` для LLM, фоновая музыка при превышении таймаута, фонетическое имя «Спотик» |
| S-70 | [`https://github.com/hu553in/yandex-alice-openai`](https://github.com/hu553in/yandex-alice-openai) | 🟡 | Отложенный ответ: отдать ack, доделать позже |
| S-71 | [`https://github.com/WhySoEvil/smarthubnewby`](https://github.com/WhySoEvil/smarthubnewby) `docs/ALICE-SKILL-SETUP.md` | 🟡 | Именованный тоннель cloudflared + секретный заголовок доверия |
| S-72 | [`https://github.com/eluceon/alice-llm-bridge`](https://github.com/eluceon/alice-llm-bridge) `docs/skill-setup.md` | 🟡 | Секретный сегмент в пути вебхука — потому что IP-allowlist невозможен |
| S-73 | [`https://yandex.cloud/ru/docs/smartwebsecurity/`](https://yandex.cloud/ru/docs/smartwebsecurity/) | 🟢 | WAF перед вебхуком; SmartCaptcha |
| S-74 | [`https://vc.ru/dev/2842111`](https://vc.ru/dev/2842111-kak-otkryt-lokalnyj-sajt-cherez-tunyl-bez-ngrok) | 🟡 | ngrok практически недоступен из российских сетей; альтернативы |

## Умный дом

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-75 | [`https://yandex.ru/dev/dialogs/smart-home/doc/ru/start`](https://yandex.ru/dev/dialogs/smart-home/doc/ru/start) | 🟢 | **Таймаут 3 секунды** для навыков Умного дома; навыки Умного дома не требуют активационной фразы; язык определяет страну публикации; поддерживаемые устройства |
| S-76 | [`https://yandex.ru/dev/dialogs/smart-home/doc/ru/auth/when-to-use`](https://yandex.ru/dev/dialogs/smart-home/doc/ru/auth/when-to-use) | 🟢 | Когда нужна связка аккаунтов; только authorization code grant |

## Станция: недокументированное

| # | Источник | Уровень | Что даёт |
|---|---|---|---|
| S-77 | [`https://github.com/bondrogeen/alisa-npm`](https://github.com/bondrogeen/alisa-npm) | 🔴 | Локальный WebSocket станции на порту **1961**, `platform: yandexmini`, команды `setVolume`/`playMusic`, эксперимент `mordovia_long_listening`. **Не часть SDK навыка**, не поддерживается |
| S-78 | [`https://github.com/AlexxIT/YandexStation`](https://github.com/AlexxIT/YandexStation) | 🔴 | Обращается к внутренним HTTP-API Яндекса с сессионной cookie, а не к навыку. Не поддерживаемый разработчиками навыков путь |
| S-79 | [`https://alice.yandex.ru/support/ru/station/settings/add-skills`](https://alice.yandex.ru/support/ru/station/settings/add-skills) | 🟢 | Пользовательская страница: как добавить навыки сторонних разработчиков |
| S-80 | [`https://alice.yandex.ru/support/ru/station/skills/`](https://alice.yandex.ru/support/ru/station/skills/) | 🟢 | Навыки сторонних разработчиков; «Алиса не реагирует на быстрые команды (без упоминания её имени)» |

## Что НЕ удалось подтвердить

Явно перечислено, чтобы эти утверждения не выглядели проверенными:

| Утверждение | Статус |
|---|---|
| `Content-Type: application/json` обязателен | **в документации не упоминается**; все SDK его отправляют |
| Платформа повторяет неудачные запросы | **не документировано**; в отличие от Yandex Messenger, где повторные попытки описаны |
| Яндекс следует за HTTP-редиректами | **не документировано** |
| Обязателен порт 443 | **не документировано**; практика показывает, что работают и другие |
| Поведение при превышении лимита ответа | **не документировано** — неизвестно, отклоняется ли запрос или игнорируется поле |
| Верхняя граница `ImageGallery` | **противоречие**: 7 в одном месте, 10 в другом |
| Замена `ё` на `е` при нормализации | **не документировано**; подтверждений ни нет, ни против |
| Требуется ли повторная модерация при смене Backend URL | **не документировано** |
| Поведение черновика (неопубликованного) при голосовом вызове | **противоречиво**: официально требует публикации, сторонние источники утверждают, что черновика достаточно |
| `meta.client_id` для станции | не документировано; в коде навыков не встречается |
