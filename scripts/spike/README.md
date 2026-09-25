# scripts/spike — измерительный стенд для U1–U6

Харнесс wave 0 плана `opencode-brain`. Проверяет шесть недокументированных
поведений живого `opencode serve` и пишет результаты в
[`docs/11-opencode-contract.md`](../../docs/11-opencode-contract.md).

`probe_opencode.py` **сам ничего не запускает и ничем не управляет** — он только
разговаривает с уже поднятым сервером. Управление процессом остаётся за оператором,
поэтому стенд не может задеть процессы opencode пользователя.

## Что нужно

- venv репозитория рабочий: `.venv/bin/python` с `httpx` (`python -m py_compile` как
  проверка);
- живой бинарь: `/home/koluchiy/.opencode/bin/opencode` (1.18.32). Не `/usr/bin/opencode`
  (1.18.5) и не `~/.npm-global/bin/opencode` (1.18.21);
- свободный порт. В этом прогоне — **4598**;
- Zen-учётка пользователя: opencode подхватывает её сам из
  `~/.local/share/opencode/auth.json`. **Читать, копировать и печатать её нельзя** и
  не нужно.

## Разово: скретч-конфиг

Живёт в `/tmp`, отдельно от `~/.config/opencode/`, который запрещено трогать.
Эталонная копия лежит в репозитории, чтобы прогон был воспроизводим:

```bash
mkdir -p /tmp/r2d2-qa/spike-config /tmp/r2d2-qa/workspace
cp scripts/spike/spike-config.opencode.json /tmp/r2d2-qa/spike-config/opencode.json
```

Конфиг задаёт глобальный `permission` и четыре агента:

| Агент | Конфигурация | Зачем |
|---|---|---|
| `spike-deny` | `permission: {"*":"deny"}` | доказать, что агентский `deny` действует |
| `spike-ask` | `{"*":"allow", "bash":"ask"}` | поднять запрос разрешения для U4 |
| `spike-bashonly` | `{"*":"deny", "bash":"ask"}` | A/B параметра `tools` без посторонних инструментов |
| `spike-voice` | `tools` выключены, `{"*":"deny"}` | голосовой агент для замеров |

## Запуск сервера

```bash
cd /tmp/r2d2-qa/workspace
OPENCODE_CONFIG_DIR=/tmp/r2d2-qa/spike-config \
OPENCODE_LOG_LEVEL=warn \
  /home/koluchiy/.opencode/bin/opencode serve --hostname 127.0.0.1 --port 4598 &
echo $! > /tmp/r2d2-qa/serve.pid
```

Дождаться готовности и **записать pid**:

```bash
until curl -fsS --max-time 2 http://127.0.0.1:4598/global/health; do sleep 0.25; done
```

Остановить — только свой pid, из файла:

```bash
kill -TERM "$(cat /tmp/r2d2-qa/serve.pid)"
```

## Прогон пробника

```bash
cd /home/koluchiy/Documents/R2_yandex_station
.venv/bin/python scripts/spike/probe_opencode.py \
  --base-url http://127.0.0.1:4598 --out /tmp/r2d2-qa/spike.json
```

Код `0` — все пробы отработали, `latency_space_bunny_free.p50` не `null`.
Один прогон занимает примерно 3–5 минут (свип моделей плюс 13 замеров).

Подмножество проб — `--only u1,u4`; полный список: `u1,u2,u3,u4,u5,u6,adversarial`.
`--directory` (по умолчанию `/tmp/r2d2-qa/workspace`) — это то, что уходит в
query-параметр `?directory=`.

## Проверка «падает громко»

С сервером, остановленным как выше, та же команда обязана выйти с кодом `2`,
напечатать `opencode unreachable at http://127.0.0.1:4598` и **не создать** файл
результата. Эталонный снимок: `/tmp/r2d2-qa/spike-fail.log`.

```bash
.venv/bin/python scripts/spike/probe_opencode.py \
  --base-url http://127.0.0.1:4598 --out /tmp/r2d2-qa/spike-should-not-exist.json
echo "exit=$?"        # 2
ls /tmp/r2d2-qa/spike-should-not-exist.json   # No such file
```

## Правила безопасности стенда

- Своим считается **только** pid, который стенд запустил сам, и только из
  `/tmp/r2d2-qa/serve.pid`. Чужие процессы opencode не трогать: пользователь держит
  несколько TUI-сессий и `opencode serve --port 45512`.
- `~/.config/opencode/`, `~/.local/share/opencode/opencode.db` и
  `~/.local/share/opencode/auth.json` — только на чтение, и то auth.json не читается
  вовсе.
- Никакой `POST /instance/dispose` никуда, кроме собственного сервера.
- Скретч-сервер обязательно снимается в `trap`/`finally` и проверяется на отсутствие
  после прогона: `pgrep -a opencode` должен показать ровно те 5 процессов, что были
  до стенда.

## Что внутри пробника

| Проба | Что меряет |
|---|---|
| `u1` | изоляция `OPENCODE_CONFIG_DIR` для агентов и `permission` + живость Zen-ключа |
| `u2` | `system` в теле: per-message или на сессию |
| `u3` | форма параметра `tools` (из `GET /doc` + поведенческий A/B) |
| `u4` | точные строки SSE `event.type` для разрешения и закрытия хода |
| `u5` | задание рабочего каталога сессии |
| `u6` | задержка `POST /session/:id/message` + свип годности моделей |
| `adversarial` | формы отказа: несуществующие агент/модель/сессия/разрешение, битые тела |

У всех проб есть жёсткий таймаут (60 с на замерах, 20 с в остальных), так что
зависший сервер не заблокирует прогон. Ни одна проба не считает успехом
`200` без проверки содержимого: opencode отдаёт `200` с `info.error`, когда модель
не ответила, и молча переключается на другую модель, если `modelID` неизвестен.
