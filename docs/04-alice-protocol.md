# Протокол Алисы: формат запроса и ответа

Навык R2D2 — это HTTPS-эндпоинт. Яндекс.Диалоги шлют `POST /webhook` с JSON,
наш сервер отвечает JSON. Ниже — выжимка полей, которые реально нужны R2D2.
Полная спецификация: https://yandex.ru/dev/dialogs/alice/doc/ru/

## Запрос (пример)

```json
{
  "meta": {
    "locale": "ru-RU",
    "timezone": "Europe/Moscow",
    "interfaces": {
      "screen": {},
      "audio_player": {}
    }
  },
  "request": {
    "type": "SimpleUtterance",
    "command": "открой браузер",
    "original_utterance": "Р2 открой браузер",
    "markup": { "dangerous_context": false },
    "payload": {}
  },
  "session": {
    "message_id": 0,
    "session_id": "2eac4854-fce721f3-b845abba-20d60",
    "skill_id": "3ad36498-f5rd-4079-a14b-788652932056",
    "user_id": "...",
    "user": {
      "user_id": "6C91DA51...",
      "access_token": null
    },
    "application": {
      "application_id": "47C73714..."
    },
    "new": true
  },
  "state": {
    "session": {},
    "user": {},
    "application": {}
  },
  "version": "1.0"
}
```

### Поля, которые использует R2D2

| Поле | Назначение в R2D2 |
|---|---|
| `request.type` | `SimpleUtterance` (голос/текст). `ButtonPressed` — нажатие кнопки (пока не используем) |
| `request.command` | Распознанная команда — это «вопрос» для мозга |
| `request.original_utterance` | Полная фраза до активации (для логов/отладки) |
| `request.markup.dangerous_context` | Яндекс-предупреждение об опасном запросе (флаг) |
| `session.new` | `true` при старте сессии → приветствие |
| `session.skill_id` | Проверяем на наш skill_id |
| `session.user.user_id` | Проверяем на наш whitelist |
| `session.application.application_id` | Устойчивый ключ памяти (переживает запуски навыка) |
| `session.session_id` | Ключ текущей сессии (для истории в рамках сессии) |
| `state.*` | Сюда можно класть маленькие стейты (пока храним в SQLite) |
| `version` | Должна быть `1.0` |

## Ответ (пример)

```json
{
  "response": {
    "text": "Открываю браузер",
    "tts": "",
    "end_session": false
  },
  "session_state": {},
  "version": "1.0"
}
```

### Поля ответа

| Поле | Описание |
|---|---|
| `response.text` | Показывается и озвучивается. Максимум 1024 символа |
| `response.tts` | Отдельный текст для озвучки (можно пустой, если text пуст) |
| `response.end_session` | `true` — завершить сессию навыка |
| `response.buttons[]` | Кнопки (на Станции без экрана не видны; пока не нужны) |
| `response.card` | Карточки (не используем на голосовой колонке) |
| `session_state` / `user_state_update` / `application_state` | Стейты (у нас — SQLite) |

## TTS-фишки (опционально)

В поле `tts` можно использовать речевые теги:

- паузы: `sil <[700]>` — 700 мс тишины;
- смена скорости/тона: `<speaker effect="...">`.

Пример «дождаться» долгой задачи:

```json
{
  "response": {
    "text": "Собираю сводку, отправлю в телеграм",
    "tts": "Собираю сводку, отправлю в телеграм",
    "end_session": false
  },
  "version": "1.0"
}
```

## Спец-интенты

| Фраза | Ответ R2D2 |
|---|---|
| «Помощь» | Краткий список команд навыка |
| «Что ты умеешь» | Бриф возможностей (коротко, голосом) |
| «Привет» / новая сессия | Короткое приветствие: «Привет, я R2Д2. Спрашивай или говори что сделать» |

## Ошибки и таймаут

- Если наш сервер не ответил за 4,5 с — Алиса скажет «навык не отвечает» и
  закроет сессию. Поэтому каждый запрос в R2D2 стараемся отдать за < 2,5 с.
- При любой внутренней ошибке возвращаем валидный JSON с коротким ответом
  («Что-то пошло не так, попробуй ещё раз»), а не 500.

## Проверка вручную (curl)

Локальный тест без туннеля:

```bash
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "meta": {"locale":"ru-RU","interfaces":{}},
    "request": {"type":"SimpleUtterance","command":"привет"},
    "session": {
      "message_id": 0,
      "session_id":"test-session",
      "skill_id":"<SKILL_ID>",
      "user":{"user_id":"<USER_ID>"},
      "application":{"application_id":"test-app"},
      "new": true
    },
    "state": {},
    "version": "1.0"
  }'
```

## Обработка нажатий кнопок (`ButtonPressed`)

Пока не используем (у Станции Лайт нет экрана). Если добавим позже:
`request.type == "ButtonPressed"` и `request.payload`.

## Ссылки

- Формат запроса: https://yandex.ru/dev/dialogs/alice/doc/ru/request.md
- Формат ответа: https://yandex.ru/dev/dialogs/alice/doc/ru/response.md
- Про версию: https://yandex.ru/dev/dialogs/alice/doc/ru/protocol.md
